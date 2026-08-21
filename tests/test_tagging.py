"""Tags aus einem gewählten Match in die Dateien schreiben.

Die Tests bauen echte beets-Objekte (``AlbumInfo``, ``TrackInfo``, ``Distance``,
``AlbumMatch``) und echte FLAC-Dateien statt Attrappen -- so schlagen sie auch
an, wenn sich die beets-API unter uns ändert.
"""

from __future__ import annotations

import pytest

from backend import beets_env, tagging


@pytest.fixture(scope="module", autouse=True)
def beets_geladen():
    beets_env.ensure_loaded()


def _album_match(tmp_path, *, data_source: str, album_id: str, tracks=None):
    from beets.autotag import AlbumInfo, AlbumMatch, Distance, TrackInfo
    from beets.library import Item

    from tests.flacfixture import write_flac

    if tracks is None:
        tracks = [("Come Together", 1, 259.0)]

    track_infos = [
        TrackInfo(title=titel, track_id=f"{album_id}-{nr}", index=nr, length=laenge)
        for titel, nr, laenge in tracks
    ]

    info = AlbumInfo(
        album="Abbey Road",
        album_id=album_id,
        artist="The Beatles",
        artist_id="a1",
        artists_ids=["a1"],
        releasegroup_id=f"{album_id}-master",
        tracks=track_infos,
        year=1969,
        media="CD",
        mediums=1,
        data_source=data_source,
    )

    items = []
    for titel, nr, laenge in tracks:
        path = write_flac(tmp_path / f"{nr:02d} {titel}.flac", seconds=laenge)
        item = Item.from_path(str(path))
        item.title = titel
        item.artist = "The Beatles"
        item.track = nr
        items.append(item)

    return AlbumMatch(
        distance=Distance(),
        info=info,
        mapping=dict(zip(items, track_infos)),
        extra_items=[],
        extra_tracks=[],
    )


class TestApplyAlbumMatch:
    def test_musicbrainz_treffer_behaelt_seine_ids(self, tmp_path):
        match = _album_match(
            tmp_path,
            data_source="MusicBrainz",
            album_id="964e8152-d86d-4b88-9b79-2f561db6c124",
        )

        result = tagging.apply_album_match(match)

        assert result.ok
        import mediafile

        for item in match.mapping:
            media = mediafile.MediaFile(item.path)
            assert media.mb_albumid == "964e8152-d86d-4b88-9b79-2f561db6c124"
            assert media.mb_releasegroupid == "964e8152-d86d-4b88-9b79-2f561db6c124-master"

    def test_discogs_treffer_bekommt_keine_mb_ids_in_die_datei(self, tmp_path):
        """Discogs' eigene numerische Release-ID darf nicht unter dem Namen
        landen, den Player und Scanner als echte MusicBrainz-ID lesen -- siehe
        Docstring von ``_mb_ids_ohne_musicbrainz_entfernen`` in tagging.py."""
        match = _album_match(tmp_path, data_source="Discogs", album_id="249504")

        result = tagging.apply_album_match(match)

        assert result.ok
        import mediafile

        for item in match.mapping:
            media = mediafile.MediaFile(item.path)
            assert media.mb_albumid == ""
            assert media.mb_releasegroupid == ""
            assert media.mb_trackid == ""
            assert not media.mb_artistids

    def test_discogs_album_behaelt_seine_uebrigen_metadaten(self, tmp_path):
        """Nur die MB-gebrandeten Felder werden geleert -- Titel, Künstler & Co.
        aus dem Discogs-Treffer landen wie gewohnt in der Datei."""
        match = _album_match(tmp_path, data_source="Discogs", album_id="249504")

        tagging.apply_album_match(match)

        import mediafile

        for item in match.mapping:
            media = mediafile.MediaFile(item.path)
            assert media.album == "Abbey Road"
            assert media.artist == "The Beatles"
