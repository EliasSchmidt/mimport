"""Übergabe an beets.

Der Import bleibt bewusst dem ``beet``-Subprozess überlassen: dort steckt die
konfigurierte Library, das Umbenennungsschema und die aktivierten Plugins. Im
Container ist das das beets aus demselben venv wie mimport -- damit gibt es nur
eine beets-Version und keine Möglichkeit, dass zwei Installationen das Schema
der ``library.db`` gegeneinander migrieren.

Aufgerufen wird mit ``-A``, also **ohne** Autotagging: die Tags stehen zu diesem
Zeitpunkt schon in den Dateien (siehe ``backend.tagging``), und beets soll sie
nicht erneut überschreiben, sondern nur noch die Dateien an ihren Platz in der
Library bringen. Warum nicht stattdessen ``--search-id``, steht in
``backend.main``.
"""

from __future__ import annotations

import fcntl
import logging
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path

from backend import beets_env
from backend.config import settings

log = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Ausgang eines Importlaufs."""

    command: list[str] = field(default_factory=list)
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    #: Nur angesehen, nichts verschoben.
    pretend: bool = False
    timed_out: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error and not self.timed_out

    @property
    def command_line(self) -> str:
        """Der Aufruf als lesbare Zeile, damit man ihn nachvollziehen kann."""
        return " ".join(self.command)

    @property
    def output(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(p for p in parts if p)


def build_command(directory: Path, *, pretend: bool = False) -> list[str]:
    """Setzt die ``beet import``-Kommandozeile zusammen.

    * ``-q`` unterdrückt jede Rückfrage im Terminal -- die Entscheidungen sind
      bereits in der Oberfläche gefallen.
    * ``-A`` schaltet das Autotagging ab. Damit läuft beets über
      ``import_asis``, greift MusicBrainz nicht erneut ab und übernimmt die Tags
      wie sie in den Dateien stehen.
    * ``-m`` verschiebt statt zu kopieren, sonst bleiben die Uploads im Staging
      liegen und tauchen beim nächsten Mal wieder auf.
    """
    command = [settings.beet_bin, "import", "-q", "-A"]
    if pretend:
        command.append("--pretend")
    elif settings.move_on_import:
        command.append("-m")
    else:
        command.append("-c")
    command.append(str(directory))
    return command


def _lock_path() -> Path:
    """Wo der Import-Lock liegt: neben der Library, die er schützt.

    Aus der beets-Konfiguration abgeleitet, damit alle Prozesse, die sich
    dieselbe ``library.db`` teilen, zwangsläufig dieselbe Lock-Datei nehmen --
    eine eigene Einstellung könnte man je Dienst unterschiedlich setzen und
    hätte den Schutz damit still ausgehebelt.

    Nur der *Pfad* wird gelesen, die Datenbank bleibt zu.
    """
    beets_env.ensure_loaded()
    from beets import config as beets_config

    return Path(beets_config["library"].as_filename()).with_suffix(".lock")


@contextmanager
def library_lock() -> Iterator[None]:
    """Lässt immer nur einen Schreibzugriff gleichzeitig an die Library.

    mimport läuft als zwei Dienste -- einer für Uploads, einer für CDs -- und
    beide rufen dasselbe ``beet import`` auf derselben ``library.db`` auf. Ein
    Import ist eine lange SQLite-Transaktion; zwei gleichzeitig geraten sich in
    die Quere. Das Matching braucht den Lock nicht, es fasst die Datenbank
    ohnehin nie an. Denselben Lock nutzt ``backend.albums`` für ``beet
    embedart``, aus demselben Grund: auch das ist ein Schreibzugriff auf
    dieselbe Library.

    Wird bewusst ohne Zeitlimit gewartet: der zweite Import soll laufen, nicht
    scheitern. Nach oben begrenzt ihn das Zeitlimit des Subprozesses.
    """
    path = _lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("w")
    except OSError as exc:
        # Lieber ohne Lock importieren als gar nicht -- bei einem einzelnen
        # laufenden Dienst ändert er ohnehin nichts.
        log.warning("Import-Lock %s nicht nutzbar (%s), fahre ohne fort.", path, exc)
        yield
        return

    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.info("Ein anderer Import läuft gerade, warte auf %s ...", path)
            fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


def run_import(directory: Path, *, pretend: bool = False) -> ImportResult:
    """Führt den Import aus und sammelt die Ausgabe ein.

    Kein erneutes Autotagging (kein Abgleich mit MusicBrainz/Discogs mehr,
    die Tags stehen ja schon in den Dateien) -- aber nicht netzfrei: fetchart
    lädt bei ``fetch_for_asis: yes`` trotzdem das Cover eines übernommenen
    MusicBrainz-Releases nach, siehe ``beets/config.yaml``. Der Lauf bleibt
    trotzdem kurz genug für ein einzelnes Album, Streaming der Ausgabe ist
    hier nicht nötig.
    """
    command = build_command(directory, pretend=pretend)
    result = ImportResult(command=command, pretend=pretend)
    log.info("Starte %s", " ".join(command))

    # Nur der echte Import schreibt in die Library; ``--pretend`` liest bloß
    # und soll nicht auf einen laufenden Import warten müssen.
    lock = nullcontext() if pretend else library_lock()

    try:
        with lock:
            proc = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.import_timeout,
                check=False,
                # Kein shell=True: die Argumente gehen unverändert an das
                # Programm, damit aus Dateinamen keine Shell-Befehle werden
                # können.
                shell=False,
            )
    except FileNotFoundError:
        result.error = (
            f"'{settings.beet_bin}' wurde nicht gefunden. Pfad zum beets des "
            "Servers über MIMPORT_BEET_BIN setzen."
        )
        return result
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.error = (
            f"Import nach {settings.import_timeout}s abgebrochen. "
            "MIMPORT_IMPORT_TIMEOUT erhöhen, falls das zu knapp ist."
        )
        return result
    except OSError as exc:
        result.error = f"Import konnte nicht gestartet werden: {exc}"
        return result

    result.returncode = proc.returncode
    result.stdout = proc.stdout or ""
    result.stderr = proc.stderr or ""
    if result.returncode != 0 and not result.error:
        result.error = f"beets endete mit Rückgabewert {result.returncode}"
    return result


@dataclass
class ImportJob:
    """Ein echter Import, der im Hintergrund läuft.

    Der Probelauf braucht das nicht -- er schreibt nichts, lädt kein Cover
    nach und passt locker in eine einzelne Anfrage. Ein echter Import kann
    dagegen an der Library-Sperre warten, ein großes Album verschieben und
    ein Cover nachladen; ohne Hintergrundauftrag hinge die Anfrage die ganze
    Zeit reglos, und die Oberfläche zeigte bis zum Schluss nichts als einen
    Spinner-Text.
    """

    session_id: str
    #: Grobe Phase, nicht Zeile für Zeile -- ``run_import`` liefert seine
    #: Ausgabe erst am Ende, es gibt also nur "läuft" und "fertig, räumt auf".
    schritt: str = "beets importiert: Tags übernehmen, Dateien einsortieren, Cover laden …"
    fertig: bool = False
    result: ImportResult | None = None
    started: float = field(default_factory=time.monotonic)
    thread: threading.Thread | None = field(default=None, repr=False)

    @property
    def dauer_text(self) -> str:
        sekunden = int(time.monotonic() - self.started)
        minuten, sek = divmod(sekunden, 60)
        return f"{minuten}:{sek:02d}"


_jobs: dict[str, ImportJob] = {}
_jobs_lock = threading.Lock()


def current(session_id: str) -> ImportJob | None:
    """Der zuletzt gestartete Hintergrund-Import dieser Session, falls es einen gibt."""
    with _jobs_lock:
        return _jobs.get(session_id)


def start_job(
    directory: Path,
    *,
    session_id: str,
    on_done: Callable[[ImportResult], None] | None = None,
) -> ImportJob:
    """Startet den echten Import im Hintergrund und kehrt sofort zurück.

    ``on_done`` läuft nur bei Erfolg, im Hintergrundthread selbst -- dorthin
    gehören Cover-Nachladen und Aufräumen (siehe ``backend.routes``), damit
    die Anfrage, die den Import ausgelöst hat, nicht auch noch darauf warten
    muss.

    Wirft nichts: Ein Absturz im Hintergrundthread bliebe sonst unbemerkt und
    der Auftrag stünde für immer auf "läuft" -- genau die Falle, die einst
    beim m4b-Bau schon mal zuschlug (siehe README, "Wenn ffmpeg hängen
    bleibt"). Der Fehler landet stattdessen sichtbar im Ergebnis.
    """
    job = ImportJob(session_id=session_id)
    with _jobs_lock:
        _jobs[session_id] = job

    def _arbeite() -> None:
        try:
            result = run_import(directory, pretend=False)
            job.result = result
            if result.ok and on_done is not None:
                job.schritt = "Import fertig, prüfe Cover und räume auf …"
                on_done(result)
        except Exception as exc:
            log.exception("Import-Auftrag für Session %s abgebrochen", session_id)
            job.result = ImportResult(
                command=build_command(directory, pretend=False),
                error=f"Import unerwartet abgebrochen: {exc}",
            )
        finally:
            job.fertig = True

    thread = threading.Thread(target=_arbeite, name=f"import-{session_id}", daemon=True)
    job.thread = thread
    thread.start()
    return job
