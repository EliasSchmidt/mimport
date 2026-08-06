"""Erzeugt minimale, aber gültige FLAC-Dateien für die Tests.

Hintergrund: Für einen echten Durchlauf braucht es Dateien, die mediafile und
mutagen als Audio akzeptieren. Ein Testkorpus mit echter Musik gehört nicht ins
Repository, und ffmpeg soll keine Testvoraussetzung sein.

Eine FLAC-Datei ist minimal gültig mit dem Kennzeichen ``fLaC`` und einem
STREAMINFO-Block. Audio-Frames fehlen -- zum Lesen und Schreiben von Tags, und
damit für alles, was mimport tut, reicht das.
"""

from __future__ import annotations

import struct
from pathlib import Path


def _streaminfo(
    *, samplerate: int, channels: int, bitdepth: int, samples: int
) -> bytes:
    """Baut den 34 Byte langen STREAMINFO-Block.

    Aufbau laut FLAC-Spezifikation: Blockgrößen und Framegrößen, dann ein
    Bitfeld aus Samplerate (20 Bit), Kanälen (3 Bit), Bittiefe (5 Bit) und
    Gesamtsamples (36 Bit), abschließend eine MD5-Summe der Audiodaten.
    """
    block = struct.pack(">HH", 4096, 4096)  # min/max blocksize
    block += b"\x00\x00\x00"  # min framesize (unbekannt)
    block += b"\x00\x00\x00"  # max framesize (unbekannt)

    # 64-Bit-Feld: 20 + 3 + 5 + 36 Bit.
    packed = (
        (samplerate & 0xFFFFF) << 44
        | ((channels - 1) & 0x7) << 41
        | ((bitdepth - 1) & 0x1F) << 36
        | (samples & 0xFFFFFFFFF)
    )
    block += struct.pack(">Q", packed)
    block += b"\x00" * 16  # MD5 der Audiodaten -- ohne Frames irrelevant
    assert len(block) == 34
    return block


def write_flac(
    path: Path,
    *,
    samplerate: int = 44100,
    channels: int = 2,
    bitdepth: int = 16,
    seconds: float = 180.0,
) -> Path:
    """Schreibt eine gültige FLAC-Datei ohne Audioinhalt."""
    path.parent.mkdir(parents=True, exist_ok=True)
    info = _streaminfo(
        samplerate=samplerate,
        channels=channels,
        bitdepth=bitdepth,
        samples=int(samplerate * seconds),
    )
    # Metadaten-Blockkopf: letzter Block (0x80) + Typ 0 (STREAMINFO) + Länge.
    header = b"\x80" + len(info).to_bytes(3, "big")
    path.write_bytes(b"fLaC" + header + info)
    return path


def write_album(
    directory: Path,
    tracks: list[tuple[str, int, float]],
    *,
    artist: str = "The Beatles",
    album: str = "Abbey Road",
) -> list[Path]:
    """Legt einen Albumordner mit getaggten FLAC-Dateien an.

    ``tracks`` ist eine Liste aus (Titel, Tracknummer, Länge in Sekunden).
    """
    import mediafile

    paths: list[Path] = []
    for title, number, length in tracks:
        path = directory / f"{number:02d} {_safe(title)}.flac"
        write_flac(path, seconds=length)

        media = mediafile.MediaFile(path)
        media.artist = artist
        media.albumartist = artist
        media.album = album
        media.title = title
        media.track = number
        media.save()
        paths.append(path)
    return paths


def _safe(name: str) -> str:
    return "".join(c if c not in '/\\:*?"<>|' else "_" for c in name)
