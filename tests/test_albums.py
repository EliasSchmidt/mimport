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

from backend import albums, tag_catalog

T = albums._TRENNER
E = albums._SATZENDE


def _zeile(
    id_, artist, album, year, pfad, mb_albumartistid="", genres="", label="",
    mb_albumartistids="", **erweitert,
) -> str:
    """Baut *einen* ``beet list``-Datensatz -- dynamisch aus den aktuellen
    Kern-/Katalogfeldern, damit ein neues Feld im Katalog nicht jeden Test
    hier zerreißt. Unbekannte ``erweitert``-Schlüssel sind ein Testfehler,
    kein leiser Fallback. Ohne ``_SATZENDE`` -- das steuert ``_aus_zeile``
    direkt an, so wie ``_saetze`` es ihm nach dem Trennen auch liefert. Für
    mehrere Datensätze als Subprozess-Stdout siehe ``_stdout``."""
    kern_werte = {
        "id": str(id_), "albumartist": artist, "album": album, "year": str(year),
        "path": str(pfad), "mb_albumartistid": mb_albumartistid, "genres": genres,
        "label": label, "mb_albumartistids": mb_albumartistids,
    }
    kern = [kern_werte[f.lstrip("$")] for f in albums._ALBUM_KERN_FELDER]
    erw_keys = {f.key for f in albums._ALBUM_ERWEITERT_FELDER}
    assert set(erweitert) <= erw_keys, f"unbekannte Testfelder: {set(erweitert) - erw_keys}"
    erw = [erweitert.get(f.key, "") for f in albums._ALBUM_ERWEITERT_FELDER]
    return T.join(kern + erw)


def _stdout(*zeilen: str) -> str:
    """Reiht Datensätze so aneinander, wie ``beet list -f`` es mit dem
    ``_SATZENDE``-Terminator tatsächlich tut (siehe ``_saetze``-Test)."""
    return "".join(f"{z}{E}\n" for z in zeilen)


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
            mb_albumartistids="",
            erweitert={f.key: "" for f in albums._ALBUM_ERWEITERT_FELDER},
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
        stdout = _stdout(
            _zeile(2, "Radiohead", "Kid A", 2000, "/music/Kid A"),
            _zeile(1, "ABBA", "Waterloo", 1974, "/music/Waterloo"),
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
        stdout = _stdout(_zeile(1, "X", "Y", 2000, "/music/Y"))
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=stdout))
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


class TestRetryMissingCover:
    """Cover Art Archive antwortet gelegentlich mit einem transienten 5xx,
    fetchart wiederholt diese eine Anfrage nicht selbst -- siehe das
    Docstring von ``retry_missing_cover``."""

    def _stdout_fuer(self, album_id: int, pfad: Path) -> str:
        return _stdout(_zeile(album_id, "X", "Y", 2020, str(pfad)))

    def test_vorhandenes_cover_ruft_fetchart_gar_nicht_erst(self, tmp_path, monkeypatch):
        (tmp_path / "cover.jpg").write_bytes(b"x")
        stdout = self._stdout_fuer(7, tmp_path)

        def fake_run(cmd, **kw):
            assert cmd[1] == "list", "fetchart darf bei vorhandenem Cover nicht laufen"
            return _proc(stdout=stdout)

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        assert albums.retry_missing_cover("mbid-123") is True

    def test_fetchart_erfolgreich_im_ersten_versuch(self, tmp_path, monkeypatch):
        stdout = self._stdout_fuer(7, tmp_path)
        aufrufe = []

        def fake_run(cmd, **kw):
            if cmd[1] == "list":
                return _proc(stdout=stdout)
            aufrufe.append(cmd)
            # Simuliert einen erfolgreichen fetchart-Lauf: das Cover landet
            # im Album-Ordner, genau wie es beets selbst täte.
            (tmp_path / "cover.jpg").write_bytes(b"geladen")
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        monkeypatch.setattr(albums.time, "sleep", lambda *_: None)

        assert albums.retry_missing_cover("mbid-123") is True
        assert len(aufrufe) == 1
        assert aufrufe[0][1] == "fetchart"
        # 'id:', nicht 'album_id:' -- Letzteres wäre bei mehreren Alben eine
        # Substring-Suche und träfe auch fremde Alben (z. B. 17, 27, ...).
        assert aufrufe[0][-1] == "id:7"

    def test_alle_versuche_scheitern(self, tmp_path, monkeypatch):
        stdout = self._stdout_fuer(7, tmp_path)
        aufrufe = []
        geschlafen = []

        def fake_run(cmd, **kw):
            if cmd[1] == "list":
                return _proc(stdout=stdout)
            aufrufe.append(cmd)
            return _proc()  # kein Cover landet im Ordner -- weiterhin leer

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        monkeypatch.setattr(albums.time, "sleep", lambda s: geschlafen.append(s))

        assert albums.retry_missing_cover("mbid-123", attempts=3, pause=1.5) is False
        assert len(aufrufe) == 3
        # Vor dem ersten Versuch wird nicht gewartet, nur zwischen den weiteren.
        assert geschlafen == [1.5, 1.5]

    def test_haengender_fetchart_bricht_den_import_nicht_ab(self, tmp_path, monkeypatch):
        """Ein einzelner Versuch, der ins 60s-Timeout von _lauf() läuft, wirft
        AlbumError -- das darf nicht aus retry_missing_cover() rausfallen,
        sonst würde ein sonst erfolgreicher Import als fehlgeschlagen
        erscheinen (siehe Docstring). Auf den zweiten, erfolgreichen Versuch
        geht es stattdessen ganz normal weiter."""
        stdout = self._stdout_fuer(7, tmp_path)
        aufrufe = []

        def fake_run(cmd, **kw):
            if cmd[1] == "list":
                return _proc(stdout=stdout)
            aufrufe.append(cmd)
            if len(aufrufe) == 1:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)
            (tmp_path / "cover.jpg").write_bytes(b"geladen")
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        monkeypatch.setattr(albums.time, "sleep", lambda *_: None)

        assert albums.retry_missing_cover("mbid-123") is True
        assert len(aufrufe) == 2

    def test_haelt_den_library_lock_je_versuch(self, tmp_path, monkeypatch):
        stdout = self._stdout_fuer(7, tmp_path)
        verlauf = []

        @contextlib.contextmanager
        def fake_lock():
            verlauf.append("enter")
            yield
            verlauf.append("exit")

        def fake_run(cmd, **kw):
            if cmd[1] == "list":
                return _proc(stdout=stdout)
            verlauf.append("beet")
            return _proc()

        monkeypatch.setattr(albums, "library_lock", fake_lock)
        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        monkeypatch.setattr(albums.time, "sleep", lambda *_: None)

        albums.retry_missing_cover("mbid-123", attempts=2)
        assert verlauf == ["enter", "beet", "exit", "enter", "beet", "exit"]

    def test_kein_album_gefunden(self, tmp_path, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc(stdout="")  # 'beet list' fand nichts

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        assert albums.retry_missing_cover("mbid-unbekannt") is False
        assert len(aufrufe) == 1, "ohne Album darf fetchart gar nicht erst laufen"

    def test_beet_list_fehlschlag_wird_abgefangen(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess,
            "run",
            lambda cmd, **kw: _proc(returncode=1, stderr="autsch"),
        )
        # Kein AlbumError nach außen -- ein Bestcase-Versuch soll den Import
        # nicht nachträglich als fehlgeschlagen erscheinen lassen.
        assert albums.retry_missing_cover("mbid-123") is False


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


class TestAlbumKuenstlerLinks:
    MBID = "83d91898-7763-47d7-b03b-b92132375c47"
    MBID2 = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"

    def _album(self, tmp_path: Path, **kw) -> albums.Album:
        kw.setdefault("albumartist", "X")
        return albums.Album(id=1, album="Y", year="2020", path=tmp_path, **kw)

    def test_ein_interpret_unverbunden(self, tmp_path):
        album = self._album(tmp_path)
        assert album.kuenstler_links == [("X", "")]

    def test_ein_interpret_verbunden(self, tmp_path):
        album = self._album(tmp_path, mb_albumartistid=self.MBID, mb_albumartistids=self.MBID)
        assert album.kuenstler_links == [("X", self.MBID)]

    def test_mehrere_interpreten_teilweise_verbunden(self, tmp_path):
        """Genau der Fall, der bisher hinter der Alles-oder-nichts-MBID
        verschwand: "A" noch offen, "B" schon verlinkt."""
        album = self._album(
            tmp_path, albumartist="A feat. B", mb_albumartistids=f"; {self.MBID2}",
        )
        assert album.kuenstler_links == [("A", ""), ("B", self.MBID2)]

    def test_altes_album_nur_mit_einzelfeld_faellt_darauf_zurueck(self, tmp_path):
        """Ein Album, das schon vor dieser Funktion einmal verbunden wurde,
        kennt nur ``mb_albumartistid`` -- die Mehrfachform fehlt."""
        album = self._album(tmp_path, mb_albumartistid=self.MBID)
        assert album.kuenstler_links == [("X", self.MBID)]

    def test_laengen_mismatch_gilt_als_unverbunden(self, tmp_path):
        """Wurde der Name nachträglich geändert, ohne die MBIDs anzufassen,
        darf die falsche Position nicht als verbunden erscheinen."""
        album = self._album(
            tmp_path, albumartist="A feat. B feat. C",
            mb_albumartistids=f"{self.MBID}; {self.MBID2}",
        )
        assert album.kuenstler_links == [("A", ""), ("B", ""), ("C", "")]


def _track_zeile(
    id_, track, title, artist, mb_artistid="", mb_artistids="", **erweitert,
) -> str:
    """Wie ``_zeile``, für Titel."""
    kern_werte = {
        "id": str(id_), "track": track, "title": title, "artist": artist,
        "mb_artistid": mb_artistid, "mb_artistids": mb_artistids,
    }
    kern = [kern_werte[f.lstrip("$")] for f in albums._TRACK_KERN_FELDER]
    erw_keys = {f.key for f in albums._TRACK_ERWEITERT_FELDER}
    assert set(erweitert) <= erw_keys, f"unbekannte Testfelder: {set(erweitert) - erw_keys}"
    erw = [erweitert.get(f.key, "") for f in albums._TRACK_ERWEITERT_FELDER]
    return T.join(kern + erw)


class TestTrackAusZeile:
    def test_gueltige_zeile(self):
        zeile = _track_zeile(5, "01", "Come Together", "The Beatles")
        assert albums._track_aus_zeile(zeile) == albums.Track(
            id=5, track="01", title="Come Together", artist="The Beatles",
            mb_artistid="", mb_artistids="",
            erweitert={f.key: "" for f in albums._TRACK_ERWEITERT_FELDER},
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


class TestTrackKuenstlerLinks:
    MBID = "83d91898-7763-47d7-b03b-b92132375c47"

    def test_ein_interpret_unverbunden(self):
        track = albums.Track(id=1, track="01", title="T", artist="X")
        assert track.kuenstler_links == [("X", "")]

    def test_mehrere_interpreten_teilweise_verbunden(self):
        track = albums.Track(
            id=1, track="01", title="T", artist="A feat. B", mb_artistids=f"{self.MBID}; ",
        )
        assert track.kuenstler_links == [("A", self.MBID), ("B", "")]


class TestGetTrack:
    def test_treffer_wird_geliefert(self, monkeypatch):
        stdout = _stdout(_track_zeile(5, "01", "Come Together", "The Beatles"))
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=stdout))
        track = albums.get_track(5)
        assert track is not None
        assert track.id == 5

    def test_kein_treffer_gibt_none(self, monkeypatch):
        monkeypatch.setattr(albums.subprocess, "run", lambda cmd, **kw: _proc(stdout=""))
        assert albums.get_track(1) is None

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="kaputt")
        )
        with pytest.raises(albums.AlbumError, match="kaputt"):
            albums.get_track(1)


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
        stdout = _stdout(
            _track_zeile(2, "10", "Zehn", "X"),
            _track_zeile(1, "2", "Zwei", "X"),
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
    MBID2 = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"

    def _album(self, tmp_path: Path, **kw) -> albums.Album:
        kw.setdefault("albumartist", "X")
        return albums.Album(id=9, album="Y", year="2020", path=tmp_path, **kw)

    def test_setzt_singular_und_plural_auf_beiden_ebenen(self, tmp_path, monkeypatch):
        """Der beim Testen gefundene Stolperstein: ohne die Pluralform bleibt
        die Datei unverändert, obwohl beets 'geändert' meldet (siehe Modul-
        doc). Beide Aufrufe müssen deshalb beide Felder tragen."""
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.set_album_artist_mbid(self._album(tmp_path), 0, self.MBID)

        assert len(aufrufe) == 3
        titel_aufruf, write_aufruf, album_aufruf = aufrufe

        assert titel_aufruf[1:4] == ["modify", "-y", "album_id:9"]
        assert titel_aufruf[4] == f"mb_albumartistid={self.MBID}"
        assert f"mb_albumartistids={self.MBID}" in titel_aufruf
        assert "-a" not in titel_aufruf

        # Nachgleich: die Titel-Zeile schreibt tatsächlich, ihr folgt deshalb
        # ein "beet write" (siehe Moduldoc von _modify) -- die Album-Zeile
        # unten läuft mit -W und bekommt keinen.
        assert write_aufruf[1:3] == ["write", "album_id:9"]

        assert "-a" in album_aufruf
        assert "-W" in album_aufruf
        assert "-I" in album_aufruf
        assert "id:9" in album_aufruf
        assert f"mb_albumartistid={self.MBID}" in album_aufruf
        assert f"mb_albumartistids={self.MBID}" in album_aufruf

    def test_setzt_nur_die_gewaehlte_position_bei_mehreren_interpreten(
        self, tmp_path, monkeypatch
    ):
        """"A feat. B": nur B verbinden darf A's (leere) Position nicht
        anfassen und muss beide Positionen erhalten bleiben lassen."""
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        album = self._album(
            tmp_path, albumartist="A feat. B", mb_albumartistids=f"{self.MBID}; "
        )
        albums.set_album_artist_mbid(album, 1, self.MBID2)

        titel_aufruf, _write, _album = aufrufe
        assert f"mb_albumartistids={self.MBID}; {self.MBID2}" in titel_aufruf
        # A bleibt an Position 0 -- die Kompat-ID nimmt trotzdem den ersten
        # gesetzten Wert, nicht den zuletzt geänderten.
        assert f"mb_albumartistid={self.MBID}" in titel_aufruf

    def test_ungueltige_position_wird_gemeldet(self, tmp_path, monkeypatch):
        def darf_nicht_laufen(cmd, **kw):
            pytest.fail("beet sollte bei ungültiger Position nicht aufgerufen werden")

        monkeypatch.setattr(albums.subprocess, "run", darf_nicht_laufen)
        with pytest.raises(albums.AlbumError, match="Ungültige Interpreten-Position"):
            albums.set_album_artist_mbid(self._album(tmp_path), 5, self.MBID)

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
        albums.set_album_artist_mbid(self._album(tmp_path), 0, self.MBID)
        assert verlauf == ["enter", "beet", "beet", "beet", "exit"]

    def test_fehlschlag_beim_ersten_aufruf_wird_gemeldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.set_album_artist_mbid(self._album(tmp_path), 0, self.MBID)

    def test_fehlschlag_beim_zweiten_aufruf_wird_gemeldet(self, tmp_path, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            if len(aufrufe) == 1:
                return _proc()
            return _proc(returncode=1, stderr="Album-Zeile kaputt")

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        with pytest.raises(albums.AlbumError, match="Album-Zeile kaputt"):
            albums.set_album_artist_mbid(self._album(tmp_path), 0, self.MBID)


class TestSetTrackArtistMbid:
    MBID = "83d91898-7763-47d7-b03b-b92132375c47"
    MBID2 = "0383dadf-2a4e-4d10-a46a-e9e041da8eb3"

    def _track(self, **kw) -> albums.Track:
        kw.setdefault("artist", "X")
        return albums.Track(id=42, track="01", title="T", **kw)

    def test_baut_das_richtige_kommando(self, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        albums.set_track_artist_mbid(self._track(), 0, self.MBID)
        modify_aufruf, write_aufruf = aufrufe
        assert modify_aufruf[1:4] == ["modify", "-y", "id:42"]
        assert modify_aufruf[4] == f"mb_artistid={self.MBID}"
        assert f"mb_artistids={self.MBID}" in modify_aufruf
        assert "-a" not in modify_aufruf

        assert write_aufruf[1:3] == ["write", "id:42"]

    def test_setzt_nur_die_gewaehlte_position_bei_mehreren_interpreten(self, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        track = self._track(artist="A feat. B", mb_artistids=f"{self.MBID}; ")
        albums.set_track_artist_mbid(track, 1, self.MBID2)
        assert f"mb_artistids={self.MBID}; {self.MBID2}" in aufrufe[0]
        assert f"mb_artistid={self.MBID}" in aufrufe[0]

    def test_ungueltige_position_wird_gemeldet(self, monkeypatch):
        def darf_nicht_laufen(cmd, **kw):
            pytest.fail("beet sollte bei ungültiger Position nicht aufgerufen werden")

        monkeypatch.setattr(albums.subprocess, "run", darf_nicht_laufen)
        with pytest.raises(albums.AlbumError, match="Ungültige Interpreten-Position"):
            albums.set_track_artist_mbid(self._track(), 3, self.MBID)

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
        albums.set_track_artist_mbid(self._track(), 0, self.MBID)
        assert verlauf == ["enter", "beet", "beet", "exit"]

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.set_track_artist_mbid(self._track(), 0, self.MBID)


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


class TestSetAlbumField:
    def _album(self, tmp_path: Path) -> albums.Album:
        return albums.Album(id=9, albumartist="X", album="Y", year="2020", path=tmp_path)

    def test_einwertiges_feld_auf_beiden_ebenen(self, tmp_path, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        feld = tag_catalog.ALBUM_FELDER_NACH_KEY["album"]
        albums.set_album_field(self._album(tmp_path), feld, "Neuer Titel")

        assert len(aufrufe) == 3
        titel_aufruf, write_aufruf, album_aufruf = aufrufe

        assert titel_aufruf[1:4] == ["modify", "-y", "album_id:9"]
        assert "album=Neuer Titel" in titel_aufruf
        assert "-a" not in titel_aufruf

        assert write_aufruf[1:3] == ["write", "album_id:9"]

        assert "-a" in album_aufruf
        assert "-W" in album_aufruf
        assert "-I" in album_aufruf
        assert "id:9" in album_aufruf
        assert "album=Neuer Titel" in album_aufruf

    def test_leerer_wert_loescht_das_feld(self, tmp_path, monkeypatch):
        """Anders als das alte update_album_fields: ein geleertes Eingabefeld
        muss den Tag auch wirklich leeren, nicht unangetastet lassen."""
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        feld = tag_catalog.ALBUM_FELDER_NACH_KEY["label"]
        albums.set_album_field(self._album(tmp_path), feld, "")
        assert "label=" in aufrufe[0]

    def test_mehrwertiges_feld_setzt_einzelform_mit(self, tmp_path, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        feld = tag_catalog.ALBUM_FELDER_NACH_KEY["genres"]
        albums.set_album_field(self._album(tmp_path), feld, "Rock; Pop")
        assert "genres=Rock; Pop" in aufrufe[0]
        assert "genre=Rock; Pop" in aufrufe[0]

    def test_kuenstler_feld_wird_abgelehnt(self, tmp_path):
        feld = tag_catalog.ALBUM_FELDER_NACH_KEY["albumartists"]
        with pytest.raises(albums.AlbumError, match="Künstler-Verknüpfung"):
            albums.set_album_field(self._album(tmp_path), feld, "Neu")

    def test_fehlschlag_wird_gemeldet(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        feld = tag_catalog.ALBUM_FELDER_NACH_KEY["album"]
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.set_album_field(self._album(tmp_path), feld, "Neu")


class TestSetTrackField:
    def test_baut_das_richtige_kommando(self, monkeypatch):
        aufrufe = []

        def fake_run(cmd, **kw):
            aufrufe.append(cmd)
            return _proc()

        monkeypatch.setattr(albums.subprocess, "run", fake_run)
        track = albums.Track(id=42, track="01", title="X", artist="Y")
        feld = tag_catalog.TRACK_FELDER_NACH_KEY["title"]
        albums.set_track_field(track, feld, "Neuer Titel")
        modify_aufruf, write_aufruf = aufrufe
        assert modify_aufruf[1:4] == ["modify", "-y", "id:42"]
        assert "title=Neuer Titel" in modify_aufruf
        assert write_aufruf[1:3] == ["write", "id:42"]

    def test_mehrwertiges_feld_setzt_einzelform_mit(self, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        track = albums.Track(id=42, track="01", title="X", artist="Y")
        feld = tag_catalog.TRACK_FELDER_NACH_KEY["composers"]
        albums.set_track_field(track, feld, "Bach; Mozart")
        assert "composers=Bach; Mozart" in aufrufe[0]
        assert "composer=Bach; Mozart" in aufrufe[0]

    def test_kuenstler_feld_wird_abgelehnt(self):
        track = albums.Track(id=42, track="01", title="X", artist="Y")
        feld = tag_catalog.TRACK_FELDER_NACH_KEY["artists"]
        with pytest.raises(albums.AlbumError, match="Künstler-Verknüpfung"):
            albums.set_track_field(track, feld, "Neu")

    def test_fehlschlag_wird_gemeldet(self, monkeypatch):
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: _proc(returncode=1, stderr="autsch")
        )
        track = albums.Track(id=42, track="01", title="X", artist="Y")
        feld = tag_catalog.TRACK_FELDER_NACH_KEY["title"]
        with pytest.raises(albums.AlbumError, match="autsch"):
            albums.set_track_field(track, feld, "Neu")


class TestSetAlbumInterpret:
    def _album(self, tmp_path: Path, **kw) -> albums.Album:
        kw.setdefault("albumartist", "X")
        return albums.Album(id=9, album="Y", year="2020", path=tmp_path, **kw)

    def test_setzt_namen_und_liste(self, tmp_path, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        albums.set_album_interpret(self._album(tmp_path), "A feat. B")
        assert "albumartist=A feat. B" in aufrufe[0]
        assert "albumartists=A; B" in aufrufe[0]

    def test_verwirft_bestehende_mbids(self, tmp_path, monkeypatch):
        """Eine geänderte Schreibweise kann nicht mehr sicher zu den alten
        Positionen gehören -- wie beim manuellen Taggen vor dem Import."""
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        album = self._album(
            tmp_path, mb_albumartistid="83d91898-7763-47d7-b03b-b92132375c47",
            mb_albumartistids="83d91898-7763-47d7-b03b-b92132375c47",
        )
        albums.set_album_interpret(album, "Anderer Name")
        assert "mb_albumartistid=" in aufrufe[0]
        assert "mb_albumartistids=" in aufrufe[0]
        # Nicht bloß irgendein "=", sondern wirklich geleert.
        felder = dict(teil.split("=", 1) for teil in aufrufe[0] if "=" in teil)
        assert felder["mb_albumartistid"] == ""
        assert felder["mb_albumartistids"] == ""


class TestSetTrackInterpret:
    def test_setzt_namen_und_liste_und_verwirft_mbids(self, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            albums.subprocess, "run", lambda cmd, **kw: (aufrufe.append(cmd), _proc())[1]
        )
        track = albums.Track(
            id=42, track="01", title="X", artist="Y",
            mb_artistid="83d91898-7763-47d7-b03b-b92132375c47",
            mb_artistids="83d91898-7763-47d7-b03b-b92132375c47",
        )
        albums.set_track_interpret(track, "A / B")
        cmd = aufrufe[0]
        assert "artist=A / B" in cmd
        assert "artists=A; B" in cmd
        felder = dict(teil.split("=", 1) for teil in cmd if "=" in teil)
        assert felder["mb_artistid"] == ""
        assert felder["mb_artistids"] == ""
