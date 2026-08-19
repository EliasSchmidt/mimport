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


def _zeile(id_, artist, album, year, pfad, mb_albumartistid="", genres="", label="") -> str:
    return T.join(
        (str(id_), artist, album, str(year), str(pfad), mb_albumartistid, genres, label)
    )


def _proc(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["beet"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestAusZeile:
    def test_gueltige_zeile(self):
        zeile = _zeile(
            3, "The Beatles", "Abbey Road", 1969, "/music/Abbey Road",
            genres="Rock", label="Apple Records",
        )
        album = albums._aus_zeile(zeile)
        assert album == albums.Album(
            id=3,
            albumartist="The Beatles",
            album="Abbey Road",
            year="1969",
            path=Path("/music/Abbey Road"),
            mb_albumartistid="",
            genres="Rock",
            label="Apple Records",
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

    def test_has_albumartist_mbid(self, tmp_path):
        ohne = albums.Album(id=1, albumartist="X", album="Y", year="2020", path=tmp_path)
        mit = albums.Album(
            id=1, albumartist="X", album="Y", year="2020", path=tmp_path,
            mb_albumartistid="83d91898-7763-47d7-b03b-b92132375c47",
        )
        assert not ohne.has_albumartist_mbid
        assert mit.has_albumartist_mbid


def _track_zeile(id_, track, title, artist, mb_artistid="") -> str:
    return T.join((str(id_), track, title, artist, mb_artistid))


class TestTrackAusZeile:
    def test_gueltige_zeile(self):
        zeile = _track_zeile(5, "01", "Come Together", "The Beatles")
        assert albums._track_aus_zeile(zeile) == albums.Track(
            id=5, track="01", title="Come Together", artist="The Beatles", mb_artistid=""
        )

    def test_falsche_feldanzahl_wird_ignoriert(self):
        assert albums._track_aus_zeile("zu\x1fwenig") is None

    def test_ungueltige_id_wird_ignoriert(self):
        zeile = _track_zeile("keine-zahl", "01", "X", "Y")
        assert albums._track_aus_zeile(zeile) is None


class TestTrackProperties:
    def test_has_artist_mbid(self):
        ohne = albums.Track(id=1, track="01", title="X", artist="Y")
        mit = albums.Track(
            id=1, track="01", title="X", artist="Y",
            mb_artistid="83d91898-7763-47d7-b03b-b92132375c47",
        )
        assert not ohne.has_artist_mbid
        assert mit.has_artist_mbid


class TestListTracks:
    def test_baut_das_richtige_kommando(self, monkeypatch):
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.list_tracks(7)
        assert gesehen["cmd"][:4] == [albums.settings.beet_bin, "list", "-f", albums._TRACK_FORMAT]
        assert gesehen["cmd"][-1] == "album_id:7"

    def test_sortiert_numerisch_nach_tracknummer(self, monkeypatch):
        stdout = "\n".join(
            [
                _track_zeile(2, "10", "Zehn", "X"),
                _track_zeile(1, "2", "Zwei", "X"),
            ]
        )
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=stdout))
        ergebnis = albums.list_tracks(1)
        assert [t.title for t in ergebnis] == ["Zwei", "Zehn"]

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="kaputt")
        )
        with pytest.raises(albums.AlbumError, match="kaputt"):
            albums.list_tracks(1)


class TestSetAlbumArtistMbid:
    MBID = "83d91898-7763-47d7-b03b-b92132375c47"

    def _album(self, tmp_path: Path) -> albums.Album:
        return albums.Album(id=9, albumartist="X", album="Y", year="2020", path=tmp_path)

    def test_setzt_singular_und_plural_auf_beiden_ebenen(self, tmp_path, monkeypatch):
        """Der beim Testen gefundene Stolperstein: ohne die Pluralform bleibt
        die Datei unverändert, obwohl beets 'geändert' meldet (siehe Modul-
        doc). Beide Aufrufe müssen deshalb beide Felder tragen."""
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.set_album_artist_mbid(self._album(tmp_path), self.MBID)

        assert len(aufrufe) == 2
        titel_aufruf, album_aufruf = aufrufe

        assert titel_aufruf[1:4] == ["modify", "-y", "album_id:9"]
        assert titel_aufruf[4] == f"mb_albumartistid={self.MBID}"
        assert f"mb_albumartistids={self.MBID}" in titel_aufruf
        assert "-a" not in titel_aufruf

        assert "-a" in album_aufruf
        assert "-W" in album_aufruf
        assert "-I" in album_aufruf
        assert "id:9" in album_aufruf
        assert f"mb_albumartistid={self.MBID}" in album_aufruf
        assert f"mb_albumartistids={self.MBID}" in album_aufruf

    def test_haelt_den_library_lock(self, tmp_path, monkeypatch):
        verlauf = []

        @contextlib.contextmanager
        def fake_lock():
            verlauf.append("enter")
            yield
            verlauf.append("exit")

        monkeypatch.setattr(albums, "library_lock", fake_lock)
        monkeypatch.setattr(
            albums.subprocess,
            "run",
            lambda cmd, **kw: (verlauf.append("beet"), _proc())[1],
        )
        albums.set_album_artist_mbid(self._album(tmp_path), self.MBID)
        assert verlauf == ["enter", "beet", "beet", "exit"]

    def test_fehlschlag_beim_ersten_aufruf_wird_gemeldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.set_album_artist_mbid(self._album(tmp_path), self.MBID)

    def test_fehlschlag_beim_zweiten_aufruf_wird_gemeldet(self, tmp_path, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            if len(aufrufe) == 1:
                return _proc()
            return _proc(returncode=1, stderr="Album-Zeile kaputt")

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        with pytest.raises(albums.AlbumError, match="Album-Zeile kaputt"):
            albums.set_album_artist_mbid(self._album(tmp_path), self.MBID)


class TestSetTrackArtistMbid:
    MBID = "83d91898-7763-47d7-b03b-b92132375c47"

    def test_baut_das_richtige_kommando(self, monkeypatch):
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.set_track_artist_mbid(42, self.MBID)
        cmd = gesehen["cmd"]
        assert cmd[1:4] == ["modify", "-y", "id:42"]
        assert cmd[4] == f"mb_artistid={self.MBID}"
        assert f"mb_artistids={self.MBID}" in cmd
        assert "-a" not in cmd

    def test_haelt_den_library_lock(self, monkeypatch):
        verlauf = []

        @contextlib.contextmanager
        def fake_lock():
            verlauf.append("enter")
            yield
            verlauf.append("exit")

        monkeypatch.setattr(albums, "library_lock", fake_lock)
        monkeypatch.setattr(
            albums.subprocess,
            "run",
            lambda cmd, **kw: (verlauf.append("beet"), _proc())[1],
        )
        albums.set_track_artist_mbid(42, self.MBID)
        assert verlauf == ["enter", "beet", "exit"]

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.set_track_artist_mbid(42, self.MBID)


class TestYearEditierbar:
    def test_echtes_jahr_bleibt(self, tmp_path):
        album = albums.Album(id=1, albumartist="X", album="Y", year="1969", path=tmp_path)
        assert album.year_editierbar == "1969"

    def test_sentinel_wird_leer(self, tmp_path):
        """'0000' ist beets' Wert für "kein Jahr bekannt" -- vorausgefüllt
        stünde sonst eine falsche Zahl im Formular, die man versehentlich
        wörtlich zurückspeichern könnte."""
        album = albums.Album(id=1, albumartist="X", album="Y", year="0000", path=tmp_path)
        assert album.year_editierbar == ""

    def test_leeres_jahr_bleibt_leer(self, tmp_path):
        album = albums.Album(id=1, albumartist="X", album="Y", year="", path=tmp_path)
        assert album.year_editierbar == ""


class TestUpdateAlbumFields:
    def _album(self, tmp_path: Path) -> albums.Album:
        return albums.Album(id=9, albumartist="X", album="Y", year="2020", path=tmp_path)

    def test_setzt_felder_auf_beiden_ebenen(self, tmp_path, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.update_album_fields(
            self._album(tmp_path), {"albumartist": "Neu", "year": "1999"}
        )

        assert len(aufrufe) == 2
        titel_aufruf, album_aufruf = aufrufe

        assert titel_aufruf[1:4] == ["modify", "-y", "album_id:9"]
        assert "albumartist=Neu" in titel_aufruf
        assert "year=1999" in titel_aufruf
        assert "-a" not in titel_aufruf

        assert "-a" in album_aufruf
        assert "-W" in album_aufruf
        assert "-I" in album_aufruf
        assert "id:9" in album_aufruf
        assert "albumartist=Neu" in album_aufruf

    def test_ohne_felder_passiert_nichts(self, tmp_path, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        albums.update_album_fields(self._album(tmp_path), {})
        assert aufrufe == []

    def test_fehlschlag_wird_gemeldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.update_album_fields(self._album(tmp_path), {"album": "Neu"})


class TestUpdateTrackFields:
    def test_baut_das_richtige_kommando(self, monkeypatch):
        gesehen = {}

        def fake_run(cmd, **kw):
            gesehen["cmd"] = cmd
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.update_track_fields(42, {"title": "Neuer Titel", "artist": "X"})
        cmd = gesehen["cmd"]
        assert cmd[1:4] == ["modify", "-y", "id:42"]
        assert "title=Neuer Titel" in cmd
        assert "artist=X" in cmd

    def test_ohne_felder_passiert_nichts(self, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        albums.update_track_fields(42, {})
        assert aufrufe == []

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.update_track_fields(42, {"title": "Neu"})
