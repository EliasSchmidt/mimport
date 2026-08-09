"""Formaterkennung: ist eine hochgeladene Datei verlustfrei?

Die Prüfung im Browser (``static/index.js``) kann nur die Endung und ein paar
Magic Bytes lesen und liegt bei ``.m4a`` grundsätzlich im Dunkeln, weil dort
ALAC und AAC im selben Container stecken. Hier auf dem Server liefert mediafile
die verbindliche Antwort -- und mediafile kommt als beets-Abhängigkeit sowieso
mit, deshalb brauchen wir weder ffprobe noch ffmpeg.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import mediafile

Quality = Literal["lossless", "lossy", "unknown"]

#: mediafile benennt Formate über ``mediafile.TYPES``. Diese Namen sind die
#: verbindliche Zuordnung, nicht die Dateiendung.
LOSSLESS_FORMATS = frozenset(
    {"FLAC", "ALAC", "APE", "WavPack", "AIFF", "WAVE", "DSD Stream File"}
)
LOSSY_FORMATS = frozenset({"MP3", "AAC", "OGG", "Opus", "Musepack", "Windows Media"})


@dataclass
class AudioInfo:
    """Was wir über eine einzelne Datei wissen."""

    path: Path
    #: Anzeigename inklusive Unterordner, wie der Nutzer ihn hochgeladen hat.
    display_name: str
    quality: Quality = "unknown"
    #: mediafile-Format, z. B. ``FLAC``. Leer, wenn nicht lesbar.
    format: str = ""
    bitdepth: int = 0
    samplerate: int = 0
    bitrate: int = 0
    length: float = 0.0
    #: Kurzer Klartext für die Oberfläche.
    detail: str = ""
    #: Gesetzt, wenn die Datei gar nicht als Audio lesbar war.
    error: str = ""

    @property
    def is_readable(self) -> bool:
        return not self.error

    @property
    def summary(self) -> str:
        """Eine Zeile für die Dateiliste, z. B. ``FLAC 44.1 kHz/16 bit``."""
        if self.error:
            return self.error
        parts = [self.format or "unbekanntes Format"]
        if self.samplerate:
            parts.append(f"{round(self.samplerate / 1000, 1)} kHz")
        if self.bitdepth:
            parts.append(f"{self.bitdepth} bit")
        elif self.bitrate:
            parts.append(f"{round(self.bitrate / 1000)} kbps")
        if self.length:
            parts.append(f"{int(self.length // 60)}:{int(self.length % 60):02d}")
        return " · ".join(parts)


def classify_format(fmt: str, bitdepth: int = 0) -> Quality:
    """Ordnet ein mediafile-Format als verlustfrei oder verlustbehaftet ein.

    ``bitdepth`` dient nur als Rückfallebene für Formate, die wir namentlich
    nicht kennen: verlustbehaftete Codecs melden dort 0, weil sie im
    Frequenzbereich arbeiten und keine Samples mit fester Bittiefe speichern.
    """
    if fmt in LOSSLESS_FORMATS:
        return "lossless"
    if fmt in LOSSY_FORMATS:
        return "lossy"
    if bitdepth > 0:
        return "lossless"
    return "unknown"


def inspect_file(path: Path, display_name: str | None = None) -> AudioInfo:
    """Liest eine Datei mit mediafile und beurteilt ihre Qualität."""
    info = AudioInfo(path=path, display_name=display_name or path.name)
    try:
        media = mediafile.MediaFile(path)
    except mediafile.FileTypeError:
        info.error = "Kein erkennbares Audioformat"
        return info
    except mediafile.UnreadableFileError as exc:
        # Nur den Grund zeigen, nicht den ganzen Serverpfad: mediafile stellt
        # ihn der Meldung voran, und in der Dateiliste steht der Name ohnehin
        # schon daneben. Auf einem Handy schiebt der Pfad alles Lesbare weg.
        grund = str(exc).rsplit(": ", 1)[-1].strip() or "unbekannter Grund"
        info.error = f"Datei nicht lesbar: {grund}"
        return info
    except Exception as exc:  # defekte Uploads sollen nicht die Seite killen
        info.error = f"Fehler beim Lesen: {exc}"
        return info

    info.format = media.format or ""
    info.bitdepth = media.bitdepth or 0
    info.samplerate = media.samplerate or 0
    info.bitrate = media.bitrate or 0
    info.length = media.length or 0.0
    info.quality = classify_format(info.format, info.bitdepth)

    if info.quality == "lossless":
        info.detail = "Verlustfrei"
    elif info.quality == "lossy":
        info.detail = (
            f"Verlustbehaftet ({info.format}) -- verlustfrei wäre besser, "
            "was hier fehlt, lässt sich später nicht zurückholen"
        )
    else:
        info.detail = "Qualität nicht bestimmbar"
    return info


def summarize(infos: list[AudioInfo]) -> dict[str, object]:
    """Zählt die Qualitätsstufen für den Hinweis über der Dateiliste."""
    lossy = [i for i in infos if i.quality == "lossy"]
    unknown = [i for i in infos if i.quality == "unknown" and i.is_readable]
    unreadable = [i for i in infos if not i.is_readable]
    return {
        "total": len(infos),
        "lossless": sum(1 for i in infos if i.quality == "lossless"),
        "lossy": len(lossy),
        "unknown": len(unknown),
        "unreadable": len(unreadable),
        "lossy_names": [i.display_name for i in lossy],
        "unreadable_names": [i.display_name for i in unreadable],
        #: Nur ein Hinweis, keine Blockade -- der Nutzer entscheidet selbst.
        "warn": bool(lossy),
    }
