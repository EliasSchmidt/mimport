"""Einfache, bewusst vorhersehbare Parser für OCR-Tracklisten.

Der Zweck ist nicht, semantisch "klug" zu raten, sondern häufige Layouts mit
klaren Regeln in ein editierbares Formular zu überführen.

Statt eines festen Katalogs an Layout-"Modi" (die in Wahrheit immer dieselben
drei Operationen kombinieren) sind das drei unabhängige Schalter: jede Zeile
kann optional eine Tracknummer vorn, eine Trennung von Interpret und Titel,
und eine Dauer am Ende haben. Das deckt jede Kombination ab, ohne dass für
neue Layouts neue Modi erfunden werden müssen.
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


@dataclass(frozen=True)
class ParseFlags:
    """Welche Bestandteile eine Zeile hat -- Grundlage für den Parser.

    Ersetzt den früheren Katalog fester Modi (``track_title_duration`` usw.):
    das waren immer nur Kombinationen dieser drei Schalter, plus zwei
    Modus-Namen, die sich (weil die Dauer schon immer unabhängig vom Modus
    erkannt wurde) in der Praxis gar nicht unterschieden.
    """

    tracknummer: bool = True
    interpret: bool = False
    dauer: bool = True


#: Ziffern *und* ihre klassischen OCR-Verwechslungen ("O" statt "0", "I"/"l"
#: statt "1") -- eine Dauer wie "4:O2" ist auf einem Backcover-Scan der
#: Normalfall, nicht die Ausnahme. Die Position (Doppelpunkt, feste
#: Gruppengröße, Zeilenende) ist eng genug, dass daraus kein echtes Wort
#: fälschlich als Dauer gelesen wird.
_DAUER_ZIFFER = "[0-9OoIl]"
_DURATION_RE = re.compile(
    rf"(?P<dur>{_DAUER_ZIFFER}{{1,2}}:{_DAUER_ZIFFER}{{2}}(?::{_DAUER_ZIFFER}{{2}})?)$"
)
_DAUER_NORMALISIEREN = str.maketrans({"O": "0", "o": "0", "I": "1", "l": "1"})
_TRACK_PREFIX_RE = re.compile(r"^\s*(?P<num>\d{1,2})\s*[.)\-:]?\s*(?P<rest>.*)$")


def _split_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


def _extract_duration(value: str) -> tuple[str, str]:
    text = value.strip()
    match = _DURATION_RE.search(text)
    if not match:
        return text, ""
    start = match.start("dur")
    dauer = match.group("dur").translate(_DAUER_NORMALISIEREN)
    return text[:start].rstrip(" -–—\t"), dauer


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


def parse_text(text: str, flags: ParseFlags) -> list[ParsedTrack]:
    """Wendet die gewählten Schalter zeilenweise auf OCR-Rohtext an."""
    result: list[ParsedTrack] = []

    for raw in _split_lines(text):
        line = raw
        duration = ""
        if flags.dauer:
            line, duration = _extract_duration(line)

        number = ""
        if flags.tracknummer:
            number, line = _strip_track_prefix(line)

        artist = ""
        if flags.interpret:
            artist, parsed_title = _split_artist_title(line)
            line = parsed_title or line

        result.append(
            ParsedTrack(number=number, artist=artist, title=line.strip(), duration=duration)
        )

    return result
