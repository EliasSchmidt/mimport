"""Konfiguration von mimport, komplett über Umgebungsvariablen steuerbar."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

#: Endungen, die wir ohne Rückfrage als verlustfrei betrachten.
LOSSLESS_EXTENSIONS = frozenset(
    {".flac", ".wav", ".aiff", ".aif", ".alac", ".ape", ".wv", ".tta"}
)

#: Endungen, die verlustbehaftet sind.
LOSSY_EXTENSIONS = frozenset(
    {".mp3", ".aac", ".ogg", ".oga", ".opus", ".wma", ".mpc", ".m4b"}
)

#: Endungen, bei denen die Endung allein nichts aussagt. ``.m4a`` kann ALAC
#: (verlustfrei) oder AAC (verlustbehaftet) enthalten, ``.mp4``/``.mka`` sind
#: reine Container. Diese Dateien klärt erst der Server über mediafile.
AMBIGUOUS_EXTENSIONS = frozenset({".m4a", ".mp4", ".mka", ".ogx"})

#: Alles, was beets überhaupt anfassen kann.
AUDIO_EXTENSIONS = LOSSLESS_EXTENSIONS | LOSSY_EXTENSIONS | AMBIGUOUS_EXTENSIONS


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class Settings:
    """Laufzeit-Einstellungen.

    Der Staging-Ordner ist bewusst getrennt von der beets-Library: hierhin
    laden Nutzer hoch, und erst der Import verschiebt die Dateien über beets
    an ihren endgültigen Platz.

    Bewusst nicht ``frozen``, damit Tests einzelne Werte ersetzen können.
    """

    #: Wurzel aller Upload-Sessions. Jede Session bekommt darunter einen
    #: eigenen Unterordner.
    staging_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MIMPORT_STAGING", "./staging")
        )
        .expanduser()
        .resolve()
    )

    #: Wo die eingelegte CD im Dateisystem auftaucht. Gemountet wird auf dem
    #: Host, hereingereicht wird nur der fertige Mount -- damit braucht der
    #: Container weder Zugriff auf ``/dev/sr0`` noch ``CAP_SYS_ADMIN``, das
    #: ``mount()`` sonst verlangen würde.
    #:
    #: Kein Schalter für das CD-Feature: ob dort etwas liegt, *ist* der
    #: Schalter. Ein Dienst ohne eingehängte CD zeigt schlicht „keine CD".
    disc_root: Path = field(
        default_factory=lambda: Path(os.environ.get("MIMPORT_DISC_PATH", "/disc"))
        .expanduser()
        .resolve()
    )

    #: Das ``beet``-Executable. Im Container ist das das beets aus demselben
    #: venv wie mimport -- damit gibt es nur eine beets-Version und keine
    #: Möglichkeit, dass zwei Installationen die Library-Datenbank
    #: gegeneinander migrieren.
    beet_bin: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_BEET_BIN", "beet")
    )

    #: AcoustID-Fingerprinting. Standardmäßig aus, weil es ``fpcalc`` und
    #: einen AcoustID-Key braucht und pro Track ein paar Sekunden CPU kostet.
    fingerprint: bool = field(
        default_factory=lambda: _env_bool("MIMPORT_FINGERPRINT", False)
    )

    #: Obergrenze pro Upload, in Bytes. Ein verlustfreies Album liegt grob bei
    #: 300-500 MB, deshalb ist der Standard großzügig.
    max_upload_bytes: int = field(
        default_factory=lambda: _env_int("MIMPORT_MAX_UPLOAD_BYTES", 4 * 1024**3)
    )

    #: Obergrenze für die Dateianzahl pro Upload.
    max_files: int = field(default_factory=lambda: _env_int("MIMPORT_MAX_FILES", 500))

    #: Sicherheitsabstand auf dem Dateisystem des Staging-Ordners. Der
    #: eigentliche Schutz gegen ein vollgeschriebenes Dateisystem -- eine
    #: Obergrenze allein hilft nicht, wenn die Platte schon vorher voll ist.
    #: Im Container liegen Staging und ``library.db`` auf demselben
    #: Docker-Dateisystem, ein volles Staging nimmt also die Datenbank mit.
    min_free_bytes: int = field(
        default_factory=lambda: _env_int("MIMPORT_MIN_FREE_BYTES", 2 * 1024**3)
    )

    #: Obergrenze für die Summe *aller* Sessions im Staging. Begrenzt, wie viel
    #: sich über viele Uploads hinweg ansammeln kann -- ``max_upload_bytes``
    #: gilt nur je Upload.
    max_staging_bytes: int = field(
        default_factory=lambda: _env_int("MIMPORT_MAX_STAGING_BYTES", 20 * 1024**3)
    )

    #: Nach so vielen Stunden ohne Änderung gilt eine Session als verwaist und
    #: wird weggeräumt. Großzügig, weil zwischen Upload und Entscheidung eine
    #: lange Pause liegen darf.
    session_ttl_hours: int = field(
        default_factory=lambda: _env_int("MIMPORT_SESSION_TTL_HOURS", 24)
    )

    #: Wurzel der Hörbuch-Bibliothek. Bewusst getrennt von der Musik: Hörbücher
    #: laufen nicht über beets -- MusicBrainz kennt sie kaum, und die Metadaten
    #: holt sich Audiobookshelf später selbst über Audible.
    audiobook_root: Path = field(
        default_factory=lambda: Path(
            os.environ.get("MIMPORT_AUDIOBOOKS", "/audiobooks")
        )
        .expanduser()
        .resolve()
    )

    #: Zielbitrate der m4b. 64k reicht für Sprache reichlich; darum geht es ja,
    #: eine Lesung braucht keine Musikqualität.
    audiobook_bitrate: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_M4B_BITRATE", "64k")
    )

    #: Auf einen Kanal mischen. Bei Lesungen halbiert das die Größe, ohne dass
    #: man etwas vermisst.
    audiobook_mono: bool = field(
        default_factory=lambda: _env_bool("MIMPORT_M4B_MONO", True)
    )

    #: Unterhalb dieser Bitrate lohnt das Umwandeln einer verlustbehafteten
    #: Quelle nicht mehr -- es wäre lossy auf lossy.
    audiobook_min_kbps: int = field(
        default_factory=lambda: _env_int("MIMPORT_M4B_MIN_KBPS", 96)
    )

    ffmpeg_bin: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_FFMPEG", "ffmpeg")
    )
    ffprobe_bin: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_FFPROBE", "ffprobe")
    )

    #: Zeitlimit für den m4b-Bau. Ein langes Hörbuch auf schwacher CPU braucht
    #: Stunden.
    m4b_timeout: int = field(
        default_factory=lambda: _env_int("MIMPORT_M4B_TIMEOUT", 6 * 3600)
    )

    #: Wie lange ffmpeg schweigen darf, bevor er als hängend gilt. Das ist das
    #: schärfere Kriterium: ein ehrlicher Encode auf dem alten Laptop läuft
    #: stundenlang, meldet dabei aber im Sekundentakt Fortschritt. Ein hängender
    #: meldet gar nichts mehr -- und darauf lässt sich viel früher reagieren als
    #: auf eine Wanduhr, die für den Normalfall großzügig stehen muss.
    m4b_stillstand: int = field(
        default_factory=lambda: _env_int("MIMPORT_M4B_STILLSTAND", 15 * 60)
    )

    #: Das CD-Laufwerk für Audio-CDs. Anders als bei der Daten-CD hilft kein
    #: Mount vom Host: eine Audio-CD hat kein Dateisystem, ihre Sektoren müssen
    #: direkt aus dem Gerät gelesen werden.
    cdrom_device: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_CDROM", "/dev/sr0")
    )

    #: Liest die Sektoren, notfalls mehrfach -- daher der Name.
    cdparanoia_bin: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_CDPARANOIA", "cdparanoia")
    )

    #: Packt das Ergebnis verlustfrei.
    flac_bin: str = field(
        default_factory=lambda: os.environ.get("MIMPORT_FLAC", "flac")
    )

    #: Zeitlimit für das Auslesen des Inhaltsverzeichnisses. Kurz -- ein
    #: Laufwerk ohne CD soll nicht minutenlang hängen.
    rip_toc_timeout: int = field(
        default_factory=lambda: _env_int("MIMPORT_RIP_TOC_TIMEOUT", 60)
    )

    #: Zeitlimit je Track. Großzügig: bei einer zerkratzten CD liest
    #: cdparanoia dieselbe Stelle viele Male.
    rip_track_timeout: int = field(
        default_factory=lambda: _env_int("MIMPORT_RIP_TRACK_TIMEOUT", 1200)
    )

    #: Verschiebt der Import die Dateien (``-m``) statt sie zu kopieren? Move
    #: ist der Standard, damit der Staging-Ordner nicht zuläuft.
    move_on_import: bool = field(default_factory=lambda: _env_bool("MIMPORT_MOVE", True))

    #: Zeitlimit für einen Import-Subprozess in Sekunden.
    import_timeout: int = field(
        default_factory=lambda: _env_int("MIMPORT_IMPORT_TIMEOUT", 1800)
    )

    def fingerprint_available(self) -> bool:
        """Ist Fingerprinting eingeschaltet *und* benutzbar?

        ``fpcalc`` kommt aus chromaprint und ist ein separates Systempaket;
        ohne das Binary hilft der eingeschaltete Schalter nichts.
        """
        if not self.fingerprint:
            return False
        return shutil.which("fpcalc") is not None

    def staging_free_bytes(self) -> int:
        """Freier Platz auf dem Dateisystem des Staging-Ordners.

        Eigene Methode, damit Tests eine volle Platte vortäuschen können, ohne
        ``shutil`` global zu ersetzen -- dieselbe Naht wie bei
        ``fingerprint_available()``.

        Lässt sich der Platz nicht ermitteln, gilt das als ``0``: dann nimmt
        mimport lieber keinen Upload mehr an, statt blind weiterzuschreiben.
        """
        try:
            return shutil.disk_usage(self.staging_root).free
        except OSError:
            log.warning(
                "Freier Platz unter %s nicht ermittelbar -- Uploads werden "
                "vorsorglich abgewiesen.",
                self.staging_root,
            )
            return 0


settings = Settings()
