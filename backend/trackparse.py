"""Einfache, bewusst vorhersehbare Parser für OCR-Tracklisten.

Der Zweck ist nicht, semantisch "klug" zu raten, sondern häufige Layouts mit
klaren Regeln in ein editierbares Formular zu überführen.

Statt eines festen Katalogs an Layout-"Modi" sind das unabhängige Schalter:
jede Zeile kann optional eine Tracknummer vorn und eine Dauer am Ende haben
(feste Position, deshalb je ein eigener Schalter statt Teil der Reihenfolge).
Was dazwischen übrig bleibt -- Titel, Interpret, Komponist -- ordnet
``felder`` in beliebiger Reihenfolge an; ``trenner`` legt fest, woran diese
Felder in derselben Zeile auseinandergehalten werden, und ``zeilenweise``
schaltet stattdessen auf ein Feld pro Zeile um (reihum). Das deckt sowohl
Backcover mit "Interpret - Titel" als auch Klassik-Compilations mit
"Komponist - Titel" (ganz ohne Track-Interpret) ab, ohne dass für jedes neue
Layout ein neuer Modus erfunden werden muss.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Feld = Literal["titel", "interpret", "komponist"]

#: Auf welches Feld von ``ParsedTrack`` sich ein Layout-Feld abbildet.
_FELD_ATTR = {"titel": "title", "interpret": "artist", "komponist": "composer"}


@dataclass
class ParsedTrack:
    """Ein geparster Track aus einer OCR-Zeile."""

    number: str = ""
    artist: str = ""
    composer: str = ""
    title: str = ""
    duration: str = ""


@dataclass(frozen=True)
class ParseFlags:
    """Welche Bestandteile eine Zeile hat -- Grundlage für den Parser.

    ``felder`` ist die Reihenfolge, in der Titel/Interpret/Komponist
    vorkommen (fehlende Felder sind einfach nicht in der Liste, ein Feld
    kann nur einmal vorkommen). ``trenner`` trennt sie innerhalb derselben
    Zeile; ``zeilenweise`` schaltet stattdessen auf ein Feld pro Zeile um,
    reihum in derselben Reihenfolge.
    """

    tracknummer: bool = True
    dauer: bool = True
    felder: tuple[Feld, ...] = ("titel",)
    zeilenweise: bool = False
    trenner: str = " - "


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


def _teile_werte(text: str, felder: tuple[Feld, ...], trenner: str) -> dict[str, str]:
    """Verteilt den freien Rest einer Zeile auf die gewählten Felder.

    Reihenfolge und Trennzeichen sind frei wählbar -- welches Feld an
    welcher Stelle steht, entscheidet ausschließlich ``felder``. Lässt sich
    die Zeile nicht in genügend Teile zerlegen (Trenner kommt zu selten vor,
    oder es ist nur ein Feld gewählt), bleibt der komplette Rest im Titel
    stehen, statt stillschweigend zu verschwinden.
    """
    text = text.strip()
    if len(felder) > 1 and trenner:
        teile = [teil.strip() for teil in text.split(trenner, len(felder) - 1)]
        if len(teile) == len(felder):
            return {_FELD_ATTR[feld]: teil for feld, teil in zip(felder, teile)}
    return {"title": text}


def _zeilengruppen(lines: list[str], groesse: int) -> list[list[str]]:
    groesse = max(groesse, 1)
    return [lines[i : i + groesse] for i in range(0, len(lines), groesse)]


def _gruppenwerte(gruppe: list[str], felder: tuple[Feld, ...]) -> dict[str, str]:
    """Ordnet eine Gruppe aufeinanderfolgender Zeilen den Feldern zu.

    Passt eine Gruppe nicht voll (letzte Zeile ohne Partner, z. B. bei
    ungerader Zeilenzahl), bleibt ihr Inhalt als Titel stehen, statt
    verworfen zu werden -- die Oberfläche zeigt ihn dann als unvollständige
    Zeile zum Nachbessern.
    """
    if len(gruppe) == len(felder):
        return {_FELD_ATTR[feld]: zeile.strip() for feld, zeile in zip(felder, gruppe)}
    return {"title": gruppe[0].strip()}


def parse_text(text: str, flags: ParseFlags) -> list[ParsedTrack]:
    """Wendet die gewählten Schalter auf OCR-Rohtext an."""
    felder = flags.felder or ("titel",)
    zeilenweise = flags.zeilenweise and len(felder) > 1
    lines = _split_lines(text)
    gruppen = _zeilengruppen(lines, len(felder)) if zeilenweise else [[line] for line in lines]

    result: list[ParsedTrack] = []
    for gruppe in gruppen:
        kopf = gruppe[0]
        duration = ""
        if flags.dauer:
            kopf, duration = _extract_duration(kopf)

        number = ""
        if flags.tracknummer:
            number, kopf = _strip_track_prefix(kopf)

        if zeilenweise:
            werte = _gruppenwerte([kopf, *gruppe[1:]], felder)
        else:
            werte = _teile_werte(kopf, felder, flags.trenner)

        result.append(
            ParsedTrack(
                number=number,
                artist=werte.get("artist", ""),
                composer=werte.get("composer", ""),
                title=werte.get("title", ""),
                duration=duration,
            )
        )

    return result
