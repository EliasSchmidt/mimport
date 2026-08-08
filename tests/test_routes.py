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
        # Kein Upload, kein Match -- das gehört zum anderen Weg.
        assert "upload-form" not in response.text
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
