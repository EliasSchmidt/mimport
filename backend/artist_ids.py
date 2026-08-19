"""Konservative Auflösung von Künstlernamen zu MusicBrainz-Artist-IDs."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import requests

log = logging.getLogger(__name__)

_MB_ARTIST_URL = "https://musicbrainz.org/ws/2/artist/"

#: MusicBrainz verlangt eine aussagekräftige Kennung mit Kontaktmöglichkeit.
USER_AGENT = "mimport/0.1.0 ( https://github.com/mimport/mimport )"

_LEERRAUM = re.compile(r"\s+")

#: MusicBrainz drosselt unauthentifizierte Anfragen serverseitig auf etwa
#: eine pro Sekunde und Adresse (Antwort dann 503). beets hält das über
#: ``requests_ratelimiter`` selbst ein -- diese Anfragen hier laufen daran
#: vorbei, deshalb drosseln wir uns selbst, statt uns auf Zufall zu verlassen.
_MIN_ABSTAND_SEKUNDEN = 1.0
_drossel_sperre = threading.Lock()
_letzte_anfrage = 0.0


def _drosseln() -> None:
    global _letzte_anfrage
    with _drossel_sperre:
        warten = _letzte_anfrage + _MIN_ABSTAND_SEKUNDEN - time.monotonic()
        if warten > 0:
            time.sleep(warten)
        _letzte_anfrage = time.monotonic()


class LookupFehlgeschlagen(Exception):
    """MusicBrainz war nicht erreichbar oder hat einen Fehler geliefert.

    Bewusst von "keine Treffer" unterschieden: Ersteres ist ein Grund, es
    später erneut zu versuchen, Letzteres nicht -- und darf deshalb auch
    nicht wie ein echtes Ergebnis im Cache landen (siehe ``search``).
    """


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
    """Fragt MusicBrainz ab oder wirft ``LookupFehlgeschlagen``.

    Bei einer Drosselantwort (503) genau einmal mit kurzer Pause erneut --
    das reicht für den Normalfall (ein einzelner Request, der sich mit einem
    parallel laufenden Match-Vorgang überschnitten hat) und vermeidet, dass
    ein einzelner Ausrutscher als "keine Treffer" endet.
    """
    suchname = str(name).strip()
    if not suchname:
        return []

    letzter_fehler: Exception | None = None
    for versuch in range(2):
        _drosseln()
        try:
            antwort = requests.get(
                _MB_ARTIST_URL,
                # Die normale MusicBrainz-Suche findet deutlich mehr Treffer als ein
                # zu enges Feld-Query wie artist:"...". Die Exaktheit prüfen wir
                # danach selbst über den zurückgegebenen Namen.
                params={"query": suchname, "fmt": "json", "limit": limit},
                headers={"User-Agent": USER_AGENT},
                timeout=timeout,
            )
        except requests.RequestException as exc:
            log.info("MusicBrainz-Künstlerlookup fehlgeschlagen für %r: %s", suchname, exc)
            raise LookupFehlgeschlagen(str(exc)) from exc

        if antwort.status_code == 503 and versuch == 0:
            letzter_fehler = LookupFehlgeschlagen("MusicBrainz drosselt (503)")
            time.sleep(1.0)
            continue

        if antwort.status_code != 200:
            log.info(
                "MusicBrainz-Künstlerlookup antwortete mit %s für %r",
                antwort.status_code,
                suchname,
            )
            raise LookupFehlgeschlagen(f"HTTP {antwort.status_code}")

        try:
            daten = antwort.json()
        except ValueError as exc:
            log.info("MusicBrainz-Künstlerlookup lieferte kein lesbares JSON für %r", suchname)
            raise LookupFehlgeschlagen("kein lesbares JSON") from exc
        return list(daten.get("artists", []) or [])

    assert letzter_fehler is not None  # for-Schleife endet nur über return oder raise
    raise letzter_fehler


@lru_cache(maxsize=512)
def search(name: str, *, timeout: float = 5.0, limit: int = 8) -> tuple[ArtistMatch, ...]:
    """Liefert mögliche MusicBrainz-Künstler zu einem Namen.

    Wirft ``LookupFehlgeschlagen`` weiter nach oben, statt sie als leeres
    Ergebnis zu behandeln: ``lru_cache`` cached nur normale Rückgaben, ein
    Wurf bleibt also unangetastet und der nächste Aufruf versucht es erneut,
    statt einen einmaligen Ausrutscher für die Laufzeit des Prozesses
    einzufrieren.
    """
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


def lookup_exact(name: str, *, timeout: float = 5.0) -> str | None:
    """Liefert die Artist-MBID bei genau einem exakten Treffer.

    Absichtlich streng: Nur wenn der Name nach Normalisierung exakt passt und
    unter den Treffern genau **eine** eindeutige MBID übrig bleibt, wird sie
    übernommen. Bei Mehrdeutigkeit, Nichtfund oder einem MusicBrainz-Ausfall
    bleibt das Feld leer, damit mimport keine falschen Künstlerverknüpfungen
    in die Dateien schreibt -- und absichtlich *nicht* gecached, damit ein
    einzelner MusicBrainz-Ausfall nicht dauerhaft jede Artist-ID-Anreicherung
    für diesen Namen unterdrückt (``search`` cached die eigentliche Anfrage
    schon; hier kommt nur noch die Auswertung dazu).
    """
    try:
        treffer = search(name, timeout=timeout, limit=10)
    except LookupFehlgeschlagen:
        return None
    exakte_ids = {match.mbid for match in treffer if match.exact}
    if len(exakte_ids) == 1:
        return next(iter(exakte_ids))
    return None
