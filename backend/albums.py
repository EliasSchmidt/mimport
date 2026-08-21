"""Bereits importierte Alben ansehen und nachträglich Cover oder Metadaten ändern.

Anders als beim Import (``backend.importer``) und beim Matching
(``backend.beets_env``) muss dieses Modul die **gefüllte** Library lesen --
genau die Datenbank, die ``beet import`` schreibt. Trotzdem wird sie nicht in
diesem Prozess geöffnet, sondern ausschließlich über den ``beet``-Subprozess
angefasst: dieselbe Begründung wie in ``backend.importer`` -- ein zweiter,
eigener Zugriff auf dieselbe ``library.db`` wäre eine zweite Möglichkeit,
Schema oder Sperren gegeneinander laufen zu lassen.

Das Cover selbst landet wie überall in mimport als ``cover.jpg`` im Ordner
(``backend.cover``). Für ein *neu* importiertes Album reicht das: beets holt
es beim nächsten Import über ``fetchart``. Für ein *schon* importiertes Album
gibt es diesen Automatismus nicht mehr -- die Datei im Ordner ändert sich,
aber die schon vorhandenen Audiodateien tragen ihr altes Cover noch in den
eigenen Tags. Deshalb bettet ``update_cover`` es zusätzlich explizit über
``beet embedart -f`` ein.

Dieselbe Lücke gibt es beim MusicBrainz-Künstlerlink: manche Alben oder
einzelne Titel haben keine ``mb_albumartistid``/``mb_artistid``, weil der
Import als-ist lief oder MusicBrainz den Namen damals nicht fand. Das lässt
sich über ``beet modify`` nachtragen -- mit einem Stolperstein, der beim
Testen auffiel: ``mb_albumartistid`` (einzeln) und ``mb_albumartistids``
(Liste, beets 2.x) teilen sich beim Schreiben ins Datei-Tag denselben
Speicherplatz. Setzt man nur das einzelne Feld, meldet ``beet modify`` zwar
„geändert" und die Datenbank stimmt -- die Datei bleibt aber unverändert,
weil das gleichzeitig mitgeschriebene leere ``…ids``-Feld das einzelne beim
Schreiben wieder leert. Beide Funktionen setzen deshalb immer beide Felder
zusammen.
"""

from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend import cover, tag_catalog, tagging
from backend.config import settings
from backend.importer import library_lock

log = logging.getLogger(__name__)

#: Trennt Felder in der ``beet list``-Ausgabe. Kein Zeichen, das in einem
#: Interpreten- oder Albumnamen vorkommen könnte -- anders als Komma, Tab oder
#: Pipe.
_TRENNER = "\x1f"

#: Trennt *Datensätze* (ein Album/Titel je Aufruf von ``beet list``)
#: voneinander -- ``splitlines()`` reicht dafür nicht: Freitextfelder wie
#: ``albumdisambig`` können selbst einen Zeilenumbruch enthalten, und der
#: würde eine einzelne Zeile sonst mitten im Datensatz zerreißen (siehe
#: ``_saetze``). Ein eigenes Zeichen statt einer echten Zeile macht das
#: unabhängig vom Inhalt jedes einzelnen Feldes.
_SATZENDE = "\x1e"

#: Womit beets mehrere Werte *innerhalb* eines Feldes trennt (siehe
#: ``_ID_MEHRWERTIG`` in ``tagging.py``). Nicht zu verwechseln mit
#: ``_TRENNER`` oben, das die Felder *untereinander* trennt.
_ID_TRENNER = "; "

#: Katalogfelder, die schon ein eigenes Dataclass-Attribut haben (mit
#: Sonderverhalten wie ``year_editierbar`` oder dem MusicBrainz-Link-UI) --
#: der Rest aus ``tag_catalog`` landet generisch im ``erweitert``-Dict. Die
#: Künstler-Felder (``albumartists``/``artists``, ``mb_*artistids``) bleiben
#: bewusst außen vor: Namen und MBID-Zuordnung laufen weiter über
#: ``kuenstler_links``, nicht über den generischen Katalog-Mechanismus.
_ALBUM_KERNFELDER = {"albumartists", "album", "year", "genres", "label", "mb_albumartistids"}
_ALBUM_ERWEITERT_FELDER = tuple(
    f for f in tag_catalog.ALBUM_FELDER if f.key not in _ALBUM_KERNFELDER
)
_TRACK_KERNFELDER = {"artists", "title", "mb_artistids"}
_TRACK_ERWEITERT_FELDER = tuple(
    f for f in tag_catalog.TRACK_FELDER if f.key not in _TRACK_KERNFELDER
)

#: Reihenfolge muss zu ``_aus_satz`` passen. ``$path`` ist bei einem Album
#: ein berechnetes Feld (der Ordner der ersten Spur), keine Datenbankspalte.
#: Die ersten neun Felder sind die "Kernfelder" mit eigenem Attribut, danach
#: kommt für jedes übrige Katalogfeld ein weiterer Wert.
_ALBUM_KERN_FELDER = (
    "$id", "$albumartist", "$album", "$year", "$path", "$mb_albumartistid",
    "$genres", "$label", "$mb_albumartistids",
)
_FELDER = _ALBUM_KERN_FELDER + tuple(f"${f.key}" for f in _ALBUM_ERWEITERT_FELDER)
_FORMAT = _TRENNER.join(_FELDER) + _SATZENDE

#: Reihenfolge muss zu ``_track_aus_satz`` passen.
_TRACK_KERN_FELDER = ("$id", "$track", "$title", "$artist", "$mb_artistid", "$mb_artistids")
_TRACK_FELDER = _TRACK_KERN_FELDER + tuple(f"${f.key}" for f in _TRACK_ERWEITERT_FELDER)
_TRACK_FORMAT = _TRENNER.join(_TRACK_FELDER) + _SATZENDE


def _saetze(stdout: str) -> list[str]:
    """Zerlegt eine ``beet list``-Ausgabe in ihre Datensätze.

    Nicht per ``splitlines()`` -- ein Datensatz kann selbst einen
    Zeilenumbruch enthalten (siehe ``_SATZENDE``). beets hängt nach jedem
    ``_SATZENDE`` noch das eigene Zeilenende an; das führende ``\\n`` jedes
    Datensatzes (außer dem ersten) gehört also nicht zum Inhalt.
    """
    return [teil.lstrip("\n") for teil in stdout.split(_SATZENDE) if teil.strip("\n")]


class AlbumError(Exception):
    """Die Library-Abfrage oder das Einbetten des Covers ist fehlgeschlagen."""


def _kuenstler_ids(namen: list[str], roh_ids: str, einzel_mbid: str) -> list[str]:
    """Baut positionsgleich zu ``namen`` eine MBID-Liste, ein Eintrag je Name.

    Fällt auf den alten Einzelwert zurück, wenn die Mehrfachform (noch) leer
    ist -- Alben/Titel, die vor der Einzel-Verknüpfung schon einmal einen MB-
    Link bekommen haben, kennen nur ``mb_albumartistid``/``mb_artistid``.
    Passt die Länge trotzdem nicht zur Namensliste (z. B. weil der
    Interpretenname nachträglich geändert wurde, ohne die MBIDs
    anzufassen), gilt das als unverbunden statt Namen und IDs falsch
    zuzuordnen.
    """
    ids = roh_ids.split(_ID_TRENNER) if roh_ids else ([einzel_mbid] if einzel_mbid else [])
    if len(ids) != len(namen):
        return [""] * len(namen)
    return ids


@dataclass(frozen=True)
class Album:
    id: int
    albumartist: str
    album: str
    year: str
    path: Path
    mb_albumartistid: str = ""
    genres: str = ""
    label: str = ""
    mb_albumartistids: str = ""
    #: Alle übrigen Katalogfelder (``tag_catalog.ALBUM_FELDER`` minus den
    #: Kernfeldern oben), Katalog-Schlüssel auf Wert.
    erweitert: dict[str, str] = field(default_factory=dict)

    @property
    def year_editierbar(self) -> str:
        """Das Jahr fürs Bearbeiten-Formular.

        '0000' ist beets' Sentinel für "kein Jahr bekannt", keine echte
        Jahreszahl -- vorausgefüllt stünde sonst eine falsche Zahl im Feld,
        die beim Speichern anschließend wörtlich zurückgeschrieben würde.
        """
        return self.year if self.year and self.year != "0000" else ""

    @property
    def cover_path(self) -> Path:
        return self.path / cover.COVER_DATEI

    @property
    def has_cover(self) -> bool:
        return cover.vorhanden(self.path)

    @property
    def cover_version(self) -> str:
        """Änderungszeit des Covers, fürs Cache-Busting in der Bild-URL."""
        try:
            return str(int(self.cover_path.stat().st_mtime_ns))
        except OSError:
            return ""

    @property
    def has_albumartist_mbid(self) -> bool:
        return bool(self.mb_albumartistid)

    @property
    def kuenstler_links(self) -> list[tuple[str, str]]:
        """(Name, MBID) je Album-Interpret, MBID leer wenn (noch) unverbunden.

        Bei mehreren Interpreten (``"A feat. B"``) einzeln aufgelöst, damit
        das UI jeden für sich verlinken oder suchen lassen kann, statt das
        ganze Feld hinter einer einzigen Ja/Nein-Verknüpfung zu verstecken.
        """
        namen = tagging.kuenstlerliste(self.albumartist)
        ids = _kuenstler_ids(namen, self.mb_albumartistids, self.mb_albumartistid)
        return list(zip(namen, ids))

    @property
    def alle_werte(self) -> dict[str, str]:
        """Jedes Katalogfeld außer den Künstler-Feldern (siehe
        ``kuenstler_links``) auf seinen aktuellen Wert -- Kernattribute und
        ``erweitert`` zusammengeführt, für generisches Rendering im Template.
        """
        return {
            "album": self.album,
            "year": self.year_editierbar,
            "genres": self.genres,
            "label": self.label,
            **self.erweitert,
        }


@dataclass(frozen=True)
class Track:
    id: int
    track: str
    title: str
    artist: str
    mb_artistid: str = ""
    mb_artistids: str = ""
    #: Wie ``Album.erweitert``, für ``tag_catalog.TRACK_FELDER``.
    erweitert: dict[str, str] = field(default_factory=dict)

    @property
    def has_artist_mbid(self) -> bool:
        return bool(self.mb_artistid)

    @property
    def kuenstler_links(self) -> list[tuple[str, str]]:
        """Wie ``Album.kuenstler_links``, für den Titel-Interpreten."""
        namen = tagging.kuenstlerliste(self.artist)
        ids = _kuenstler_ids(namen, self.mb_artistids, self.mb_artistid)
        return list(zip(namen, ids))

    @property
    def alle_werte(self) -> dict[str, str]:
        """Wie ``Album.alle_werte``, für ``tag_catalog.TRACK_FELDER``."""
        return {"title": self.title, **self.erweitert}


def _lauf(args: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [settings.beet_bin, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            # Kein shell=True: Album- und Interpretennamen gehen unverändert
            # als Argument durch, ohne dass ein Sonderzeichen darin etwas
            # auslösen könnte.
            shell=False,
        )
    except FileNotFoundError as exc:
        raise AlbumError(
            f"'{settings.beet_bin}' wurde nicht gefunden. Pfad zum beets des "
            "Servers über MIMPORT_BEET_BIN setzen."
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise AlbumError("beets antwortet nicht.") from exc


def _aus_zeile(satz: str) -> Album | None:
    teile = satz.split(_TRENNER)
    if len(teile) != len(_FELDER):
        # Eine Ausgabe, die nicht zum Format passt, ignorieren statt
        # abzubrechen -- ein einzelner kaputter Datensatz soll nicht die
        # ganze Liste unbrauchbar machen.
        log.warning("Unerwartete beet-list-Ausgabe ignoriert: %r", satz)
        return None
    kern, erweitert_werte = teile[: len(_ALBUM_KERN_FELDER)], teile[len(_ALBUM_KERN_FELDER) :]
    id_roh, albumartist, album, year, pfad, mb_albumartistid, genres, label, mb_albumartistids = kern
    try:
        id_ = int(id_roh)
    except ValueError:
        log.warning("Album ohne gültige ID ignoriert: %r", satz)
        return None
    return Album(
        id=id_,
        albumartist=albumartist,
        album=album,
        year=year,
        path=Path(pfad),
        mb_albumartistid=mb_albumartistid,
        genres=genres,
        label=label,
        mb_albumartistids=mb_albumartistids,
        erweitert=dict(zip((f.key for f in _ALBUM_ERWEITERT_FELDER), erweitert_werte)),
    )


def _track_aus_zeile(satz: str) -> Track | None:
    teile = satz.split(_TRENNER)
    if len(teile) != len(_TRACK_FELDER):
        log.warning("Unerwartete beet-list-Ausgabe (Track) ignoriert: %r", satz)
        return None
    kern = teile[: len(_TRACK_KERN_FELDER)]
    erweitert_werte = teile[len(_TRACK_KERN_FELDER) :]
    id_roh, track, title, artist, mb_artistid, mb_artistids = kern
    try:
        id_ = int(id_roh)
    except ValueError:
        log.warning("Track ohne gültige ID ignoriert: %r", satz)
        return None
    return Track(
        id=id_,
        track=track,
        title=title,
        artist=artist,
        mb_artistid=mb_artistid,
        mb_artistids=mb_artistids,
        erweitert=dict(zip((f.key for f in _TRACK_ERWEITERT_FELDER), erweitert_werte)),
    )


def _track_sortschluessel(track: str) -> tuple[int, str]:
    try:
        return (int(track), "")
    except ValueError:
        # Kein numerischer Tracktitel -- ans Ende, aber stabil sortiert.
        return (10**9, track)


def list_albums(query: str = "") -> list[Album]:
    """Alle Alben aus der Library, alphabetisch nach Interpret und Titel.

    ``query`` geht unverändert als ein einzelnes Argument an ``beet list`` --
    freier Text (etwa ein Interpretenname) reicht als Fuzzy-Suche in beets
    eigener Query-Sprache.
    """
    args = ["list", "-a", "-f", _FORMAT]
    if query:
        args.append(query)
    proc = _lauf(args, timeout=30)
    if proc.returncode != 0:
        raise AlbumError(
            proc.stderr.strip() or "Die Albenliste ließ sich nicht abrufen."
        )
    alben = [a for satz in _saetze(proc.stdout) if (a := _aus_zeile(satz))]
    alben.sort(key=lambda a: (a.albumartist.lower(), a.year, a.album.lower()))
    return alben


def get_album(album_id: int) -> Album | None:
    """Ein einzelnes Album über seine beets-ID.

    ``id:`` statt ``album_id:`` -- Letzteres ist ein Feld der *Items*, die
    Album-Abfrage (``-a``) kennt nur ihr eigenes ``id``.
    """
    treffer = list_albums(f"id:{album_id}")
    return treffer[0] if treffer else None


def list_tracks(album_id: int) -> list[Track]:
    """Die Titel eines Albums, der Tracknummer nach sortiert."""
    proc = _lauf(
        ["list", "-f", _TRACK_FORMAT, f"album_id:{album_id}"], timeout=30
    )
    if proc.returncode != 0:
        raise AlbumError(
            proc.stderr.strip() or "Die Titelliste ließ sich nicht abrufen."
        )
    tracks = [t for satz in _saetze(proc.stdout) if (t := _track_aus_zeile(satz))]
    tracks.sort(key=lambda t: _track_sortschluessel(t.track))
    return tracks


def get_track(track_id: int) -> Track | None:
    """Ein einzelner Titel über seine beets-ID."""
    proc = _lauf(["list", "-f", _TRACK_FORMAT, f"id:{track_id}"], timeout=30)
    if proc.returncode != 0:
        raise AlbumError(proc.stderr.strip() or "Der Titel ließ sich nicht abrufen.")
    for satz in _saetze(proc.stdout):
        if (t := _track_aus_zeile(satz)) is not None:
            return t
    return None


def update_cover(album: Album, bild_pfad: Path) -> None:
    """Bettet ein Bild in alle Dateien eines Albums ein.

    ``embedart -f`` fragt über eine Item-Query, keine Album-Query -- deshalb
    hier über ``album_id`` statt über ``album.id`` direkt. Das Datenbankfeld
    ``artpath`` fasst das dabei nicht an. Hatte das Album schon ein Cover,
    zeigt es weiterhin richtig auf dieselbe, gerade überschriebene Datei; gab
    es noch keins, bleibt ``artpath`` leer, obwohl jetzt eine ``cover.jpg`` im
    Ordner liegt. Das stört nur, wer sich auf dieses Feld verlässt -- mimport
    selbst prüft für „hat ein Cover" die Datei direkt (``Album.has_cover``).
    """
    with library_lock():
        proc = _lauf(
            ["embedart", "-y", "-f", str(bild_pfad), f"album_id:{album.id}"],
            timeout=120,
        )
    if proc.returncode != 0:
        raise AlbumError(
            proc.stderr.strip() or "Das Cover ließ sich nicht einbetten."
        )


def retry_missing_cover(
    mb_albumid: str, *, attempts: int = 3, pause: float = 5.0
) -> bool:
    """Versucht bis zu ``attempts``-mal, das Cover eines frisch importierten
    MusicBrainz-Albums nachzuladen.

    Hintergrund: Die Cover Art Archive (``coverartarchive.org``) antwortet
    gelegentlich mit einem transienten 5xx, und beets' ``fetchart``-Plugin
    wiederholt diese eine Anfrage nicht selbst -- anders als das
    ``musicbrainz``-Plugin für seine eigenen Anfragen. Ein einzelner
    Ausrutscher beim automatischen Fetch während ``beet import -A`` bedeutet
    sonst dauerhaft kein Cover, obwohl MusicBrainz eins hat. Nachgewiesen bei
    der Entwicklung: mehrere ``beet fetchart``-Läufe hintereinander gegen
    dieselbe Release-ID, abwechselnd HTTP 500, HTTP 502 und "no art found",
    bis einer schließlich durchging -- ``pause`` ist bewusst nicht knapp
    bemessen, weil ein einzelner Zwei-Sekunden-Abstand bei der Prüfung nicht
    gereicht hätte.

    Nur für MusicBrainz gedacht -- fetchart kennt für Discogs keine
    automatische Quelle, dafür lädt ``backend.cover.von_url_holen`` das Bild
    schon beim Übernehmen des Kandidaten selbst herunter.

    Bewusst *nach* dem Import aufgerufen, nicht als Teil davon: Der eigentliche
    Import-Erfolg (Tags stehen, Dateien liegen richtig) hängt nicht am Cover,
    und ein paar Sekunden Wartezeit sollen die Rückmeldung "Import
    abgeschlossen" nicht verzögern, wenn der erste Versuch schon reicht --
    genau der Normalfall.

    Gibt zurück, ob das Album danach ein Cover hat (auch wenn schon vorher
    eins da war). Kein Fehler, wenn nicht -- nur ein Bestcase-Versuch, und
    fotografieren bleibt über die Album-Seite immer noch möglich.
    """
    try:
        treffer = list_albums(f"mb_albumid:{mb_albumid}")
    except AlbumError as exc:
        log.warning("Cover-Nachschlag für %s übersprungen: %s", mb_albumid, exc)
        return False
    if not treffer:
        return False

    album = treffer[0]
    if album.has_cover:
        return True

    for versuch in range(attempts):
        if versuch:
            time.sleep(pause)
        try:
            with library_lock():
                # 'id:', nicht 'album_id:' -- Letzteres ist ein Item-Feld
                # (siehe get_album()) und beim 'fetchart'-Kommando eine
                # Substring-Suche: 'album_id:1' träfe auch die Alben 10, 11,
                # 21, .... Empirisch geprüft an einer Library mit elf Alben.
                proc = _lauf(["fetchart", f"id:{album.id}"], timeout=60)
        except AlbumError as exc:
            # _lauf() wirft z. B. bei einem hängenden Subprozess (60s-Timeout)
            # oder einem plötzlich verschwundenen beet-Binary. Ein Bestcase-
            # Versuch darf den sonst erfolgreichen Import nicht als
            # fehlgeschlagen erscheinen lassen -- also weiter zum nächsten
            # Versuch statt die Ausnahme aus dieser Funktion rausfallen zu
            # lassen.
            log.warning(
                "fetchart-Wiederholung %d/%d für Album %d fehlgeschlagen: %s",
                versuch + 1,
                attempts,
                album.id,
                exc,
            )
            continue
        if proc.returncode != 0:
            log.warning(
                "fetchart-Wiederholung %d/%d für Album %d fehlgeschlagen: %s",
                versuch + 1,
                attempts,
                album.id,
                proc.stderr.strip(),
            )
        if cover.vorhanden(album.path):
            log.info(
                "Cover für Album %d nach %d Wiederholung(en) doch noch geladen",
                album.id,
                versuch + 1,
            )
            return True

    log.info(
        "Cover für Album %d blieb nach %d Wiederholungen aus -- Cover Art "
        "Archive hat vermutlich keins für dieses Release, oder war die ganze "
        "Zeit über nicht erreichbar.",
        album.id,
        attempts,
    )
    return False


def _modify(vorwahl: list[str], query: str, felder: dict[str, str], *, timeout: int) -> None:
    """``beet modify -y <vorwahl> <query> feld=wert …``, Fehler als ``AlbumError``."""
    args = ["modify", "-y", *vorwahl, query, *(f"{k}={v}" for k, v in felder.items())]
    proc = _lauf(args, timeout=timeout)
    if proc.returncode != 0:
        raise AlbumError(
            proc.stderr.strip() or "Das Ändern der Tags ist fehlgeschlagen."
        )


def set_album_artist_mbid(album: Album, index: int, mbid: str) -> None:
    """Verknüpft *einen* Album-Interpreten (bei "A feat. B" per Position) mit
    einer MusicBrainz-ID, ohne die MBIDs der anderen Interpreten zu verlieren.

    ``mb_albumartistids`` trägt weiterhin alle Positionen -- auch die noch
    unverbundenen als leerer Platzhalter --, damit ``Album.kuenstler_links``
    Namen und IDs später wieder richtig zuordnen kann. Das einzelne
    ``mb_albumartistid`` bekommt zur Kompatibilität mit älteren Abspielern
    die erste gesetzte ID.

    Zwei getrennte ``beet modify``-Aufrufe, weil Album- und Item-Zeilen in
    beets unabhängig sind und ``-a`` nicht zuverlässig in die Dateien
    zurückschreibt (siehe Moduldoc): Erst die Titel über ``album_id:`` -- das
    schreibt Datenbank *und* Datei --, danach die Album-Zeile selbst über
    ``id:`` mit ``-a``, nur für die eigene Anzeige. ``-W`` unterdrückt dabei
    den erneuten, hier überflüssigen Dateizugriff; ``-I`` das erneute
    Durchreichen an die (schon richtigen) Titel.
    """
    namen = tagging.kuenstlerliste(album.albumartist)
    ids = _kuenstler_ids(namen, album.mb_albumartistids, album.mb_albumartistid)
    if not 0 <= index < len(ids):
        raise AlbumError("Ungültige Interpreten-Position.")
    ids[index] = mbid
    felder = {
        "mb_albumartistid": next((i for i in ids if i), ""),
        "mb_albumartistids": _ID_TRENNER.join(ids),
    }
    with library_lock():
        _modify([], f"album_id:{album.id}", felder, timeout=120)
        _modify(["-a", "-W", "-I"], f"id:{album.id}", felder, timeout=30)


def set_track_artist_mbid(track: Track, index: int, mbid: str) -> None:
    """Wie ``set_album_artist_mbid``, für den Interpreten eines Titels.

    Anders als beim Album gibt es hier keine zweite, unabhängige Zeile --
    ``id:`` trifft direkt den Titel, ein Aufruf genügt für Datenbank und
    Datei.
    """
    namen = tagging.kuenstlerliste(track.artist)
    ids = _kuenstler_ids(namen, track.mb_artistids, track.mb_artistid)
    if not 0 <= index < len(ids):
        raise AlbumError("Ungültige Interpreten-Position.")
    ids[index] = mbid
    felder = {
        "mb_artistid": next((i for i in ids if i), ""),
        "mb_artistids": _ID_TRENNER.join(ids),
    }
    with library_lock():
        _modify([], f"id:{track.id}", felder, timeout=60)


def set_album_interpret(album: Album, wert: str) -> None:
    """Ändert den rohen Interpretennamen-Text (nicht die MB-Verknüpfung).

    Setzt zugleich die mehrwertige Namensliste (``albumartists``) neu, damit
    Navidrome & Co. bei mehreren Interpreten korrekt gruppieren -- und
    verwirft alle bisherigen MusicBrainz-IDs: eine geänderte Schreibweise
    kann nicht mehr sicher zu den alten Positionen gehören (siehe
    ``_kuenstler_ids``). Genau das tut auch das manuelle Taggen vor dem
    Import, wenn der Name nach einer MB-Auswahl geändert wird
    (``static/index.js``, "Name geändert -- Match bitte neu prüfen").
    """
    namen = tagging.kuenstlerliste(wert)
    felder = {
        "albumartist": wert,
        "albumartists": _ID_TRENNER.join(namen),
        "mb_albumartistid": "",
        "mb_albumartistids": "",
    }
    with library_lock():
        _modify([], f"album_id:{album.id}", felder, timeout=120)
        _modify(["-a", "-W", "-I"], f"id:{album.id}", felder, timeout=30)


def set_track_interpret(track: Track, wert: str) -> None:
    """Wie ``set_album_interpret``, für den Interpreten eines Titels."""
    namen = tagging.kuenstlerliste(wert)
    felder = {
        "artist": wert,
        "artists": _ID_TRENNER.join(namen),
        "mb_artistid": "",
        "mb_artistids": "",
    }
    with library_lock():
        _modify([], f"id:{track.id}", felder, timeout=60)


def set_album_field(album: Album, feld: tag_catalog.Feld, wert: str) -> None:
    """Setzt ein beliebiges Katalogfeld auf Album-Ebene, egal ob leer oder nicht.

    Anders als ``update_album_fields``: ein leerer Wert *löscht* das Feld,
    statt es unangetastet zu lassen -- ein direkt editierbares Eingabefeld
    muss den Tag auch wirklich leeren können, wenn man seinen Inhalt
    entfernt. Mehrwertige Felder (``feld.einzelform`` gesetzt) bekommen ihre
    Einzelform automatisch mit demselben Wert mitgesetzt (siehe Moduldoc --
    nachgemessen harmlos, selbst wenn beets die Einzelform selbst als
    deprecated behandelt).

    Künstler-Felder (``feld.kuenstler_link``) laufen nicht hierüber, sondern
    über ``set_album_interpret``/``set_album_artist_mbid`` -- die brauchen
    die MBID-Invalidierung bzw. Positions-Logik, die hier fehlt.
    """
    if feld.kuenstler_link:
        raise AlbumError("Dieses Feld läuft über die Künstler-Verknüpfung.")
    felder = {feld.key: wert}
    if feld.einzelform:
        felder[feld.einzelform] = wert
    with library_lock():
        _modify([], f"album_id:{album.id}", felder, timeout=120)
        _modify(["-a", "-W", "-I"], f"id:{album.id}", felder, timeout=30)


def set_track_field(track: Track, feld: tag_catalog.Feld, wert: str) -> None:
    """Wie ``set_album_field``, für ein Katalogfeld auf Track-Ebene."""
    if feld.kuenstler_link:
        raise AlbumError("Dieses Feld läuft über die Künstler-Verknüpfung.")
    felder = {feld.key: wert}
    if feld.einzelform:
        felder[feld.einzelform] = wert
    with library_lock():
        _modify([], f"id:{track.id}", felder, timeout=60)


