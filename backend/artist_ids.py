"""Konservative Auflösung von Künstlernamen zu MusicBrainz-Artist-IDs."""

from __future__ import annotations

import logging
import re
from functools import lru_cache

import requests

log = logging.getLogger(__name__)

_MB_ARTIST_URL = "https://musicbrainz.org/ws/2/artist/"

#: MusicBrainz verlangt eine aussagekräftige Kennung mit Kontaktmöglichkeit.
USER_AGENT = "mimport/0.1.0 ( https://github.com/mimport/mimport )"

_LEERRAUM = re.compile(r"\s+")


def _normalisiert(name: object) -> str:
    """Vergleicht nur inhaltlich: Groß-/Kleinschreibung und Leerraum egal."""
    return _LEERRAUM.sub(" ", str(name).strip()).casefold()


@lru_cache(maxsize=512)
def lookup_exact(name: str, *, timeout: float = 5.0) -> str | None:
    """Liefert die Artist-MBID bei genau einem exakten Treffer.

    Absichtlich streng: Nur wenn der Name nach Normalisierung exakt passt und
    unter den Treffern genau **eine** eindeutige MBID übrig bleibt, wird sie
    übernommen. Bei Mehrdeutigkeit oder Nichtfund bleibt das Feld leer, damit
    mimport keine falschen Künstlerverknüpfungen in die Dateien schreibt.
    """
    suchname = str(name).strip()
    if not suchname:
        return None

    try:
        antwort = requests.get(
            _MB_ARTIST_URL,
            params={"query": f'artist:"{suchname}"', "fmt": "json", "limit": 10},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        log.info("MusicBrainz-Künstlerlookup fehlgeschlagen für %r: %s", suchname, exc)
        return None

    if antwort.status_code != 200:
        log.info(
            "MusicBrainz-Künstlerlookup antwortete mit %s für %r",
            antwort.status_code,
            suchname,
        )
        return None

    try:
        daten = antwort.json()
    except ValueError:
        log.info("MusicBrainz-Künstlerlookup lieferte kein lesbares JSON für %r", suchname)
        return None

    ziel = _normalisiert(suchname)
    treffer = {
        str(artist.get("id") or "").strip().lower()
        for artist in daten.get("artists", [])
        if _normalisiert(artist.get("name") or "") == ziel and artist.get("id")
    }
    if len(treffer) == 1:
        return next(iter(treffer))
    return None
