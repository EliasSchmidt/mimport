"""Match-Kandidaten von beets holen und für die Oberfläche aufbereiten.

Das ist der Kern von mimport: statt den Nutzer im Terminal durch ``beet
import`` zu klicken, zeigen wir dieselben Kandidaten im Browser -- mit der
Sicherheit des Matches, dem Grund für Abzüge und einer Liste dessen, was am
Match fehlt.

Alle drei Angaben kommen direkt aus beets und müssen nicht nachgebaut werden:

* ``AlbumMatch.distance`` -- 0.0 ist perfekt, wir zeigen ``(1 - distance)`` als
  Sicherheit in Prozent.
* ``Distance.items()`` -- die einzelnen Abzüge, also *warum* die Sicherheit
  niedrig ist (z. B. ``missing_tracks``).
* ``extra_tracks`` / ``extra_items`` -- Tracks des Releases ohne passende Datei
  bzw. hochgeladene Dateien ohne passenden Track.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import beets_env

log = logging.getLogger(__name__)

#: Klartext für die Abzüge, die beets vergibt. Fällt auf den Rohnamen zurück,
#: falls beets oder ein Plugin einen unbekannten Schlüssel liefert.
PENALTY_LABELS: dict[str, str] = {
    "album": "Albumtitel weicht ab",
    "album_id": "Album-ID weicht ab",
    "albumdisambig": "Album-Zusatz weicht ab",
    "artist": "Künstler weicht ab",
    "catalognum": "Katalognummer weicht ab",
    "country": "Land weicht ab",
    "data_source": "andere Datenquelle",
    "label": "Label weicht ab",
    "media": "Medium weicht ab (z. B. CD statt Vinyl)",
    "medium": "Disc-Nummer weicht ab",
    "mediums": "Anzahl der Discs weicht ab",
    "missing_tracks": "Tracks des Releases fehlen im Upload",
    "track_artist": "Track-Künstler weicht ab",
    "track_id": "Track-ID weicht ab",
    "track_index": "Trackreihenfolge weicht ab",
    "track_length": "Tracklängen weichen ab",
    "track_title": "Tracktitel weichen ab",
    "tracks": "einzelne Tracks passen nicht genau",
    "unmatched_tracks": "Dateien passen zu keinem Track des Releases",
    "year": "Jahr weicht ab",
}

#: Release-URL je Datenquelle, für den Link auf der Kandidatenkarte. Der
#: Schlüssel ist ``data_source.lower()`` -- beets liefert "MusicBrainz" bzw.
#: "Discogs" (aus dem Klassennamen ohne "Plugin" abgeleitet), nicht die
#: Kleinschreibung des Plugin-Namens.
_RELEASE_URL_TEMPLATES: dict[str, str] = {
    "musicbrainz": "https://musicbrainz.org/release/{album_id}",
    "discogs": "https://www.discogs.com/release/{album_id}",
}

#: MusicBrainz-Release-ID: 8-4-4-4-12 Hexzeichen.
_MBID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def extract_mbid(raw: str) -> str | None:
    """Zieht eine Release-ID aus Nutzereingaben.

    Nimmt sowohl die nackte ID als auch eine kopierte MusicBrainz-URL wie
    ``https://musicbrainz.org/release/964e8152-.../``.
    """
    if not raw:
        return None
    match = _MBID_RE.search(raw.strip())
    return match.group(0).lower() if match else None


@dataclass
class TrackPairing:
    """Eine Zeile der Gegenüberstellung Datei -> Track des Releases."""

    filename: str
    old_title: str
    new_title: str
    old_track: int | None
    new_track: int | None
    #: Längenunterschied in Sekunden, falls beide Längen bekannt sind.
    length_delta: float | None = None

    @property
    def title_changed(self) -> bool:
        return (self.old_title or "").strip() != (self.new_title or "").strip()

    @property
    def track_changed(self) -> bool:
        return self.old_track != self.new_track


@dataclass
class Candidate:
    """Ein Match-Vorschlag, fertig für die Anzeige."""

    index: int
    album: str
    albumartist: str
    album_id: str
    #: Sicherheit in Prozent, 100 = perfekt.
    confidence: float = 0.0
    #: ``none`` | ``low`` | ``medium`` | ``strong`` -- als Name, nicht als Zahl:
    #: ``Recommendation`` ist ein IntEnum und ``Recommendation.none`` ist 0,
    #: wäre in Templates also fälschlich "falsch".
    recommendation: str = "none"
    year: int | None = None
    country: str = ""
    label: str = ""
    media: str = ""
    mediums: int | None = None
    catalognum: str = ""
    albumdisambig: str = ""
    data_source: str = ""
    url: str = ""
    #: Abzüge als (Klartext, Anteil in Prozentpunkten), stärkster zuerst.
    penalties: list[tuple[str, float]] = field(default_factory=list)
    #: Tracks des Releases, für die keine Datei da ist.
    missing_tracks: list[str] = field(default_factory=list)
    #: Hochgeladene Dateien, die zu keinem Track passen.
    unmatched_files: list[str] = field(default_factory=list)
    pairings: list[TrackPairing] = field(default_factory=list)
    #: Zieht der "Tracktitel weichen ab"-Abzug daher, dass die Dateien schlicht
    #: keine Titel-Tags tragen (typisch für einen Rip) -- nicht daher, dass
    #: vorhandene Titel nicht passen. Nur dann ist die Erklärung "kein
    #: Warnzeichen" zutreffend.
    titel_fehlen_komplett: bool = False

    @property
    def confidence_class(self) -> str:
        """Grobe Einordnung für die farbliche Kennzeichnung."""
        if self.recommendation == "strong":
            return "strong"
        if self.recommendation == "medium":
            return "medium"
        if self.recommendation == "low":
            return "low"
        return "none"

    @property
    def is_complete(self) -> bool:
        return not self.missing_tracks and not self.unmatched_files


def _track_number(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _build_pairings(match: Any) -> list[TrackPairing]:
    """Stellt jede Datei ihrem zugeordneten Track gegenüber.

    ``match.mapping`` hat die hochgeladenen ``Item``-Objekte als Schlüssel und
    die ``TrackInfo`` des Releases als Wert.
    """
    pairings: list[TrackPairing] = []
    for item, track_info in match.mapping.items():
        delta: float | None = None
        if item.length and track_info.length:
            delta = round(float(item.length) - float(track_info.length), 1)
        pairings.append(
            TrackPairing(
                filename=Path(str(item.path, "utf-8", "replace")).name
                if isinstance(item.path, bytes)
                else Path(str(item.path)).name,
                old_title=item.title or "",
                new_title=track_info.title or "",
                old_track=_track_number(item.track),
                new_track=_track_number(track_info.index or track_info.medium_index),
                length_delta=delta,
            )
        )
    pairings.sort(key=lambda p: (p.new_track is None, p.new_track or 0))
    return pairings


def _titel_fehlen_komplett(match: Any) -> bool:
    """Tragen die hochgeladenen Dateien überhaupt Titel-Tags?

    Entscheidet, was ein "Tracktitel weichen ab"-Abzug bedeutet: bei einem
    Rip (keine Datei hat einen Titel) ist er unvermeidlich und kein
    Warnzeichen: beets kann nichts vergleichen, was nicht da ist. Bei einem
    Upload mit echten, nur falschen Titeln bedeutet derselbe Abzug dagegen
    tatsächlich, dass der Kandidat nicht passt -- die Erklärung wäre dort
    irreführend.
    """
    return not any((item.title or "").strip() for item in match.mapping)


def serialize_candidate(match: Any, index: int) -> Candidate:
    """Übersetzt einen ``AlbumMatch`` in ein anzeigefertiges Objekt."""
    info = match.info
    distance = match.distance

    penalties: list[tuple[str, float]] = []
    hat_titel_abzug = False
    for raw_key, value in distance.items():
        key = str(raw_key)
        if key == "track_title":
            hat_titel_abzug = True
        label = PENALTY_LABELS.get(key, key.replace("_", " "))
        penalties.append((label, round(float(value) * 100, 1)))
    penalties.sort(key=lambda pair: pair[1], reverse=True)

    album_id = str(info.album_id or "")
    url = ""
    if album_id:
        template = _RELEASE_URL_TEMPLATES.get((info.data_source or "").lower())
        if template:
            url = template.format(album_id=album_id)

    return Candidate(
        index=index,
        album=info.album or "(ohne Titel)",
        albumartist=info.artist or "(unbekannt)",
        album_id=album_id,
        confidence=round((1.0 - distance.distance) * 100, 1),
        recommendation="none",  # wird vom Aufrufer aus der Proposal gesetzt
        year=info.year,
        country=info.country or "",
        label=info.label or "",
        media=info.media or "",
        mediums=info.mediums,
        catalognum=info.catalognum or "",
        albumdisambig=info.albumdisambig or "",
        data_source=info.data_source or "",
        url=url,
        penalties=penalties,
        missing_tracks=[
            f"{t.index or '?'}. {t.title or '(ohne Titel)'}" for t in match.extra_tracks
        ],
        unmatched_files=[
            Path(str(i.path, "utf-8", "replace")).name
            if isinstance(i.path, bytes)
            else Path(str(i.path)).name
            for i in match.extra_items
        ],
        pairings=_build_pairings(match),
        titel_fehlen_komplett=hat_titel_abzug and _titel_fehlen_komplett(match),
    )


@dataclass
class MatchResult:
    """Was wir dem Nutzer nach einer Suche zeigen."""

    current_artist: str
    current_album: str
    #: Empfehlung von beets für den besten Kandidaten.
    recommendation: str
    candidates: list[Candidate] = field(default_factory=list)
    #: Gesetzt, wenn die Suche fehlschlug (kein Netz, MusicBrainz down, ...).
    error: str = ""
    #: Hinweis, wenn die Suche zwar lief, aber nichts fand.
    note: str = ""

    @property
    def has_candidates(self) -> bool:
        return bool(self.candidates)


def load_items(paths: list[Path]) -> list[Any]:
    """Liest die hochgeladenen Dateien als beets-``Item``-Objekte.

    Das geschieht ohne Library-Datenbank -- ``Item.from_path`` liest nur die
    Tags aus der Datei selbst.
    """
    beets_env.ensure_loaded()
    from beets.library import Item

    items: list[Any] = []
    for path in paths:
        try:
            items.append(Item.from_path(str(path)))
        except Exception as exc:  # noqa: BLE001 -- eine unlesbare Datei soll die übrigen nicht stoppen
            log.warning("Datei nicht als Audio lesbar, übersprungen: %s (%s)", path, exc)
    return items


def find_candidates(
    paths: list[Path],
    *,
    mbid: str | None = None,
    artist: str | None = None,
    album: str | None = None,
) -> MatchResult:
    """Sucht Match-Kandidaten für einen Satz Dateien.

    ``mbid`` schränkt die Suche auf genau einen Release ein; ``artist``/``album``
    überschreiben die Suchbegriffe, die beets sonst aus den vorhandenen Tags
    ableitet.

    Läuft synchron und kann einige Sekunden dauern (MusicBrainz drosselt auf
    etwa eine Anfrage pro Sekunde). Die Endpunkte sind deshalb als ``def``
    definiert, damit FastAPI sie in den Threadpool schiebt.
    """
    beets_env.ensure_loaded()
    from beets.autotag import Recommendation, tag_album

    items = load_items(paths)
    if not items:
        return MatchResult(
            current_artist="",
            current_album="",
            recommendation="none",
            error="Keine lesbaren Audiodateien in dieser Auswahl.",
        )

    search_ids = [mbid] if mbid else []
    search_artist = artist or None
    search_name = album or None
    # beets verwirft *beide* Suchbegriffe, sobald einer der beiden fehlt, und
    # sucht stattdessen komplett mit den (oft falschen) Tags aus den Dateien
    # weiter -- siehe tag_album(): "if not (search_artist and search_name):
    # search_artist, search_name = cur_artist, cur_album". Wer hier nur den
    # Künstler einträgt und das Album leer lässt, würde also seine Eingabe
    # stillschweigend verlieren. Ein Leerzeichen als Platzhalter für das
    # jeweils andere Feld hält beets' Prüfung zufrieden, wird aber beim
    # eigentlichen MusicBrainz-Query wieder herausgefiltert (dort wird jeder
    # Kriteriumswert getrimmt und bei Leere verworfen).
    if search_artist or search_name:
        search_artist = search_artist or " "
        search_name = search_name or " "
    try:
        current_artist, current_album, proposal = tag_album(
            items,
            search_artist=search_artist,
            search_name=search_name,
            search_ids=search_ids,
        )
    except Exception as exc:  # Netzfehler, MusicBrainz-Ausfall, Plugin-Fehler
        log.exception("Match-Suche fehlgeschlagen")
        return MatchResult(
            current_artist="",
            current_album="",
            recommendation="none",
            error=f"Suche fehlgeschlagen: {exc}",
        )

    recommendation = Recommendation(proposal.recommendation).name
    candidates = [
        serialize_candidate(match, index)
        for index, match in enumerate(proposal.candidates)
    ]
    # Die Empfehlung von beets bezieht sich auf den besten Kandidaten.
    if candidates:
        candidates[0].recommendation = recommendation

    note = ""
    if not candidates:
        if mbid:
            note = (
                "Zu dieser MusicBrainz-ID wurde kein Release gefunden. Ist es "
                "eine Release-ID (nicht Release-Group oder Recording)?"
            )
        else:
            note = (
                "Keine Kandidaten gefunden. Versuche es mit Künstler und Album "
                "als Suchbegriffe oder gib eine MusicBrainz-Release-ID an."
            )

    return MatchResult(
        current_artist=current_artist or "",
        current_album=current_album or "",
        recommendation=recommendation,
        candidates=candidates,
        note=note,
    )


def find_candidate_by_id(
    paths: list[Path], album_id: str, *, mbid: str | None = None
) -> Any | None:
    """Holt einen bestimmten ``AlbumMatch`` erneut, um ihn anzuwenden.

    Die Kandidaten aus :func:`find_candidates` sind reine Anzeigeobjekte; zum
    Schreiben der Tags brauchen wir den echten ``AlbumMatch`` mit seiner
    Zuordnung zu den ``Item``-Objekten. Deshalb suchen wir gezielt nochmal --
    mit der Release-ID ist das eine einzige, schnelle Abfrage.
    """
    beets_env.ensure_loaded()
    from beets.autotag import tag_album

    items = load_items(paths)
    if not items:
        return None

    _, _, proposal = tag_album(items, search_ids=[mbid or album_id])
    for match in proposal.candidates:
        if str(match.info.album_id) == str(album_id):
            return match
    # Kein Kandidat trägt die gewünschte ID -- lieber "nicht gefunden" melden
    # (siehe routes.choose()) als stillschweigend ein anderes Album zu
    # übernehmen, das der Nutzer gar nicht ausgewählt hat.
    return None
