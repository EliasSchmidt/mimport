"""Durchlauf durch alle Fragmente mit echten Dateien.

Diese Tests rendern jedes Template mindestens einmal. Ohne sie fällt ein Fehler
in der Jinja-Syntax oder ein umbenanntes Feld erst im Browser auf.

Die Audiodateien sind echt (siehe ``flacfixture``), die MusicBrainz-Abfrage ist
ersetzt -- geprüft wird die Darstellung, nicht das Netz.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend import beets_env, matching, sessions
from backend.main import app
from tests.flacfixture import write_album, write_flac

TRACKS = [
    ("Come Together", 1, 259.0),
    ("Something", 2, 182.0),
    ("Maxwell's Silver Hammer", 3, 207.0),
]


@pytest.fixture(scope="module", autouse=True)
def beets_geladen():
    beets_env.ensure_loaded()


@pytest.fixture
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def album_session(tmp_path):
    """Eine Session mit drei echten, getaggten FLAC-Dateien."""
    session = sessions.create_session()
    write_album(session.directory / "Abbey Road", TRACKS)
    return session


def _fake_match(*, missing: int = 0, unmatched_paths: list | None = None):
    """Baut einen ``AlbumMatch`` über echte Dateien der Session."""
    from beets.autotag import AlbumInfo, AlbumMatch, Distance, TrackInfo

    tracks = [
        TrackInfo(title=title, track_id=f"t{n}", index=n, length=length)
        for title, n, length in TRACKS
    ]
    extra_tracks = [
        TrackInfo(title=f"Here Comes the Sun {i}", track_id=f"x{i}", index=10 + i,
                  length=185.0)
        for i in range(missing)
    ]
    info = AlbumInfo(
        album="Abbey Road",
        album_id="964e8152-d86d-4b88-9b79-2f561db6c124",
        artist="The Beatles",
        artist_id="b10bbbfc-cf9e-42e0-be17-e2c3e1d2600d",
        tracks=tracks + extra_tracks,
        year=1969,
        country="GB",
        label="Apple Records",
        catalognum="PCS 7088",
        media="CD",
        mediums=1,
        data_source="MusicBrainz",
    )
    distance = Distance()
    if missing:
        distance.add("missing_tracks", 0.6)
    return info, tracks, extra_tracks, distance


def _album_match_for(paths, *, missing: int = 0):
    from beets.autotag import AlbumMatch

    info, tracks, extra_tracks, distance = _fake_match(missing=missing)
    items = matching.load_items(paths)
    return AlbumMatch(
        distance=distance,
        info=info,
        mapping=dict(zip(items, tracks)),
        extra_items=[],
        extra_tracks=extra_tracks,
    )


class TestDateiliste:
    def test_verlustfreie_dateien_werden_als_solche_gezeigt(self, client, tmp_path):
        flac = write_flac(tmp_path / "01 Come Together.flac", seconds=259.0)
        response = client.post(
            "/upload",
            files={"files": ("Abbey Road/01 Come Together.flac", flac.read_bytes(), "audio/flac")},
        )
        assert response.status_code == 200
        assert "verlustfrei" in response.text
        assert "FLAC" in response.text
        assert "44.1 kHz" in response.text
        # Der Warnhinweis darf hier gerade nicht erscheinen.
        assert "verlustbehaftet" not in response.text

    def test_hinweis_bei_verlustbehafteter_datei(self, client, tmp_path, monkeypatch):
        """MP3 im Upload muss den Hinweis auslösen -- ohne zu blockieren."""
        import mediafile

        from backend import audio

        # Eine echte MP3 zu bauen wäre unnötig aufwendig; entscheidend ist, was
        # mediafile über das Format sagt.
        echt = audio.inspect_file

        def als_mp3(path, display_name=None):
            info = echt(path, display_name=display_name)
            info.format = "MP3"
            info.bitdepth = 0
            info.bitrate = 320000
            info.quality = audio.classify_format("MP3")
            info.detail = "Verlustbehaftet"
            return info

        monkeypatch.setattr(audio, "inspect_file", als_mp3)

        flac = write_flac(tmp_path / "song.flac")
        response = client.post(
            "/upload", files={"files": ("song.mp3", flac.read_bytes(), "audio/mpeg")}
        )
        assert "verlustbehaftet" in response.text
        assert "verlustfrei wäre besser" in response.text or "lade FLAC" in response.text
        # Es bleibt ein Hinweis: die Suche nach Matches ist weiter möglich.
        assert "Matches suchen" in response.text


class TestKandidatenAnzeige:
    def test_sicherheit_grund_und_luecken_erscheinen(
        self, client, album_session, monkeypatch
    ):
        """Der Kern der Oberfläche: Prozent, Abzug und fehlende Tracks."""
        match = _album_match_for(album_session.audio_paths, missing=11)
        candidate = matching.serialize_candidate(match, 0)
        candidate.recommendation = "low"

        monkeypatch.setattr(
            matching,
            "find_candidates",
            lambda paths, **kwargs: matching.MatchResult(
                current_artist="The Beatles",
                current_album="Abbey Road",
                recommendation="low",
                candidates=[candidate],
            ),
        )

        response = client.post(f"/match/{album_session.session_id}", data={})
        assert response.status_code == 200

        # Sicherheit in Prozent.
        assert f"{candidate.confidence}" in response.text
        assert "Sicherheit" in response.text
        # Der Grund, nicht nur die Zahl.
        assert "Tracks des Releases fehlen im Upload" in response.text
        # Die fehlenden Titel namentlich.
        assert "Here Comes the Sun 0" in response.text
        assert "11 Track(s) fehlen" in response.text
        # Gegenüberstellung der Tags.
        assert "Come Together" in response.text
        # Link zur Quelle.
        assert "musicbrainz.org/release/964e8152" in response.text
        # Und die Möglichkeit, den Match zu übernehmen.
        assert "Diesen Match übernehmen" in response.text

    def test_vollstaendiger_match_meldet_keine_luecken(
        self, client, album_session, monkeypatch
    ):
        match = _album_match_for(album_session.audio_paths)
        candidate = matching.serialize_candidate(match, 0)
        candidate.recommendation = "strong"

        monkeypatch.setattr(
            matching,
            "find_candidates",
            lambda paths, **kwargs: matching.MatchResult(
                current_artist="The Beatles",
                current_album="Abbey Road",
                recommendation="strong",
                candidates=[candidate],
            ),
        )
        response = client.post(f"/match/{album_session.session_id}", data={})
        assert "100.0" in response.text
        assert "keine Lücken" in response.text
        assert "Eindeutiger Treffer" in response.text

    def test_kein_treffer_bietet_handarbeit_an(
        self, client, album_session, monkeypatch
    ):
        monkeypatch.setattr(
            matching,
            "find_candidates",
            lambda paths, **kwargs: matching.MatchResult(
                current_artist="",
                current_album="",
                recommendation="none",
                note="Keine Kandidaten gefunden.",
            ),
        )
        response = client.post(f"/match/{album_session.session_id}", data={})
        assert "Kein Treffer" in response.text
        # Ohne Match muss der Weg über eigene Tags offenstehen.
        assert "Tags selbst setzen" in response.text

    def test_suchfehler_wird_angezeigt(self, client, album_session, monkeypatch):
        monkeypatch.setattr(
            matching,
            "find_candidates",
            lambda paths, **kwargs: matching.MatchResult(
                current_artist="",
                current_album="",
                recommendation="none",
                error="MusicBrainz nicht erreichbar",
            ),
        )
        response = client.post(f"/match/{album_session.session_id}", data={})
        assert "MusicBrainz nicht erreichbar" in response.text


class TestUebernehmenUndImport:
    def test_tags_werden_geschrieben_und_bestaetigt(
        self, client, album_session, monkeypatch
    ):
        """Ein echter Durchlauf: Tags landen wirklich in den Dateien."""
        match = _album_match_for(album_session.audio_paths)
        monkeypatch.setattr(
            matching, "find_candidate_by_id", lambda paths, album_id, **kw: match
        )

        response = client.post(
            f"/choose/{album_session.session_id}",
            data={"album_id": "964e8152-d86d-4b88-9b79-2f561db6c124"},
        )
        assert response.status_code == 200
        assert "Tags geschrieben" in response.text
        assert "An beets übergeben" in response.text
        assert "Probelauf" in response.text

        # Gegenprobe in der Datei selbst.
        import mediafile

        erste = sorted(album_session.audio_paths)[0]
        assert mediafile.MediaFile(erste).mb_albumid == (
            "964e8152-d86d-4b88-9b79-2f561db6c124"
        )

    def test_handgesetzte_tags_landen_in_den_dateien(self, client, album_session):
        response = client.post(
            f"/manual/{album_session.session_id}",
            data={"albumartist": "Eigenes", "album": "Selbstgemacht", "year": "1999"},
        )
        assert response.status_code == 200
        assert "Tags geschrieben" in response.text

        import mediafile

        media = mediafile.MediaFile(sorted(album_session.audio_paths)[0])
        assert media.albumartist == "Eigenes"
        assert media.album == "Selbstgemacht"
        assert media.year == 1999

    def test_probelauf_zeigt_kommandozeile(self, client, album_session, monkeypatch):
        from backend import importer

        monkeypatch.setattr(importer.settings, "beet_bin", "true")
        response = client.post(
            f"/import/{album_session.session_id}", data={"pretend": "1"}
        )
        assert response.status_code == 200
        assert "Probelauf" in response.text
        # Nachvollziehbar, was aufgerufen wurde -- inklusive -A.
        assert "--pretend" in response.text
        assert "-A" in response.text
        # Und die Dateien liegen noch da.
        assert album_session.audio_paths

    def test_fehlgeschlagener_import_wird_gemeldet(
        self, client, album_session, monkeypatch
    ):
        from backend import beets_env, importer

        monkeypatch.setattr(importer.settings, "beet_bin", "false")
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
        response = client.post(f"/import/{album_session.session_id}", data={})
        assert "Import fehlgeschlagen" in response.text
