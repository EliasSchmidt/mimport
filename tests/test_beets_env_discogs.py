"""Aktivierung von Discogs als zweite Metadatenquelle.

Arbeitet bewusst mit einer eigenen ``confuse.Configuration`` statt dem
globalen ``beets.config`` -- das Plugin darf in diesen Tests nie wirklich
geladen werden (kein Netz, kein Token), und andere Tests im selben
Pytest-Lauf dürfen sich nicht auf einen mutierten globalen Zustand verlassen
müssen.
"""

from __future__ import annotations

import confuse
import pytest

from backend import beets_env
from backend.config import Settings


def _config(plugins: list[str] | None = None) -> confuse.Configuration:
    cfg = confuse.Configuration("mimporttest", read=False)
    cfg.set({"plugins": plugins or ["musicbrainz"]})
    return cfg


class TestDiscogsAktivierung:
    def test_ohne_token_bleibt_discogs_draussen(self, monkeypatch):
        monkeypatch.delenv("MIMPORT_DISCOGS_TOKEN", raising=False)
        monkeypatch.setattr(
            "backend.config.settings", Settings(discogs_user_token="")
        )

        cfg = _config()
        beets_env._activate_discogs_if_configured(cfg)

        assert cfg["plugins"].as_str_seq() == ["musicbrainz"]

    def test_mit_token_wird_discogs_ergaenzt(self, monkeypatch):
        monkeypatch.setattr(
            "backend.config.settings",
            Settings(discogs_user_token="ein-geheimer-token"),
        )

        cfg = _config()
        beets_env._activate_discogs_if_configured(cfg)

        assert cfg["plugins"].as_str_seq() == ["musicbrainz", "discogs"]
        assert cfg["discogs"]["user_token"].as_str() == "ein-geheimer-token"

    def test_ist_idempotent_bei_bereits_aktivem_plugin(self, monkeypatch):
        """Ein zweiter Aufruf (z. B. durch einen erneuten ensure_loaded()
        in einem anderen Test-Setup) darf 'discogs' nicht doppelt eintragen."""
        monkeypatch.setattr(
            "backend.config.settings",
            Settings(discogs_user_token="ein-geheimer-token"),
        )

        cfg = _config(plugins=["musicbrainz", "discogs"])
        beets_env._activate_discogs_if_configured(cfg)

        assert cfg["plugins"].as_str_seq() == ["musicbrainz", "discogs"]

    def test_token_kommt_aus_der_umgebungsvariable(self, monkeypatch):
        monkeypatch.setenv("MIMPORT_DISCOGS_TOKEN", "aus-der-umgebung")
        assert Settings().discogs_user_token == "aus-der-umgebung"

    def test_ohne_umgebungsvariable_ist_der_token_leer(self, monkeypatch):
        monkeypatch.delenv("MIMPORT_DISCOGS_TOKEN", raising=False)
        assert Settings().discogs_user_token == ""
