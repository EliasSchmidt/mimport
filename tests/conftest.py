"""Gemeinsame Test-Vorbereitung.

Wichtig: Die Tests dürfen weder die beets-Library des Systems anfassen noch in
den echten Staging-Ordner schreiben. Deshalb bekommt jeder Testlauf ein eigenes
``BEETSDIR`` und eine eigene Staging-Wurzel.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Damit "import backend..." ohne Installation funktioniert.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(scope="session", autouse=True)
def isolierte_beets_umgebung(tmp_path_factory):
    """Verhindert, dass Tests die echte beets-Konfiguration oder DB berühren."""
    import os

    beetsdir = tmp_path_factory.mktemp("beetsdir")
    os.environ["BEETSDIR"] = str(beetsdir)
    yield beetsdir


@pytest.fixture(autouse=True)
def isoliertes_staging(tmp_path, monkeypatch):
    """Staging-Wurzel pro Test, damit nichts im echten Upload-Ordner landet."""
    from backend import config

    monkeypatch.setattr(config.settings, "staging_root", tmp_path / "staging")
    return tmp_path / "staging"
