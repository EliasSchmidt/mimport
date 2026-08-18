from __future__ import annotations

from backend import artist_ids


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


def test_suche_verwendet_normale_musicbrainz_freitextsuche(monkeypatch):
    gesehen = {}

    def fake_get(url, *, params, headers, timeout):
        gesehen["url"] = url
        gesehen["params"] = params
        gesehen["headers"] = headers
        gesehen["timeout"] = timeout
        return _Response({"artists": []})

    monkeypatch.setattr(artist_ids.requests, "get", fake_get)
    artist_ids.search.cache_clear()
    artist_ids.lookup_exact.cache_clear()

    artist_ids.search("Harmonic Brass")

    assert gesehen["url"] == artist_ids._MB_ARTIST_URL
    assert gesehen["params"]["query"] == "Harmonic Brass"
    assert gesehen["params"]["fmt"] == "json"


def test_lookup_exact_bleibt_trotz_breiter_suche_konservativ(monkeypatch):
    monkeypatch.setattr(
        artist_ids,
        "_suche_roh",
        lambda name, timeout=5.0, limit=10: [
            {"id": "1", "name": "Harmonic Brass", "type": "Orchester"},
            {"id": "2", "name": "First Harmonic Brass Band", "type": "Gruppe"},
        ],
    )
    artist_ids.search.cache_clear()
    artist_ids.lookup_exact.cache_clear()

    treffer = artist_ids.search("Harmonic Brass")
    assert [t.name for t in treffer] == ["Harmonic Brass", "First Harmonic Brass Band"]
    assert treffer[0].exact is True
    assert artist_ids.lookup_exact("Harmonic Brass") == "1"
