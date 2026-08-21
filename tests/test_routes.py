"""Endpunkte über den TestClient.

Der Schwerpunkt liegt auf dem Upload: Dateinamen kommen vom Browser und dürfen
niemals aus dem Staging-Ordner herausführen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import routes, sessions
from backend.main import app


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def isolierte_hoerbuecher(tmp_path, monkeypatch):
    from backend import config

    wurzel = tmp_path / "audiobooks"
    monkeypatch.setattr(config.settings, "audiobook_root", wurzel)
    monkeypatch.setattr(config.settings, "min_free_bytes", 0)
    return wurzel


class TestIndex:
    """Die Startseite stellt nur die eine Frage, die beiden Wege sind getrennt."""

    def test_startseite_fragt_nach_dem_modus(self, client):
        response = client.get("/")
        assert response.status_code == 200
        assert 'href="/musik"' in response.text
        assert 'href="/hoerbuch"' in response.text
        # Nichts vom eigentlichen Ablauf gehört hierher.
        assert "Dateien auswählen" not in response.text
        assert "upload-form" not in response.text

    def test_musikseite_hat_den_ganzen_ablauf(self, client):
        response = client.get("/musik")
        assert response.status_code == 200
        assert "Quelle wählen" in response.text
        assert "Match auswählen" in response.text
        assert "upload-form" in response.text

    def test_musikseite_zeigt_keine_hoerbuecher(self, client):
        """Auf der Musikseite wäre der halbe Hörbuch-Weg sinnlos."""
        response = client.get("/musik")
        assert 'hx-get="/audiobook"' not in response.text

    def test_hoerbuchseite_zeigt_nur_hoerbuecher(self, client):
        response = client.get("/hoerbuch")
        assert response.status_code == 200
        assert 'hx-get="/audiobook"' in response.text
        # Kein Match und kein beets-Import -- das gehört zum Musikweg.
        assert "Match auswählen" not in response.text

    def test_zurueck_zur_auswahl(self, client):
        for pfad in ("/musik", "/hoerbuch"):
            assert 'href="/"' in client.get(pfad).text


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


class TestAudiobookUpload:
    def test_fragment_bietet_den_upload_an(self, client):
        response = client.get("/audiobook")
        assert response.status_code == 200
        assert "audiobook-upload-form" in response.text
        assert "Dateien übernehmen" in response.text
        assert 'id="audiobook-loading"' in response.text

    def test_ordnerstruktur_wird_im_buch_erhalten(self, client, isolierte_hoerbuecher):
        response = client.post(
            "/audiobook/upload",
            data={"autor": "Frank Herbert", "titel": "Der Wüstenplanet"},
            files={"files": ("CD 1/01 Kapitel.mp3", b"ID3\x00\x00\x00\x00", "audio/mpeg")},
        )
        assert response.status_code == 200
        assert (
            isolierte_hoerbuecher
            / "Frank Herbert"
            / "Der Wüstenplanet"
            / "CD 1"
            / "01 Kapitel.mp3"
        ).is_file()

    def test_erster_flacher_upload_landet_direkt_im_buch(self, client, isolierte_hoerbuecher):
        response = client.post(
            "/audiobook/upload",
            data={"autor": "A", "titel": "B"},
            files={"files": ("01 Kapitel.mp3", b"ID3\x00\x00\x00\x00", "audio/mpeg")},
        )
        assert response.status_code == 200
        assert (isolierte_hoerbuecher / "A" / "B" / "01 Kapitel.mp3").is_file()

    def test_zweiter_upload_normalisiert_zu_cd_ordnern(self, client, isolierte_hoerbuecher):
        daten = {"autor": "A", "titel": "B"}
        client.post(
            "/audiobook/upload",
            data=daten,
            files={"files": ("01 Erste.mp3", b"ID3\x00\x00\x00\x00", "audio/mpeg")},
        )

        response = client.post(
            "/audiobook/upload",
            data=daten,
            files={"files": ("01 Zweite.mp3", b"ID3\x00\x00\x00\x00", "audio/mpeg")},
        )
        assert response.status_code == 200
        buch = isolierte_hoerbuecher / "A" / "B"
        assert (buch / "CD 1" / "01 Erste.mp3").is_file()
        assert (buch / "CD 2" / "01 Zweite.mp3").is_file()


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
        """Ohne MusicBrainz-Treffer sind Genre, Jahr und Albumkünstler Pflicht --
        eine komplett leere Übernahme soll das klar benennen, statt stillschweigend
        nichts zu schreiben oder mit halben Metadaten durchzurutschen."""
        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.post(f"/manual/{session.session_id}", data={})
        assert "Genre" in response.text
        assert "Jahr" in response.text
        assert "Albumkünstler" in response.text


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
        # Ein Probelauf hat nichts abgeschlossen -- der "Fertig"-Knopf würde hier
        # vorgaukeln, dass schon etwas passiert ist.
        assert "Fertig" not in response.text

    def test_abgeschlossener_import_bietet_einen_fertig_knopf(self, client, monkeypatch):
        """Nach einem echten (nicht simulierten) Import soll es zurück in ein
        sauberes /musik gehen, statt mitten in der abgehakten Sitzung
        hängenzubleiben."""
        from backend import beets_env, importer

        monkeypatch.setattr(
            beets_env,
            "health",
            lambda: {
                "beets_version": "2.13.1",
                "beet_cli_version": "2.13.1",
                "metadata_sources": ["musicbrainz"],
                "fingerprint": False,
                "problems": [],
                "import_ready": True,
            },
        )
        monkeypatch.setattr(
            importer,
            "run_import",
            lambda directory, pretend=False: importer.ImportResult(
                command=["beet", "import", "-A", str(directory)],
                returncode=0,
                stdout="importiert",
            ),
        )

        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        response = client.post(f"/import/{session.session_id}", data={})

        assert "Import abgeschlossen" in response.text
        assert 'href="/musik"' in response.text

    def test_abgeschlossener_import_vergisst_den_zugehoerigen_rip(
        self, client, monkeypatch
    ):
        """Nach dem Import einer gerippten CD darf der Audio-CD-Reiter beim
        nächsten Öffnen nicht mehr den Kopf der längst importierten Session
        zeigen -- sonst bleibt nur "Verwerfen" von Hand, um wieder rippen zu
        können."""
        from backend import beets_env, importer, rip, sessions

        monkeypatch.setattr(
            beets_env,
            "health",
            lambda: {
                "beets_version": "2.13.1",
                "beet_cli_version": "2.13.1",
                "metadata_sources": ["musicbrainz"],
                "fingerprint": False,
                "problems": [],
                "import_ready": True,
            },
        )
        monkeypatch.setattr(
            importer,
            "run_import",
            lambda directory, pretend=False: importer.ImportResult(
                command=["beet", "import", "-A", str(directory)],
                returncode=0,
                stdout="importiert",
            ),
        )

        session = sessions.create_session()
        (session.directory / "a.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        job = rip.RipJob(zustand="fertig", session_id=session.session_id)
        monkeypatch.setattr(rip, "_job", job)

        client.post(f"/import/{session.session_id}", data={})

        assert rip.current() is None


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


class TestRip:
    """Die Audio-CD: kein Dateisystem, muss gelesen werden."""

    @pytest.fixture(autouse=True)
    def kein_alter_auftrag(self):
        from backend import rip

        rip._job = None
        yield
        rip._job = None

    @pytest.fixture
    def werkzeuge_da(self, monkeypatch):
        from backend import rip

        monkeypatch.setattr(
            rip,
            "tools_available",
            lambda: {"cdparanoia": True, "flac": True, "device": True},
        )

    def test_fehlende_werkzeuge_werden_gemeldet(self, client, monkeypatch):
        from backend import rip

        monkeypatch.setattr(
            rip,
            "tools_available",
            lambda: {"cdparanoia": False, "flac": False, "device": True},
        )
        response = client.get("/rip")
        assert "Werkzeuge fehlen" in response.text

    def test_bereit_ohne_auftrag(self, client, werkzeuge_da):
        response = client.get("/rip")
        assert "Audio-CD lesen" in response.text

    def test_start_zeigt_fortschritt(self, client, werkzeuge_da, monkeypatch):
        from backend import discid, rip
        from tests.test_discid import CDPARANOIA_AUSGABE
        from tests.test_rip import _FakeThread

        monkeypatch.setattr(
            rip, "read_toc", lambda: discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        )
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        response = client.post("/rip")
        assert "Tracks fertig" in response.text
        # Solange gelesen wird, pusht /rip/events den Fortschritt statt dass
        # die Seite ihn abholt.
        assert 'sse-connect="/rip/events"' in response.text
        assert 'hx-trigger="sse:fertig"' in response.text

    def test_kein_laufwerk_meldet_klar(self, client, werkzeuge_da, monkeypatch):
        from backend import rip

        def kaputt():
            raise rip.RipError("Im Laufwerk wurde keine Audio-CD erkannt.")

        monkeypatch.setattr(rip, "read_toc", kaputt)
        response = client.post("/rip")
        assert "keine Audio-CD erkannt" in response.text

    def test_dateien_erst_nach_fertigem_rip(self, client, werkzeuge_da):
        response = client.get("/rip/files")
        assert "kein fertiger Rip" in response.text

    def test_fertiger_rip_liefert_die_dateiliste(
        self, client, werkzeuge_da, monkeypatch
    ):
        """Ab hier ist der Weg derselbe wie bei Upload und Daten-CD."""
        from backend import rip, sessions

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        job = rip.RipJob(zustand="fertig", session_id=session.session_id)
        monkeypatch.setattr(rip, "_job", job)

        response = client.get("/rip/files")
        assert "/match/" in response.text
        assert "01 Track 1.flac" in response.text

    def test_verwerfen_gibt_das_laufwerk_frei(self, client, werkzeuge_da, monkeypatch):
        from backend import rip, sessions

        session = sessions.create_session()
        job = rip.RipJob(zustand="fertig", session_id=session.session_id)
        monkeypatch.setattr(rip, "_job", job)

        response = client.request("DELETE", "/rip")
        assert "Audio-CD lesen" in response.text
        assert not session.directory.exists()

    def test_fertiger_rip_bietet_weitere_disc_an(
        self, client, werkzeuge_da, monkeypatch
    ):
        """Ein Mehrfach-CD-Album lässt sich nicht am Stück einlegen."""
        from backend import rip, sessions

        session = sessions.create_session()
        job = rip.RipJob(zustand="fertig", session_id=session.session_id)
        monkeypatch.setattr(rip, "_job", job)

        response = client.get("/rip")
        assert "Weitere Disc rippen" in response.text
        assert f'value="{session.session_id}"' in response.text

    def test_fertiger_rip_bietet_zurueckstellen_an(
        self, client, werkzeuge_da, monkeypatch
    ):
        """Für ein anderes Album muss sich das Laufwerk freigeben lassen, ohne
        den fertigen Rip zu verwerfen."""
        from backend import rip, sessions

        session = sessions.create_session()
        job = rip.RipJob(zustand="fertig", session_id=session.session_id)
        monkeypatch.setattr(rip, "_job", job)

        response = client.get("/rip")
        assert "/rip?sitzung_loeschen=false" in response.text

    def test_zurueckstellen_behaelt_die_session_und_gibt_das_laufwerk_frei(
        self, client, werkzeuge_da, monkeypatch
    ):
        from backend import rip, sessions

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        job = rip.RipJob(zustand="fertig", session_id=session.session_id)
        monkeypatch.setattr(rip, "_job", job)

        response = client.request("DELETE", "/rip?sitzung_loeschen=false")

        assert "Audio-CD lesen" in response.text
        assert session.directory.exists()
        assert (session.directory / "01 Track 1.flac").is_file()
        assert rip.current() is None

        # Das Laufwerk ist wirklich frei -- ein neuer Rip lässt sich starten.
        from tests.test_discid import CDPARANOIA_AUSGABE
        from tests.test_rip import _FakeThread

        monkeypatch.setattr(
            rip, "read_toc", lambda: rip.discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        )
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        neuer_start = client.post("/rip")
        assert neuer_start.status_code == 200
        neuer_job = rip.current()
        assert neuer_job is not None
        assert neuer_job.session_id != session.session_id

    def test_weitere_disc_haengt_an_dieselbe_session_an(
        self, client, werkzeuge_da, monkeypatch
    ):
        """Die zweite CD landet in derselben Session, nicht in einer neuen."""
        from backend import discid, rip, sessions
        from tests.test_discid import CDPARANOIA_AUSGABE
        from tests.test_rip import _FakeThread

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        monkeypatch.setattr(
            rip, "read_toc", lambda: discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        )
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        response = client.post("/rip", data={"session_id": session.session_id})

        assert response.status_code == 200
        job = rip.current()
        assert job is not None
        assert job.session_id == session.session_id
        assert job.neue_session is False
        # Die erste Disc ist vor der zweiten nach "CD 1" umgezogen, sonst
        # kollidieren die Dateinamen beider Discs.
        assert (session.directory / "CD 1" / "01 Track 1.flac").is_file()
        assert (session.directory / "CD 2").is_dir()

    def test_weitere_disc_mit_unbekannter_session_meldet_fehler(
        self, client, werkzeuge_da, monkeypatch
    ):
        from backend import discid, rip
        from tests.test_discid import CDPARANOIA_AUSGABE
        from tests.test_rip import _FakeThread

        monkeypatch.setattr(
            rip, "read_toc", lambda: discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        )
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        response = client.post("/rip", data={"session_id": "x" * 20})
        assert "gibt es nicht" in response.text

    def test_laufwerk_freigeben_behaelt_die_session(
        self, client, werkzeuge_da, monkeypatch
    ):
        """Scheitert die zweite Disc, soll die erste nicht mit verschwinden."""
        from backend import rip, sessions

        session = sessions.create_session()
        (session.directory / "CD 1" / "01 Track 1.flac").parent.mkdir(parents=True)
        (session.directory / "CD 1" / "01 Track 1.flac").write_bytes(
            b"fLaC\x00\x00\x00\x22"
        )
        job = rip.RipJob(
            zustand="fehler",
            session_id=session.session_id,
            neue_session=False,
            fehler="Track 1 ließ sich nicht lesen.",
        )
        monkeypatch.setattr(rip, "_job", job)

        response = client.request("DELETE", "/rip?sitzung_loeschen=false")

        assert "Audio-CD lesen" in response.text
        assert session.directory.exists()
        assert (session.directory / "CD 1" / "01 Track 1.flac").is_file()


class TestRipEvents:
    """/rip/events -- pusht statt zu pollen.

    Ein tatsächlich laufender Auftrag lässt den Generator in einer
    Sleep-Schleife hängen; über den TestClient wäre das ein Hang ohne
    Timeout. Getestet werden deshalb nur die Zustände, in denen der Strom
    sofort mit "fertig" endet -- das deckt die Fallunterscheidung ab, ohne
    eine laufende Verbindung offen zu halten.
    """

    @pytest.fixture(autouse=True)
    def kein_alter_auftrag(self):
        from backend import rip

        rip._job = None
        yield
        rip._job = None

    def test_ohne_auftrag_endet_sofort(self, client):
        response = client.get("/rip/events")
        assert response.status_code == 200
        assert "event: fertig" in response.text

    def test_eigener_auftrag_fertig_endet_sofort(self, client, monkeypatch):
        from backend import rip

        job = rip.RipJob(modus="musik", zustand="fertig")
        monkeypatch.setattr(rip, "_job", job)

        response = client.get("/rip/events")
        assert "event: fertig" in response.text

    def test_fremder_auftrag_fertig_endet_sofort(self, client, monkeypatch):
        """Das Hörbuch-Rip ist durch -- das Laufwerk ist wieder frei."""
        from backend import rip

        job = rip.RipJob(modus="hoerbuch", zustand="fertig")
        monkeypatch.setattr(rip, "_job", job)

        response = client.get("/rip/events")
        assert "event: fertig" in response.text


class TestFortschrittsPartials:
    """Die kleinen Templates, die sowohl beim Erst-Rendern als auch bei jedem
    SSE-Push verwendet werden -- ein Fehler hierträfe beide Wege."""

    def test_rip_fortschritt(self):
        from backend import rip
        from backend.templates import templates

        job = rip.RipJob(modus="musik", zustand="rippt", track=3, tracks_gesamt=9)
        html = templates.get_template("_rip_fortschritt.html").render(job=job)
        assert "3 von 9 Tracks fertig" in html
        assert "läuft seit" in html

    def test_audiobook_rip_fortschritt(self):
        from backend import rip
        from backend.templates import templates

        job = rip.RipJob(modus="hoerbuch", zustand="rippt", track=2, tracks_gesamt=5)
        html = templates.get_template("_audiobook_rip_fortschritt.html").render(job=job)
        assert "2 von 5 Tracks fertig" in html

    def test_audiobook_m4b_fortschritt(self):
        from backend import audiobook
        from backend.templates import templates

        m4b = audiobook.M4bJob(buch="Frank Herbert/Dune", zustand="encodieren")
        html = templates.get_template("_audiobook_m4b_fortschritt.html").render(
            m4b=m4b, stillstand_minuten=10
        )
        assert "Frank Herbert" in html
        assert "10 Minuten ohne Regung" in html


class TestOffeneSitzungen:
    """Ein geschlossener Tab darf einen Upload nicht kosten."""

    FLAC = b"fLaC\x00\x00\x00\x22"

    def test_ohne_sitzungen_bleibt_die_liste_leer(self, client):
        response = client.get("/sessions")
        assert response.status_code == 200
        assert "Unterbrochene Sitzungen" not in response.text

    def test_verlorene_sitzung_wird_wiedergefunden_und_fortgesetzt(self, client):
        """Der eigentliche Fall: Session-ID weg, Dateien noch da."""
        hochgeladen = client.post(
            "/upload",
            files={"files": ("Abbey Road/01 Come Together.flac", self.FLAC, "audio/flac")},
        )
        assert "Come Together" in hochgeladen.text

        # Der Browser ist zu, die ID existiert nirgends mehr. Neue Seite:
        liste = client.get("/sessions")
        assert "offene Sitzung" in liste.text
        assert "Abbey Road" in liste.text

        # Über die Liste zurück in denselben Ablauf.
        import re

        treffer = re.search(r'hx-get="/session/([A-Za-z0-9_-]+)"', liste.text)
        assert treffer, "Die Liste muss einen Weg zurück anbieten"

        fortgesetzt = client.get(f"/session/{treffer.group(1)}")
        assert "Come Together" in fortgesetzt.text
        assert "/match/" in fortgesetzt.text

    def test_unbekannte_sitzung_gibt_404(self, client):
        response = client.get("/session/AAAAAAAAAAAAAAAAAA")
        assert response.status_code == 404

    def test_weitere_disc_haengt_ohne_laufenden_rip_job_an(self, client, monkeypatch):
        """Der eigentliche Fall aus dem Feedback: keine laufende Rip-Anzeige
        (Server neu gestartet, Job zurückgesetzt, ganz anderer Auftrag) -- die
        Sitzung selbst reicht, um eine weitere Disc anzuhängen."""
        from backend import discid, rip, sessions
        from tests.test_discid import CDPARANOIA_AUSGABE
        from tests.test_rip import _FakeThread

        monkeypatch.setattr(
            rip,
            "tools_available",
            lambda: {"cdparanoia": True, "flac": True, "device": True},
        )
        monkeypatch.setattr(
            rip, "read_toc", lambda: discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        )
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(self.FLAC)
        # Kein Job -- weder laufend noch fertig, unabhängig davon, was ein
        # zuvor gelaufener Test hinterlassen hat.
        monkeypatch.setattr(rip, "_job", None)

        liste = client.get("/sessions")
        assert "Weitere Disc rippen" in liste.text
        assert f'value="{session.session_id}"' in liste.text

        response = client.post("/rip", data={"session_id": session.session_id})
        assert response.status_code == 200

        job = rip.current()
        assert job is not None
        assert job.session_id == session.session_id
        assert job.neue_session is False
        assert (session.directory / "CD 2").is_dir()

    def test_ohne_laufwerk_fehlt_der_knopf(self, client, monkeypatch):
        from backend import rip, sessions

        monkeypatch.setattr(
            rip,
            "tools_available",
            lambda: {"cdparanoia": True, "flac": True, "device": False},
        )

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(self.FLAC)

        liste = client.get("/sessions")
        assert "Weitere Disc rippen" not in liste.text
        assert "Fortsetzen" in liste.text

    def test_verwerfen_aus_der_liste_zeigt_den_neuen_stand(self, client):
        from backend import sessions

        erste = sessions.create_session()
        (erste.directory / "A").mkdir()
        (erste.directory / "A" / "a.flac").write_bytes(self.FLAC)
        zweite = sessions.create_session()
        (zweite.directory / "B").mkdir()
        (zweite.directory / "B" / "b.flac").write_bytes(self.FLAC)

        response = client.request("DELETE", f"/sessions/{erste.session_id}")
        # Die verworfene verschwindet aus der Liste, die andere bleibt stehen --
        # ein leerer Bereich wäre hier falsch.
        assert erste.session_id not in response.text
        assert zweite.session_id in response.text
        assert not erste.directory.exists()
        assert zweite.directory.is_dir()
        assert "offene Sitzung" in response.text

    def test_cd_uebernahme_behaelt_den_ordnernamen(self, client, tmp_path, monkeypatch):
        """Sonst hieße die Sitzung später nur nach ihrer ersten Datei."""
        from backend.config import settings

        wurzel = tmp_path / "disc"
        (wurzel / "Revolver").mkdir(parents=True)
        (wurzel / "Revolver" / "01 Taxman.flac").write_bytes(self.FLAC)
        monkeypatch.setattr(settings, "disc_root", wurzel)

        client.post("/disc", data={"folder": "Revolver"})
        liste = client.get("/sessions")
        assert "Revolver" in liste.text


class TestHoerbuecher:
    """Eigener Weg: kein Match, kein beets, eigene Ablage."""

    @pytest.fixture(autouse=True)
    def bibliothek(self, tmp_path, monkeypatch):
        from backend import audiobook, rip

        wurzel = tmp_path / "audiobooks"
        monkeypatch.setattr(audiobook.settings, "audiobook_root", wurzel)
        rip._job = None
        audiobook._m4b_job = None
        yield wurzel
        rip._job = None
        audiobook._m4b_job = None

    def test_bereich_wird_ausgeliefert(self, client):
        response = client.get("/audiobook")
        assert response.status_code == 200
        assert "Autor" in response.text and "Titel" in response.text

    def test_ohne_angaben_kein_rip(self, client):
        response = client.post("/audiobook/rip", data={"autor": "", "titel": ""})
        assert "beide gebraucht" in response.text

    def test_pfadausbruch_im_titel(self, client, bibliothek):
        client.post("/audiobook/rip", data={"autor": "../..", "titel": "../etc"})
        # Nichts oberhalb der Bibliothek angelegt.
        assert not (bibliothek.parent / "etc").exists()

    def test_datencd_wird_ins_buch_kopiert(self, client, bibliothek, tmp_path, monkeypatch):
        """Liegt eine Daten-CD ein, wird kopiert statt gerippt."""
        from backend.config import settings

        cd = tmp_path / "disc"
        cd.mkdir()
        (cd / "01 Kapitel.mp3").write_bytes(b"\xff\xfb\x00\x00")
        monkeypatch.setattr(settings, "disc_root", cd)

        response = client.post(
            "/audiobook/rip", data={"autor": "Frank Herbert", "titel": "Dune"}
        )
        assert "kopiert" in response.text
        buch = bibliothek / "Frank Herbert" / "Dune"
        # Erste Daten-CD landet flach -- eine MP3-CD trägt meist das ganze Buch.
        assert (buch / "01 Kapitel.mp3").is_file()

    def test_buch_taucht_in_der_liste_auf(self, client, bibliothek):
        buch = bibliothek / "Frank Herbert" / "Dune"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.get("/audiobook")
        assert "Frank Herbert" in response.text
        assert "Dune" in response.text

    def test_m4b_fuer_unbekanntes_buch(self, client):
        response = client.post("/audiobook/m4b", data={"buch": "../etc"})
        assert "nicht zur Bibliothek" in response.text or "gibt es nicht" in response.text

    def test_laufender_rip_verbindet_per_sse(self, client, bibliothek, monkeypatch):
        from backend import rip

        job = rip.RipJob(modus="hoerbuch", zustand="rippt", track=1, tracks_gesamt=3)
        monkeypatch.setattr(rip, "_job", job)

        response = client.get("/audiobook")
        assert 'sse-connect="/audiobook/events"' in response.text
        assert 'sse-swap="fortschritt-rip"' in response.text
        assert "fortschritt-m4b" not in response.text

    def test_ohne_laufenden_auftrag_keine_sse_verbindung(self, client, bibliothek):
        response = client.get("/audiobook")
        assert "sse-connect" not in response.text


class TestAudiobookEvents:
    """/audiobook/events -- bedient Rip und m4b-Bau über eine Verbindung.

    Wie bei TestRipEvents: nur die sofort mit "fertig" endenden Zustände,
    ein tatsächlich laufender Auftrag würde den TestClient hängen lassen.
    """

    @pytest.fixture(autouse=True)
    def kein_alter_auftrag(self):
        from backend import audiobook, rip

        rip._job = None
        audiobook._m4b_job = None
        yield
        rip._job = None
        audiobook._m4b_job = None

    def test_ohne_auftraege_endet_sofort(self, client):
        response = client.get("/audiobook/events")
        assert response.status_code == 200
        assert "event: fertig" in response.text

    def test_beide_auftraege_fertig_endet_sofort(self, client, monkeypatch):
        from backend import audiobook, rip

        monkeypatch.setattr(
            rip, "_job", rip.RipJob(modus="hoerbuch", zustand="fertig")
        )
        monkeypatch.setattr(
            audiobook, "_m4b_job", audiobook.M4bJob(buch="x", zustand="fertig")
        )

        response = client.get("/audiobook/events")
        assert "event: fertig" in response.text

    def test_musik_rip_zaehlt_hier_nicht_als_eigener_auftrag(self, client, monkeypatch):
        """Ein Musik-Rip gehört nicht auf die Hörbuchseite -- auch nicht als
        Grund, auf ihn zu warten."""
        from backend import rip

        monkeypatch.setattr(
            rip, "_job", rip.RipJob(modus="musik", zustand="rippt", track=1)
        )

        response = client.get("/audiobook/events")
        assert "event: fertig" in response.text


class TestLaufwerkGehoertNurEinemModus:
    """Ein Laufwerk, ein Auftrag -- aber zwei Seiten, die ihn zeigen könnten."""

    @pytest.fixture(autouse=True)
    def kein_alter_auftrag(self):
        from backend import rip

        rip._job = None
        yield
        rip._job = None

    @pytest.fixture
    def werkzeuge(self, monkeypatch):
        from backend import rip

        monkeypatch.setattr(
            rip,
            "tools_available",
            lambda: {"cdparanoia": True, "flac": True, "device": True},
        )

    def test_hoerbuch_rip_erscheint_nicht_im_musikbereich(
        self, client, werkzeuge, monkeypatch
    ):
        """Sonst stünde derselbe Fortschritt zweimal auf der Seite."""
        from backend import rip

        job = rip.RipJob(modus="hoerbuch", zustand="rippt", track=4, tracks_gesamt=9)
        monkeypatch.setattr(rip, "_job", job)

        musik = client.get("/rip")
        assert "4 von 9 Tracks fertig" not in musik.text
        assert "liest gerade ein Hörbuch" in musik.text

    def test_musik_rip_erscheint_nicht_im_hoerbuchbereich(
        self, client, werkzeuge, monkeypatch, tmp_path
    ):
        from backend import audiobook, rip

        monkeypatch.setattr(
            audiobook.settings, "audiobook_root", tmp_path / "audiobooks"
        )
        job = rip.RipJob(modus="musik", zustand="rippt", track=4, tracks_gesamt=9)
        monkeypatch.setattr(rip, "_job", job)

        hoerbuch = client.get("/audiobook")
        assert "4 von 9 Tracks fertig" not in hoerbuch.text

    def test_eigener_auftrag_wird_sehr_wohl_gezeigt(
        self, client, werkzeuge, monkeypatch
    ):
        from backend import rip

        job = rip.RipJob(modus="musik", zustand="rippt", track=4, tracks_gesamt=9)
        monkeypatch.setattr(rip, "_job", job)

        assert "4 von 9 Tracks fertig" in client.get("/rip").text


class TestHoerbuchFortsetzen:
    """Für die nächste Disc soll man Autor und Titel nicht neu tippen."""

    @pytest.fixture(autouse=True)
    def bibliothek(self, tmp_path, monkeypatch):
        from backend import audiobook, rip

        wurzel = tmp_path / "audiobooks"
        monkeypatch.setattr(audiobook.settings, "audiobook_root", wurzel)
        rip._job = None
        audiobook._m4b_job = None
        yield wurzel
        rip._job = None
        audiobook._m4b_job = None

    def test_autorfeld_ist_leer(self, client, bibliothek):
        """Hier stand einmal der Autor des ersten Buchs der Liste."""
        buch = bibliothek / "Astrid Lindgren" / "Ronja"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.get("/audiobook")
        # Das Buch steht in der Liste ...
        assert "Astrid Lindgren" in response.text
        # ... aber nicht als Wert im Eingabefeld.
        assert 'name="autor" required placeholder="Frank Herbert">' in response.text
        assert 'value="Astrid Lindgren"' not in response.text

    def test_naechste_cd_knopf_je_buch(self, client, bibliothek):
        buch = bibliothek / "Astrid Lindgren" / "Ronja"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.get("/audiobook")
        assert ">Nächste CD</button>" in response.text
        assert "Dateien hinzufügen" in response.text
        assert 'value="Astrid Lindgren/Ronja"' in response.text
        assert 'hx-indicator="#audiobook-loading"' in response.text

    def test_fortsetzen_ueber_den_buchpfad(self, client, bibliothek, tmp_path, monkeypatch):
        """Ohne Autor und Titel, allein über den Pfad des Buchs."""
        from backend.config import settings

        buch = bibliothek / "Astrid Lindgren" / "Ronja"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        # Daten-CD einlegen, damit kopiert statt gerippt wird.
        cd = tmp_path / "disc"
        cd.mkdir()
        (cd / "02 Kapitel.mp3").write_bytes(b"\xff\xfb\x00\x00")
        monkeypatch.setattr(settings, "disc_root", cd)

        response = client.post("/audiobook/rip", data={"buch": "Astrid Lindgren/Ronja"})
        assert "kopiert" in response.text
        # Die zweite Disc landet neben der ersten, nicht in einem neuen Buch.
        assert (buch / "CD 2" / "02 Kapitel.mp3").is_file()
        # Kein zweites Buch -- der Staging-Ordner zählt nicht, der ist kein Buch.
        assert sorted(
            p.name for p in bibliothek.iterdir() if not p.name.startswith(".")
        ) == ["Astrid Lindgren"]

    def test_fremder_buchpfad_wird_abgewiesen(self, client, bibliothek):
        response = client.post("/audiobook/rip", data={"buch": "../../etc"})
        assert "nicht zur Bibliothek" in response.text or "gibt es nicht" in response.text

    def test_fertiges_buch_hat_keinen_naechste_cd_knopf(self, client, bibliothek):
        """Nach dem Bündeln wäre eine weitere Disc nur Verwirrung."""
        buch = bibliothek / "Astrid Lindgren" / "Ronja"
        buch.mkdir(parents=True)
        (buch / "Ronja.m4b").write_bytes(b"x")

        response = client.get("/audiobook")
        assert ">Nächste CD</button>" not in response.text
        assert "Dateien hinzufügen" not in response.text


class TestVonVornUeberDieOberflaeche:
    """Ein fertiges Buch: kein „Nächste CD", aber auch keine Sackgasse."""

    @pytest.fixture(autouse=True)
    def bibliothek(self, tmp_path, monkeypatch):
        from backend import audiobook, rip

        wurzel = tmp_path / "audiobooks"
        monkeypatch.setattr(audiobook.settings, "audiobook_root", wurzel)
        rip._job = None
        audiobook._m4b_job = None
        yield wurzel
        rip._job = None
        audiobook._m4b_job = None

    def _fertiges_buch(self, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)
        (buch / "Buch.m4b").write_bytes(b"fertige fassung")
        return buch

    def test_knopfwechsel_bei_fertigem_buch(self, client, bibliothek):
        self._fertiges_buch(bibliothek)
        html = client.get("/audiobook").text
        assert ">Nächste CD</button>" not in html
        assert ">Von vorn einlesen</button>" in html

    def test_fehlstart_laesst_das_buch_unangetastet(self, client, bibliothek, monkeypatch):
        """Ohne Laufwerk darf die fertige m4b nicht umbenannt werden."""
        from backend import rip

        buch = self._fertiges_buch(bibliothek)

        def kein_laufwerk():
            raise rip.RipError("Im Laufwerk wurde keine Audio-CD erkannt.")

        monkeypatch.setattr(rip, "read_toc", kein_laufwerk)

        response = client.post(
            "/audiobook/rip", data={"buch": "Autor/Buch", "von_vorn": "true"}
        )
        assert "keine Audio-CD erkannt" in response.text
        # Entscheidend: die m4b heißt noch wie vorher.
        assert (buch / "Buch.m4b").read_bytes() == b"fertige fassung"
        assert not list(buch.glob("*.ersetzt"))

    def test_erfolgreicher_start_legt_die_m4b_beiseite(
        self, client, bibliothek, tmp_path, monkeypatch
    ):
        from backend.config import settings

        buch = self._fertiges_buch(bibliothek)
        # Daten-CD einlegen, dann läuft es ohne Laufwerk durch.
        cd = tmp_path / "disc"
        cd.mkdir()
        (cd / "01 Kapitel.mp3").write_bytes(b"\xff\xfb\x00\x00")
        monkeypatch.setattr(settings, "disc_root", cd)

        response = client.post(
            "/audiobook/rip", data={"buch": "Autor/Buch", "von_vorn": "true"}
        )
        assert "liegt jetzt als" in response.text
        assert not (buch / "Buch.m4b").exists()
        beiseite = list(buch.glob("*.ersetzt"))
        assert len(beiseite) == 1
        # Nicht gelöscht, nur umbenannt.
        assert beiseite[0].read_bytes() == b"fertige fassung"


class TestParallelerBetrieb:
    """Zwei Bücher gleichzeitig ja, dasselbe Buch nein."""

    @pytest.fixture(autouse=True)
    def bibliothek(self, tmp_path, monkeypatch):
        from backend import audiobook, rip

        wurzel = tmp_path / "audiobooks"
        monkeypatch.setattr(audiobook.settings, "audiobook_root", wurzel)
        rip._job = None
        audiobook._m4b_job = None
        yield wurzel
        rip._job = None
        audiobook._m4b_job = None

    def _buch(self, bibliothek, name, *, mit_quellen=True):
        buch = bibliothek / "Autor" / name
        if mit_quellen:
            (buch / "CD 1").mkdir(parents=True)
            (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        else:
            buch.mkdir(parents=True)
        return buch

    def test_rip_am_selben_buch_wird_abgelehnt(self, client, bibliothek, monkeypatch):
        """Der m4b-Bau räumt leere Disc-Ordner weg -- auch den des Rips."""
        from backend import audiobook

        self._buch(bibliothek, "Buch")
        laufend = audiobook.M4bJob(
            buch=str(bibliothek / "Autor" / "Buch"), zustand="encodiert"
        )
        monkeypatch.setattr(audiobook, "_m4b_job", laufend)

        response = client.post("/audiobook/rip", data={"buch": "Autor/Buch"})
        assert "läuft gerade der m4b-Bau" in response.text

    def test_anderes_buch_darf_sehr_wohl(self, client, bibliothek, tmp_path, monkeypatch):
        from backend import audiobook
        from backend.config import settings

        self._buch(bibliothek, "Buch A")
        self._buch(bibliothek, "Buch B")
        laufend = audiobook.M4bJob(
            buch=str(bibliothek / "Autor" / "Buch A"), zustand="encodiert"
        )
        monkeypatch.setattr(audiobook, "_m4b_job", laufend)

        # Daten-CD, damit es ohne Laufwerk durchläuft.
        cd = tmp_path / "disc"
        cd.mkdir()
        (cd / "02.mp3").write_bytes(b"\xff\xfb\x00\x00")
        monkeypatch.setattr(settings, "disc_root", cd)

        response = client.post("/audiobook/rip", data={"buch": "Autor/Buch B"})
        assert "kopiert" in response.text
        assert "läuft gerade" not in response.text

    def test_m4b_am_selben_buch_wird_abgelehnt(self, client, bibliothek, monkeypatch):
        from backend import rip

        buch = self._buch(bibliothek, "Buch")
        laufend = rip.RipJob(
            modus="hoerbuch", zustand="rippt", buch=str(buch),
            disc_ordner=str(buch / "CD 2"),
        )
        monkeypatch.setattr(rip, "_job", laufend)

        response = client.post("/audiobook/m4b", data={"buch": "Autor/Buch"})
        assert "wird gerade eine Disc eingelesen" in response.text

    def test_beide_balken_gleichzeitig_sichtbar(self, client, bibliothek, monkeypatch):
        """Vorher verdeckte der Rip den m4b-Fortschritt komplett."""
        from backend import audiobook, rip

        rip_job = rip.RipJob(
            modus="hoerbuch", zustand="rippt", track=2, tracks_gesamt=9,
            buch=str(bibliothek / "Autor" / "Buch B"),
            disc_ordner=str(bibliothek / "Autor" / "Buch B" / "CD 1"),
        )
        m4b_job = audiobook.M4bJob(
            buch=str(bibliothek / "Autor" / "Buch A"),
            zustand="encodiert", sekunden_gesamt=100.0, sekunden_fertig=42.0,
        )
        monkeypatch.setattr(rip, "_job", rip_job)
        monkeypatch.setattr(audiobook, "_m4b_job", m4b_job)

        html = client.get("/audiobook").text
        assert "2 von 9 Tracks fertig" in html
        assert "42 %" in html
        # Und man erkennt, welches Buch welcher Balken ist.
        assert "Buch A" in html and "Buch B" in html


class TestBenachrichtigungsmarker:
    """Die Zustandsmarker, an denen static/notify.js den Übergang erkennt.

    Ein Rip dauert eine halbe Stunde, ein Encode Stunden -- ohne Signal müsste
    man die Seite im Auge behalten.
    """

    @pytest.fixture(autouse=True)
    def kein_alter_auftrag(self):
        from backend import audiobook, rip

        rip._job = None
        audiobook._m4b_job = None
        yield
        rip._job = None
        audiobook._m4b_job = None

    @pytest.fixture
    def werkzeuge(self, monkeypatch):
        from backend import rip

        monkeypatch.setattr(
            rip,
            "tools_available",
            lambda: {"cdparanoia": True, "flac": True, "device": True},
        )

    def test_skript_ist_eingebunden(self, client):
        for pfad in ("/", "/musik", "/hoerbuch"):
            assert "/static/notify.js" in client.get(pfad).text

    def test_ohne_auftrag_kein_marker(self, client, werkzeuge):
        assert 'data-auftrag="rip-musik"' not in client.get("/rip").text

    def test_laufender_rip_meldet_laeuft(self, client, werkzeuge, monkeypatch):
        from backend import rip

        monkeypatch.setattr(
            rip, "_job", rip.RipJob(zustand="rippt", track=1, tracks_gesamt=9)
        )
        html = client.get("/rip").text
        assert 'data-auftrag="rip-musik"' in html
        assert 'data-zustand="laeuft"' in html

    def test_fertiger_rip_meldet_fertig(self, client, werkzeuge, monkeypatch):
        from backend import rip

        job = rip.RipJob(zustand="fertig", tracks_gesamt=9)
        job.meldung = "9 Tracks gelesen in 23:41."
        monkeypatch.setattr(rip, "_job", job)

        html = client.get("/rip").text
        assert 'data-zustand="fertig"' in html
        assert "23:41" in html

    def test_fehler_meldet_fehler(self, client, werkzeuge, monkeypatch):
        from backend import rip

        monkeypatch.setattr(
            rip, "_job", rip.RipJob(zustand="fehler", fehler="Track 3 unlesbar")
        )
        assert 'data-zustand="fehler"' in client.get("/rip").text

    def test_hoerbuch_hat_zwei_getrennte_marker(
        self, client, monkeypatch, tmp_path
    ):
        """Rip und Bündeln laufen parallel -- jeder braucht seinen eigenen."""
        from backend import audiobook, rip

        monkeypatch.setattr(
            audiobook.settings, "audiobook_root", tmp_path / "audiobooks"
        )
        monkeypatch.setattr(
            rip,
            "_job",
            rip.RipJob(modus="hoerbuch", zustand="rippt", buch=str(tmp_path / "A" / "B")),
        )
        monkeypatch.setattr(
            audiobook,
            "_m4b_job",
            audiobook.M4bJob(buch=str(tmp_path / "A" / "C"), zustand="fertig"),
        )

        html = client.get("/audiobook").text
        assert 'data-auftrag="rip-hoerbuch"' in html
        assert 'data-auftrag="m4b"' in html
        # Der eine läuft, der andere ist fertig -- beides muss unterscheidbar sein.
        assert 'data-zustand="laeuft"' in html
        assert 'data-zustand="fertig"' in html


class TestCoverEntgegennehmen:
    """Das fertige Bild kommt vom Handy; hier landet es am richtigen Platz."""

    JPEG = b"\xff\xd8\xff\xe0" + b"x" * 200

    @pytest.fixture(autouse=True)
    def bibliothek(self, tmp_path, monkeypatch):
        from backend import audiobook

        wurzel = tmp_path / "audiobooks"
        monkeypatch.setattr(audiobook.settings, "audiobook_root", wurzel)
        return wurzel

    def test_dialog_liegt_auf_beiden_seiten(self, client):
        for pfad in ("/musik", "/hoerbuch"):
            html = client.get(pfad).text
            assert 'id="cover-dialog"' in html
            assert "/static/cover.js" in html

    def test_startseite_braucht_ihn_nicht(self, client):
        """Dort gibt es nichts, wozu ein Cover gehören könnte."""
        assert 'id="cover-dialog"' not in client.get("/").text

    def test_hoerbuch_cover_landet_im_buchordner(self, client, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.post(
            "/cover/audiobook?buch=Autor/Buch",
            files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")},
        )
        assert "Cover übernommen" in response.text
        assert (buch / "cover.jpg").read_bytes() == self.JPEG

    def test_cover_geht_auch_in_eine_fertige_m4b(self, client, bibliothek):
        """Der ganze Weg, den kein Modultest abdeckt.

        Er hängt an drei Dingen, die nur hier zusammenkommen: dem Rückgabewert
        von cover.speichern, der Weiche auf „schon gebündelt" und der Sperre.
        """
        import shutil
        import subprocess

        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            pytest.skip("ffmpeg fehlt")

        from backend import audiobook

        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "sine=f=440:d=3", "-c:a", "aac",
             str(audiobook.m4b_pfad(buch))],
            check=True,
        )
        bild = (bibliothek / "vorlage.jpg")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=green:s=300x300:d=1", "-frames:v", "1", str(bild)],
            check=True,
        )

        response = client.post(
            "/cover/audiobook?buch=Autor/Buch",
            files={"bild": ("cover.jpg", bild.read_bytes(), "image/jpeg")},
        )
        assert response.status_code == 200
        assert "in die m4b übernommen" in response.text, response.text[:400]

        _, _, bilder = audiobook._probe_eckdaten(audiobook.m4b_pfad(buch))
        assert bilder == 1, "Das Cover steckt nicht in der Datei"
        # Und daneben liegt es weiterhin als Ordnerbild -- davon lebt die
        # Beschriftung „Cover ersetzen", und Audiobookshelf nimmt es auch.
        assert (buch / "cover.jpg").is_file()

    def test_cover_wartet_auf_einen_laufenden_bau(self, client, bibliothek):
        """Ein „Neu bauen" darf die Datei nicht unter den Händen wegziehen."""
        from backend import audiobook

        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)
        audiobook.m4b_pfad(buch).write_bytes(b"nicht wirklich eine m4b")

        job = audiobook.M4bJob(buch=str(buch))
        job.zustand = "encodiert"
        audiobook._m4b_job = job
        try:
            response = client.post(
                "/cover/audiobook?buch=Autor/Buch",
                files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")},
            )
            assert "läuft gerade der m4b-Bau" in response.text
            assert "geht nicht verloren" in response.text
            # Das Bild ist trotzdem angekommen und muss nicht neu fotografiert
            # werden.
            assert (buch / "cover.jpg").read_bytes() == self.JPEG
        finally:
            audiobook._m4b_job = None

    def test_cover_wird_fuer_die_liste_ausgeliefert(self, client, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)
        (buch / "cover.jpg").write_bytes(self.JPEG)

        antwort = client.get("/audiobook/cover?buch=Autor/Buch&v=123")
        assert antwort.status_code == 200
        assert antwort.content == self.JPEG
        # Die Adresse trägt die Änderungszeit, das Bild darf also bleiben.
        assert "immutable" in antwort.headers["cache-control"]

    def test_auch_folder_jpg_wird_gefunden(self, client, bibliothek):
        """Von Hand kopierte Sammlungen bringen oft diesen Namen mit.

        ``has_cover`` prüft fünf Namen. Wer beim Ausliefern nur ``cover.jpg``
        sucht, zeigt bei genau diesen Büchern ein kaputtes Bild -- die Liste
        führt sie ja als „hat Cover".
        """
        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)
        (buch / "folder.jpg").write_bytes(self.JPEG)

        antwort = client.get("/audiobook/cover?buch=Autor/Buch")
        assert antwort.status_code == 200
        assert antwort.content == self.JPEG

    def test_ohne_cover_gibt_es_404(self, client, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)
        assert client.get("/audiobook/cover?buch=Autor/Buch").status_code == 404

    def test_cover_fuehrt_nicht_aus_der_bibliothek(self, client, bibliothek):
        """Ein Endpunkt, der eine Datei zu einem Pfad liefert, ist das Ziel."""
        (bibliothek / "Autor").mkdir(parents=True)
        for versuch in ("../../etc/passwd", "/etc/passwd", "Autor/../../geheim"):
            antwort = client.get("/audiobook/cover", params={"buch": versuch})
            assert antwort.status_code == 404, versuch

    def test_liste_zeigt_das_cover_mit_cache_schluessel(self, client, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        (buch / "cover.jpg").write_bytes(self.JPEG)

        html = client.get("/audiobook").text
        assert "/audiobook/cover?buch=Autor/Buch" in html
        # Ohne die Änderungszeit in der Adresse bliebe ein neues Cover
        # unsichtbar, weil der Browser das alte behalten darf.
        import re
        assert re.search(r"/audiobook/cover\?buch=Autor/Buch&amp;v=\d{9,}", html), html[:600]

    def test_buch_ohne_cover_bekommt_einen_platzhalter(self, client, bibliothek):
        """Sonst wären die Zeilen unterschiedlich hoch."""
        buch = bibliothek / "Autor" / "Ohne"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        html = client.get("/audiobook").text
        assert "cover-mini leer" in html
        assert "/audiobook/cover?buch=Autor/Ohne" not in html

    def test_musik_cover_landet_in_der_session(self, client):
        session = sessions.create_session()
        (session.directory / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        response = client.post(
            f"/cover/session/{session.session_id}",
            files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")},
        )
        assert response.status_code == 200
        # beets zieht genau diesen Namen über fetchart heran.
        assert (session.directory / "cover.jpg").read_bytes() == self.JPEG

    def test_musikseite_bietet_cover_knopf_fuer_die_session(self, client):
        session = sessions.create_session()
        (session.directory / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        html = client.get(f"/session/{session.session_id}").text
        assert "coverAufnehmen(" in html
        assert f"/cover/session/{session.session_id}" in html
        assert "Cover fotografieren" in html

    def test_cover_wird_fuer_die_session_vorschau_ausgeliefert(self, client):
        session = sessions.create_session()
        (session.directory / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        (session.directory / "cover.jpg").write_bytes(self.JPEG)

        antwort = client.get(f"/cover/session/{session.session_id}?v=123")
        assert antwort.status_code == 200
        assert antwort.content == self.JPEG
        assert "immutable" in antwort.headers["cache-control"]

    def test_musikseite_zeigt_cover_vorschau_mit_cache_schluessel(self, client):
        session = sessions.create_session()
        (session.directory / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        (session.directory / "cover.jpg").write_bytes(self.JPEG)

        html = client.get(f"/session/{session.session_id}").text
        assert f"/cover/session/{session.session_id}" in html
        assert "Cover neu fotografieren" in html
        import re
        assert re.search(rf"/cover/session/{session.session_id}\?v=\d{{9,}}", html), html

    def test_kein_bild_wird_abgelehnt(self, client, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        buch.mkdir(parents=True)

        response = client.post(
            "/cover/audiobook?buch=Autor/Buch",
            files={"bild": ("cover.jpg", b"<?php echo 1; ?>", "image/jpeg")},
        )
        assert "nicht nach einem Bild" in response.text
        assert not (buch / "cover.jpg").exists()

    def test_fremder_buchpfad(self, client):
        response = client.post(
            "/cover/audiobook?buch=../../etc",
            files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")},
        )
        assert "nicht zur Bibliothek" in response.text or "gibt es nicht" in response.text

    def test_unbekannte_session(self, client):
        response = client.post(
            "/cover/session/AAAAAAAAAAAAAAAAAA",
            files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")},
        )
        assert response.status_code == 404

    def test_knopf_erscheint_je_buch(self, client, bibliothek):
        buch = bibliothek / "Autor" / "Buch"
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        html = client.get("/audiobook").text
        assert "coverAufnehmen(" in html
        assert "/cover/audiobook?buch=Autor/Buch" in html


class TestManuellTaggen:
    """Von Hand taggen, auch für Sampler."""

    def _session_mit(self, namen):
        from tests.flacfixture import write_flac

        session = sessions.create_session()
        for name in namen:
            write_flac(session.directory / name, seconds=5)
        return session

    def test_dateiliste_bietet_direkten_einstieg_ohne_manuelles_formular(self, client):
        """Das Handtagging-Formular gehörte früher fest zu Schritt 2 (_files.html)
        UND zu Schritt 3 (_candidates.html) -- mit identischen Element-IDs
        gleichzeitig im DOM, sobald beide Schritte sichtbar waren. Schritt 2
        bietet jetzt nur noch den Einstieg, das Formular selbst lebt nur noch
        in Schritt 3."""
        session = self._session_mit(["01.flac", "02.flac"])
        html = client.get(f"/session/{session.session_id}").text

        assert "Tags selbst setzen" not in html
        assert 'name="titel:01.flac"' not in html
        assert 'hx-post="/manual-start/' in html
        assert "Ohne MusicBrainz von Hand taggen" in html

    def test_manual_start_liefert_das_formular_ohne_musicbrainz_anfrage(self, client, monkeypatch):
        from backend import artist_ids

        def keine_suche(*a, **k):
            raise AssertionError("MusicBrainz hätte hier nicht angefragt werden dürfen")

        monkeypatch.setattr(artist_ids, "search", keine_suche)

        session = self._session_mit(["01.flac", "02.flac"])
        response = client.post(f"/manual-start/{session.session_id}")
        html = response.text

        assert response.status_code == 200
        assert "von Hand taggen" in html
        assert 'name="titel:01.flac"' in html
        assert 'name="interpret:02.flac"' in html
        assert 'name="compilation"' in html
        assert "data-genre-feld" in html
        assert "Beim Tippen erscheinen Vorschläge aus dem Projektkatalog" in html
        assert 'Artist-ID in die Datei' in html
        assert 'Track-Künstler' in html
        assert 'hx-post="/artist-match/' in html
        assert "keydown[key==&#39;Enter&#39;]" in html or "keydown[key=='Enter']" in html
        assert 'hx-include="closest [data-manual-form]"' in html
        assert 'Noch kein MusicBrainz-Match ausgewählt.' in html

    def test_sampler_bekommt_je_track_einen_interpreten(self, client):
        import mediafile

        session = self._session_mit(["01.flac", "02.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Various Artists",
                "album": "Sampler 99",
                "compilation": "true",
                "genre": "Jazz",
                "year": "1999",
                "titel:01.flac": "Erstes",
                "interpret:01.flac": "Miles Davis",
                "titel:02.flac": "Zweites",
                "interpret:02.flac": "Bill Evans feat. Scott LaFaro",
            },
        )

        erste = mediafile.MediaFile(session.directory / "01.flac")
        zweite = mediafile.MediaFile(session.directory / "02.flac")
        assert erste.title == "Erstes" and erste.artist == "Miles Davis"
        assert zweite.title == "Zweites"
        # "feat." wird zu zwei Einträgen -- Navidrome liest genau das.
        assert zweite.artists == ["Bill Evans", "Scott LaFaro"]
        assert erste.albumartist == "Various Artists"
        assert erste.comp is True

    def test_komponist_je_track_ueberschreibt_das_albumweite_feld(self, client):
        """Klassik-Compilation: das albumweite Komponisten-Feld bleibt leer,
        weil kein einzelner Komponist fürs ganze Album stimmt -- stattdessen
        trägt jede Zeile ihren eigenen ein."""
        import mediafile

        session = self._session_mit(["01.flac", "02.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Various Artists",
                "album": "Klassik-Sampler",
                "compilation": "true",
                "genre": "Klassik",
                "year": "2001",
                "titel:01.flac": "Erstes",
                "interpret:01.flac": "Berliner Philharmoniker",
                "komponist:01.flac": "Johann Sebastian Bach",
                "titel:02.flac": "Zweites",
                "interpret:02.flac": "Wiener Philharmoniker",
                "komponist:02.flac": "Bach; Vivaldi",
            },
        )

        erste = mediafile.MediaFile(session.directory / "01.flac")
        zweite = mediafile.MediaFile(session.directory / "02.flac")
        assert erste.composer == "Johann Sebastian Bach"
        assert zweite.composers == ["Bach", "Vivaldi"]

    def test_manuell_korrigierte_tracknummer_wird_uebernommen(self, client):
        """"Nr." in der Tabelle überschreibt die sonst aus der Position
        abgeleitete Tracknummer -- wichtig, wenn die Dateireihenfolge nicht
        der tatsächlichen Trackreihenfolge entspricht."""
        import mediafile

        session = self._session_mit(["01.flac", "02.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Can",
                "genre": "Krautrock",
                "year": "1971",
                "nr:01.flac": "5",
                "nr:02.flac": "3",
            },
        )

        erste = mediafile.MediaFile(session.directory / "01.flac")
        zweite = mediafile.MediaFile(session.directory / "02.flac")
        assert erste.track == 5
        assert zweite.track == 3
        # "Track 5 von 2" wäre in sich widersprüchlich: sobald irgendeine
        # Nummer von Hand korrigiert wurde, sagt die Dateianzahl dieser
        # Sitzung nichts mehr verlässlich über die Gesamtzahl der Tracks aus.
        assert not erste.tracktotal
        assert not zweite.tracktotal

    def test_ohne_manuelle_nummer_zaehlt_weiter_die_position(self, client):
        import mediafile

        session = self._session_mit(["01.flac", "02.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={"albumartist": "Can", "genre": "Krautrock", "year": "1971"},
        )

        erste = mediafile.MediaFile(session.directory / "01.flac")
        zweite = mediafile.MediaFile(session.directory / "02.flac")
        assert erste.track == 1
        assert zweite.track == 2
        assert erste.tracktotal == 2
        assert zweite.tracktotal == 2

    def test_genre_kommt_jetzt_auch_mehrfach_an(self, client):
        """Vorher verschwand es: beets 2.x kennt nur 'genres'."""
        import mediafile

        session = self._session_mit(["01.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "genre": "Krautrock; Psychedelic Rock",
                "albumartist": "Can",
                "year": "1971",
            },
        )

        medien = mediafile.MediaFile(session.directory / "01.flac")
        # mediafile liefert im einfachen Feld nur den ersten Eintrag zurück;
        # die eigentliche Mehrfachliste steht in ``genres``.
        assert medien.genre == "Krautrock"
        assert medien.genres == ["Krautrock", "Psychedelic Rock"]

    def test_komponist_landet_getrennt_vom_interpreten_in_der_datei(self, client):
        """Klassik: Chor/Orchester steht bei Künstler, der Komponist separat."""
        import mediafile

        session = self._session_mit(["01.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Windsbacher Knabenchor",
                "album": "Nun singet und seid froh",
                "composer": "Johann Sebastian Bach",
                "genre": "Chormusik",
                "year": "1985",
            },
        )

        medien = mediafile.MediaFile(session.directory / "01.flac")
        assert medien.albumartist == "Windsbacher Knabenchor"
        assert medien.composer == "Johann Sebastian Bach"
        assert medien.composers == ["Johann Sebastian Bach"]

    def test_restlicher_katalog_landet_ebenfalls_in_der_datei(self, client):
        """Label, Katalognummer & Co. kamen früher gar nicht am Formular an --
        jetzt reicht der Formularname direkt dem Katalog-Schlüssel (siehe
        ``_MANUAL_ALBUM_BASIS_ZUSATZ``/``_MANUAL_ALBUM_ERWEITERT`` in
        routes.py), keine eigene Übersetzung nötig. ``albumtypes`` ist
        mehrwertig -- prüft gleich mit, dass die Einzelform (``albumtype``)
        automatisch mitgeschrieben wird."""
        import mediafile

        session = self._session_mit(["01.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Can",
                "album": "Tago Mago",
                "genre": "Krautrock",
                "year": "1971",
                "label": "United Artists",
                "catalognum": "UAS 29 211/12",
                "country": "DE",
                "disctotal": "2",
                "albumtypes": "album; live",
                "barcode": "042284226626",
            },
        )

        medien = mediafile.MediaFile(session.directory / "01.flac")
        assert medien.label == "United Artists"
        assert medien.catalognum == "UAS 29 211/12"
        assert medien.country == "DE"
        assert medien.disctotal == 2
        assert medien.albumtypes == ["album", "live"]
        assert medien.albumtype == "album"
        assert medien.barcode == "042284226626"

    def test_mehrere_komponisten_werden_aufgetrennt(self, client):
        import mediafile

        session = self._session_mit(["01.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "composer": "Johann Sebastian Bach; Georg Friedrich Händel",
                "albumartist": "Windsbacher Knabenchor",
                "genre": "Chormusik",
                "year": "1985",
            },
        )

        medien = mediafile.MediaFile(session.directory / "01.flac")
        assert medien.composer == "Johann Sebastian Bach"
        assert medien.composers == ["Johann Sebastian Bach", "Georg Friedrich Händel"]

    def test_manuell_gewaehlte_artist_ids_werden_uebernommen(self, client):
        import mediafile

        session = self._session_mit(["01.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Alligatoah",
                "mb_albumartistids": "bde41239-2535-4b28-b6e5-1074f990a14a",
                "artist": "Alligatoah",
                "mb_artistids": "bde41239-2535-4b28-b6e5-1074f990a14a",
                "album": "Mein lokales Album",
                "genre": "Deutschrap",
                "year": "2015",
            },
        )

        medien = mediafile.MediaFile(session.directory / "01.flac")
        assert medien.albumartist == "Alligatoah"
        assert medien.artist == "Alligatoah"
        assert medien.mb_albumartistids == ["bde41239-2535-4b28-b6e5-1074f990a14a"]
        assert medien.mb_artistids == ["bde41239-2535-4b28-b6e5-1074f990a14a"]

    def test_artist_match_liefert_vorschlaege(self, client, monkeypatch):
        from backend import routes
        from backend.artist_ids import ArtistMatch

        session = self._session_mit(["01.flac"])
        monkeypatch.setattr(
            routes.artist_ids,
            "search",
            lambda name: (
                ArtistMatch(
                    name="Alligatoah",
                    mbid="bde41239-2535-4b28-b6e5-1074f990a14a",
                    disambiguation="Rapper aus Deutschland",
                    area="Deutschland",
                    kind="Person",
                    exact=True,
                ),
            ),
        )

        response = client.post(
            f"/artist-match/{session.session_id}",
            data={"field": "artist", "artist": "Alligatoah"},
        )
        assert response.status_code == 200
        assert "Eindeutiger MusicBrainz-Treffer gefunden" in response.text
        assert "Alligatoah" in response.text
        assert "bde41239-2535-4b28-b6e5-1074f990a14a" in response.text
        assert 'data-artist-choose' in response.text

    def test_artist_match_trennt_mehrere_namen_getrennt_auf(self, client, monkeypatch):
        """``Artist A / Artist B`` (z. B. Chor + Dirigent) soll nicht als ein
        einziger String bei MusicBrainz gesucht werden -- das fände so gut
        wie nie einen Treffer -- sondern als zwei getrennte Suchen mit je
        eigener Auswahl."""
        from backend import routes
        from backend.artist_ids import ArtistMatch

        gesuchte_namen: list[str] = []

        def stub(name, **kwargs):
            gesuchte_namen.append(name)
            return (
                ArtistMatch(name=name, mbid=f"mbid-{name}", exact=True),
            )

        monkeypatch.setattr(routes.artist_ids, "search", stub)

        session = self._session_mit(["01.flac"])
        response = client.post(
            f"/artist-match/{session.session_id}",
            data={"field": "albumartist", "albumartist": "Windsbacher Knabenchor / Karl-Friedrich Beringer"},
        )
        assert response.status_code == 200
        assert gesuchte_namen == ["Windsbacher Knabenchor", "Karl-Friedrich Beringer"]
        assert "2 Künstler erkannt" in response.text
        assert 'data-name-slot="0"' in response.text
        assert 'data-name-slot="1"' in response.text
        assert "Windsbacher Knabenchor" in response.text
        assert "Karl-Friedrich Beringer" in response.text

    def test_artist_match_funktioniert_auch_je_track(self, client, monkeypatch):
        """Bisher lief die MusicBrainz-Zuordnung für Track-Künstler in der
        Tabelle "Titel je Track" nur still beim Schreiben mit -- ohne dass der
        Nutzer sie je zu sehen bekam. Die Lupe je Zeile nutzt denselben
        Lookup-Endpunkt wie beim albumweiten Feld, nur mit einem
        "interpret:<Dateiname>"-Feldnamen statt "artist"."""
        from backend import routes
        from backend.artist_ids import ArtistMatch

        session = self._session_mit(["01.flac", "02.flac"])
        monkeypatch.setattr(
            routes.artist_ids,
            "search",
            lambda name: (
                ArtistMatch(name="Bill Evans", mbid="5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5", exact=True),
            ),
        )

        response = client.post(
            f"/artist-match/{session.session_id}",
            data={"field": "interpret:02.flac", "interpret:02.flac": "Bill Evans"},
        )
        assert response.status_code == 200
        assert "Eindeutiger MusicBrainz-Treffer gefunden" in response.text
        assert "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5" in response.text
        assert 'data-field="interpret:02.flac"' in response.text

    def test_bestaetigte_artist_id_gilt_nur_fuer_diesen_track(self, client, monkeypatch):
        """Die Lupe-Suche schreibt die gewählte Artist-ID über
        "mbinterpret:<Dateiname>" -- nur für die Zeile, in der sie bestätigt
        wurde. Ein anderer Track mit demselben Interpretennamen, aber ohne
        eigene Bestätigung, bleibt vom stillen Exakt-Treffer-Abgleich beim
        Schreiben unberührt (hier: kein Treffer, also keine ID)."""
        import mediafile

        from backend import tagging

        monkeypatch.setattr(tagging.artist_ids, "lookup_exact", lambda name, **kwargs: None)

        session = self._session_mit(["01.flac", "02.flac"])
        client.post(
            f"/manual/{session.session_id}",
            data={
                "albumartist": "Various Artists",
                "compilation": "true",
                "genre": "Jazz",
                "year": "1965",
                "titel:01.flac": "Erstes",
                "interpret:01.flac": "Bill Evans",
                "mbinterpret:01.flac": "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5",
                "titel:02.flac": "Zweites",
                "interpret:02.flac": "Bill Evans",
            },
        )

        erste = mediafile.MediaFile(session.directory / "01.flac")
        zweite = mediafile.MediaFile(session.directory / "02.flac")
        assert erste.mb_artistids == ["5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5"]
        # lookup_exact liefert absichtlich nichts zurück -- ohne eigene
        # Bestätigung bleibt der zweite Track ohne Artist-ID.
        assert zweite.mb_artistids in (None, [])

    def test_ohne_eingabe_wird_nichts_geschrieben(self, client):
        session = self._session_mit(["01.flac"])
        response = client.post(f"/manual/{session.session_id}", data={})
        assert "Genre" in response.text
        assert "Jahr" in response.text
        assert "Albumkünstler" in response.text

    def test_sampler_ohne_track_kuenstler_wird_zurueckgewiesen(self, client):
        """Bei einem Sampler ist "Various Artists" beim Albumkünstler nur ein
        Platzhalter -- ohne MusicBrainz-Treffer zählt dann jede Zeile in
        "Titel je Track" für sich."""
        session = self._session_mit(["01.flac", "02.flac"])
        response = client.post(
            f"/manual/{session.session_id}",
            data={
                "compilation": "true",
                "genre": "Jazz",
                "year": "1999",
                "interpret:01.flac": "Miles Davis",
                # 02.flac bleibt absichtlich ohne Track-Künstler.
            },
        )
        assert "02.flac" in response.text
        assert "Track-Künstler" in response.text

        import mediafile

        medien = mediafile.MediaFile(session.directory / "02.flac")
        assert medien.title is None


class TestEntwurfWiederherstellen:
    """Halb ausgefüllte Tagging-Felder überleben eine unterbrochene Sitzung.

    Getestet wird die reine HTTP-Seite (Feldwerte im ausgelieferten HTML) --
    dass ein Browser das Formular tatsächlich per Debounce zwischendurch
    abschickt, prüft test_mobil.py mit einem echten Browser.
    """

    def _session_mit(self, namen):
        from tests.flacfixture import write_flac

        session = sessions.create_session()
        for name in namen:
            write_flac(session.directory / name, seconds=5)
        return session

    def test_gespeicherter_entwurf_taucht_beim_naechsten_laden_wieder_auf(self, client):
        session = self._session_mit(["01.flac"])
        client.post(
            f"/entwurf/{session.session_id}",
            data={
                "albumartist": "Windsbacher Knabenchor",
                "album": "Nun singet und seid froh",
                "year": "1985",
                "genre": "Chormusik",
                "compilation": "true",
                "titel:01.flac": "Stille Nacht",
                "komponist:01.flac": "Franz Gruber",
                "interpret:01.flac": "Windsbacher Knabenchor",
                "mbinterpret:01.flac": "3079b492-4324-4894-83b0-e0b19d59b2ca",
            },
        )

        html = client.get(f"/session/{session.session_id}").text
        assert 'value="Windsbacher Knabenchor"' in html
        assert 'value="Nun singet und seid froh"' in html
        assert 'value="1985"' in html
        assert 'value="Chormusik"' in html
        assert 'name="komponist:01.flac" value="Franz Gruber"' in html
        assert 'name="compilation" value="true" data-sampler checked' in html
        assert 'name="titel:01.flac" value="Stille Nacht"' in html
        # Die im Entwurf gemerkte Artist-ID kommt mit zurück, zusammen mit dem
        # Namen, zu dem sie gehört -- verwirft das Browser-Skript die ID
        # selbst, sobald der Name in dieser Zeile künftig abweicht.
        assert 'name="mbinterpret:01.flac" data-artist-mbid="interpret:01.flac"' in html
        assert 'value="3079b492-4324-4894-83b0-e0b19d59b2ca"' in html
        assert 'data-selected-name="Windsbacher Knabenchor"' in html

    def test_entwurf_ueberschreiben_nimmt_eine_veraltete_artist_id_mit(self, client):
        """``save_draft`` ersetzt den gespeicherten Stand komplett (siehe
        dessen Docstring) statt einzelne Felder zu mischen. Tippt ein zweites
        Gerät in derselben Zeile einen neuen Namen und sichert, ohne je die ID
        des ersten Geräts gesehen zu haben, verschwindet dessen
        "mbinterpret:"-Eintrag also mit -- es bleibt kein Rest einer Zuordnung
        stehen, die zum neuen Namen gar nicht mehr passt."""
        session = self._session_mit(["01.flac"])
        client.post(
            f"/entwurf/{session.session_id}",
            data={
                "interpret:01.flac": "Bill Evans",
                "mbinterpret:01.flac": "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5",
            },
        )
        client.post(
            f"/entwurf/{session.session_id}",
            data={"interpret:01.flac": "Bill Evans Trio"},
        )

        html = client.get(f"/session/{session.session_id}").text
        assert 'value="Bill Evans Trio"' in html
        assert "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5" not in html

    def test_formular_bietet_refresh_button_fuer_geraetewechsel(self, client):
        """Wer zwischen Handy und PC wechselt, soll den zuletzt gesicherten
        Entwurf direkt aus dem Formular heraus nachladen können, statt über
        das Sitzungen-Dropdown zu müssen."""
        session = self._session_mit(["01.flac"])
        formular = client.post(f"/manual-start/{session.session_id}").text
        assert "Entwurf vom Server laden" in formular
        refresh_start = formular.index("Entwurf vom Server laden")
        knopf = formular[max(0, refresh_start - 700):refresh_start]
        assert f'hx-post="/manual-start/{session.session_id}"' in knopf
        assert 'hx-target="#candidates"' in knopf
        assert "event.stopPropagation()" in knopf

    def test_ohne_entwurf_bleibt_das_formular_beim_fortsetzen_zu(self, client):
        """Ohne Entwurf gibt es nichts zum Wiederherstellen -- Schritt 3 bleibt
        beim Fortsetzen zu, statt ungefragt ein leeres Formular aufzuklappen."""
        session = self._session_mit(["01.flac"])
        html = client.get(f"/session/{session.session_id}").text
        assert "hx-swap-oob" not in html
        assert 'value="Windsbacher' not in html

        formular = client.post(f"/manual-start/{session.session_id}").text
        assert 'name="albumartist" data-albumartist data-artist-field="albumartist"' in formular
        assert 'value="Windsbacher' not in formular

    def test_geschriebene_tags_raeumen_den_entwurf_weg(self, client):
        session = self._session_mit(["01.flac"])
        client.post(f"/entwurf/{session.session_id}", data={"album": "Zwischenstand"})
        assert sessions.load_draft(session) == {"album": "Zwischenstand"}

        client.post(
            f"/manual/{session.session_id}",
            data={"album": "Endgültig", "albumartist": "Jemand", "genre": "Pop", "year": "2020"},
        )
        assert sessions.load_draft(session) == {}

    def test_matches_suchen_wischt_den_entwurf_nicht_weg(self, client, monkeypatch):
        """_candidates.html bindet _manual.html mit ein. Ohne den Entwurf beim
        /match-Rendern wären die schon eingetippten Felder nach einem Klick
        auf "Matches suchen" plötzlich leer -- und der nächste Autosave-Tick
        hätte diese Leere sogar in den Entwurf zurückgeschrieben."""
        from backend import matching

        session = self._session_mit(["01.flac"])
        client.post(
            f"/entwurf/{session.session_id}",
            data={"albumartist": "Windsbacher Knabenchor", "year": "1985"},
        )

        monkeypatch.setattr(
            matching,
            "find_candidates",
            lambda *a, **k: matching.MatchResult(
                current_artist="", current_album="", recommendation="none"
            ),
        )
        html = client.post(f"/match/{session.session_id}", data={}).text

        assert 'value="Windsbacher Knabenchor"' in html
        assert 'value="1985"' in html

    def test_parser_lauf_sichert_sofort_den_entwurf(self, client):
        """Ohne das hier stand ein erkannter Text erst im Entwurf, sobald
        danach noch irgendwo getippt wurde -- der htmx-Swap nach "Parser
        anwenden" löst das normale (tippbasierte) Autosave nicht aus."""
        session = self._session_mit(["01.flac"])
        response = client.post(
            f"/ocr/parse/{session.session_id}",
            data={
                "ocr_text": "01 Interpret - Titel",
                "tracknummer": "true",
                "feld1": "interpret",
                "feld2": "titel",
            },
        )
        assert response.status_code == 200

        draft = sessions.load_draft(session)
        assert draft["ocr_text"] == "01 Interpret - Titel"
        assert draft["tracknummer"] == "true"
        assert draft["feld1"] == "interpret"
        assert draft["feld2"] == "titel"
        assert "dauer" not in draft
        assert draft["titel:01.flac"] == "Titel"
        assert draft["interpret:01.flac"] == "Interpret"
        assert draft["nr:01.flac"] == "01"

    def test_parser_naechste_zeile_paart_titel_und_interpret(self, client):
        session = self._session_mit(["01.flac", "02.flac"])
        response = client.post(
            f"/ocr/parse/{session.session_id}",
            data={
                "ocr_text": "The Earl of Oxfords March\nWilliam Byrd\nFive Pieces\nAnthony Holborn",
                "feld1": "titel",
                "feld2": "interpret",
                "zeilenweise": "true",
            },
        )
        assert response.status_code == 200

        draft = sessions.load_draft(session)
        assert draft["feld1"] == "titel"
        assert draft["feld2"] == "interpret"
        assert draft["zeilenweise"] == "true"
        assert draft["titel:01.flac"] == "The Earl of Oxfords March"
        assert draft["interpret:01.flac"] == "William Byrd"
        assert draft["titel:02.flac"] == "Five Pieces"
        assert draft["interpret:02.flac"] == "Anthony Holborn"

    def test_parser_lauf_verwirft_artist_id_bei_geaendertem_namen(self, client):
        """Zeile 1 hatte schon eine per Lupe bestätigte Artist-ID im Entwurf.
        Läuft der Parser danach erneut und liefert für dieselbe Zeile einen
        anderen Namen, überschreibt er "interpret:01.flac" -- die alte
        Artist-ID galt fürs alte Wort und darf nicht daran kleben bleiben."""
        session = self._session_mit(["01.flac"])
        client.post(
            f"/entwurf/{session.session_id}",
            data={
                "interpret:01.flac": "William Byrd",
                "mbinterpret:01.flac": "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5",
            },
        )

        client.post(
            f"/ocr/parse/{session.session_id}",
            data={
                "ocr_text": "01 Anthony Holborn - Titel",
                "tracknummer": "true",
                "feld1": "interpret",
                "feld2": "titel",
            },
        )

        draft = sessions.load_draft(session)
        assert draft["interpret:01.flac"] == "Anthony Holborn"
        assert "mbinterpret:01.flac" not in draft

    def test_parser_lauf_behaelt_artist_id_bei_gleichem_namen(self, client):
        """Liefert der Parser für dieselbe Zeile denselben Namen wie schon im
        Entwurf, bleibt die dazu bestätigte Artist-ID stehen -- ein erneuter
        Parser-Lauf (z. B. nach einem neuen Foto mit demselben Text) darf eine
        gültige Zuordnung nicht grundlos wegwerfen."""
        session = self._session_mit(["01.flac"])
        client.post(
            f"/entwurf/{session.session_id}",
            data={
                "interpret:01.flac": "William Byrd",
                "mbinterpret:01.flac": "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5",
            },
        )

        client.post(
            f"/ocr/parse/{session.session_id}",
            data={
                "ocr_text": "01 William Byrd - Titel",
                "tracknummer": "true",
                "feld1": "interpret",
                "feld2": "titel",
            },
        )

        draft = sessions.load_draft(session)
        assert draft["interpret:01.flac"] == "William Byrd"
        assert draft["mbinterpret:01.flac"] == "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5"

    def test_datei_upload_wird_beim_entwurf_ignoriert(self, client):
        """``hx-params=\"not bild\"`` verhindert, dass jeder Autosave-Tick das
        OCR-Backcoverfoto erneut mitschickt -- diese Route soll trotzdem nicht
        daran scheitern, falls ein Client es doch schickt."""
        session = self._session_mit(["01.flac"])
        response = client.post(
            f"/entwurf/{session.session_id}",
            data={"album": "Etwas"},
            files={"bild": ("cover.jpg", b"\xff\xd8\xff", "image/jpeg")},
        )
        assert response.status_code == 200
        draft = sessions.load_draft(session)
        assert draft.get("album") == "Etwas"


class TestAlbenCover:
    """Cover für ein bereits importiertes Album nachträglich ändern.

    ``beet embedart`` selbst läuft nicht mit -- das Kommando und dessen Sperre
    prüft ``test_albums.py``. Hier zählt nur die HTTP-Seite: Album finden,
    Bild speichern, Fehler sauber melden.
    """

    JPEG = b"\xff\xd8\xff\xe0" + b"x" * 200

    @pytest.fixture
    def album(self, tmp_path, monkeypatch):
        from backend import albums

        ordner = tmp_path / "The Beatles" / "Abbey Road"
        ordner.mkdir(parents=True)
        eintrag = albums.Album(
            id=1, albumartist="The Beatles", album="Abbey Road", year="1969",
            path=ordner,
        )
        monkeypatch.setattr(
            routes.albums, "get_album", lambda album_id: eintrag if album_id == 1 else None
        )
        # Die Detailseite lädt nach jeder Aktion auch die Titelliste; ohne
        # diesen Default würde jeder Cover-Test einen echten beet-Subprozess
        # anstoßen.
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [])
        return eintrag

    def test_seite_laedt_liste_erst_per_htmx_nach(self, client, monkeypatch):
        """``/albums`` selbst darf nicht auf ``list_albums`` warten -- das geht
        über den ``beet``-Subprozess und würde sonst jede Navigation auf diese
        Seite blockieren. Die Liste hängt hinter einem hx-get."""

        def nicht_aufrufen(q=""):
            raise AssertionError("list_albums() darf beim ersten Laden nicht laufen")

        monkeypatch.setattr(routes.albums, "list_albums", nicht_aufrufen)
        response = client.get("/albums")
        assert response.status_code == 200
        assert 'hx-get="/albums/liste"' in response.text
        assert 'hx-trigger="load"' in response.text
        assert 'id="cover-dialog"' in response.text

    def test_liste_haengt_suchbegriff_an_den_hx_get_an(self, client, monkeypatch):
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [])
        response = client.get("/albums?q=Beatles")
        assert response.status_code == 200
        assert 'hx-get="/albums/liste?q=Beatles"' in response.text

    def test_seite_listet_alben(self, client, album, monkeypatch):
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [album])
        response = client.get("/albums/liste")
        assert response.status_code == 200
        assert "Abbey Road" in response.text
        assert "The Beatles" in response.text

    def test_fehler_beim_listen_wird_angezeigt(self, client, monkeypatch):
        from backend import albums

        def kaputt(q=""):
            raise albums.AlbumError("beets antwortet nicht.")

        monkeypatch.setattr(routes.albums, "list_albums", kaputt)
        response = client.get("/albums/liste")
        assert response.status_code == 200
        assert "beets antwortet nicht." in response.text

    def test_ohne_cover_gibt_es_404(self, client, album):
        assert client.get("/cover/album/1").status_code == 404

    def test_unbekanntes_album_gibt_es_404(self, client, album):
        assert client.get("/cover/album/999").status_code == 404

    def test_vorhandenes_cover_wird_ausgeliefert(self, client, album):
        (album.path / "cover.jpg").write_bytes(self.JPEG)
        antwort = client.get("/cover/album/1?v=123")
        assert antwort.status_code == 200
        assert antwort.content == self.JPEG
        assert "immutable" in antwort.headers["cache-control"]

    def test_neues_cover_landet_im_albumordner_und_wird_eingebettet(
        self, client, album, monkeypatch
    ):
        aufrufe = []
        monkeypatch.setattr(
            routes.albums, "update_cover", lambda a, pfad: aufrufe.append((a.id, pfad))
        )

        response = client.post(
            "/cover/album/1", files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")}
        )
        assert response.status_code == 200
        assert (album.path / "cover.jpg").read_bytes() == self.JPEG
        assert aufrufe == [(1, album.path / "cover.jpg")]
        # Die Antwort ist das Detail-Fragment des Albums, nicht die Liste --
        # der Dialog ersetzt #album-detail auf der Detailseite.
        assert "Abbey Road" in response.text

    def test_unbekanntes_album_beim_hochladen_gibt_es_404(self, client, album):
        response = client.post(
            "/cover/album/999", files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")}
        )
        assert response.status_code == 404

    def test_kaputtes_bild_wird_gemeldet_ohne_einzubetten(
        self, client, album, monkeypatch
    ):
        aufrufe = []
        monkeypatch.setattr(
            routes.albums, "update_cover", lambda a, pfad: aufrufe.append((a.id, pfad))
        )

        response = client.post(
            "/cover/album/1",
            files={"bild": ("cover.jpg", b"kein bild", "image/jpeg")},
        )
        assert response.status_code == 200
        assert "sieht nicht nach einem Bild aus" in response.text
        assert not aufrufe, "bei einem unbrauchbaren Bild darf nichts eingebettet werden"

    def test_fehlschlag_beim_einbetten_wird_gemeldet(self, client, album, monkeypatch):
        from backend import albums

        def kaputt(a, pfad):
            raise albums.AlbumError("Das Cover ließ sich nicht einbetten.")

        monkeypatch.setattr(routes.albums, "update_cover", kaputt)

        response = client.post(
            "/cover/album/1", files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")}
        )
        assert response.status_code == 200
        assert "Das Cover ließ sich nicht einbetten." in response.text
        # Das Bild liegt trotzdem schon im Ordner -- nichts geht verloren.
        assert (album.path / "cover.jpg").read_bytes() == self.JPEG


class TestAlbenArtistMbid:
    """Album- und Track-Interpret nachträglich mit MusicBrainz verknüpfen.

    Die eigentlichen ``beet modify``-Aufrufe prüft ``test_albums.py``. Hier
    zählt nur die HTTP-Seite: Suche anzeigen, Auswahl übernehmen, Fehler
    melden -- für Album und Track getrennt, weil es getrennte Endpunkte sind.
    """

    from backend.artist_ids import ArtistMatch

    MATCH = ArtistMatch(
        name="The Beatles",
        mbid="b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d",
        exact=True,
    )

    @pytest.fixture
    def album(self, tmp_path, monkeypatch):
        from backend import albums

        ordner = tmp_path / "The Beatles" / "Abbey Road"
        ordner.mkdir(parents=True)
        eintrag = albums.Album(
            id=1, albumartist="The Beatles", album="Abbey Road", year="1969",
            path=ordner, genres="Rock", label="Apple",
        )
        monkeypatch.setattr(
            routes.albums, "get_album", lambda album_id: eintrag if album_id == 1 else None
        )
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [eintrag])
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [])
        return eintrag

    def test_detailseite_zeigt_album_tags(self, client, album):
        response = client.get("/albums/1")
        assert response.status_code == 200
        assert "The Beatles" in response.text
        assert "Abbey Road" in response.text
        assert "Rock" in response.text
        assert "Apple" in response.text
        assert "MB-Link fixen" in response.text
        assert 'id="album-detail"' in response.text

    def test_detailseite_unbekanntes_album_gibt_404(self, client, album):
        assert client.get("/albums/999").status_code == 404

    def test_detailseite_zeigt_titel_und_deren_mb_status(self, client, album, monkeypatch):
        from backend import albums

        offen = albums.Track(id=10, track="01", title="Come Together", artist="The Beatles")
        verknuepft = albums.Track(
            id=11, track="02", title="Something", artist="The Beatles",
            mb_artistid=self.MATCH.mbid,
        )
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [offen, verknuepft])
        response = client.get("/albums/1")
        assert "Come Together" in response.text
        assert "Something" in response.text
        assert "mb-badge" in response.text  # Something hat den Link schon
        assert 'artist-lookup' in response.text  # Come Together bekommt die Suche

    def test_lupe_traegt_position_pro_interpret(self, client, album, monkeypatch):
        # Der gemeinsame Such-Dialog (mbSucheOeffnen in static/index.js) bekommt
        # den Suchpfad -- inklusive Position -- erst beim Klick über
        # data-lookup-pfad. Bei mehreren Interpreten ("A feat. B") muss jede
        # Lupe weiterhin ihre eigene Position tragen, sonst würde "Übernehmen"
        # im Dialog immer nur den ersten Namen treffen.
        from backend import albums

        mehrfach = albums.Track(
            id=10, track="01", title="...", artist="Bill Evans feat. Scott LaFaro"
        )
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [mehrfach])
        response = client.get("/albums/1")
        assert 'data-lookup-pfad="/albums/1/tracks/10/artist-lookup/0"' in response.text
        assert 'data-lookup-pfad="/albums/1/tracks/10/artist-lookup/1"' in response.text
        assert 'data-lookup-pfad="/albums/1/artist-lookup/0"' in response.text

    def test_album_suche_zeigt_treffer(self, client, album, monkeypatch):
        monkeypatch.setattr(routes.artist_ids, "search", lambda name: (self.MATCH,))
        response = client.post(
            "/albums/1/artist-lookup/0", data={"name": "The Beatles"}
        )
        assert response.status_code == 200
        assert self.MATCH.mbid in response.text
        assert 'hx-post="/albums/1/artist-apply/0"' in response.text
        assert 'hx-target="#album-detail"' in response.text

    def test_album_suche_ohne_treffer(self, client, album, monkeypatch):
        monkeypatch.setattr(routes.artist_ids, "search", lambda name: ())
        response = client.post("/albums/1/artist-lookup/0", data={"name": "Nichts da"})
        assert "Kein MusicBrainz-Treffer" in response.text

    def test_album_uebernehmen_ruft_die_verknuepfung_auf(self, client, album, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_artist_mbid",
            lambda a, index, mbid: aufrufe.append((a.id, index, mbid)),
        )
        response = client.post("/albums/1/artist-apply/0", data={"mbid": self.MATCH.mbid})
        assert response.status_code == 200
        assert aufrufe == [(1, 0, self.MATCH.mbid)]
        # Antwort ist das neu gerenderte Detail-Fragment.
        assert "Abbey Road" in response.text

    def test_album_uebernehmen_unbekanntes_album(self, client, album):
        assert client.post("/albums/999/artist-apply/0", data={"mbid": "x"}).status_code == 404

    def test_album_uebernehmen_meldet_fehlschlag(self, client, album, monkeypatch):
        from backend import albums

        def kaputt(a, index, mbid):
            raise albums.AlbumError("Das Ändern der Tags ist fehlgeschlagen.")

        monkeypatch.setattr(routes.albums, "set_album_artist_mbid", kaputt)
        response = client.post("/albums/1/artist-apply/0", data={"mbid": self.MATCH.mbid})
        assert "Das Ändern der Tags ist fehlgeschlagen." in response.text

    @pytest.fixture
    def track(self):
        from backend import albums

        return albums.Track(id=10, track="01", title="Come Together", artist="The Beatles")

    def test_track_suche_zeigt_treffer(self, client, album, monkeypatch):
        monkeypatch.setattr(routes.artist_ids, "search", lambda name: (self.MATCH,))
        response = client.post(
            "/albums/1/tracks/10/artist-lookup/0", data={"name": "The Beatles"}
        )
        assert response.status_code == 200
        assert self.MATCH.mbid in response.text
        assert 'hx-post="/albums/1/tracks/10/artist-apply/0"' in response.text
        assert 'hx-target="#album-detail"' in response.text

    def test_track_uebernehmen_ruft_die_verknuepfung_auf(self, client, album, track, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: track)
        monkeypatch.setattr(
            routes.albums,
            "set_track_artist_mbid",
            lambda t, index, mbid: aufrufe.append((t.id, index, mbid)),
        )
        response = client.post(
            "/albums/1/tracks/10/artist-apply/0", data={"mbid": self.MATCH.mbid}
        )
        assert response.status_code == 200
        assert aufrufe == [(10, 0, self.MATCH.mbid)]

    def test_track_uebernehmen_unbekannter_titel(self, client, album, monkeypatch):
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: None)
        response = client.post(
            "/albums/1/tracks/999/artist-apply/0", data={"mbid": self.MATCH.mbid}
        )
        assert response.status_code == 404

    def test_track_uebernehmen_meldet_fehlschlag(self, client, album, track, monkeypatch):
        from backend import albums

        def kaputt(t, index, mbid):
            raise albums.AlbumError("Das Ändern der Tags ist fehlgeschlagen.")

        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: track)
        monkeypatch.setattr(routes.albums, "set_track_artist_mbid", kaputt)
        response = client.post(
            "/albums/1/tracks/10/artist-apply/0", data={"mbid": self.MATCH.mbid}
        )
        assert "Das Ändern der Tags ist fehlgeschlagen." in response.text


class TestAlbenBearbeiten:
    """Metadaten eines bereits importierten Albums nachträglich korrigieren --
    gesammelt über die Detailseite /albums/<id> und erst beim "Speichern"
    tatsächlich geschrieben (``POST /albums/<id>/save``), nicht mehr Feld für
    Feld bei jeder Änderung. Die eigentlichen ``beet modify``-Aufrufe prüft
    ``test_albums.py``, hier zählt nur die HTTP-Seite: Dispatch aufs richtige
    Feld/Titel, Künstler-Felder laufen separat, Fehler werden gemeldet -- und
    vor allem: ein Feld, das gar nicht im POST-Body steht, löst keinen
    einzigen Schreibaufruf aus.
    """

    @pytest.fixture
    def album(self, tmp_path, monkeypatch):
        from backend import albums

        ordner = tmp_path / "The Beatles" / "Abbey Road"
        ordner.mkdir(parents=True)
        eintrag = albums.Album(
            id=1, albumartist="The Beatles", album="Abbey Road", year="1969",
            path=ordner, genres="Rock", label="Apple Records",
        )
        monkeypatch.setattr(
            routes.albums, "get_album", lambda album_id: eintrag if album_id == 1 else None
        )
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [eintrag])
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [])
        return eintrag

    def test_detailseite_zeigt_felder_vorbefuellt(self, client, album):
        response = client.get("/albums/1")
        assert 'name="album:album" value="Abbey Road" data-feld-eingabe' in response.text
        assert 'data-original="Abbey Road"' in response.text
        assert 'name="album:genres"' in response.text and 'data-original="Rock"' in response.text
        assert 'name="album:label"' in response.text
        assert 'data-original="Apple Records"' in response.text

    def test_leeres_jahr_wird_nicht_mit_sentinel_vorbefuellt(self, tmp_path, client, monkeypatch):
        """Die eigentliche Logik prüft TestYearEditierbar in test_albums.py --
        hier zählt nur, dass die Seite das rendert, was Album.year_editierbar
        liefert."""
        from backend import albums

        ordner = tmp_path / "X" / "Y"
        ordner.mkdir(parents=True)
        eintrag = albums.Album(id=2, albumartist="X", album="Y", year="0000", path=ordner)
        assert eintrag.year_editierbar == ""
        monkeypatch.setattr(
            routes.albums, "get_album", lambda album_id: eintrag if album_id == 2 else None
        )
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [])
        response = client.get("/albums/2")
        assert 'name="album:year" value=""' in response.text
        assert 'data-original=""' in response.text

    def test_geaendertes_album_feld_wird_geschrieben(self, client, album, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_field",
            lambda a, feld, wert: aufrufe.append((a.id, feld.key, wert)),
        )
        response = client.post(
            "/albums/1/save", data={"album:album": "Abbey Road (Remaster)"}
        )
        assert response.status_code == 200
        assert aufrufe == [(1, "album", "Abbey Road (Remaster)")]

    def test_nicht_gesendetes_feld_wird_nicht_geschrieben(self, client, album, monkeypatch):
        """Die eigentliche neue Garantie: ein Feld, das gar nicht erst im
        POST-Body steht (weil static/index.js es beim Speichern-Klick als
        unverändert herausgefiltert hat), löst keinen Schreibaufruf aus --
        weder auf Album- noch auf Titel-Ebene."""
        album_aufrufe = []
        track_aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_field",
            lambda a, feld, wert: album_aufrufe.append((a.id, feld.key, wert)),
        )
        monkeypatch.setattr(
            routes.albums,
            "set_track_field",
            lambda t, feld, wert: track_aufrufe.append((t.id, feld.key, wert)),
        )
        response = client.post("/albums/1/save", data={})
        assert response.status_code == 200
        assert album_aufrufe == []
        assert track_aufrufe == []

    def test_mehrere_felder_ueber_album_und_titel_in_einem_speichern(
        self, client, album, monkeypatch
    ):
        """Ein Speichern-Klick deckt Album- und mehrere Titel-Felder in einem
        Aufruf ab -- genau das Szenario, für das die Präfix-Konvention
        (``album:``/``track:<id>:``) gebraucht wird."""
        from backend import albums

        titel = albums.Track(id=5, track="01", title="Come Together", artist="The Beatles")
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: titel if track_id == 5 else None)
        album_aufrufe = []
        track_aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_field",
            lambda a, feld, wert: album_aufrufe.append((a.id, feld.key, wert)),
        )
        monkeypatch.setattr(
            routes.albums,
            "set_track_field",
            lambda t, feld, wert: track_aufrufe.append((t.id, feld.key, wert)),
        )
        response = client.post(
            "/albums/1/save",
            data={"album:label": "X", "track:5:title": "Y"},
        )
        assert response.status_code == 200
        assert album_aufrufe == [(1, "label", "X")]
        assert track_aufrufe == [(5, "title", "Y")]

    def test_leerer_wert_wird_durchgereicht(self, client, album, monkeypatch):
        """Anders als früher: ein geleertes Feld löscht den Tag, wird also
        nicht stillschweigend übersprungen -- solange es überhaupt gesendet
        wird (siehe test_nicht_gesendetes_feld_wird_nicht_geschrieben)."""
        aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_field",
            lambda a, feld, wert: aufrufe.append((a.id, feld.key, wert)),
        )
        response = client.post("/albums/1/save", data={"album:label": ""})
        assert response.status_code == 200
        assert aufrufe == [(1, "label", "")]

    def test_kuenstler_feld_laeuft_ueber_set_album_interpret(self, client, album, monkeypatch):
        aufrufe = []
        monkeypatch.setattr(
            routes.albums, "set_album_field",
            lambda *a: (_ for _ in ()).throw(AssertionError("falscher Pfad")),
        )
        monkeypatch.setattr(
            routes.albums, "set_album_interpret",
            lambda a, wert: aufrufe.append((a.id, wert)),
        )
        response = client.post(
            "/albums/1/save", data={"album:albumartists": "Neue Band"}
        )
        assert response.status_code == 200
        assert aufrufe == [(1, "Neue Band")]

    def test_unbekanntes_feld_wird_uebersprungen(self, client, album, monkeypatch):
        """Anders als bei der alten Einzelfeld-Route kein 400 mehr -- ein
        Speichern-Klick trägt viele Felder auf einmal, ein einzelner
        unbekannter Schlüssel soll die übrigen nicht mitreißen."""
        aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_field",
            lambda a, feld, wert: aufrufe.append((a.id, feld.key, wert)),
        )
        response = client.post(
            "/albums/1/save",
            data={"album:nichtimkatalog": "x", "album:label": "Y"},
        )
        assert response.status_code == 200
        assert aufrufe == [(1, "label", "Y")]

    def test_unbekanntes_album_gibt_404(self, client, album):
        response = client.post("/albums/999/save", data={"album:album": "X"})
        assert response.status_code == 404

    def test_fehlschlag_wird_gemeldet(self, client, album, monkeypatch):
        from backend import albums

        def kaputt(a, feld, wert):
            raise albums.AlbumError("Das Ändern der Tags ist fehlgeschlagen.")

        monkeypatch.setattr(routes.albums, "set_album_field", kaputt)
        response = client.post("/albums/1/save", data={"album:album": "X"})
        assert "Das Ändern der Tags ist fehlgeschlagen." in response.text

    def test_track_felder_sind_vorbefuellt(self, client, album, monkeypatch):
        from backend import albums

        titel = albums.Track(id=10, track="01", title="Come Together", artist="The Beatles")
        monkeypatch.setattr(routes.albums, "list_tracks", lambda album_id: [titel])
        response = client.get("/albums/1")
        assert 'name="track:10:title" value="Come Together"' in response.text

    def test_track_geaendertes_feld_wird_geschrieben(self, client, album, monkeypatch):
        from backend import albums

        titel = albums.Track(id=10, track="01", title="Come Together", artist="The Beatles")
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: titel)
        aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_track_field",
            lambda t, feld, wert: aufrufe.append((t.id, feld.key, wert)),
        )
        response = client.post(
            "/albums/1/save", data={"track:10:title": "Neuer Titel"}
        )
        assert response.status_code == 200
        assert aufrufe == [(10, "title", "Neuer Titel")]

    def test_track_kuenstler_feld_laeuft_ueber_set_track_interpret(
        self, client, album, monkeypatch
    ):
        from backend import albums

        titel = albums.Track(id=10, track="01", title="Come Together", artist="The Beatles")
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: titel)
        aufrufe = []
        monkeypatch.setattr(
            routes.albums, "set_track_interpret",
            lambda t, wert: aufrufe.append((t.id, wert)),
        )
        response = client.post(
            "/albums/1/save", data={"track:10:artists": "Jemand anders"}
        )
        assert response.status_code == 200
        assert aufrufe == [(10, "Jemand anders")]

    def test_track_unbekannter_titel_wird_uebersprungen(self, client, album, monkeypatch):
        """Anders als bei der alten Einzelfeld-Route kein 404 -- ein Titel,
        der zwischen Laden und Speichern verschwunden ist, soll die übrigen
        gesendeten Felder nicht blockieren."""
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: None)
        aufrufe = []
        monkeypatch.setattr(
            routes.albums,
            "set_album_field",
            lambda a, feld, wert: aufrufe.append((a.id, feld.key, wert)),
        )
        response = client.post(
            "/albums/1/save",
            data={"track:999:title": "X", "album:label": "Y"},
        )
        assert response.status_code == 200
        assert aufrufe == [(1, "label", "Y")]

    def test_track_fehlschlag_wird_gemeldet(self, client, album, monkeypatch):
        from backend import albums

        titel = albums.Track(id=10, track="01", title="Come Together", artist="The Beatles")
        monkeypatch.setattr(routes.albums, "get_track", lambda track_id: titel)

        def kaputt(t, feld, wert):
            raise albums.AlbumError("Das Ändern der Tags ist fehlgeschlagen.")

        monkeypatch.setattr(routes.albums, "set_track_field", kaputt)
        response = client.post(
            "/albums/1/save", data={"track:10:title": "X"}
        )
        assert "Das Ändern der Tags ist fehlgeschlagen." in response.text
