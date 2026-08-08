"""Eine Audio-CD rippen.

Eine Audio-CD (CDDA) hat kein Dateisystem -- man kann ihre Tracks nicht
kopieren, sie müssen ausgelesen und in Dateien gegossen werden. Das ist der
Unterschied zur Daten-CD in ``backend.disc``.

Zwei Werkzeuge, klar getrennt: ``cdparanoia`` liest die Sektoren (und liest
notfalls mehrfach, daher der Name), ``flac`` packt sie verlustfrei. Bewusst
nicht ``abcde``: das würde zusätzlich taggen, und die Tags setzt mimport
selbst -- siehe ``backend.tagging``.

Gerippt wird **Track für Track**, nicht die ganze CD in einem Aufruf. Das
kostet nichts und bringt zweierlei: der Fortschritt ist exakt bekannt, und ein
unlesbarer Track reißt nicht den gesamten Lauf mit.

Warum ein Hintergrundauftrag: ein Rip dauert 10 bis 40 Minuten. Anders als der
Import, den man aussitzen kann, will man hier zusehen. Es gibt genau ein
Laufwerk, also auch genau einen Auftrag zur Zeit -- mehr Verwaltung braucht es
nicht.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from backend import discid, sessions
from backend.config import settings

log = logging.getLogger(__name__)

#: Zustände eines Auftrags. ``fertig`` und ``fehler`` sind Endzustände.
ZUSTAENDE = ("liest_toc", "rippt", "fertig", "fehler")


class RipError(Exception):
    """Der Rip ließ sich nicht starten oder brach ab."""


@dataclass
class RipJob:
    """Ein laufender oder abgeschlossener Rip.

    Wird aus dem Arbeits-Thread beschrieben und aus den Anfrage-Threads
    gelesen. Die einzelnen Felder sind kleine, für sich atomare Werte -- für
    eine Fortschrittsanzeige genügt das, ein Lock je Zugriff wäre Zierrat.
    """

    zustand: str = "liest_toc"
    track: int = 0
    tracks_gesamt: int = 0
    meldung: str = "Lese das Inhaltsverzeichnis …"
    session_id: str | None = None
    disc_id: str | None = None
    releases: list[discid.ReleaseHint] = field(default_factory=list)
    fehler: str | None = None

    #: "musik" geht über Staging, Match und beets. "hoerbuch" schreibt direkt
    #: in die Hörbuch-Bibliothek und endet dort.
    modus: str = "musik"
    buch: str | None = None
    disc_ordner: str | None = None

    @property
    def laeuft(self) -> bool:
        return self.zustand not in ("fertig", "fehler")

    @property
    def prozent(self) -> int:
        if not self.tracks_gesamt:
            return 0
        return min(100, round(100 * self.track / self.tracks_gesamt))

    @property
    def buch_anzeige(self) -> str:
        """Autor und Titel, wie sie im Pfad stehen."""
        if not self.buch:
            return ""
        pfad = Path(self.buch)
        return f"{pfad.parent.name} – {pfad.name}"

    @property
    def disc_anzeige(self) -> str:
        """Welche Disc gerade gelesen wird, etwa ``CD 3``."""
        return Path(self.disc_ordner).name if self.disc_ordner else ""


#: Es gibt ein Laufwerk, also einen Auftrag. Kein Verzeichnis, keine IDs.
_job: RipJob | None = None
_job_lock = threading.Lock()


def current() -> RipJob | None:
    """Der aktuelle Auftrag, falls es einen gibt."""
    return _job


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Ruft ein Programm auf und gibt das Ergebnis zurück.

    Kein ``shell=True``: Track-Nummern und Pfade gehen unverändert an das
    Programm, damit daraus keine Shell-Befehle werden können.
    """
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        shell=False,
    )


def read_toc() -> discid.Toc:
    """Liest das Inhaltsverzeichnis der eingelegten CD."""
    try:
        ergebnis = _run(
            [settings.cdparanoia_bin, "-d", settings.cdrom_device, "-Q"],
            timeout=settings.rip_toc_timeout,
        )
    except FileNotFoundError as exc:
        raise RipError(
            f"„{settings.cdparanoia_bin}“ wurde nicht gefunden. Ohne cdparanoia "
            "lässt sich keine Audio-CD lesen."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise RipError(
            "Das Laufwerk hat nicht geantwortet. Liegt eine CD ein?"
        ) from exc
    except OSError as exc:
        raise RipError(f"Das Laufwerk ließ sich nicht ansprechen: {exc}") from exc

    # cdparanoia schreibt das Inhaltsverzeichnis nach stderr.
    ausgabe = (ergebnis.stderr or "") + (ergebnis.stdout or "")
    try:
        return discid.parse_cdparanoia_toc(ausgabe)
    except discid.DiscIdError as exc:
        raise RipError(str(exc)) from exc


def _rip_track(nummer: int, ziel: Path) -> None:
    """Liest einen Track und schreibt ihn als FLAC nach ``ziel``."""
    wav = ziel.with_suffix(".wav")
    try:
        ergebnis = _run(
            [
                settings.cdparanoia_bin,
                "-d",
                settings.cdrom_device,
                "--",
                str(nummer),
                str(wav),
            ],
            timeout=settings.rip_track_timeout,
        )
        if ergebnis.returncode != 0 or not wav.exists():
            raise RipError(
                f"Track {nummer} ließ sich nicht lesen. "
                f"{(ergebnis.stderr or '').strip()[-200:]}"
            )

        # Die Tracknummer muss mit. Sie ist das Einzige, was eine frisch
        # gerippte Datei über sich weiß, und ohne sie ordnet beets die Dateien
        # allein nach Spieldauer den Tracks zu -- bei ähnlich langen Stücken
        # kommt dabei eine vertauschte Reihenfolge heraus. Mit ihr trifft die
        # Zuordnung exakt. Direkt beim Packen gesetzt, damit es kein zweiter
        # Schreibvorgang wird.
        packen = _run(
            [
                settings.flac_bin,
                "--best",
                "--silent",
                "-f",
                f"--tag=TRACKNUMBER={nummer}",
                "-o",
                str(ziel),
                str(wav),
            ],
            timeout=settings.rip_track_timeout,
        )
        if packen.returncode != 0 or not ziel.exists():
            raise RipError(
                f"Track {nummer} ließ sich nicht in FLAC packen. "
                f"{(packen.stderr or '').strip()[-200:]}"
            )
    except FileNotFoundError as exc:
        raise RipError(f"Programm nicht gefunden: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RipError(
            f"Track {nummer} dauerte zu lange. Ist die CD stark zerkratzt?"
        ) from exc
    finally:
        # Das WAV ist Zwischenprodukt und belegt das Vierfache des FLAC.
        wav.unlink(missing_ok=True)


def _arbeite(
    job: RipJob,
    toc: discid.Toc,
    zielordner: Path,
    *,
    bei_fehler: Callable[[], None],
    mit_lookup: bool = True,
) -> None:
    """Der eigentliche Rip. Läuft im Hintergrund-Thread.

    ``bei_fehler`` wird bewusst von außen gegeben und nicht aus ``zielordner``
    abgeleitet: Bei Musik ist das Ziel eine Wegwerf-Session, die im Fehlerfall
    komplett verschwinden soll. Bei einem Hörbuch ist es der Ordner *einer*
    Disc innerhalb eines Buchs, in dem schon Stunden Arbeit aus vorherigen
    Discs liegen können -- dort darf nur die angefangene Disc weg.
    """
    try:
        job.tracks_gesamt = toc.track_count
        job.zustand = "rippt"

        for index, nummer in enumerate(
            range(toc.first_track, toc.first_track + toc.track_count), start=1
        ):
            job.track = index - 1
            job.meldung = f"Lese Track {index} von {toc.track_count} …"
            ziel = zielordner / f"{index:02d} Track {index}.flac"
            _rip_track(nummer, ziel)
            job.track = index

        if mit_lookup:
            job.meldung = "Frage MusicBrainz nach der CD …"
            try:
                job.releases = discid.lookup(job.disc_id or "")
            except discid.DiscIdError as exc:
                # Kein Grund, den fertigen Rip zu verwerfen -- die Suche geht
                # auch von Hand.
                log.warning("DiscID-Abfrage fehlgeschlagen: %s", exc)
                job.releases = []

        job.zustand = "fertig"
        job.meldung = f"{toc.track_count} Tracks gelesen."
        log.info("Rip fertig: %s (%s)", zielordner, job.disc_id)

    except RipError as exc:
        job.zustand = "fehler"
        job.fehler = str(exc)
        job.meldung = "Der Rip ist fehlgeschlagen."
        bei_fehler()
        log.warning("Rip abgebrochen: %s", exc)
    except Exception as exc:  # noqa: BLE001 -- der Thread darf nie still sterben
        job.zustand = "fehler"
        job.fehler = f"Unerwarteter Fehler: {exc}"
        job.meldung = "Der Rip ist fehlgeschlagen."
        bei_fehler()
        log.exception("Rip mit unerwartetem Fehler abgebrochen")


def start(*, allowance: int) -> RipJob:
    """Startet einen Rip, wenn Laufwerk und Platz es hergeben.

    ``allowance`` ist die Obergrenze in Bytes; geprüft wird gegen die
    *unkomprimierte* Größe der CD, obwohl FLAC deutlich darunter landet. Nach
    oben abzuschätzen ist hier richtig -- der Platz muss zwischendurch auch für
    das WAV reichen.
    """
    job, toc = _vorbereiten(allowance)

    session = sessions.create_session()
    job.session_id = session.session_id
    job.meldung = f"Lese {toc.track_count} Tracks …"

    _starten(
        job,
        toc,
        session.directory,
        # Eine Musik-Session ist ein Wegwerfordner: schlägt der Rip fehl, soll
        # nichts davon übrig bleiben.
        bei_fehler=lambda: _session_verwerfen(job),
    )
    return job


def _vorbereiten(allowance: int) -> tuple[RipJob, discid.Toc]:
    """Auftrag anlegen, Inhaltsverzeichnis lesen, Platz prüfen.

    Der Teil, den beide Betriebsarten gemeinsam haben. Danach unterscheiden
    sie sich nur noch darin, wohin geschrieben und was im Fehlerfall
    aufgeräumt wird.
    """
    global _job

    with _job_lock:
        if _job is not None and _job.laeuft:
            raise RipError("Es läuft bereits ein Rip. Es gibt nur ein Laufwerk.")
        job = RipJob()
        _job = job

    try:
        toc = read_toc()
    except RipError as exc:
        job.zustand = "fehler"
        job.fehler = str(exc)
        job.meldung = "Die CD ließ sich nicht lesen."
        raise

    if toc.raw_bytes > allowance:
        job.zustand = "fehler"
        job.fehler = (
            f"Für diese CD ist zu wenig Platz frei: sie braucht beim Lesen bis "
            f"zu {toc.raw_bytes / 1024**3:.1f} GB."
        )
        job.meldung = "Zu wenig Platz."
        raise RipError(job.fehler)

    job.disc_id = discid.calculate(toc)
    job.tracks_gesamt = toc.track_count
    return job, toc


def _session_verwerfen(job: RipJob) -> None:
    if job.session_id:
        sessions.delete_session(job.session_id)
        job.session_id = None


def _starten(
    job: RipJob,
    toc: discid.Toc,
    zielordner: Path,
    *,
    bei_fehler: Callable[[], None],
    mit_lookup: bool = True,
) -> None:
    zielordner.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(
        target=_arbeite,
        args=(job, toc, zielordner),
        kwargs={"bei_fehler": bei_fehler, "mit_lookup": mit_lookup},
        name="mimport-rip",
        daemon=True,
    )
    thread.start()


def start_audiobook(*, allowance: int, buch: Path, disc_ordner: Path) -> RipJob:
    """Rippt eine Hörbuch-CD in den Ordner eines Buchs.

    Kein DiscID-Lookup: MusicBrainz kennt Hörbücher praktisch nicht, und die
    Metadaten holt sich später Audiobookshelf über Audible. Der Rip endet
    hier, es folgt kein Match und kein beets-Import.
    """
    job, toc = _vorbereiten(allowance)
    job.modus = "hoerbuch"
    job.buch = str(buch)
    job.disc_ordner = str(disc_ordner)
    job.meldung = f"Lese {toc.track_count} Tracks …"

    _starten(
        job,
        toc,
        disc_ordner,
        # Nur die angefangene Disc, niemals das Buch: dort liegen womöglich
        # schon Stunden Arbeit aus vorherigen CDs.
        bei_fehler=lambda: shutil.rmtree(disc_ordner, ignore_errors=True),
        mit_lookup=False,
    )
    return job


def reset() -> None:
    """Vergisst einen abgeschlossenen Auftrag, damit ein neuer starten kann."""
    global _job

    with _job_lock:
        if _job is not None and _job.laeuft:
            raise RipError("Der laufende Rip lässt sich nicht verwerfen.")
        _job = None


def tools_available() -> dict[str, bool]:
    """Ist alles da, was zum Rippen nötig ist?

    Das Laufwerk zählt mit: beide Dienste laufen dasselbe Image, cdparanoia ist
    also überall vorhanden. Nur wo das Gerät auch hereingereicht wurde, ergibt
    der Rip-Bereich Sinn -- sonst böte der Upload-Dienst einen Knopf an, der
    nur scheitern kann. Wie beim Daten-CD-Pfad entscheidet die Umgebung, nicht
    ein Schalter.

    Dieselbe Naht wie ``fingerprint_available()``: eine Stelle, die Tests
    ersetzen können, statt ``shutil`` global zu verbiegen.
    """
    return {
        "cdparanoia": shutil.which(settings.cdparanoia_bin) is not None,
        "flac": shutil.which(settings.flac_bin) is not None,
        "device": Path(settings.cdrom_device).exists(),
    }
