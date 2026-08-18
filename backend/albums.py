"""Bereits importierte Alben ansehen und nachträglich ihr Cover ändern.

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
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from backend import cover
from backend.config import settings
from backend.importer import library_lock

log = logging.getLogger(__name__)

#: Trennt Felder in der ``beet list``-Ausgabe. Kein Zeichen, das in einem
#: Interpreten- oder Albumnamen vorkommen könnte -- anders als Komma, Tab oder
#: Pipe.
_TRENNER = "\x1f"

#: Reihenfolge muss zu ``_aus_zeile`` passen. ``$path`` ist bei einem Album
#: ein berechnetes Feld (der Ordner der ersten Spur), keine Datenbankspalte.
_FELDER = ("$id", "$albumartist", "$album", "$year", "$path")
_FORMAT = _TRENNER.join(_FELDER)


class AlbumError(Exception):
    """Die Library-Abfrage oder das Einbetten des Covers ist fehlgeschlagen."""


@dataclass(frozen=True)
class Album:
    id: int
    albumartist: str
    album: str
    year: str
    path: Path

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
    id_roh, albumartist, album, year, pfad = teile
    try:
        id_ = int(id_roh)
    except ValueError:
        log.warning("Album ohne gültige ID ignoriert: %r", zeile)
        return None
    return Album(
        id=id_, albumartist=albumartist, album=album, year=year, path=Path(pfad)
    )


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
