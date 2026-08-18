"""Konservative Auflösung von Künstlernamen zu MusicBrainz-Artist-IDs."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import requests

log = logging.getLogger(__name__)

_MB_ARTIST_URL = "https://musicbrainz.org/ws/2/artist/"

#: MusicBrainz verlangt eine aussagekräftige Kennung mit Kontaktmöglichkeit.
USER_AGENT = "mimport/0.1.0 ( https://github.com/mimport/mimport )"

_LEERRAUM = re.compile(r"\s+")


@dataclass(frozen=True)
class ArtistMatch:
    name: str
    mbid: str
    disambiguation: str = ""
    area: str = ""
    kind: str = ""
    exact: bool = False


def _normalisiert(name: object) -> str:
    """Vergleicht nur inhaltlich: Groß-/Kleinschreibung und Leerraum egal."""
    return _LEERRAUM.sub(" ", str(name).strip()).casefold()


def _suche_roh(name: str, *, timeout: float = 5.0, limit: int = 10) -> list[dict[str, Any]]:
    suchname = str(name).strip()
    if not suchname:
        return []

    try:
        antwort = requests.get(
            _MB_ARTIST_URL,
            params={"query": f'artist:"{suchname}"', "fmt": "json", "limit": limit},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.info("MusicBrainz-Künstlerlookup fehlgeschlagen für %r: %s", suchname, exc)
        return []

    if antwort.status_code != 200:
        log.info(
            "MusicBrainz-Künstlerlookup antwortete mit %s für %r",
            antwort.status_code,
            suchname,
        )
        return []

    try:
        daten = antwort.json()
    except ValueError:
        log.info("MusicBrainz-Künstlerlookup lieferte kein lesbares JSON für %r", suchname)
        return []
    return list(daten.get("artists", []) or [])


@lru_cache(maxsize=512)
def search(name: str, *, timeout: float = 5.0, limit: int = 8) -> tuple[ArtistMatch, ...]:
    """Liefert mögliche MusicBrainz-Künstler zu einem Namen."""
    suchname = str(name).strip()
    if not suchname:
        return ()

    ziel = _normalisiert(suchname)
    treffer: list[ArtistMatch] = []
    gesehen: set[str] = set()
    for artist in _suche_roh(suchname, timeout=timeout, limit=limit):
        mbid = str(artist.get("id") or "").strip().lower()
        artist_name = str(artist.get("name") or "").strip()
        if not mbid or not artist_name or mbid in gesehen:
            continue
        gesehen.add(mbid)
        area = str((artist.get("area") or {}).get("name") or artist.get("country") or "").strip()
        treffer.append(
            ArtistMatch(
                name=artist_name,
                mbid=mbid,
                disambiguation=str(artist.get("disambiguation") or "").strip(),
                area=area,
                kind=str(artist.get("type") or "").strip(),
                exact=_normalisiert(artist_name) == ziel,
            )
        )
    treffer.sort(key=lambda match: (not match.exact, match.name.casefold(), match.area.casefold()))
    return tuple(treffer)


@lru_cache(maxsize=512)
def lookup_exact(name: str, *, timeout: float = 5.0) -> str | None:
    """Liefert die Artist-MBID bei genau einem exakten Treffer.

    Absichtlich streng: Nur wenn der Name nach Normalisierung exakt passt und
    unter den Treffern genau **eine** eindeutige MBID übrig bleibt, wird sie
    übernommen. Bei Mehrdeutigkeit oder Nichtfund bleibt das Feld leer, damit
    mimport keine falschen Künstlerverknüpfungen in die Dateien schreibt.
    """
    exakte_ids = {match.mbid for match in search(name, timeout=timeout, limit=10) if match.exact}
    if len(exakte_ids) == 1:
        return next(iter(exakte_ids))
    return None
