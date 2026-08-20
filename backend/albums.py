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
from dataclasses import dataclass
from pathlib import Path

from backend import cover, tagging
from backend.config import settings
from backend.importer import library_lock

log = logging.getLogger(__name__)

#: Trennt Felder in der ``beet list``-Ausgabe. Kein Zeichen, das in einem
#: Interpreten- oder Albumnamen vorkommen könnte -- anders als Komma, Tab oder
#: Pipe.
_TRENNER = "\x1f"

#: Womit beets mehrere Werte *innerhalb* eines Feldes trennt (siehe
#: ``_ID_MEHRWERTIG`` in ``tagging.py``). Nicht zu verwechseln mit
#: ``_TRENNER`` oben, das die Felder *untereinander* trennt.
_ID_TRENNER = "; "

#: Reihenfolge muss zu ``_aus_zeile`` passen. ``$path`` ist bei einem Album
#: ein berechnetes Feld (der Ordner der ersten Spur), keine Datenbankspalte.
#: ``$mb_albumartistids`` steht am Ende, damit bestehende Indizes stabil
#: bleiben.
_FELDER = (
    "$id", "$albumartist", "$album", "$year", "$path", "$mb_albumartistid",
    "$genres", "$label", "$mb_albumartistids",
)
_FORMAT = _TRENNER.join(_FELDER)

#: Reihenfolge muss zu ``_track_aus_zeile`` passen.
_TRACK_FELDER = ("$id", "$track", "$title", "$artist", "$mb_artistid", "$mb_artistids")
_TRACK_FORMAT = _TRENNER.join(_TRACK_FELDER)


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


@dataclass(frozen=True)
class Track:
    id: int
    track: str
    title: str
    artist: str
    mb_artistid: str = ""
    mb_artistids: str = ""

    @property
    def has_artist_mbid(self) -> bool:
        return bool(self.mb_artistid)

    @property
    def kuenstler_links(self) -> list[tuple[str, str]]:
        """Wie ``Album.kuenstler_links``, für den Titel-Interpreten."""
        namen = tagging.kuenstlerliste(self.artist)
        ids = _kuenstler_ids(namen, self.mb_artistids, self.mb_artistid)
        return list(zip(namen, ids))


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


def _aus_zeile(zeile: str) -> Album | None:
    teile = zeile.split(_TRENNER)
    if len(teile) != len(_FELDER):
        # Eine Ausgabe, die nicht zum Format passt, ignorieren statt
        # abzubrechen -- eine einzelne kaputte Zeile soll nicht die ganze
        # Liste unbrauchbar machen.
        log.warning("Unerwartete beet-list-Zeile ignoriert: %r", zeile)
        return None
    id_roh, albumartist, album, year, pfad, mb_albumartistid, genres, label, mb_albumartistids = teile
    try:
        id_ = int(id_roh)
    except ValueError:
        log.warning("Album ohne gültige ID ignoriert: %r", zeile)
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
    )


def _track_aus_zeile(zeile: str) -> Track | None:
    teile = zeile.split(_TRENNER)
    if len(teile) != len(_TRACK_FELDER):
        log.warning("Unerwartete beet-list-Zeile (Track) ignoriert: %r", zeile)
        return None
    id_roh, track, title, artist, mb_artistid, mb_artistids = teile
    try:
        id_ = int(id_roh)
    except ValueError:
        log.warning("Track ohne gültige ID ignoriert: %r", zeile)
        return None
    return Track(
        id=id_,
        track=track,
        title=title,
        artist=artist,
        mb_artistid=mb_artistid,
        mb_artistids=mb_artistids,
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
    alben = [a for zeile in proc.stdout.splitlines() if (a := _aus_zeile(zeile))]
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
    tracks = [t for zeile in proc.stdout.splitlines() if (t := _track_aus_zeile(zeile))]
    tracks.sort(key=lambda t: _track_sortschluessel(t.track))
    return tracks


def get_track(track_id: int) -> Track | None:
    """Ein einzelner Titel über seine beets-ID."""
    proc = _lauf(["list", "-f", _TRACK_FORMAT, f"id:{track_id}"], timeout=30)
    if proc.returncode != 0:
        raise AlbumError(proc.stderr.strip() or "Der Titel ließ sich nicht abrufen.")
    for zeile in proc.stdout.splitlines():
        if (t := _track_aus_zeile(zeile)) is not None:
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


def update_album_fields(album: Album, felder: dict[str, str]) -> None:
    """Ändert Albumkünstler, Albumtitel, Jahr oder Genre nachträglich.

    Dasselbe zweistufige Muster wie bei ``set_album_artist_mbid``: erst die
    Titel über ``album_id:`` -- das schreibt Datenbank *und* Datei --, danach
    die Album-Zeile selbst über ``id:`` mit ``-a``, nur für die eigene
    Anzeige.
    """
    if not felder:
        return
    with library_lock():
        _modify([], f"album_id:{album.id}", felder, timeout=120)
        _modify(["-a", "-W", "-I"], f"id:{album.id}", felder, timeout=30)


def update_track_fields(track_id: int, felder: dict[str, str]) -> None:
    """Ändert Titel oder Interpret eines einzelnen Titels nachträglich.

    Wie ``set_track_artist_mbid``: ``id:`` trifft direkt den Titel, ein
    Aufruf genügt für Datenbank und Datei.
    """
    if not felder:
        return
    with library_lock():
        _modify([], f"id:{track_id}", felder, timeout=60)
