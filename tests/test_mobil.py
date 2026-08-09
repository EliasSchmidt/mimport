"""Passt die Oberfläche auf ein schmales Handy?

Gemessen wird gegen 360 Pixel -- das schmalste, womit realistisch zu rechnen
ist. Das Kriterium ist objektiv: läuft der Inhalt breiter als das Fenster,
muss die Seite seitlich gescrollt werden, und genau das soll nicht passieren.

Braucht Playwright mitsamt Browser. Fehlt beides, wird übersprungen -- die
Prüfung ist nützlich, aber keine Voraussetzung für den Rest.
"""

from __future__ import annotations

import socket
import threading
import time
from contextlib import closing

import pytest

BREITE = 360

playwright = pytest.importorskip("playwright.sync_api", reason="playwright fehlt")


def freier_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Ein echter Server -- die Fragmente kommen ja per htmx nach."""
    import uvicorn

    from backend import audiobook, config

    tmp = tmp_path_factory.mktemp("mobil")
    config.settings.staging_root = tmp / "staging"
    config.settings.audiobook_root = tmp / "audiobooks"
    config.settings.cdrom_device = "/gibtsnicht"
    config.settings.disc_root = tmp / "keine-cd"

    # Ein Buch mit langem Namen: der Härtefall für die Bibliothekstabelle.
    buch = config.settings.audiobook_root / "Astrid Lindgren" / "Ronja Räubertochter"
    (buch / "CD 1").mkdir(parents=True)
    (buch / "CD 1" / "01 Ein reichlich langer Titel.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
    audiobook._m4b_job = None

    from backend.main import app

    port = freier_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as exc:  # Browser nicht installiert
            pytest.skip(f"Chromium fehlt: {exc}")
        yield b
        b.close()


def ueberlauf(seite) -> int:
    """Um wie viele Pixel ist der Inhalt breiter als das Fenster?"""
    return seite.evaluate("document.documentElement.scrollWidth") - BREITE


@pytest.mark.parametrize("pfad", ["/", "/musik", "/hoerbuch"])
def test_seiten_passen_in_die_breite(browser, server, pfad):
    seite = browser.new_page(viewport={"width": BREITE, "height": 780})
    try:
        seite.goto(server + pfad, wait_until="networkidle")
        assert ueberlauf(seite) <= 0, f"{pfad} läuft um {ueberlauf(seite)}px über"
    finally:
        seite.close()


def test_dateiliste_nach_upload_passt(browser, server):
    """Die Tabelle mit langen Dateinamen -- dort war es am engsten."""
    seite = browser.new_page(viewport={"width": BREITE, "height": 780})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [
                {
                    "name": f"{i:02d} Ein langer Stücktitel ohne Umbruchstellen.flac",
                    "mimeType": "audio/flac",
                    "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34,
                }
                for i in (1, 2)
            ],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        assert ueberlauf(seite) <= 0

        # Auch mit aufgeklapptem Formular für das Taggen von Hand.
        seite.click("details.manuell > summary")
        seite.click("details.je-track > summary")
        seite.wait_for_timeout(200)
        assert ueberlauf(seite) <= 0
    finally:
        seite.close()


def test_hoerbuch_bibliothek_passt(browser, server):
    seite = browser.new_page(viewport={"width": BREITE, "height": 780})
    try:
        seite.goto(server + "/hoerbuch", wait_until="networkidle")
        seite.wait_for_selector("#audiobook table", timeout=10000)
        assert ueberlauf(seite) <= 0
    finally:
        seite.close()
