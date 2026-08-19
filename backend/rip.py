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
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from backend import discid, sessions
from backend.config import AUDIO_EXTENSIONS, settings

log = logging.getLogger(__name__)

#: Zustände eines Auftrags. ``fertig`` und ``fehler`` sind Endzustände.
ZUSTAENDE = ("liest_toc", "rippt", "fertig", "fehler")

#: Wie ein Disc-Unterordner innerhalb einer Musik-Session heißt -- dieselbe
#: Konvention wie bei Hörbüchern (``backend.audiobook._DISC_RE``).
_DISC_RE = re.compile(r"^CD (\d+)$")


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

    #: Hat dieser Auftrag seine Session selbst angelegt (True), oder ist er
    #: einer bereits bestehenden beigetreten, um eine weitere Disc eines
    #: Mehrfach-CD-Albums anzuhängen (False)? Nur wer die Session angelegt
    #: hat, darf sie beim Verwerfen auch löschen -- sonst risse "Verwerfen"
    #: nach einer fehlgeschlagenen zweiten Disc die bereits gelesene erste
    #: mit weg.
    neue_session: bool = True

    @property
    def laeuft(self) -> bool:
        return self.zustand not in ("fertig", "fehler")

    #: Fortschritt innerhalb des laufenden Tracks, 0 bis 1.
    track_anteil: float = 0.0

    #: Wann der Auftrag begann und endete. Die Dauer steht in der Oberfläche,
    #: weil sie sonst niemand kennt: „10 bis 40 Minuten" war eine Schätzung,
    #: und für Zeitlimits und Bitraten braucht es gemessene Werte.
    gestartet: float = field(default_factory=time.monotonic)
    beendet: float | None = None

    #: Was cdparanoia gerade tut, sofern es nicht bloß liest -- „Kratzer
    #: erkannt", „liest langsamer" und Ähnliches.
    muehsam: str = ""

    @property
    def prozent(self) -> int:
        """Fortschritt über die ganze CD.

        Der angefangene Track zählt anteilig mit: bei neun Tracks wäre ein
        Balken sonst neun Sprünge, und ein zäher Track sähe wie ein Stillstand
        aus.
        """
        if not self.tracks_gesamt:
            return 0
        fertig = self.track + min(1.0, max(0.0, self.track_anteil))
        return min(100, round(100 * fertig / self.tracks_gesamt))

    @property
    def dauer(self) -> float:
        """Sekunden seit dem Start, beim fertigen Auftrag die Gesamtzeit."""
        ende = self.beendet if self.beendet is not None else time.monotonic()
        return max(0.0, ende - self.gestartet)

    @property
    def dauer_text(self) -> str:
        gesamt = int(self.dauer)
        stunden, rest = divmod(gesamt, 3600)
        minuten, sekunden = divmod(rest, 60)
        if stunden:
            return f"{stunden}:{minuten:02d}:{sekunden:02d}"
        return f"{minuten}:{sekunden:02d}"

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


def _lesen(
    nummer: int, wav: Path, fortschritt: Callable[[str, int], None] | None
) -> int:
    """Ruft cdparanoia auf und verfolgt seine Meldungen mit.

    Gibt den Rückgabewert zurück. Die letzten Ausgabezeilen wandern ins Log --
    bei einem Fehler steht dort, woran es lag, ohne dass sie in die Meldung an
    den Nutzer müssen.
    """
    befehl = [
        settings.cdparanoia_bin,
        "-d",
        settings.cdrom_device,
        # Meldungen nach stderr, auch ohne Terminal.
        "-e",
        "--",
        str(nummer),
        str(wav),
    ]
    try:
        prozess = subprocess.Popen(
            befehl,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RipError(
            f"„{settings.cdparanoia_bin}“ wurde nicht gefunden."
        ) from exc

    letzte: list[str] = []
    assert prozess.stderr is not None
    for zeile in prozess.stderr:
        stand = parse_progress(zeile)
        if stand is not None:
            if fortschritt is not None:
                fortschritt(*stand)
            continue
        # Alles, was keine Fortschrittsmeldung ist, kann eine Fehlerursache
        # sein -- die letzten Zeilen genügen dafür.
        if zeile.strip():
            letzte.append(zeile.strip())
            del letzte[:-10]

    try:
        prozess.wait(timeout=settings.rip_track_timeout)
    except subprocess.TimeoutExpired as exc:
        prozess.kill()
        raise RipError(
            f"Track {nummer} dauerte zu lange. Ist die CD stark zerkratzt?"
        ) from exc

    if prozess.returncode != 0:
        log.warning("cdparanoia (Track %s): %s", nummer, " | ".join(letzte))
    return prozess.returncode


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
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


#: Eine Fortschrittsmeldung von ``cdparanoia -e``, etwa
#: ``##: 0 [read] @ 1009008``. Die Zahl hinter dem ``@`` steht in Samples.
_FORTSCHRITT = re.compile(r"^##:\s*(-?\d+)\s*\[(\w+)\]\s*@\s*(\d+)")

#: Rückmeldungen, die auf Leseprobleme hindeuten. cdparanoia benennt sie
#: selbst; „read" ist der Normalfall, alles andere heißt, dass es sich mit der
#: Stelle schwertut.
_MUEHSAM = {
    "verify": "prüft nach",
    "fixup_edge": "korrigiert Ränder",
    "fixup_atom": "korrigiert",
    "scratch": "Kratzer erkannt",
    "repair": "repariert",
    "skip": "Stelle übersprungen",
    "drift": "Drift",
    "backoff": "liest langsamer",
    "overlap": "sucht Überlappung",
    "readerr": "Lesefehler",
}


def parse_progress(zeile: str) -> tuple[str, int] | None:
    """Liest eine Fortschrittszeile: gibt Zustand und Sektor zurück.

    ``None``, wenn die Zeile keine Fortschrittsmeldung ist -- cdparanoia
    schreibt auch Kopfzeilen und Hinweise auf denselben Kanal.
    """
    treffer = _FORTSCHRITT.match(zeile.strip())
    if treffer is None:
        return None
    _, zustand, samples = treffer.groups()
    return zustand.lower(), int(samples) // discid.SAMPLES_PER_SECTOR


def _rip_track(
    nummer: int, ziel: Path, *, fortschritt: Callable[[str, int], None] | None = None
) -> None:
    """Liest einen Track und schreibt ihn als FLAC nach ``ziel``.

    Mit ``-e`` meldet cdparanoia laufend, wo es steht -- die Option ist genau
    dafür gedacht („for wrapper scripts"). Das lohnt sich weniger wegen der
    Feinheit als bei zerkratzten CDs: liest das Laufwerk dieselbe Stelle
    minutenlang neu, stünde der Balken sonst still und man wüsste nicht, ob
    noch etwas passiert.
    """
    wav = ziel.with_suffix(".wav")
    try:
        ergebnis = _lesen(nummer, wav, fortschritt)
        if ergebnis != 0 or not wav.exists():
            raise RipError(f"Track {nummer} ließ sich nicht lesen.")

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
    danach: Callable[[], None] | None = None,
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
            job.track_anteil = 0.0
            job.muehsam = ""
            job.meldung = f"Lese Track {index} von {toc.track_count} …"
            ziel = zielordner / f"{index:02d} Track {index}.flac"

            laenge = toc.track_sectors(index - 1)
            start = toc.track_start(index - 1)

            def melden(
                zustand: str, sektor: int, _laenge: int = laenge, _start: int = start
            ) -> None:
                if _laenge > 0:
                    # cdparanoia meldet die Position auf der ganzen CD, nicht
                    # innerhalb des Tracks. Bei Track 1 fällt beides zusammen
                    # -- deshalb war der Fehler an einer Beispielausgabe von
                    # Track 1 nicht zu sehen und der Balken ab Track 2 sofort
                    # bei hundert Prozent.
                    im_track = sektor - _start if sektor >= _start else sektor
                    anteil = min(1.0, max(0.0, im_track / _laenge))
                    # Nur vorwärts: bei einer schwierigen Stelle liest
                    # cdparanoia zurück und noch einmal ("backoff", "overlap").
                    # Das ist Fehlerkorrektur, kein Rückschritt -- ein Balken,
                    # der zurückspringt, sieht dagegen nach Fehler aus.
                    job.track_anteil = max(job.track_anteil, anteil)
                job.muehsam = _MUEHSAM.get(zustand, "")

            _rip_track(nummer, ziel, fortschritt=melden)
            job.track = index
            job.track_anteil = 0.0
            job.muehsam = ""

        if mit_lookup:
            job.meldung = "Frage MusicBrainz nach der CD …"
            try:
                job.releases = discid.lookup(job.disc_id or "")
            except discid.DiscIdError as exc:
                # Kein Grund, den fertigen Rip zu verwerfen -- die Suche geht
                # auch von Hand.
                log.warning("DiscID-Abfrage fehlgeschlagen: %s", exc)
                job.releases = []

        if danach is not None:
            danach()

        job.beendet = time.monotonic()
        job.zustand = "fertig"
        job.meldung = f"{toc.track_count} Tracks gelesen in {job.dauer_text}."
        log.info("Rip fertig: %s (%s)", zielordner, job.disc_id)

    except RipError as exc:
        job.beendet = time.monotonic()
        job.zustand = "fehler"
        job.fehler = str(exc)
        job.meldung = f"Der Rip ist nach {job.dauer_text} fehlgeschlagen."
        bei_fehler()
        log.warning("Rip abgebrochen: %s", exc)
    except Exception as exc:
        job.beendet = time.monotonic()
        job.zustand = "fehler"
        job.fehler = f"Unerwarteter Fehler: {exc}"
        job.meldung = f"Der Rip ist nach {job.dauer_text} fehlgeschlagen."
        bei_fehler()
        log.exception("Rip mit unerwartetem Fehler abgebrochen")


def start(*, allowance: int, session_id: str | None = None) -> RipJob:
    """Startet einen Rip, wenn Laufwerk und Platz es hergeben.

    ``allowance`` ist die Obergrenze in Bytes; geprüft wird gegen die
    *unkomprimierte* Größe der CD, obwohl FLAC deutlich darunter landet. Nach
    oben abzuschätzen ist hier richtig -- der Platz muss zwischendurch auch für
    das WAV reichen.

    Ohne ``session_id`` entsteht eine neue Session, wie bisher. Mit ``session_id``
    wird stattdessen einer bestehenden Session eine weitere Disc angehängt --
    für Musikalben, die sich über mehrere physische CDs erstrecken. Ohne diesen
    Weg landete die zweite CD entweder in einer eigenen, zweiten Session (zwei
    halbe Alben statt eines) oder überschriebe die erste, weil beide bei
    "01 Track 1.flac" anfangen.
    """
    job, toc = _vorbereiten(allowance)

    if session_id:
        try:
            session = sessions.get_session(session_id)
        except sessions.SessionError as exc:
            job.zustand = "fehler"
            job.fehler = str(exc)
            job.meldung = "Sitzung nicht gefunden."
            raise RipError(job.fehler) from exc
        job.neue_session = False
        zielordner = _naechste_disc(session.directory)
        job.disc_ordner = str(zielordner)
    else:
        session = sessions.create_session()
        zielordner = session.directory

    job.session_id = session.session_id
    job.meldung = f"Lese {toc.track_count} Tracks …"

    _starten(
        job,
        toc,
        zielordner,
        # Eine frische Session ist ein Wegwerfordner: schlägt der Rip fehl,
        # soll nichts davon übrig bleiben. Bei einer weiteren Disc gehören der
        # Session aber schon zuvor gelesene Discs -- dort darf nur der
        # angefangene Disc-Ordner weg.
        bei_fehler=(
            (lambda: _session_verwerfen(job))
            if job.neue_session
            else (lambda: shutil.rmtree(zielordner, ignore_errors=True))
        ),
    )
    return job


def _naechste_disc(session_dir: Path) -> Path:
    """Ordner für die nächste Disc einer Musik-Session, legt ihn gleich an.

    Die erste Disc einer Session liegt flach im Session-Ordner (siehe
    ``start()``) -- ein einsames "CD 1" wäre für die meisten Alben, die aus
    genau einer CD bestehen, unnötiger Ballast. Kommt aber eine zweite Disc
    dazu, muss die erste vorher nach "CD 1" umziehen: sonst kollidieren die
    Dateinamen, die beide Discs gleich vergeben (``01 Track 1.flac`` usw.).
    """
    belegt = {
        int(treffer.group(1))
        for kind in session_dir.iterdir()
        if kind.is_dir() and (treffer := _DISC_RE.match(kind.name))
    }
    flach = [
        p
        for p in session_dir.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    if flach and not belegt:
        ziel = session_dir / "CD 1"
        ziel.mkdir()
        for datei in flach:
            datei.rename(ziel / datei.name)
        belegt = {1}
        log.info("Bisherige Tracks von %s nach „CD 1“ geräumt.", session_dir)

    nummer = 1
    while nummer in belegt:
        nummer += 1
    ziel = session_dir / f"CD {nummer}"
    ziel.mkdir(parents=True, exist_ok=True)
    return ziel


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
    danach: Callable[[], None] | None = None,
) -> None:
    zielordner.mkdir(parents=True, exist_ok=True)
    thread = threading.Thread(
        target=_arbeite,
        args=(job, toc, zielordner),
        kwargs={"bei_fehler": bei_fehler, "mit_lookup": mit_lookup, "danach": danach},
        name="mimport-rip",
        daemon=True,
    )
    thread.start()


def start_audiobook(*, allowance: int, buch: Path, disc_ordner: Path) -> RipJob:
    """Rippt eine Hörbuch-CD in den Ordner eines Buchs.

    Kein DiscID-Lookup: MusicBrainz kennt Hörbücher praktisch nicht, und die
    Metadaten holt sich später Audiobookshelf über Audible. Der Rip endet
    hier, es folgt kein Match und kein beets-Import.

    Geschrieben wird zunächst neben die Bibliothek, nicht hinein: eine halb
    gelesene Disc im Buchordner würde Audiobookshelf bei einem Scan als
    unvollständiges Buch einlesen, und ein Rip dauert eine halbe Stunde. Erst
    am Ende wird der fertige Ordner an seinen Platz geschoben -- auf demselben
    Dateisystem ist das ein Umbenennen und kostet nichts.
    """
    from backend import audiobook

    job, toc = _vorbereiten(allowance)
    job.modus = "hoerbuch"
    job.buch = str(buch)
    job.disc_ordner = str(disc_ordner)
    job.meldung = f"Lese {toc.track_count} Tracks …"

    arbeit = audiobook.neuer_arbeitsordner("disc")

    def ablegen() -> None:
        # Der Zielname erst jetzt: zwischen Start und Ende könnte eine weitere
        # Disc dazugekommen sein, und ein rename auf einen belegten Ordner
        # scheitert.
        buchpfad = Path(job.buch or "")
        audiobook.discs_normalisieren(buchpfad)
        ziel = audiobook.next_disc_dir(buchpfad)
        job.disc_ordner = str(audiobook.fertigstellen(arbeit, ziel))

    _starten(
        job,
        toc,
        arbeit,
        # Nur der Arbeitsordner, niemals das Buch: dort liegen womöglich schon
        # Stunden Arbeit aus vorherigen CDs.
        bei_fehler=lambda: shutil.rmtree(arbeit, ignore_errors=True),
        mit_lookup=False,
        danach=ablegen,
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
