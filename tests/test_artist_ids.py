from __future__ import annotations

import pytest

from backend import artist_ids


class _Response:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _ohne_drosselpause(monkeypatch):
    """Kein echtes ``time.sleep`` in Tests -- weder Drosselabstand noch Retry."""
    monkeypatch.setattr(artist_ids.time, "sleep", lambda _sekunden: None)
    artist_ids.search.cache_clear()
    yield
    artist_ids.search.cache_clear()


def test_suche_verwendet_normale_musicbrainz_freitextsuche(monkeypatch):
    gesehen = {}

    def fake_get(url, *, params, headers, timeout):
        gesehen["url"] = url
        gesehen["params"] = params
        gesehen["headers"] = headers
        gesehen["timeout"] = timeout
        return _Response({"artists": []})

    monkeypatch.setattr(artist_ids.requests, "get", fake_get)

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

    treffer = artist_ids.search("Harmonic Brass")
    assert [t.name for t in treffer] == ["Harmonic Brass", "First Harmonic Brass Band"]
    assert treffer[0].exact is True
    assert artist_ids.lookup_exact("Harmonic Brass") == "1"


def test_ein_503_wird_einmal_wiederholt_und_dann_geladen(monkeypatch):
    antworten = iter([_Response({}, status_code=503), _Response({"artists": [
        {"id": "1", "name": "Harmonic Brass"},
    ]})])
    monkeypatch.setattr(artist_ids.requests, "get", lambda *a, **k: next(antworten))

    treffer = artist_ids.search("Harmonic Brass")

    assert [t.name for t in treffer] == ["Harmonic Brass"]


def test_fehlgeschlagene_suche_wird_nicht_dauerhaft_als_leer_gecacht(monkeypatch):
    """Ein einzelner MusicBrainz-Ausfall darf eine Suche nicht für immer leer machen.

    Vorher hing hier ein ``@lru_cache`` direkt über der Fehlerbehandlung: ein
    503 oder ein Timeout kamen als leeres Tupel zurück, und genau dieses leere
    Tupel wurde für die Laufzeit des Prozesses gecached -- ein späterer Klick
    mit demselben Namen fand dann nie wieder etwas, obwohl MusicBrainz längst
    wieder erreichbar war.
    """
    monkeypatch.setattr(
        artist_ids.requests, "get", lambda *a, **k: _Response({}, status_code=503)
    )
    with pytest.raises(artist_ids.LookupFehlgeschlagen):
        artist_ids.search("Windsbacher Knabenchor")

    monkeypatch.setattr(
        artist_ids.requests,
        "get",
        lambda *a, **k: _Response(
            {"artists": [{"id": "abc", "name": "Windsbacher Knabenchor"}]}
        ),
    )
    treffer = artist_ids.search("Windsbacher Knabenchor")
    assert [t.name for t in treffer] == ["Windsbacher Knabenchor"]


def test_lookup_exact_liefert_none_statt_fehler_bei_ausfall(monkeypatch):
    monkeypatch.setattr(
        artist_ids.requests, "get", lambda *a, **k: _Response({}, status_code=503)
    )
    assert artist_ids.lookup_exact("Windsbacher Knabenchor") is None
