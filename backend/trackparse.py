"""Einfache, bewusst vorhersehbare Parser für OCR-Tracklisten.

Der Zweck ist nicht, semantisch "klug" zu raten, sondern häufige Layouts mit
klaren Regeln in ein editierbares Formular zu überführen.

Statt eines festen Katalogs an Layout-"Modi" (die in Wahrheit immer dieselben
Operationen kombinieren) sind das unabhängige Schalter: jede Zeile kann
optional eine Tracknummer vorn und eine Dauer am Ende haben; der Interpret
kann fehlen, in derselben Zeile stehen (in beiden Reihenfolgen) oder sich mit
dem Titel zeilenweise abwechseln. Das deckt die üblichen Backcover-Layouts
ab, ohne dass für jedes neue Layout ein neuer Modus erfunden werden muss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

InterpretModus = Literal["", "interpret_titel", "titel_interpret", "naechste_zeile"]


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

    ``interpret`` ist bewusst kein Ja/Nein-Schalter neben
    ``tracknummer``/``dauer``: die möglichen Layouts schließen sich
    gegenseitig aus, ein Auswahlfeld statt mehrerer Checkboxen macht das in
    der Oberfläche und im Code unmissverständlich.
    """

    tracknummer: bool = True
    interpret: InterpretModus = ""
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


def _split_zeile(text: str) -> tuple[str, str] | None:
    """Trennt eine Zeile an " - " in zwei Teile, ohne sie zuzuordnen --
    welcher Teil Titel und welcher Interpret ist, entscheidet der Aufrufer
    anhand der gewählten Reihenfolge."""
    if " - " not in text:
        return None
    erster, zweiter = text.split(" - ", 1)
    return erster.strip(), zweiter.strip()


def _zeilenpaare(lines: list[str], interpret: InterpretModus) -> list[tuple[str, str]]:
    """Bildet (Titelzeile, Interpretzeile)-Paare -- oder Titelzeilen ohne Partner.

    Bei ``naechste_zeile`` halbiert sich die Trackzahl gegenüber der
    Zeilenzahl; eine Zeile ohne Partner (ungerade Zeilenzahl) bleibt als
    Titel ohne Interpret stehen, statt verworfen zu werden.
    """
    if interpret != "naechste_zeile":
        return [(line, "") for line in lines]
    paare = list(zip(lines[0::2], lines[1::2]))
    if len(lines) % 2:
        paare.append((lines[-1], ""))
    return paare


def parse_text(text: str, flags: ParseFlags) -> list[ParsedTrack]:
    """Wendet die gewählten Schalter auf OCR-Rohtext an."""
    result: list[ParsedTrack] = []

    for titel_zeile, interpret_zeile in _zeilenpaare(_split_lines(text), flags.interpret):
        line = titel_zeile
        duration = ""
        if flags.dauer:
            line, duration = _extract_duration(line)

        number = ""
        if flags.tracknummer:
            number, line = _strip_track_prefix(line)

        artist = ""
        if flags.interpret == "naechste_zeile":
            artist = interpret_zeile.strip()
        elif flags.interpret in ("interpret_titel", "titel_interpret"):
            geteilt = _split_zeile(line)
            if geteilt is not None:
                erster, zweiter = geteilt
                if flags.interpret == "titel_interpret":
                    line, artist = erster, zweiter
                else:
                    artist, line = erster, zweiter

        result.append(
            ParsedTrack(number=number, artist=artist, title=line.strip(), duration=duration)
        )

    return result
