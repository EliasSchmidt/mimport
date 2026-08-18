"""Kleiner Genre-Katalog für Vorschläge im Handformular."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

_GENRE_DATEI = Path(__file__).resolve().with_name("genres.txt")


@lru_cache(maxsize=1)
def katalog() -> tuple[str, ...]:
    """Lädt den Genre-Katalog aus der Projektdatei.

    Die Liste ist absichtlich klein und kuratiert: genug für sinnvolle
    Vorschläge beim Tippen, aber weiterhin vollständig frei editierbar.
    """
    try:
        zeilen = _GENRE_DATEI.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ()

    gesehen: set[str] = set()
    ergebnis: list[str] = []
    for roh in zeilen:
        genre = roh.strip()
        if not genre or genre.startswith("#"):
            continue
        schluessel = genre.casefold()
        if schluessel in gesehen:
            continue
        gesehen.add(schluessel)
        ergebnis.append(genre)
    return tuple(ergebnis)
