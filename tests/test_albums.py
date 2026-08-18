"""Bereits importierte Alben lesen und ihr Cover ändern.

Wie in ``test_importer.py``: die Subprozess-Aufrufe selbst werden gemockt,
nicht gegen eine echte beets-Library getestet -- das eigentliche Verhalten von
``beet list`` und ``beet embedart`` ist beets' Verantwortung, hier zählt nur,
dass mimport die richtigen Kommandos baut und ihre Fehler sauber übersetzt.
"""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import pytest

from backend import albums

T = albums._TRENNER


def _zeile(id_, artist, album, year, pfad) -> str:
    return T.join((str(id_), artist, album, str(year), str(pfad)))


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["beet"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestAusZeile:
    def test_gueltige_zeile(self):
        zeile = _zeile(3, "The Beatles", "Abbey Road", 1969, "/music/Abbey Road")
        album = albums._aus_zeile(zeile)
        assert album == albums.Album(
            id=3,
            albumartist="The Beatles",
            album="Abbey Road",
            year="1969",
            path=Path("/music/Abbey Road"),
        )

    def test_falsche_feldanzahl_wird_ignoriert(self):
        assert albums._aus_zeile("zu\x1fwenig") is None

    def test_ungueltige_id_wird_ignoriert(self):
        zeile = _zeile("keine-zahl", "X", "Y", 2000, "/music/Y")
        assert albums._aus_zeile(zeile) is None


class TestListAlbums:
    def test_baut_das_richtige_kommando(self, monkeypatch):
        gesehen = {}

        def fake_run(command, **kwargs):
            gesehen["command"] = command
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.list_albums()
        assert gesehen["command"][:4] == [albums.settings.beet_bin, "list", "-a", "-f"]
        assert gesehen["command"][4] == albums._FORMAT

    def test_suchbegriff_wird_angehaengt(self, monkeypatch):
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.list_albums("Beatles")
        assert gesehen["cmd"][-1] == "Beatles"

    def test_kein_suchbegriff_haengt_nichts_an(self, monkeypatch):
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.list_albums()
        assert gesehen["cmd"][-1] == albums._FORMAT

    def test_parst_und_sortiert(self, monkeypatch):
        stdout = "\n".join(
            [
                _zeile(2, "Radiohead", "Kid A", 2000, "/music/Kid A"),
                _zeile(1, "ABBA", "Waterloo", 1974, "/music/Waterloo"),
                "",  # leere Zeile am Ende, wie bei echter beet-Ausgabe
            ]
        )
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=stdout))
        ergebnis = albums.list_albums()
        assert [a.albumartist for a in ergebnis] == ["ABBA", "Radiohead"]

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="kaputt")
        )
        with pytest.raises(albums.AlbumError, match="kaputt"):
            albums.list_albums()

    def test_fehlendes_binary_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(albums.settings, "beet_bin", "/gibt/es/nicht/beet")
        with pytest.raises(albums.AlbumError, match="nicht gefunden"):
            albums.list_albums()

    def test_zeitueberschreitung_wird_gemeldet(self, monkeypatch):
        def fake_run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        with pytest.raises(albums.AlbumError):
            albums.list_albums()


class TestGetAlbum:
    def test_fragt_ueber_id_nicht_album_id(self, monkeypatch):
        """'-a' kennt nur das Feld 'id', 'album_id' gehört den Items."""
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.get_album(42)
        assert gesehen["cmd"][-1] == "id:42"

    def test_kein_treffer_gibt_none(self, monkeypatch):
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=""))
        assert albums.get_album(1) is None

    def test_treffer_wird_geliefert(self, monkeypatch):
        zeile = _zeile(1, "X", "Y", 2000, "/music/Y")
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=zeile))
        album = albums.get_album(1)
        assert album is not None
        assert album.id == 1


class TestUpdateCover:
    def _album(self, tmp_path: Path) -> albums.Album:
        return albums.Album(
            id=7, albumartist="X", album="Y", year="2020", path=tmp_path
        )

    def test_baut_das_richtige_kommando(self, tmp_path, monkeypatch):
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        bild = tmp_path / "cover.jpg"
        bild.write_bytes(b"x")
        albums.update_cover(self._album(tmp_path), bild)
        cmd = gesehen["cmd"]
        assert cmd[1] == "embedart"
        assert "-y" in cmd
        assert "-f" in cmd
        assert str(bild) in cmd
        assert cmd[-1] == "album_id:7"

    def test_haelt_den_library_lock(self, tmp_path, monkeypatch):
        verlauf = []

        @contextlib.contextmanager
        def fake_lock():
            verlauf.append("enter")
            yield
            verlauf.append("exit")

        monkeypatch.setattr(albums, "library_lock", fake_lock)
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: (verlauf.append("beet"), _proc())[1])

        albums.update_cover(self._album(tmp_path), tmp_path / "cover.jpg")
        assert verlauf == ["enter", "beet", "exit"]

    def test_fehlschlag_wird_gemeldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.update_cover(self._album(tmp_path), tmp_path / "cover.jpg")


class TestAlbumProperties:
    def test_cover_path_ist_cover_jpg_im_ordner(self, tmp_path):
        album = albums.Album(id=1, albumartist="X", album="Y", year="2020", path=tmp_path)
        assert album.cover_path == tmp_path / "cover.jpg"

    def test_has_cover_ohne_datei(self, tmp_path):
        album = albums.Album(id=1, albumartist="X", album="Y", year="2020", path=tmp_path)
        assert not album.has_cover
        assert album.cover_version == ""

    def test_has_cover_mit_datei(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(b"x")
        album = albums.Album(id=1, albumartist="X", album="Y", year="2020", path=tmp_path)
        assert album.has_cover
        assert album.cover_version != ""
