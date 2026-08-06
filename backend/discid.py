"""DiscID einer Audio-CD berechnen und bei MusicBrainz auflösen.

Der Grund, warum es das überhaupt braucht: eine frisch gerippte CD hat **keine
Tags**. ``tag_album`` leitet seine Suchbegriffe sonst aus den vorhandenen Tags
ab und findet ohne sie nichts. Die DiscID ersetzt das durch etwas Besseres als
Raten -- sie identifiziert die Pressung exakt.

Ab der Release-ID läuft alles wie gehabt: ``matching.find_candidate_by_id()``
nimmt genau so eine ID entgegen, dieselbe Tür wie das Feld „MusicBrainz-Release-ID"
in der Oberfläche.

Die Berechnung ist reine Rechnerei und in ``tests/test_discid.py`` gegen den
Testvektor aus der MusicBrainz-Dokumentation geprüft -- dafür braucht es kein
Laufwerk.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

#: Eine CD hat höchstens 99 Tracks; die Hash-Eingabe hat immer genau so viele
#: Plätze, ungenutzte werden mit 0 aufgefüllt.
MAX_TRACKS = 99

#: Zwischen Sektor 0 und dem ersten Track liegen zwei Sekunden Vorlauf. CDs
#: zählen 75 Sektoren je Sekunde, cdparanoia zählt ohne diesen Vorlauf.
PREGAP_SECTORS = 150

#: Ein Sektor CD-Audio: 44100 Hz, 2 Kanäle, 16 Bit, 1/75 Sekunde.
BYTES_PER_SECTOR = 2352

_MB_DISCID_URL = "https://musicbrainz.org/ws/2/discid/{}"

#: MusicBrainz verlangt eine aussagekräftige Kennung mit Kontaktmöglichkeit.
USER_AGENT = "mimport/0.1.0 ( https://github.com/mimport/mimport )"


class DiscIdError(Exception):
    """TOC nicht lesbar oder Abfrage fehlgeschlagen."""


@dataclass(frozen=True)
class Toc:
    """Das Inhaltsverzeichnis einer Audio-CD.

    ``offsets`` sind die Startsektoren der Tracks *einschließlich* Vorlauf, so
    wie MusicBrainz sie erwartet -- der erste Track liegt also normalerweise
    bei 150, nicht bei 0.
    """

    first_track: int
    last_track: int
    leadout: int
    offsets: tuple[int, ...]

    @property
    def track_count(self) -> int:
        return len(self.offsets)

    @property
    def total_sectors(self) -> int:
        return self.leadout - self.offsets[0] if self.offsets else 0

    @property
    def raw_bytes(self) -> int:
        """Wie groß die CD unkomprimiert wäre.

        Grundlage für die Platzprüfung vor dem Rippen: FLAC landet bei grob
        60 %, aber nach oben abschätzen ist hier das Richtige.
        """
        return self.total_sectors * BYTES_PER_SECTOR

    @property
    def total_seconds(self) -> float:
        return self.total_sectors / 75


def calculate(toc: Toc) -> str:
    """Die MusicBrainz-DiscID eines TOC.

    Gehasht wird eine Zeichenkette aus Hex-Ziffern: erster Track, letzter
    Track, dann 100 Offsets zu je acht Stellen -- an erster Stelle das
    Leadout, danach die 99 Track-Plätze. Das Ergebnis ist base64, aber mit
    ``.`` statt ``+``, ``_`` statt ``/`` und ``-`` statt ``=``, damit es in
    eine URL passt.
    """
    if not toc.offsets:
        raise DiscIdError("Die CD enthält keine Audio-Tracks.")

    teile = [
        f"{toc.first_track:02X}",
        f"{toc.last_track:02X}",
        f"{toc.leadout:08X}",
    ]
    for index in range(MAX_TRACKS):
        wert = toc.offsets[index] if index < len(toc.offsets) else 0
        teile.append(f"{wert:08X}")

    roh = hashlib.sha1("".join(teile).encode("ascii")).digest()
    return (
        base64.b64encode(roh)
        .decode("ascii")
        .replace("+", ".")
        .replace("/", "_")
        .replace("=", "-")
    )


#: Eine Trackzeile von ``cdparanoia -Q``:
#:     "  1.    15363 [03:24.63]        0 [00:00.00]    no   no  2"
#: Interessant sind Länge und Startsektor.
_TRACK_LINE = re.compile(r"^\s*(\d+)\.\s+(\d+)\s+\[[\d:.]+\]\s+(\d+)\s+\[")


def parse_cdparanoia_toc(ausgabe: str) -> Toc:
    """Liest das Inhaltsverzeichnis aus der Ausgabe von ``cdparanoia -Q``.

    cdparanoia zählt Sektoren ab dem ersten Track, MusicBrainz ab dem Anfang
    der CD -- deshalb kommt auf jeden Wert der Vorlauf von 150 Sektoren
    obendrauf. Das Leadout ist entsprechend Startsektor plus Länge des letzten
    Tracks, ebenfalls mit Vorlauf.
    """
    nummern: list[int] = []
    offsets: list[int] = []
    letztes_ende = 0

    for zeile in ausgabe.splitlines():
        treffer = _TRACK_LINE.match(zeile)
        if not treffer:
            continue
        nummer, laenge, beginn = (int(g) for g in treffer.groups())
        nummern.append(nummer)
        offsets.append(beginn + PREGAP_SECTORS)
        letztes_ende = beginn + laenge + PREGAP_SECTORS

    if not offsets:
        raise DiscIdError(
            "Im Laufwerk wurde keine Audio-CD erkannt. Eine Daten-CD wird über "
            "den Ordner-Auswähler eingelesen, nicht hier."
        )

    return Toc(
        first_track=nummern[0],
        last_track=nummern[-1],
        leadout=letztes_ende,
        offsets=tuple(offsets),
    )


@dataclass(frozen=True)
class ReleaseHint:
    """Ein Release, den MusicBrainz zu dieser DiscID kennt."""

    mbid: str
    title: str
    date: str
    country: str

    @property
    def label(self) -> str:
        beiwerk = " · ".join(teil for teil in (self.date, self.country) if teil)
        return f"{self.title} ({beiwerk})" if beiwerk else self.title


def lookup(disc_id: str, *, timeout: float = 15.0) -> list[ReleaseHint]:
    """Fragt MusicBrainz, welche Releases zu dieser DiscID gehören.

    Direkt über ``ws/2``, nicht über den MusicBrainz-Client von beets: der
    liegt dort in einem privaten Modul (``beetsplug/_utils``), und mimport
    hängt sich nur an öffentliche Schnittstellen.

    Mehrere Treffer sind normal -- dieselbe CD erscheint oft als mehrere
    Pressungen. Keine Treffer heißt: die CD kennt MusicBrainz nicht, dann
    bleibt die Suche von Hand.
    """
    try:
        antwort = requests.get(
            _MB_DISCID_URL.format(disc_id),
            params={"fmt": "json"},
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise DiscIdError(f"MusicBrainz war nicht erreichbar: {exc}") from exc

    if antwort.status_code == 404:
        return []
    if not antwort.ok:
        raise DiscIdError(
            f"MusicBrainz antwortete mit {antwort.status_code}."
        )

    try:
        daten = antwort.json()
    except ValueError as exc:
        raise DiscIdError("Antwort von MusicBrainz war nicht lesbar.") from exc

    treffer = []
    for release in daten.get("releases", []):
        if not release.get("id"):
            continue
        treffer.append(
            ReleaseHint(
                mbid=release["id"],
                title=release.get("title") or "ohne Titel",
                date=release.get("date") or "",
                country=release.get("country") or "",
            )
        )
    return treffer
