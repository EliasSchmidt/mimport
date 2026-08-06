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

    @property
    def laeuft(self) -> bool:
        return self.zustand not in ("fertig", "fehler")

    @property
    def prozent(self) -> int:
        if not self.tracks_gesamt:
            return 0
        return min(100, round(100 * self.track / self.tracks_gesamt))


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


def _arbeite(job: RipJob, toc: discid.Toc, session: sessions.StagingSession) -> None:
    """Der eigentliche Rip. Läuft im Hintergrund-Thread."""
    try:
        job.tracks_gesamt = toc.track_count
        job.zustand = "rippt"

        for index, nummer in enumerate(
            range(toc.first_track, toc.first_track + toc.track_count), start=1
        ):
            job.track = index - 1
            job.meldung = f"Lese Track {index} von {toc.track_count} …"
            ziel = session.directory / f"{index:02d} Track {index}.flac"
            _rip_track(nummer, ziel)
            job.track = index

        job.meldung = "Frage MusicBrainz nach der CD …"
        try:
            job.releases = discid.lookup(job.disc_id or "")
        except discid.DiscIdError as exc:
            # Kein Grund, den fertigen Rip zu verwerfen -- die Suche geht auch
            # von Hand.
            log.warning("DiscID-Abfrage fehlgeschlagen: %s", exc)
            job.releases = []

        job.zustand = "fertig"
        job.meldung = f"{toc.track_count} Tracks gelesen."
        log.info("Rip fertig: %s (%s)", session.session_id, job.disc_id)

    except RipError as exc:
        job.zustand = "fehler"
        job.fehler = str(exc)
        job.meldung = "Der Rip ist fehlgeschlagen."
        sessions.delete_session(session.session_id)
        job.session_id = None
        log.warning("Rip abgebrochen: %s", exc)
    except Exception as exc:  # noqa: BLE001 -- der Thread darf nie still sterben
        job.zustand = "fehler"
        job.fehler = f"Unerwarteter Fehler: {exc}"
        job.meldung = "Der Rip ist fehlgeschlagen."
        sessions.delete_session(session.session_id)
        job.session_id = None
        log.exception("Rip mit unerwartetem Fehler abgebrochen")


def start(*, allowance: int) -> RipJob:
    """Startet einen Rip, wenn Laufwerk und Platz es hergeben.

    ``allowance`` ist die Obergrenze in Bytes; geprüft wird gegen die
    *unkomprimierte* Größe der CD, obwohl FLAC deutlich darunter landet. Nach
    oben abzuschätzen ist hier richtig -- der Platz muss zwischendurch auch für
    das WAV reichen.
    """
    global _job

    with _job_lock:
        if _job is not None and _job.laeuft:
            raise RipError(
                "Es läuft bereits ein Rip. Es gibt nur ein Laufwerk."
            )
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
    session = sessions.create_session()
    job.session_id = session.session_id
    job.tracks_gesamt = toc.track_count
    job.meldung = f"Lese {toc.track_count} Tracks …"

    thread = threading.Thread(
        target=_arbeite, args=(job, toc, session), name="mimport-rip", daemon=True
    )
    thread.start()
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
