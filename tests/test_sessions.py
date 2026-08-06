"""Staging-Sessions und die Abwehr manipulierter Pfade.

Dateinamen kommen vom Browser und sind grundsätzlich als feindlich zu behandeln.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import sessions


@pytest.fixture(autouse=True)
def staging(tmp_path, monkeypatch):
    """Staging-Wurzel für jeden Test in ein eigenes Temp-Verzeichnis legen."""
    monkeypatch.setattr(sessions.settings, "staging_root", tmp_path / "staging")
    return tmp_path / "staging"


class TestSanitizeRelativePath:
    def test_ordnerstruktur_bleibt_erhalten(self):
        # Das Album-Matching braucht den Ordner als zusammenhängende Menge.
        result = sessions.sanitize_relative_path("Abbey Road/01 Come Together.flac")
        assert result == Path("Abbey Road/01 Come Together.flac")

    def test_aufstieg_wird_entfernt(self):
        result = sessions.sanitize_relative_path("../../../etc/passwd")
        assert ".." not in result.parts
        assert not result.is_absolute()

    def test_absoluter_pfad_wird_relativ(self):
        result = sessions.sanitize_relative_path("/etc/passwd")
        assert not result.is_absolute()
        assert result.parts[-1] == "passwd"

    def test_windows_pfad(self):
        result = sessions.sanitize_relative_path(r"C:\Users\x\song.mp3")
        assert not result.is_absolute()
        assert result.parts[-1] == "song.mp3"
        assert "C:" not in result.parts

    def test_nullbyte_und_steuerzeichen(self):
        result = sessions.sanitize_relative_path("bö\x00se\x1f.flac")
        assert "\x00" not in str(result)
        assert "\x1f" not in str(result)

    def test_versteckte_datei_wird_entpunktet(self):
        result = sessions.sanitize_relative_path(".ssh/authorized_keys")
        assert not result.parts[0].startswith(".")

    def test_leerer_name_bekommt_platzhalter(self):
        assert sessions.sanitize_relative_path("") == Path("unbenannt")

    def test_tiefe_wird_begrenzt(self):
        result = sessions.sanitize_relative_path("/".join(str(i) for i in range(20)))
        assert len(result.parts) <= 4

    def test_umlaute_bleiben_erhalten(self):
        result = sessions.sanitize_relative_path("Björk/Jóga.flac")
        assert result == Path("Björk/Jóga.flac")


class TestSessions:
    def test_anlegen_und_wiederfinden(self):
        created = sessions.create_session()
        assert created.directory.is_dir()

        found = sessions.get_session(created.session_id)
        assert found.directory == created.directory.resolve()

    def test_unbekannte_session(self):
        with pytest.raises(sessions.SessionError):
            sessions.get_session("a" * 20)

    @pytest.mark.parametrize(
        "bad_id",
        ["../../etc", "..", "kurz", "", "hat/schrägstrich", "hat.punkt.drin" * 9],
    )
    def test_ungueltige_ids_werden_abgewiesen(self, bad_id):
        with pytest.raises(sessions.SessionError):
            sessions.get_session(bad_id)

    def test_zielpfad_bleibt_in_der_session(self):
        session = sessions.create_session()
        target = sessions.target_path(session, Path("Album/track.flac"))
        assert target.is_relative_to(session.directory.resolve())

    def test_zielpfad_ausserhalb_wird_abgewiesen(self):
        session = sessions.create_session()
        # sanitize_relative_path würde das schon abfangen; target_path ist die
        # zweite, unabhängige Sperre.
        with pytest.raises(sessions.SessionError):
            sessions.target_path(session, Path("../../ausbruch.flac"))

    def test_audio_dateien_werden_gefunden(self):
        session = sessions.create_session()
        (session.directory / "Album").mkdir()
        (session.directory / "Album" / "a.flac").write_bytes(b"x")
        (session.directory / "Album" / "cover.jpg").write_bytes(b"x")
        (session.directory / "b.MP3").write_bytes(b"x")

        names = {p.name for p in session.audio_paths}
        # Bilder gehören nicht dazu, Großschreibung der Endung schon.
        assert names == {"a.flac", "b.MP3"}

    def test_verwerfen_loescht_alles(self):
        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"x")
        sessions.delete_session(session.session_id)
        assert not session.directory.exists()

    def test_aufraeumen_nur_wenn_leer(self):
        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"x")
        sessions.cleanup_if_empty(session)
        assert session.directory.is_dir()  # noch Dateien drin

        (session.directory / "a.flac").unlink()
        sessions.cleanup_if_empty(session)
        assert not session.directory.exists()
