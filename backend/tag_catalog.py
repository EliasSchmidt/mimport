"""Katalog aller Album-/Track-Felder, die mimport nachträglich bearbeiten lässt.

Einzige Quelle der Wahrheit für: welche Felder es gibt, wie sie beets-seitig
heißen, ob sie mehrwertig sind (und mit welcher Einzelform sie sich beim
Schreiben ins Datei-Tag den Speicherplatz teilen -- siehe ``albums``-
Moduldoc), und in welcher Gruppe sie im UI stehen. ``tagging.py`` (Import,
in-process über ``beets.library.Item``) und ``albums.py`` (Bearbeiten nach dem
Import, über ``beet modify``-Subprozess) lesen beide von hier -- fehlt ein
Feld auf der einen Seite, fällt das jetzt auf, statt zwei parallel
gepflegte Listen leise auseinanderlaufen zu lassen.

Bewusst NICHT jedes Feld, das beets oder mediafile theoretisch kennen:

- Automatisch berechnet: ReplayGain (``rg_*``), R128-Gain (``r128_*``) --
  von Hand gesetzt wären das erfundene Werte.
- Binär oder eigens verwaltet: Cover (``art``/``images``, läuft über
  ``backend.cover``).
- Ausdrücklich nicht gewünscht: AcoustID (``acoustid_fingerprint``/-``id``).
- Vom Encoder gesetzt, nicht von Hand: ``encoder``.
- Mehrzeilig: ``comments``/``lyrics``/``synced_lyrics``. Die aktuelle
  ``beet list -f``-Zeilenlogik in ``albums.py`` verträgt eingebettete
  Zeilenumbrüche (Satzende-Marker statt Zeilenumbruch), Freitext dieser
  Länge bleibt trotzdem vorerst außen vor -- ein eigenes Eingabefeld dafür
  ist ein separates UI-Thema.
- ``date``/``original_date``: zusammengesetzt aus ``year``/``month``/``day``
  bzw. ``original_year``/-``month``/-``day``. Die Teile reichen und
  vermeiden, dasselbe Tag über zwei Felder editierbar zu machen.

Beschränkt außerdem auf das, was ``beets.library.Album`` bzw. ``.Item``
tatsächlich als Spalte führt (nachgeprüft gegen ``library.Album._fields``/
``library.Item._fields``) UND was ``mediafile`` als Datei-Tag kennt -- nicht
jedes von beets mitgeführte Feld erfüllt beides (``composer`` etwa ist als
Spalte inzwischen deprecated, ``catalognums``/``languages`` gibt es nur als
Datei-Tag, nicht als über ``beet list`` abrufbare Spalte). Wo nur die
Einzelform abrufbar ist, bleibt das Feld einwertig.

Mehrwertige Felder mit einer Einzelform, die *auch* ein echtes
``mediafile``-Feld ist, schreiben immer beide zusammen -- nachgemessen: das
ist selbst dann harmlos, wenn beets die Einzelform für ``beet modify`` als
deprecated behandelt und den Wert dort verwirft (``composer``/``composers``),
und für andere Paare (``genre``/``genres``, ``mb_albumartistid``/-``ids``)
nachweislich nötig, weil sich beide sonst denselben Speicherplatz im
Datei-Tag streitig machen.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

#: "kuenstler": Namen, getrennt wie bei Navidrome ("feat.", "/", ";") --
#:   siehe ``tagging._KUENSTLER_TRENNER``.
#: "genre": nur am Semikolon getrennt -- siehe ``tagging._GENRE_TRENNER``.
#: "liste": generisch am Semikolon getrennt, sonst keine Sonderregel (u. a.
#:   alle MusicBrainz-IDs).
#: ``None``: einwertiges Feld.
MehrwertigArt = Literal["kuenstler", "genre", "liste"] | None


@dataclass(frozen=True)
class Feld:
    #: Kanonischer beets-Schlüssel. Bei mehrwertigen Feldern die Pluralform,
    #: die den Wert tatsächlich als Liste führt.
    key: str
    label: str
    #: "basis" | "musicbrainz" | "erweitert" -- steuert nur die Gruppierung
    #: im UI, nicht das Verhalten.
    gruppe: str
    #: Gegenstück, das beim Schreiben mit demselben Wert mitgesetzt wird
    #: (siehe Moduldoc). ``None`` bei einwertigen Feldern oder wenn keine
    #: taugliche Einzelform existiert (z. B. ``remixers``).
    einzelform: str | None = None
    mehrwertig_art: MehrwertigArt = None
    #: "zahl" rendert ein <input type="number"> -- nur für Felder, die
    #: tatsächlich reine Ganzzahlen sind (Jahr, Tracknummer, ...). Codes wie
    #: Katalognummer oder Barcode bleiben "text", auch wenn sie nur Ziffern
    #: enthalten: führende Nullen und Trennzeichen dürfen nicht verloren
    #: gehen, und ein Spinner-Feld wäre dafür ohnehin unpassend.
    typ: Literal["text", "bool", "zahl"] = "text"
    #: Interpret/Album-Künstler: eigenes UI (Link zu MusicBrainz oder
    #: Suchwidget je Namen, siehe ``Album.kuenstler_links``) statt eines
    #: normalen Texteingabefelds -- das Feld selbst bleibt trotzdem Teil des
    #: Katalogs, damit es nicht doppelt gepflegt werden muss.
    kuenstler_link: bool = False
    #: Nimmt im Grid zwei Zellen statt einer -- für Felder, die typischerweise
    #: längeren Freitext tragen (Album, Interpret, Genre, ...). Reine
    #: Darstellungssache, ändert nichts am Schreibverhalten.
    breit: bool = False
    hinweis: str = ""

    @property
    def mehrwertig(self) -> bool:
        return self.mehrwertig_art is not None


ALBUM_FELDER: tuple[Feld, ...] = (
    # -- Basis ---------------------------------------------------------
    # Künstler-Felder: eigenes UI (siehe Feld.kuenstler_link), key/einzelform
    # trotzdem so benannt wie überall sonst -- das hält _MEHRWERTIG in
    # tagging.py mit dem heutigen, hartcodierten Dict deckungsgleich.
    Feld("albumartists", "Interpret", "basis", einzelform="albumartist",
         mehrwertig_art="kuenstler", kuenstler_link=True, breit=True),
    Feld("album", "Album", "basis", breit=True),
    Feld("year", "Jahr", "basis", typ="zahl"),
    Feld("month", "Monat", "basis", typ="zahl"),
    Feld("day", "Tag", "basis", typ="zahl"),
    Feld("genres", "Genre", "basis", einzelform="genre", mehrwertig_art="genre",
         breit=True),
    Feld("label", "Label", "basis", breit=True),
    Feld("comp", "Sampler (Various Artists)", "basis", typ="bool"),
    Feld("catalognum", "Katalognummer", "basis"),
    Feld("country", "Land", "basis"),
    Feld("disctotal", "Anzahl CDs", "basis", typ="zahl"),
    # -- MusicBrainz -----------------------------------------------------
    Feld("mb_albumartistids", "MB-Interpret-ID", "musicbrainz", einzelform="mb_albumartistid",
         mehrwertig_art="liste", kuenstler_link=True),
    Feld("mb_albumid", "MB-Release-ID", "musicbrainz",
         hinweis="Nur setzen, wenn genau dieser Release stimmt."),
    Feld("mb_releasegroupid", "MB-Release-Group-ID", "musicbrainz"),
    # -- Erweitert ---------------------------------------------------------
    Feld("albumartists_credit", "Interpret (Schreibweise)", "erweitert",
         einzelform="albumartist_credit", mehrwertig_art="liste"),
    Feld("albumartists_sort", "Interpret (Sortierung)", "erweitert",
         einzelform="albumartist_sort", mehrwertig_art="liste"),
    Feld("albumdisambig", "Album-Zusatz", "erweitert"),
    Feld("albumstatus", "Status", "erweitert", hinweis="z. B. official, promotion, bootleg"),
    Feld("albumtypes", "Album-Typ", "erweitert", einzelform="albumtype", mehrwertig_art="liste",
         hinweis="z. B. album, ep, single -- mehrere mit ;"),
    Feld("asin", "ASIN", "erweitert"),
    Feld("barcode", "Barcode", "erweitert"),
    Feld("language", "Sprache", "erweitert"),
    Feld("original_year", "Original-Jahr", "erweitert", typ="zahl"),
    Feld("original_month", "Original-Monat", "erweitert", typ="zahl"),
    Feld("original_day", "Original-Tag", "erweitert", typ="zahl"),
    Feld("script", "Schriftsystem", "erweitert"),
)

TRACK_FELDER: tuple[Feld, ...] = (
    # -- Basis ---------------------------------------------------------
    Feld("artists", "Interpret", "basis", einzelform="artist", mehrwertig_art="kuenstler",
         kuenstler_link=True),
    Feld("title", "Titel", "basis"),
    Feld("track", "Tracknummer", "basis", typ="zahl"),
    Feld("disc", "CD-Nummer", "basis", typ="zahl", hinweis="Bei Mehrfach-CD-Alben"),
    Feld("composers", "Komponist", "basis", einzelform="composer", mehrwertig_art="kuenstler",
         hinweis="Mehrere mit / oder ;"),
    # -- MusicBrainz -----------------------------------------------------
    Feld("mb_artistids", "MB-Interpret-ID", "musicbrainz", einzelform="mb_artistid",
         mehrwertig_art="liste", kuenstler_link=True),
    Feld("mb_trackid", "MB-Track-ID", "musicbrainz"),
    Feld("mb_releasetrackid", "MB-Release-Track-ID", "musicbrainz"),
    Feld("mb_workid", "MB-Work-ID", "musicbrainz", hinweis="Bei Klassik: das musikalische Werk"),
    # -- Erweitert ---------------------------------------------------------
    Feld("artists_credit", "Interpret (Schreibweise)", "erweitert", einzelform="artist_credit",
         mehrwertig_art="liste"),
    Feld("artists_sort", "Interpret (Sortierung)", "erweitert", einzelform="artist_sort",
         mehrwertig_art="liste"),
    Feld("composer_sort", "Komponist (Sortierung)", "erweitert"),
    Feld("disctitle", "CD-Titel", "erweitert"),
    Feld("grouping", "Gruppierung", "erweitert"),
    Feld("initial_key", "Tonart", "erweitert"),
    Feld("isrc", "ISRC", "erweitert"),
    Feld("lyricists", "Texter", "erweitert", einzelform="lyricist", mehrwertig_art="kuenstler"),
    Feld("arrangers", "Arrangeur", "erweitert", einzelform="arranger", mehrwertig_art="kuenstler"),
    Feld("remixers", "Remixer", "erweitert", mehrwertig_art="kuenstler"),
    Feld("subtitle", "Untertitel", "erweitert"),
    Feld("tracktotal", "Titel gesamt", "erweitert", typ="zahl"),
    Feld("media", "Medium", "erweitert", hinweis="z. B. CD, Vinyl, Digital Media"),
    Feld("bpm", "BPM", "erweitert", typ="zahl"),
)


def _nach_key(felder: tuple[Feld, ...]) -> dict[str, Feld]:
    return {f.key: f for f in felder}


ALBUM_FELDER_NACH_KEY = _nach_key(ALBUM_FELDER)
TRACK_FELDER_NACH_KEY = _nach_key(TRACK_FELDER)

#: Reihenfolge der Gruppen im UI -- nicht alphabetisch (dann stünde
#: "erweitert" vor "musicbrainz").
_GRUPPEN_REIHENFOLGE = ("basis", "musicbrainz", "erweitert")


def gruppiert(felder: tuple[Feld, ...]) -> list[tuple[str, list[Feld]]]:
    """Felder nach ``gruppe`` sortiert, in fester UI-Reihenfolge.

    Nicht der Jinja-Filter ``groupby`` -- der sortiert alphabetisch nach dem
    Gruppennamen, hier soll aber "basis" vor "musicbrainz" vor "erweitert"
    stehen.
    """
    eimer: dict[str, list[Feld]] = {g: [] for g in _GRUPPEN_REIHENFOLGE}
    for f in felder:
        eimer[f.gruppe].append(f)
    return [(g, eimer[g]) for g in _GRUPPEN_REIHENFOLGE if eimer[g]]


#: Vorberechnet fürs Template -- ändert sich nur, wenn der Katalog selbst
#: sich ändert, nicht je Anfrage.
ALBUM_GRUPPEN = gruppiert(ALBUM_FELDER)
TRACK_GRUPPEN = gruppiert(TRACK_FELDER)
