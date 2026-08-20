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
    from beets.autotag import AlbumInfo, Distance, TrackInfo

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

        erste = min(album_session.audio_paths)
        assert mediafile.MediaFile(erste).mb_albumid == (
            "964e8152-d86d-4b88-9b79-2f561db6c124"
        )

    def test_handgesetzte_tags_landen_in_den_dateien(self, client, album_session):
        response = client.post(
            f"/manual/{album_session.session_id}",
            data={"albumartist": "Eigenes", "album": "Selbstgemacht", "year": "1999", "genre": "Pop"},
        )
        assert response.status_code == 200
        assert "Tags geschrieben" in response.text

        import mediafile

        media = mediafile.MediaFile(min(album_session.audio_paths))
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


class TestMehrwertigeTags:
    """Was in die Dateien geschrieben wird, damit Navidrome es richtig liest.

    In beets 2.x heißen die Felder ``genres``, ``artists``, ``albumartists``.
    Ein einzelner String landet dort als flexibles Attribut und wird **nicht**
    in die Datei geschrieben -- genau das passierte vorher mit dem
    Genre-Formularfeld.
    """

    def test_genre_landet_wirklich_in_der_datei(self, tmp_path):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        pfad = write_flac(tmp_path / "t.flac", seconds=5)
        tagging.apply_manual_tags([pfad], {"genres": "Jazz"})

        medien = mediafile.MediaFile(pfad)
        assert medien.genres == ["Jazz"]
        # Das einwertige Feld muss auch stehen -- ältere Scanner lesen nur das.
        assert medien.genre == "Jazz"

    @pytest.mark.parametrize(
        "eingabe,erwartet",
        [
            ("Bill Evans feat. Scott LaFaro", ["Bill Evans", "Scott LaFaro"]),
            ("A ft. B", ["A", "B"]),
            ("A / B", ["A", "B"]),
            ("A; B", ["A", "B"]),
            # Diese dürfen NICHT zerlegt werden.
            ("AC/DC", ["AC/DC"]),
            ("Simon & Garfunkel", ["Simon & Garfunkel"]),
            ("Crosby, Stills & Nash", ["Crosby, Stills & Nash"]),
        ],
    )
    def test_trennzeichen_wie_bei_navidrome(self, eingabe, erwartet):
        from backend import tagging

        assert tagging._kuenstlerwerte(eingabe) == erwartet

    @pytest.mark.parametrize(
        "eingabe,erwartet",
        [
            ("Jazz", ["Jazz"]),
            ("Jazz; Fusion", ["Jazz", "Fusion"]),
            ("R&B/Soul", ["R&B/Soul"]),
            ("Folk, World, & Country", ["Folk, World, & Country"]),
        ],
    )
    def test_genres_werden_nur_am_semikolon_getrennt(self, eingabe, erwartet):
        from backend import tagging

        assert tagging._genrewerte(eingabe) == erwartet

    def test_mbids_werden_nur_bei_vollstaendiger_auflosung_gesetzt(self, tmp_path, monkeypatch):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        monkeypatch.setattr(
            tagging.artist_ids,
            "lookup_exact",
            lambda name: {
                "Miles Davis": "561d854a-6a28-4aa7-8c99-323e6ce46c2a",
                "John Coltrane": None,
            }.get(name),
        )

        pfad = write_flac(tmp_path / "t.flac", seconds=5)
        tagging.apply_manual_tags([pfad], {"artists": "Miles Davis; John Coltrane"})

        medien = mediafile.MediaFile(pfad)
        assert medien.artists == ["Miles Davis", "John Coltrane"]
        assert medien.mb_artistid in (None, "")
        assert medien.mb_artistids in (None, [])

    def test_sampler_bekommt_je_track_einen_kuenstler(self, tmp_path, monkeypatch):
        """Der Fall Various Artists: Albumkünstler gleich, Interpret je Track."""
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        ids = {
            "Various Artists": "89ad4ac3-39f7-470e-963a-56509c546377",
            "Miles Davis": "561d854a-6a28-4aa7-8c99-323e6ce46c2a",
            "Bill Evans": "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5",
        }
        monkeypatch.setattr(tagging.artist_ids, "lookup_exact", lambda name: ids.get(name))

        dateien = [write_flac(tmp_path / f"{i:02d}.flac", seconds=5) for i in (1, 2)]
        tagging.apply_manual_tags(
            dateien,
            {"albumartist": "Various Artists", "album": "Sampler", "comp": True},
            je_track={
                "01.flac": {"title": "Eins", "artists": "Miles Davis"},
                "02.flac": {"title": "Zwei", "artists": "Bill Evans"},
            },
        )

        erste, zweite = (mediafile.MediaFile(p) for p in dateien)
        assert erste.artist == "Miles Davis"
        assert zweite.artist == "Bill Evans"
        assert erste.mb_artistids == [ids["Miles Davis"]]
        assert zweite.mb_artistids == [ids["Bill Evans"]]
        # Zusammengehalten wird das Album über den Albumkünstler ...
        assert erste.albumartist == zweite.albumartist == "Various Artists"
        assert erste.mb_albumartistids == zweite.mb_albumartistids == [ids["Various Artists"]]
        # ... und über das Compilation-Flag, das Navidrome dafür liest.
        assert erste.comp is True and zweite.comp is True


class TestTracknummernBeimHandtaggen:
    """Ohne Nummer benennt beets jede Datei zu „00 <Titel>"."""

    def _dateien(self, tmp_path, anzahl=3):
        from tests.flacfixture import write_flac

        return [
            write_flac(tmp_path / f"{i:02d} Track {i}.flac", seconds=60 + i)
            for i in range(1, anzahl + 1)
        ]

    def test_nummer_kommt_aus_der_reihenfolge(self, tmp_path):
        import mediafile

        from backend import tagging

        dateien = self._dateien(tmp_path)
        tagging.apply_manual_tags(
            dateien,
            {"album": "Sampler"},
            je_track={p.name: {"title": f"Stück {i}"} for i, p in enumerate(dateien, 1)},
        )

        nummern = [mediafile.MediaFile(p).track for p in dateien]
        assert nummern == [1, 2, 3]
        # Und die Gesamtzahl, damit Abspieler das Album vollständig sehen.
        assert all(mediafile.MediaFile(p).tracktotal == 3 for p in dateien)

    def test_vorhandene_nummer_bleibt(self, tmp_path):
        """Ein Rip setzt sie schon -- die darf nicht überschrieben werden."""
        import mediafile

        from backend import tagging

        dateien = self._dateien(tmp_path, 2)
        # Umgekehrt nummeriert: so ließe sich ein Versehen erkennen.
        for nummer, pfad in zip((7, 4), dateien):
            medien = mediafile.MediaFile(pfad)
            medien.track = nummer
            medien.save()

        tagging.apply_manual_tags(dateien, {"album": "Sampler"})

        assert [mediafile.MediaFile(p).track for p in dateien] == [7, 4]


class TestSamplerAlbumkuenstler:
    """Ein Sampler braucht einen Albumkünstler in der **Datei**.

    beets trägt „Various Artists" nur in seine Library ein und schreibt es
    nicht zurück -- nachgemessen nach einem echten Import. Navidrome liest
    aber die Datei; ohne Eintrag gruppiert es die Stücke nicht zu einem Album,
    weil dort je Track ein anderer Interpret steht.
    """

    def test_sampler_ohne_angabe_bekommt_various_artists(self, tmp_path):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        dateien = [write_flac(tmp_path / f"{i:02d}.flac", seconds=60) for i in (1, 2)]
        tagging.apply_manual_tags(
            dateien,
            {"comp": True, "album": "Sampler"},
            je_track={"01.flac": {"artists": "Haydn"}, "02.flac": {"artists": "Bach"}},
        )

        for pfad in dateien:
            medien = mediafile.MediaFile(pfad)
            assert medien.albumartist == "Various Artists"
            assert medien.comp is True
        # Die Interpreten bleiben je Track verschieden -- darum geht es ja.
        assert mediafile.MediaFile(dateien[0]).artist == "Haydn"
        assert mediafile.MediaFile(dateien[1]).artist == "Bach"

    def test_eigene_angabe_wird_nicht_ueberschrieben(self, tmp_path):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        pfad = write_flac(tmp_path / "01.flac", seconds=60)
        tagging.apply_manual_tags(
            [pfad], {"comp": True, "albumartist": "Deutsche Grammophon"}
        )
        assert mediafile.MediaFile(pfad).albumartist == "Deutsche Grammophon"

    def test_ohne_sampler_kein_eingriff(self, tmp_path):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        pfad = write_flac(tmp_path / "01.flac", seconds=60)
        tagging.apply_manual_tags([pfad], {"album": "Ein Album"})
        assert mediafile.MediaFile(pfad).albumartist in ("", None)

    def test_name_kommt_aus_der_beets_konfiguration(self):
        from backend import tagging

        # Nicht fest verdrahtet: wer va_name ändert, soll es wiederfinden.
        assert tagging.sampler_name() == "Various Artists"


class TestKomponistJeTrack:
    """Klassik-Compilations mischen mehrere Komponisten -- das albumweite
    Komponisten-Feld gilt dort für keinen Track richtig. Der Komponist je
    Track überschreibt es, genau wie der Track-Künstler beim Sampler."""

    def test_eigener_komponist_ueberschreibt_das_albumweite_feld(self, tmp_path):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        dateien = [write_flac(tmp_path / f"{i:02d}.flac", seconds=5) for i in (1, 2)]
        tagging.apply_manual_tags(
            dateien,
            {"composers": "Johann Sebastian Bach", "album": "Klassik-Sampler"},
            je_track={"02.flac": {"composers": "Ludwig van Beethoven"}},
        )

        erste, zweite = (mediafile.MediaFile(p) for p in dateien)
        # Ohne eigenen Eintrag gilt weiter das albumweite Feld ...
        assert erste.composer == "Johann Sebastian Bach"
        assert erste.composers == ["Johann Sebastian Bach"]
        # ... eine Zeile mit eigenem Komponisten überschreibt nur sich selbst.
        assert zweite.composer == "Ludwig van Beethoven"
        assert zweite.composers == ["Ludwig van Beethoven"]

    def test_mehrere_komponisten_je_track_werden_getrennt(self, tmp_path):
        import mediafile

        from backend import tagging
        from tests.flacfixture import write_flac

        pfad = write_flac(tmp_path / "01.flac", seconds=5)
        tagging.apply_manual_tags(
            [pfad], {}, je_track={"01.flac": {"composers": "Bach; Vivaldi"}}
        )

        medien = mediafile.MediaFile(pfad)
        # Anders als bei Genre/Künstler legt mediafile für Komponisten keinen
        # separaten einwertigen Vorbis-Tag an -- "composer" und "composers"
        # teilen sich denselben Tag, der hier zwei echte COMPOSER-Einträge
        # bekommt. Ein Scanner, der nur den ersten liest, sieht "Bach".
        assert medien.composers == ["Bach", "Vivaldi"]
        assert medien.composer == "Bach"
