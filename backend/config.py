"""Konfiguration von mimport, komplett über Umgebungsvariablen steuerbar."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

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


settings = Settings()
