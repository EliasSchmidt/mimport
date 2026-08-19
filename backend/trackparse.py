"""Einfache, bewusst vorhersehbare Parser für OCR-Tracklisten.

Der Zweck ist nicht, semantisch "klug" zu raten, sondern häufige Layouts mit
klaren Regeln in ein editierbares Formular zu überführen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ParsedTrack:
    """Ein geparster Track aus einer OCR-Zeile."""

    number: str = ""
    artist: str = ""
    title: str = ""
    duration: str = ""


_MODE_LABELS: dict[str, str] = {
    "plain_title": "Nur Titel (eine Zeile = ein Track)",
    "track_title": "Tracknr + Titel (z. B. 01 Titel)",
    "track_dash_title": "Tracknr - Titel (z. B. 01 - Titel)",
    "track_title_duration": "Tracknr Titel Dauer (z. B. 01 Titel 3:45)",
    "artist_dash_title": "Artist - Titel",
    "track_artist_dash_title": "Tracknr Artist - Titel",
    "track_artist_dash_title_duration": "Tracknr Artist - Titel - 3:45",
}


def modes() -> list[dict[str, str]]:
    """Verfügbare Parser-Modi für die Oberfläche."""
    return [{"value": key, "label": value} for key, value in _MODE_LABELS.items()]


_DURATION_RE = re.compile(r"(?P<dur>\d{1,2}:\d{2}(?::\d{2})?)$")
_TRACK_PREFIX_RE = re.compile(r"^\s*(?P<num>\d{1,2})\s*[.)\-:]?\s*(?P<rest>.*)$")


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_duration(value: str) -> tuple[str, str]:
    text = value.strip()
    match = _DURATION_RE.search(text)
    if not match:
        return text, ""
    start = match.start("dur")
    return text[:start].rstrip(" -–—\t"), match.group("dur")


def _strip_track_prefix(line: str) -> tuple[str, str]:
    match = _TRACK_PREFIX_RE.match(line)
    if not match:
        return "", line.strip()
    return match.group("num"), match.group("rest").strip()


def _split_artist_title(text: str) -> tuple[str, str]:
    if " - " not in text:
        return "", text.strip()
    artist, title = text.split(" - ", 1)
    return artist.strip(), title.strip()


def parse_text(text: str, mode: str) -> list[ParsedTrack]:
    """Wendet einen Parser-Modus auf OCR-Rohtext an."""
    lines = _split_lines(text)
    result: list[ParsedTrack] = []

    for raw in lines:
        line, duration = _extract_duration(raw)
        number = ""
        artist = ""
        title = line

        if mode == "plain_title":
            pass
        elif mode == "track_title" or mode == "track_dash_title" or mode == "track_title_duration":
            number, rest = _strip_track_prefix(line)
            title = rest or line
        elif mode == "artist_dash_title":
            artist, parsed_title = _split_artist_title(line)
            title = parsed_title or line
        elif mode == "track_artist_dash_title" or mode == "track_artist_dash_title_duration":
            number, rest = _strip_track_prefix(line)
            artist, parsed_title = _split_artist_title(rest)
            title = parsed_title or rest or line
        else:
            # Unbekannter Modus: robust auf den einfachsten Modus zurückfallen.
            title = line

        result.append(
            ParsedTrack(
                number=number,
                artist=artist,
                title=title.strip(),
                duration=duration,
            )
        )

    return result
