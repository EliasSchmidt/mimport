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
        assert "Dateien auswählen" in response.text
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
        # Solange gelesen wird, holt sich die Anzeige selbst nach.
        assert "every 2s" in response.text

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
        assert "Unterbrochene Sitzungen" in liste.text
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
        assert "Unterbrochene Sitzungen" in response.text

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
        from backend import audiobook

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

    def test_formular_bietet_je_track_felder(self, client):
        session = self._session_mit(["01.flac", "02.flac"])
        html = client.get(f"/session/{session.session_id}").text

        assert "von Hand taggen" in html
        assert 'name="titel:01.flac"' in html
        assert 'name="interpret:02.flac"' in html
        assert 'name="compilation"' in html
        assert 'list="genre-vorschlaege-' in html
        assert 'mehrere mit <span class="mono">;</span>' in html
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

    def test_genre_kommt_jetzt_auch_mehrfach_an(self, client):
        """Vorher verschwand es: beets 2.x kennt nur 'genres'."""
        import mediafile

        session = self._session_mit(["01.flac"])
        client.post(f"/manual/{session.session_id}", data={"genre": "Krautrock; Psychedelic Rock"})

        medien = mediafile.MediaFile(session.directory / "01.flac")
        # mediafile liefert im einfachen Feld nur den ersten Eintrag zurück;
        # die eigentliche Mehrfachliste steht in ``genres``.
        assert medien.genre == "Krautrock"
        assert medien.genres == ["Krautrock", "Psychedelic Rock"]

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

    def test_ohne_eingabe_wird_nichts_geschrieben(self, client):
        session = self._session_mit(["01.flac"])
        response = client.post(f"/manual/{session.session_id}", data={})
        assert "kein Feld" in response.text


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
        return eintrag

    def test_seite_listet_alben(self, client, album, monkeypatch):
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [album])
        response = client.get("/albums")
        assert response.status_code == 200
        assert "Abbey Road" in response.text
        assert "The Beatles" in response.text
        assert 'id="cover-dialog"' in response.text

    def test_fehler_beim_listen_wird_angezeigt(self, client, monkeypatch):
        from backend import albums

        def kaputt(q=""):
            raise albums.AlbumError("beets antwortet nicht.")

        monkeypatch.setattr(routes.albums, "list_albums", kaputt)
        response = client.get("/albums")
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
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [album])
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

    def test_unbekanntes_album_beim_hochladen_gibt_es_404(self, client, album):
        response = client.post(
            "/cover/album/999", files={"bild": ("cover.jpg", self.JPEG, "image/jpeg")}
        )
        assert response.status_code == 404

    def test_kaputtes_bild_wird_gemeldet_ohne_einzubetten(
        self, client, album, monkeypatch
    ):
        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [album])
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

        monkeypatch.setattr(routes.albums, "list_albums", lambda q="": [album])

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
