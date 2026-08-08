"""Aufbereitung der Match-Kandidaten.

Die Tests bauen echte beets-Objekte (``AlbumInfo``, ``TrackInfo``, ``Distance``,
``AlbumMatch``) statt Attrappen -- so schlagen sie auch an, wenn sich die
beets-API unter uns ändert.
"""

from __future__ import annotations

import pytest

from backend import beets_env, matching


@pytest.fixture(scope="module", autouse=True)
def beets_geladen():
    beets_env.ensure_loaded()


class TestExtractMbid:
    def test_nackte_id(self):
        raw = "964e8152-d86d-4b88-9b79-2f561db6c124"
        assert matching.extract_mbid(raw) == raw

    def test_aus_url(self):
        assert (
            matching.extract_mbid(
                "https://musicbrainz.org/release/964e8152-d86d-4b88-9b79-2f561db6c124"
            )
            == "964e8152-d86d-4b88-9b79-2f561db6c124"
        )

    def test_mit_anhang_und_leerzeichen(self):
        assert (
            matching.extract_mbid(
                "  https://musicbrainz.org/release/964e8152-d86d-4b88-9b79-2f561db6c124/cover-art  "
            )
            == "964e8152-d86d-4b88-9b79-2f561db6c124"
        )

    def test_grossschreibung_wird_normalisiert(self):
        assert (
            matching.extract_mbid("964E8152-D86D-4B88-9B79-2F561DB6C124")
            == "964e8152-d86d-4b88-9b79-2f561db6c124"
        )

    @pytest.mark.parametrize("raw", ["", "   ", "kein-uuid", "1234", "abbey road"])
    def test_unbrauchbare_eingaben(self, raw):
        assert matching.extract_mbid(raw) is None


def _album_match(*, missing: int = 0, unmatched: int = 0, penalty: float = 0.0):
    """Baut einen echten ``AlbumMatch`` mit einstellbaren Lücken."""
    from beets.autotag import AlbumInfo, AlbumMatch, Distance, TrackInfo
    from beets.library import Item

    tracks = [
        TrackInfo(title="Come Together", track_id="t1", index=1, length=259.0),
        TrackInfo(title="Something", track_id="t2", index=2, length=182.0),
    ]
    extra_tracks = [
        TrackInfo(title=f"Fehlt {i}", track_id=f"x{i}", index=10 + i, length=200.0)
        for i in range(missing)
    ]

    info = AlbumInfo(
        album="Abbey Road",
        album_id="964e8152-d86d-4b88-9b79-2f561db6c124",
        artist="The Beatles",
        artist_id="a1",
        tracks=tracks + extra_tracks,
        year=1969,
        country="GB",
        label="Apple Records",
        media="CD",
        mediums=1,
        data_source="MusicBrainz",
    )

    items = [
        Item(title="come together", artist="The Beatles", track=1, length=260.0),
        Item(title="Something", artist="The Beatles", track=2, length=182.0),
    ]
    for index, item in enumerate(items):
        item.path = f"/staging/{index + 1:02d} track.flac".encode()

    extra_items = []
    for i in range(unmatched):
        extra = Item(title=f"Bonus {i}", artist="X", track=90 + i, length=100.0)
        extra.path = f"/staging/bonus{i}.flac".encode()
        extra_items.append(extra)

    distance = Distance()
    if penalty:
        distance.add("missing_tracks", penalty)

    return AlbumMatch(
        distance=distance,
        info=info,
        mapping=dict(zip(items, tracks)),
        extra_items=extra_items,
        extra_tracks=extra_tracks,
    )


class TestSerializeCandidate:
    def test_grunddaten(self):
        candidate = matching.serialize_candidate(_album_match(), 0)
        assert candidate.album == "Abbey Road"
        assert candidate.albumartist == "The Beatles"
        assert candidate.year == 1969
        assert candidate.label == "Apple Records"
        assert candidate.album_id == "964e8152-d86d-4b88-9b79-2f561db6c124"

    def test_perfekter_match_ergibt_hundert_prozent(self):
        # Sicherheit ist 1 - distance, und distance 0 heißt perfekt.
        candidate = matching.serialize_candidate(_album_match(), 0)
        assert candidate.confidence == 100.0
        assert candidate.penalties == []
        assert candidate.is_complete

    def test_abzug_senkt_die_sicherheit_und_wird_benannt(self):
        candidate = matching.serialize_candidate(_album_match(penalty=1.0), 0)
        assert candidate.confidence < 100.0
        assert candidate.penalties, "Abzug muss sichtbar sein"
        labels = [label for label, _ in candidate.penalties]
        # Der Klartext ersetzt den Rohnamen aus beets.
        assert "Tracks des Releases fehlen im Upload" in labels

    def test_fehlende_tracks_werden_aufgelistet(self):
        candidate = matching.serialize_candidate(_album_match(missing=3), 0)
        assert len(candidate.missing_tracks) == 3
        assert not candidate.is_complete
        assert "Fehlt 0" in candidate.missing_tracks[0]

    def test_dateien_ohne_zuordnung_werden_aufgelistet(self):
        candidate = matching.serialize_candidate(_album_match(unmatched=2), 0)
        assert candidate.unmatched_files == ["bonus0.flac", "bonus1.flac"]
        assert not candidate.is_complete

    def test_musicbrainz_link_wird_gebaut(self):
        candidate = matching.serialize_candidate(_album_match(), 0)
        assert candidate.url.endswith("964e8152-d86d-4b88-9b79-2f561db6c124")

    def test_gegenueberstellung_zeigt_aenderungen(self):
        candidate = matching.serialize_candidate(_album_match(), 0)
        assert len(candidate.pairings) == 2

        erste = candidate.pairings[0]
        assert erste.old_title == "come together"
        assert erste.new_title == "Come Together"
        assert erste.title_changed  # Groß-/Kleinschreibung zählt als Änderung
        assert erste.length_delta == 1.0  # 260 s Datei gegen 259 s Release

        zweite = candidate.pairings[1]
        assert not zweite.title_changed

    def test_gegenueberstellung_ist_nach_tracknummer_sortiert(self):
        candidate = matching.serialize_candidate(_album_match(), 0)
        nummern = [p.new_track for p in candidate.pairings]
        assert nummern == sorted(n for n in nummern if n is not None)


class TestConfidenceClass:
    @pytest.mark.parametrize(
        "recommendation,expected",
        [("strong", "strong"), ("medium", "medium"), ("low", "low"), ("none", "none")],
    )
    def test_einordnung(self, recommendation, expected):
        candidate = matching.serialize_candidate(_album_match(), 0)
        candidate.recommendation = recommendation
        assert candidate.confidence_class == expected


class TestFindCandidates:
    def test_ohne_lesbare_dateien_kommt_eine_meldung(self, tmp_path):
        fake = tmp_path / "keine-musik.flac"
        fake.write_bytes(b"nicht wirklich flac")
        result = matching.find_candidates([fake])
        assert result.error
        assert not result.has_candidates

    def test_netzfehler_wird_abgefangen(self, monkeypatch, tmp_path):
        """Ein Ausfall von MusicBrainz darf die Seite nicht sprengen."""
        monkeypatch.setattr(
            matching, "load_items", lambda paths: [object()]  # ein Item genügt
        )

        def explodiert(*args, **kwargs):
            raise RuntimeError("MusicBrainz nicht erreichbar")

        import beets.autotag

        monkeypatch.setattr(beets.autotag, "tag_album", explodiert)
        result = matching.find_candidates([tmp_path / "irgendwas.flac"])
        assert "MusicBrainz nicht erreichbar" in result.error
        assert not result.has_candidates


@pytest.mark.network
class TestGegenMusicbrainz:
    """Prüft den echten Weg. Braucht Netz, deshalb hinter einem Marker."""

    def test_unvollstaendiges_album_meldet_fehlende_tracks(self, monkeypatch):
        from beets.library import Item

        # Sechs der siebzehn Tracks von Abbey Road.
        tracks = [
            ("Come Together", 1, 259),
            ("Something", 2, 182),
            ("Maxwell's Silver Hammer", 3, 207),
            ("Oh! Darling", 4, 206),
            ("Octopus's Garden", 5, 171),
            ("I Want You (She's So Heavy)", 6, 467),
        ]
        items = [
            Item(artist="The Beatles", album="Abbey Road", title=t, track=n, length=l)
            for t, n, l in tracks
        ]
        monkeypatch.setattr(matching, "load_items", lambda paths: items)

        result = matching.find_candidates([])
        assert result.has_candidates

        best = result.candidates[0]
        assert best.album.startswith("Abbey Road")
        # Die Lücke muss benannt werden, nicht nur die Sicherheit senken.
        assert best.missing_tracks
        assert best.confidence < 100
        assert any("fehlen" in label for label, _ in best.penalties)


class TestBeetCliVersion:
    """Die Versionsauslesung entscheidet, ob der Import freigegeben wird."""

    def test_hinweiszeile_vor_der_version(self, monkeypatch, tmp_path):
        """beets schreibt beim Migrieren des Schemas eine Meldung davor.

        Wurde die als Version gelesen, galt sie als Versionsunterschied und der
        Import war gesperrt -- beim ersten Start also zuverlässig.
        """
        import subprocess

        from backend import beets_env

        ausgabe = (
            "Created database backup at: '/data/library.db-before-items.bak'.\n"
            "beets version 2.13.1\n"
            "Python version 3.12.7\n"
        )
        monkeypatch.setattr(
            beets_env.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, ausgabe, ""),
        )
        assert beets_env.beet_cli_version("beet") == "2.13.1"

    def test_normale_ausgabe(self, monkeypatch):
        import subprocess

        from backend import beets_env

        monkeypatch.setattr(
            beets_env.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(
                a[0], 0, "beets version 2.13.1\nplugins: musicbrainz\n", ""
            ),
        )
        assert beets_env.beet_cli_version("beet") == "2.13.1"

    def test_ohne_erkennbare_version_lieber_nichts(self, monkeypatch):
        """Ein Hinweistext als 'Version' würde als Unterschied gelesen."""
        import subprocess

        from backend import beets_env

        monkeypatch.setattr(
            beets_env.subprocess,
            "run",
            lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "irgendwas\n", ""),
        )
        assert beets_env.beet_cli_version("beet") is None
