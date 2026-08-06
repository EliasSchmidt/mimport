"""Endpunkte über den TestClient.

Der Schwerpunkt liegt auf dem Upload: Dateinamen kommen vom Browser und dürfen
niemals aus dem Staging-Ordner herausführen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import sessions
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


class TestIndex:
    def test_seite_wird_ausgeliefert(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert "mimport" in response.text
        # Die vier Schritte sollen in der Seite stehen.
        assert "Dateien auswählen" in response.text
        assert "Match auswählen" in response.text


class TestUpload:
    def test_ohne_audiodateien(self, client):
        response = client.post(
            "/upload", files={"files": ("liesmich.txt", b"kein audio", "text/plain")}
        )
        assert response.status_code == 200
        assert "Keine Audiodateien" in response.text

    def test_pfadausbruch_landet_im_staging(self, client, isoliertes_staging):
        """Ein Dateiname mit ../ darf nichts außerhalb anlegen."""
        response = client.post(
            "/upload",
            files={"files": ("../../../ausbruch.flac", b"fLaC\x00\x00\x00\x22", "audio/flac")},
        )
        assert response.status_code == 200

        # Nichts oberhalb der Staging-Wurzel angelegt.
        assert not (isoliertes_staging.parent / "ausbruch.flac").exists()
        entkommen = list(isoliertes_staging.parent.glob("ausbruch.flac"))
        assert not entkommen

        # Die Datei liegt innerhalb einer Session.
        gefunden = list(isoliertes_staging.rglob("ausbruch.flac"))
        assert len(gefunden) == 1
        assert gefunden[0].is_relative_to(isoliertes_staging)

    def test_ordnerstruktur_wird_uebernommen(self, client, isoliertes_staging):
        # Das Frontend schickt den relativen Pfad als Dateinamen mit.
        response = client.post(
            "/upload",
            files={
                "files": (
                    "Abbey Road/01 Come Together.flac",
                    b"fLaC\x00\x00\x00\x22",
                    "audio/flac",
                )
            },
        )
        assert response.status_code == 200
        gefunden = list(isoliertes_staging.rglob("01 Come Together.flac"))
        assert len(gefunden) == 1
        assert gefunden[0].parent.name == "Abbey Road"

    def test_zu_viele_dateien(self, client, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(settings, "max_files", 2)
        files = [
            ("files", (f"track{i}.flac", b"fLaC\x00\x00\x00\x22", "audio/flac"))
            for i in range(3)
        ]
        response = client.post("/upload", files=files)
        assert "Zu viele Dateien" in response.text

    def test_groessenlimit(self, client, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(settings, "max_upload_bytes", 10)
        response = client.post(
            "/upload",
            files={"files": ("gross.flac", b"fLaC" + b"\x00" * 500, "audio/flac")},
        )
        assert "Limit" in response.text

    def test_unlesbare_datei_wird_gemeldet_nicht_verschluckt(
        self, client, isoliertes_staging
    ):
        """Eine .flac-Datei, die kein FLAC ist, muss auffallen."""
        response = client.post(
            "/upload",
            files={"files": ("kaputt.flac", b"das ist kein flac", "audio/flac")},
        )
        assert response.status_code == 200
        assert "nicht lesbar" in response.text.lower()


class TestSessionEndpunkte:
    @pytest.mark.parametrize("pfad", ["/match/", "/choose/", "/import/"])
    def test_unbekannte_session_gibt_404(self, client, pfad):
        response = client.post(f"{pfad}{'a' * 20}", data={"album_id": "x"})
        assert response.status_code == 404

    @pytest.mark.parametrize("session_id", ["kurz", "hat.punkte.drin", "%2e%2e"])
    def test_ungueltige_session_id_gibt_404(self, client, session_id):
        # "..' als Pfadsegment normalisiert der HTTP-Client selbst weg, deshalb
        # hier Werte, die tatsächlich beim Endpunkt ankommen.
        response = client.post(f"/match/{session_id}", data={})
        assert response.status_code == 404

    def test_verwerfen_loescht_die_session(self, client):
        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"fLaC")

        response = client.delete(f"/session/{session.session_id}")
        assert response.status_code == 200
        assert not session.directory.exists()

    def test_match_ohne_dateien(self, client):
        session = sessions.create_session()
        response = client.post(f"/match/{session.session_id}", data={})
        assert "keine Dateien" in response.text

    def test_unbrauchbare_mbid_wird_zurueckgewiesen(self, client):
        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.post(
            f"/match/{session.session_id}", data={"mbid": "das ist keine id"}
        )
        # Fällt auf, bevor irgendeine Netzabfrage passiert.
        assert "MusicBrainz-ID" in response.text

    def test_manuelle_tags_ohne_eingabe(self, client):
        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.post(f"/manual/{session.session_id}", data={})
        assert "kein Feld" in response.text


class TestImportSperre:
    def test_import_gesperrt_ohne_beets_cli(self, client, monkeypatch):
        """Ohne funktionierendes beet-CLI darf nicht importiert werden."""
        from backend import beets_env

        monkeypatch.setattr(
            beets_env,
            "health",
            lambda: {
                "beets_version": "2.13.1",
                "beet_cli_version": None,
                "metadata_sources": ["musicbrainz"],
                "fingerprint": False,
                "problems": ["beet ist nicht aufrufbar"],
                "import_ready": False,
            },
        )
        session = sessions.create_session()
        response = client.post(f"/import/{session.session_id}", data={})
        assert "gesperrt" in response.text

    def test_probelauf_ist_auch_ohne_freigabe_erlaubt(self, client, monkeypatch):
        from backend import beets_env, importer

        monkeypatch.setattr(
            beets_env,
            "health",
            lambda: {
                "beets_version": "2.13.1",
                "beet_cli_version": None,
                "metadata_sources": ["musicbrainz"],
                "fingerprint": False,
                "problems": ["Versionsunterschied"],
                "import_ready": False,
            },
        )
        monkeypatch.setattr(importer.settings, "beet_bin", "true")

        session = sessions.create_session()
        response = client.post(f"/import/{session.session_id}", data={"pretend": "1"})
        assert "gesperrt" not in response.text
        assert "Probelauf" in response.text


class TestUploadGrenzen:
    """Die App darf das Dateisystem des Servers nicht vollschreiben können."""

    FLAC = ("a.flac", b"fLaC\x00\x00\x00\x22", "audio/flac")

    def test_kein_platz_auf_dem_dateisystem(self, client, monkeypatch, isoliertes_staging):
        """Der freie Platz schlägt jede konfigurierte Obergrenze."""
        from backend.config import settings

        monkeypatch.setattr(settings, "staging_free_bytes", lambda: 0)

        response = client.post("/upload", files={"files": self.FLAC})
        assert "nicht genug Speicherplatz" in response.text
        # Es darf auch keine leere Session zurückbleiben.
        assert list(isoliertes_staging.iterdir()) == []

    def test_gesamtbudget_erschoepft(self, client, monkeypatch, isoliertes_staging):
        """Viele kleine Uploads dürfen sich nicht unbegrenzt summieren."""
        from backend.config import settings

        monkeypatch.setattr(settings, "staging_free_bytes", lambda: 100 * 1024**3)
        monkeypatch.setattr(settings, "max_staging_bytes", 0)

        response = client.post("/upload", files={"files": self.FLAC})
        assert "Staging-Bereich ist ausgelastet" in response.text
        assert list(isoliertes_staging.iterdir()) == []

    def test_belegtes_staging_schmaelert_das_budget(
        self, client, monkeypatch, isoliertes_staging
    ):
        """Was schon im Staging liegt, wird angerechnet."""
        from backend import sessions
        from backend.config import settings

        belegt = sessions.create_session()
        (belegt.directory / "alt.flac").write_bytes(b"x" * 1000)

        monkeypatch.setattr(settings, "staging_free_bytes", lambda: 100 * 1024**3)
        monkeypatch.setattr(settings, "max_staging_bytes", 1000)

        response = client.post("/upload", files={"files": self.FLAC})
        assert "Staging-Bereich ist ausgelastet" in response.text

    def test_platz_reicht_gerade_so(self, client, monkeypatch, isoliertes_staging):
        """Gegenprobe: knapp über der Grenze geht der Upload durch."""
        from backend.config import settings

        monkeypatch.setattr(settings, "min_free_bytes", 1000)
        monkeypatch.setattr(settings, "staging_free_bytes", lambda: 1000 + 10_000)

        response = client.post("/upload", files={"files": self.FLAC})
        assert response.status_code == 200
        assert "nicht genug Speicherplatz" not in response.text

    def test_verwaiste_session_wird_vor_dem_upload_weggeraeumt(
        self, client, monkeypatch, isoliertes_staging
    ):
        """Der Sweep gibt Platz frei, bevor das Budget berechnet wird."""
        import os
        import time

        from backend import sessions

        alte = sessions.create_session()
        (alte.directory / "alt.flac").write_bytes(b"x" * 1000)
        alt = time.time() - 48 * 3600
        os.utime(alte.directory / "alt.flac", (alt, alt))
        os.utime(alte.directory, (alt, alt))

        response = client.post("/upload", files={"files": self.FLAC})
        assert response.status_code == 200
        assert not alte.directory.exists()


class TestVerbindungsabbruch:
    def test_abbruch_laesst_nichts_liegen(self, client, monkeypatch, isoliertes_staging):
        """Ein geschlossener Tab mitten im Upload darf kein Fragment hinterlassen.

        Ohne Aufräumen sammelt sich das an, bis das Dateisystem voll ist -- dafür
        braucht es keine Absicht.
        """
        from starlette.datastructures import UploadFile
        from starlette.requests import ClientDisconnect

        async def read_bricht_ab(self, size: int = -1) -> bytes:
            raise ClientDisconnect()

        monkeypatch.setattr(UploadFile, "read", read_bricht_ab)

        with pytest.raises(ClientDisconnect):
            client.post(
                "/upload",
                files={"files": ("a.flac", b"fLaC\x00\x00\x00\x22", "audio/flac")},
            )

        assert list(isoliertes_staging.iterdir()) == []

    def test_fehlende_staging_wurzel_wird_neu_angelegt(
        self, client, isoliertes_staging
    ):
        """Verschwindet der Staging-Ordner, darf das keinen Dauerfehler geben.

        Ohne angelegte Wurzel scheitert die Platzabfrage und mimport würde
        jeden Upload mit einer irreführenden Meldung über Speicherplatz
        abweisen, statt den Ordner einfach wieder anzulegen.
        """
        import shutil

        shutil.rmtree(isoliertes_staging, ignore_errors=True)
        assert not isoliertes_staging.exists()

        response = client.post(
            "/upload",
            files={"files": ("a.flac", b"fLaC\x00\x00\x00\x22", "audio/flac")},
        )
        assert response.status_code == 200
        assert "Speicherplatz" not in response.text
        assert isoliertes_staging.is_dir()


class TestDisc:
    """Die eingelegte Daten-CD als zweite Quelle für denselben Ablauf."""

    FLAC = b"fLaC\x00\x00\x00\x22"

    @pytest.fixture
    def cd(self, tmp_path, monkeypatch):
        from backend.config import settings

        wurzel = tmp_path / "disc"
        (wurzel / "Abbey Road").mkdir(parents=True)
        (wurzel / "Abbey Road" / "01 Come Together.flac").write_bytes(self.FLAC)
        monkeypatch.setattr(settings, "disc_root", wurzel)
        return wurzel

    def test_ohne_cd_sagt_die_seite_das(self, client):
        response = client.get("/disc")
        assert response.status_code == 200
        assert "Keine CD eingelegt" in response.text

    def test_alben_werden_gelistet(self, client, cd):
        response = client.get("/disc")
        assert "Abbey Road" in response.text
        assert "Übernehmen" in response.text

    def test_uebernehmen_fuehrt_in_den_bestehenden_ablauf(self, client, cd):
        """Nach dem Kopieren ist die Antwort dieselbe wie nach einem Upload."""
        response = client.post("/disc", data={"folder": "Abbey Road"})
        assert response.status_code == 200
        # Das Fragment der Dateiliste, mit dem Schritt 2 bis 4 weitergehen.
        assert "Come Together" in response.text
        assert "/match/" in response.text

    def test_pfadausbruch_wird_abgewiesen(self, client, cd):
        response = client.post("/disc", data={"folder": "../../etc"})
        assert "gehört nicht zur eingelegten CD" in response.text

    def test_ordner_ohne_musik(self, client, cd):
        (cd / "Scans").mkdir()
        (cd / "Scans" / "cover.jpg").write_bytes(b"\xff\xd8\xff")
        response = client.post("/disc", data={"folder": "Scans"})
        assert "keine Audiodateien" in response.text

    def test_zu_viele_dateien(self, client, cd, monkeypatch):
        from backend.config import settings

        monkeypatch.setattr(settings, "max_files", 0)
        response = client.post("/disc", data={"folder": "Abbey Road"})
        assert "Zu viele Dateien" in response.text

    def test_kein_platz(self, client, cd, monkeypatch, isoliertes_staging):
        """Die Grenzen gelten für die CD genauso -- vorab geprüft."""
        from backend.config import settings

        monkeypatch.setattr(settings, "staging_free_bytes", lambda: 0)
        monkeypatch.setattr(settings, "min_free_bytes", 1)

        response = client.post("/disc", data={"folder": "Abbey Road"})
        assert "nicht genug Speicherplatz" in response.text
        assert list(isoliertes_staging.iterdir()) == []

    def test_liste_bleibt_nach_dem_import_abrufbar(self, client, cd):
        """Das nächste Album derselben CD ist der erwartete nächste Schritt."""
        client.post("/disc", data={"folder": "Abbey Road"})
        response = client.get("/disc")
        assert "Abbey Road" in response.text

    def test_erste_session_ueberlebt_die_zweite_uebernahme(
        self, client, cd, isoliertes_staging
    ):
        """Album 1 darf nicht verschwinden, während man Album 2 übernimmt.

        Beide Einstiege -- Upload und CD -- räumen vor dem Anlegen verwaiste
        Sessions weg. Eine frische Sitzung darf das nie treffen, sonst wäre die
        Entscheidung über Album 1 unterwegs verloren.
        """
        (cd / "Revolver").mkdir()
        (cd / "Revolver" / "01 Taxman.flac").write_bytes(self.FLAC)

        client.post("/disc", data={"folder": "Abbey Road"})
        client.post("/disc", data={"folder": "Revolver"})

        assert len(list(isoliertes_staging.iterdir())) == 2

    def test_upload_laesst_die_cd_session_in_ruhe(
        self, client, cd, isoliertes_staging
    ):
        """Auch der andere Einstieg darf eine frische CD-Sitzung nicht wegräumen."""
        client.post("/disc", data={"folder": "Abbey Road"})
        client.post(
            "/upload", files={"files": ("b.flac", self.FLAC, "audio/flac")}
        )

        assert len(list(isoliertes_staging.iterdir())) == 2
