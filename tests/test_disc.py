"""Import von einer eingelegten Daten-CD.

Der Inhalt einer fremden CD ist nicht vertrauenswürdiger als ein Upload:
Ordnernamen kommen aus deren Dateisystem, und über Rock Ridge kann sie
Symlinks tragen, die aus dem Mount herauszeigen.
"""

from __future__ import annotations

import pytest

from backend import disc

FLAC = b"fLaC\x00\x00\x00\x22"


@pytest.fixture
def cd(tmp_path, monkeypatch):
    """Eine eingelegte CD mit zwei Alben in Unterordnern."""
    wurzel = tmp_path / "disc"
    (wurzel / "Abbey Road").mkdir(parents=True)
    (wurzel / "Abbey Road" / "01 Come Together.flac").write_bytes(FLAC)
    (wurzel / "Abbey Road" / "02 Something.flac").write_bytes(FLAC)
    (wurzel / "Revolver").mkdir()
    (wurzel / "Revolver" / "01 Taxman.mp3").write_bytes(b"\xff\xfb\x00\x00")
    # Beiwerk, das kein Album ist.
    (wurzel / "Scans").mkdir()
    (wurzel / "Scans" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
    monkeypatch.setattr(disc.settings, "disc_root", wurzel)
    return wurzel


class TestIsAvailable:
    def test_ohne_mount_keine_cd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disc.settings, "disc_root", tmp_path / "gibtsnicht")
        assert disc.is_available() is False

    def test_leeres_verzeichnis_gilt_als_keine_cd(self, tmp_path, monkeypatch):
        leer = tmp_path / "leer"
        leer.mkdir()
        monkeypatch.setattr(disc.settings, "disc_root", leer)
        assert disc.is_available() is False

    def test_mit_inhalt(self, cd):
        assert disc.is_available() is True


class TestListAlbums:
    def test_ein_eintrag_je_ordner_mit_musik(self, cd):
        alben = {a.relative: a for a in disc.list_albums()}
        # Scans enthält keine Audiodateien und taucht nicht auf.
        assert set(alben) == {"Abbey Road", "Revolver"}
        assert alben["Abbey Road"].track_count == 2
        assert alben["Revolver"].track_count == 1

    def test_musik_im_hauptverzeichnis(self, cd):
        (cd / "lose.flac").write_bytes(FLAC)
        alben = {a.relative: a for a in disc.list_albums()}
        assert "" in alben
        assert alben[""].display == "(Hauptverzeichnis der CD)"

    def test_ohne_cd_leere_liste(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disc.settings, "disc_root", tmp_path / "weg")
        assert disc.list_albums() == []

    def test_groesse_wird_gemeldet(self, cd):
        alben = {a.relative: a for a in disc.list_albums()}
        assert alben["Abbey Road"].total_bytes == 2 * len(FLAC)


class TestResolveFolder:
    def test_gueltiger_ordner(self, cd):
        assert disc.resolve_folder("Abbey Road") == (cd / "Abbey Road").resolve()

    def test_leerer_pfad_ist_die_wurzel(self, cd):
        assert disc.resolve_folder("") == cd.resolve()

    def test_aufstieg_wird_abgewiesen(self, cd):
        with pytest.raises(disc.DiscError):
            disc.resolve_folder("../../etc")

    def test_absoluter_pfad_wird_abgewiesen(self, cd):
        with pytest.raises(disc.DiscError):
            disc.resolve_folder("/etc")

    def test_symlink_aus_der_cd_heraus(self, cd, tmp_path):
        """Rock Ridge erlaubt Symlinks -- die dürfen nicht hinausführen."""
        geheim = tmp_path / "geheim"
        geheim.mkdir()
        (geheim / "passwort.flac").write_bytes(FLAC)
        (cd / "raus").symlink_to(geheim)

        with pytest.raises(disc.DiscError):
            disc.resolve_folder("raus")

    def test_unbekannter_ordner(self, cd):
        with pytest.raises(disc.DiscError):
            disc.resolve_folder("Gibts Nicht")

    def test_ohne_cd(self, tmp_path, monkeypatch):
        monkeypatch.setattr(disc.settings, "disc_root", tmp_path / "weg")
        with pytest.raises(disc.DiscError):
            disc.resolve_folder("egal")


class TestCopyToSession:
    def test_dateien_landen_im_staging(self, cd):
        session = disc.copy_to_session(cd / "Abbey Road")
        namen = {p.name for p in session.audio_paths}
        assert namen == {"01 Come Together.flac", "02 Something.flac"}

    def test_nur_dieser_ordner_keine_unterordner(self, cd):
        """Eine Auswahl ist ein Album -- Unterordner gehören nicht dazu."""
        (cd / "Abbey Road" / "Bonus").mkdir()
        (cd / "Abbey Road" / "Bonus" / "extra.flac").write_bytes(FLAC)

        session = disc.copy_to_session(cd / "Abbey Road")
        assert {p.name for p in session.audio_paths} == {
            "01 Come Together.flac",
            "02 Something.flac",
        }

    def test_symlinks_werden_uebergangen(self, cd, tmp_path):
        ziel = tmp_path / "fremd.flac"
        ziel.write_bytes(FLAC)
        (cd / "Abbey Road" / "eingeschleust.flac").symlink_to(ziel)

        session = disc.copy_to_session(cd / "Abbey Road")
        assert "eingeschleust.flac" not in {p.name for p in session.audio_paths}

    def test_ordner_ohne_musik(self, cd):
        with pytest.raises(disc.DiscError):
            disc.copy_to_session(cd / "Scans")

    def test_lesefehler_laesst_nichts_liegen(self, cd, monkeypatch, isoliertes_staging):
        """Eine zerkratzte CD ist der Normalfall, kein Sonderfall."""
        import shutil

        def kaputt(quelle, ziel, *args, **kwargs):
            raise OSError(5, "Input/output error", str(quelle))

        monkeypatch.setattr(shutil, "copy2", kaputt)

        with pytest.raises(disc.DiscError, match="nicht vollständig lesen"):
            disc.copy_to_session(cd / "Abbey Road")

        # Kein halbes Album im Staging -- das gegen MusicBrainz zu matchen
        # wäre schlimmer als ein klarer Fehler.
        assert list(isoliertes_staging.iterdir()) == []

    def test_zaehlung_vor_dem_kopieren(self, cd):
        anzahl, groesse = disc.folder_size(cd / "Abbey Road")
        assert anzahl == 2
        assert groesse == 2 * len(FLAC)


class TestSizeLabel:
    """Auch ein kleiner Ordner soll eine sinnvolle Größe zeigen, nicht '0 MB'."""

    @pytest.mark.parametrize(
        "bytes_, erwartet",
        [
            (500, "1 KB"),
            (50 * 1024, "50 KB"),
            (5 * 1024**2, "5 MB"),
            (3 * 1024**3, "3.0 GB"),
        ],
    )
    def test_einheit_passt_zur_groesse(self, bytes_, erwartet):
        ordner = disc.AlbumFolder(
            relative="x", display="x", track_count=1, total_bytes=bytes_
        )
        assert ordner.size_label == erwartet


class TestCopyIntoRekursiv:
    """Hörbuch-CDs legen ihre Kapitel oft in einen Unterordner."""

    def test_unterordner_werden_gefunden(self, cd, tmp_path):
        (cd / "Disc 1").mkdir()
        (cd / "Disc 1" / "01 Kapitel.mp3").write_bytes(b"\xff\xfb\x00\x00")
        (cd / "Disc 1" / "02 Kapitel.mp3").write_bytes(b"\xff\xfb\x00\x00")

        ziel = tmp_path / "buch"
        anzahl = disc.copy_into(cd, ziel, rekursiv=True)

        assert anzahl >= 2
        assert (ziel / "Disc 1" / "01 Kapitel.mp3").is_file()

    def test_struktur_bleibt_erhalten(self, tmp_path, monkeypatch):
        """Sonst überschreiben sich gleichnamige Dateien aus mehreren Discs."""
        wurzel = tmp_path / "disc"
        for ordner in ("Disc 1", "Disc 2"):
            (wurzel / ordner).mkdir(parents=True)
            (wurzel / ordner / "01 Kapitel.mp3").write_bytes(b"\xff\xfb\x00\x00")
        monkeypatch.setattr(disc.settings, "disc_root", wurzel)

        ziel = tmp_path / "buch"
        anzahl = disc.copy_into(wurzel, ziel, rekursiv=True)

        assert anzahl == 2
        assert (ziel / "Disc 1" / "01 Kapitel.mp3").is_file()
        assert (ziel / "Disc 2" / "01 Kapitel.mp3").is_file()

    def test_ohne_rekursiv_bleibt_es_flach(self, cd, tmp_path):
        ziel = tmp_path / "flach"
        disc.copy_into(cd / "Abbey Road", ziel)
        assert (ziel / "01 Come Together.flac").is_file()
