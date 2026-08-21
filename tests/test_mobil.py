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
    # Mit Cover: die Bildspalte nimmt Platz weg, und genau das war beim letzten
    # Mal die Ursache für zu enge Texte in der Tabelle.
    #
    # Ein echtes JPEG, keine Attrappe aus ein paar Bytes: die Kachel hat feste
    # Maße im CSS, ein kaputtes Bild misst sich also genauso wie ein geladenes.
    # Der Test unten prüft deshalb naturalWidth -- sonst bliebe er grün, wenn
    # die Adresse ins Leere zeigt, und das ist zweimal an einem Tag passiert.
    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("ffmpeg"):
        _subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", "color=c=teal:s=300x300:d=1", "-frames:v", "1",
             str(buch / "cover.jpg")],
            check=True,
        )
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
        except Exception as exc:  # noqa: BLE001 -- Browser nicht installiert
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


def test_dateiliste_nach_upload_passt(browser, server, monkeypatch):
    """Die Tabelle mit langen Dateinamen -- dort war es am engsten."""
    from backend import artist_ids

    monkeypatch.setattr(
        artist_ids,
        "search",
        lambda name, **kwargs: (
            artist_ids.ArtistMatch(
                name=name,
                mbid="cafebabe-0000-0000-0000-000000000000",
                disambiguation="ein ziemlich langer Erklärtext zur Einordnung",
                area="Vereinigte Staaten von Amerika",
                kind="Person",
                exact=True,
            ),
        ),
    )

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

        # Auch mit aufgeklapptem Formular für das Taggen von Hand -- die
        # Tabelle "Titel je Track" steht darin als eigene Karte, ohne
        # weiteres Aufklappen.
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")
        seite.wait_for_timeout(200)
        assert ueberlauf(seite) <= 0

        # Und mit einer eingeblendeten Trefferliste bei der Track-Künstler-
        # Lupe -- der breiteste Zustand dieser Zeile.
        erste_zeile = seite.locator(".je-track tbody tr").first
        erste_zeile.locator('[data-artist-field^="interpret:"]').fill("Ein langer Künstlername")
        erste_zeile.locator(".lookup-button").click()
        erste_zeile.locator(".artist-match-item").wait_for(timeout=5000)
        seite.wait_for_timeout(150)
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


def test_coverspalte_druckt_den_buchtitel_nicht_zusammen(browser, server):
    """Eine Spalte mehr ist genau das, was die Tabelle zuletzt gesprengt hat.

    Gemessen wird nicht nur der Überlauf der Seite, sondern auch die Breite,
    die dem Buchtitel bleibt -- ein Titel in einer 60 Pixel schmalen Spalte
    passt formal in den Bildschirm und ist trotzdem unlesbar.
    """
    seite = browser.new_page(viewport={"width": BREITE, "height": 780})
    try:
        seite.goto(server + "/hoerbuch", wait_until="networkidle")
        seite.wait_for_selector("#audiobook table", timeout=10000)

        assert ueberlauf(seite) <= 0, "Die Coverspalte sprengt die Breite"

        geladen = seite.evaluate(
            "[...document.querySelectorAll('#audiobook img.cover-mini')]"
            ".map(e => e.naturalWidth)"
        )
        if not geladen:
            pytest.skip("ffmpeg fehlt, kein echtes Cover in der Bibliothek")
        assert all(b > 0 for b in geladen), (
            f"Bild nicht geladen (naturalWidth {geladen}) -- die Adresse zeigt "
            "ins Leere, und die feste CSS-Größe verdeckt das"
        )

        bild = seite.locator("#audiobook img.cover-mini").first
        kasten = bild.bounding_box()
        assert kasten and kasten["width"] >= 32, f"Cover zu klein: {kasten}"
        # Quadratisch -- ein verzerrtes Cover sieht nach Fehler aus.
        assert abs(kasten["width"] - kasten["height"]) < 2, kasten

        titel = seite.locator("#audiobook td.name").first.bounding_box()
        assert titel and titel["width"] >= 120, (
            f"Für den Buchtitel bleiben nur {titel['width'] if titel else 0} px"
        )
    finally:
        seite.close()


def test_samplerhaken_stellt_die_felder_ein(browser, server):
    """Ein Sampler hat keinen einheitlichen Interpreten, sondern einen
    Albumkünstler als Sammelbegriff. Das Häkchen soll das einstellen, nicht
    nur behaupten."""
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        albumartist = seite.locator("[data-albumartist]")
        # Das Feld liegt neben einem versteckten Eingang für die MusicBrainz-ID
        # (Artist-Lookup) -- ohne den Typ wären es zwei Treffer im Strict Mode.
        interpret = seite.locator("[data-alle-interpreten] input[type=text]")
        hinweis = seite.locator("[data-sampler-hinweis]")

        assert albumartist.input_value() == ""
        assert interpret.is_enabled()
        assert hinweis.is_hidden()

        seite.check("[data-sampler]")
        assert albumartist.input_value() == "Various Artists"
        assert interpret.is_disabled(), "ein gemeinsamer Interpret ergibt hier keinen Sinn"
        assert hinweis.is_visible()

        # Zurücknehmen räumt auf, was wir gesetzt haben.
        seite.uncheck("[data-sampler]")
        assert albumartist.input_value() == ""
        assert interpret.is_enabled()
    finally:
        seite.close()


def test_samplerhaken_verwirft_bestaetigte_track_kuenstler_id(browser, server, monkeypatch):
    """"Sampler" leert das Feld "Track-Künstler für alle Tracks" per Skript --
    ohne "input"-Ereignis, das den sonst üblichen Abgleich auslösen würde.
    War dort zuvor eine Artist-ID per Lupe bestätigt, muss sie mit dem jetzt
    leeren Namen verschwinden, sonst würde sie beim Schreiben ohne
    erkennbaren Grund in jeden Track ohne eigene Bestätigung übernommen."""
    from backend import artist_ids

    monkeypatch.setattr(
        artist_ids,
        "search",
        lambda name, **kwargs: (
            artist_ids.ArtistMatch(name=name, mbid="cafebabe-0000-0000-0000-000000000000", exact=True),
        ),
    )

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        seite.fill("[data-artist-field=artist]", "Harmonic Brass")
        seite.locator("[data-artist-field=artist]").locator(
            "xpath=ancestor::div[contains(@class,'lookup-field')]"
        ).locator(".lookup-button").click()
        artist_ergebnis = seite.locator("[data-artist-results=artist]")
        artist_ergebnis.locator(".artist-match-item").wait_for(timeout=5000)
        artist_ergebnis.get_by_role("button", name="Übernehmen").click()

        interpret = seite.locator("[data-alle-interpreten] input[type=text]")
        mbid = seite.locator("[data-artist-mbid=artist]")
        assert interpret.input_value() == "Harmonic Brass"
        assert mbid.input_value() == "cafebabe-0000-0000-0000-000000000000"

        seite.check("[data-sampler]")
        assert interpret.input_value() == ""
        assert mbid.input_value() == ""
        assert "Noch kein MusicBrainz-Match" in artist_ergebnis.inner_text()
    finally:
        seite.close()


def test_samplerhaken_verwirft_bestaetigte_albumkuenstler_id_beim_zuruecknehmen(browser, server, monkeypatch):
    """"Various Artists" ist selbst ein echter MusicBrainz-Eintrag -- wird er
    per Lupe bestätigt, während "Sampler" ihn selbst eingesetzt hat, und dann
    "Sampler" wieder abgehakt, leert das Skript den Albumkünstler zurück auf
    "" (siehe "Nur zurücknehmen, was wir selbst gesetzt haben"). Ohne
    passenden Fix bliebe die bestätigte Artist-ID an diesem jetzt leeren
    Namen kleben und würde beim Schreiben in jeden Track ohne eigenen
    Albumkünstler übernommen."""
    from backend import artist_ids

    monkeypatch.setattr(
        artist_ids,
        "search",
        lambda name, **kwargs: (
            artist_ids.ArtistMatch(name=name, mbid="89ad4ac3-39f7-470e-963a-56509c546377", exact=True),
        ),
    )

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        seite.check("[data-sampler]")
        albumartist = seite.locator("[data-albumartist]")
        mbid = seite.locator("[data-artist-mbid=albumartist]")
        assert albumartist.input_value() == "Various Artists"

        seite.locator("[data-albumartist-label] .lookup-button").click()
        albumartist_ergebnis = seite.locator("[data-artist-results=albumartist]")
        albumartist_ergebnis.locator(".artist-match-item").wait_for(timeout=5000)
        albumartist_ergebnis.get_by_role("button", name="Übernehmen").click()
        assert mbid.input_value() == "89ad4ac3-39f7-470e-963a-56509c546377"

        seite.uncheck("[data-sampler]")
        assert albumartist.input_value() == ""
        assert mbid.input_value() == ""
    finally:
        seite.close()


def test_eigener_albumkuenstler_bleibt_stehen(browser, server):
    """Wer ein Label einträgt, will es behalten -- auch beim Umschalten."""
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        seite.fill("[data-albumartist]", "Deutsche Grammophon")
        seite.check("[data-sampler]")
        assert seite.locator("[data-albumartist]").input_value() == "Deutsche Grammophon"
        seite.uncheck("[data-sampler]")
        assert seite.locator("[data-albumartist]").input_value() == "Deutsche Grammophon"
    finally:
        seite.close()


def test_refresh_knopf_holt_entwurf_vom_anderen_geraet_ohne_schritt_4_aufzudecken(browser, server):
    """Der Refresh-Knopf im Handtagging-Formular simuliert genau den Fall, für
    den er gebaut wurde: auf einem anderen Gerät wurde weitergetippt (hier per
    direktem POST an /entwurf, wie es der Autosave des anderen Geräts täte),
    und der Knopf soll das im gerade offenen Formular sichtbar machen -- ohne
    dabei fälschlich Schritt 4 "Importieren" aufzudecken, wie es der Klick auf
    ein x-beliebiges Element in .manual-form sonst auslöst."""
    import requests

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        session_id = seite.locator("#files-inner").get_attribute("data-session")

        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        # Noch kein Entwurf gesichert: der Knopf darf das offene Formular
        # weder leeren noch Schritt 4 aufdecken. Ohne Entwurf klappt die Karte
        # danach wieder zu -- wie beim ganz normalen Einstieg auch.
        seite.click("button:has-text('Entwurf vom Server laden')")
        seite.wait_for_timeout(300)
        assert seite.locator("#result-step").is_hidden()
        assert seite.locator("details.manual").get_attribute("open") is None

        # "Anderes Gerät" sichert per Autosave-Endpunkt einen Entwurf.
        antwort = requests.post(
            f"{server}/entwurf/{session_id}",
            data={"albumartist": "Vom Handy getippt", "year": "1999"},
            timeout=5,
        )
        assert antwort.status_code == 200

        seite.click("details.manual > summary")
        albumartist = seite.locator("[data-albumartist]")
        assert albumartist.input_value() == ""
        seite.click("button:has-text('Entwurf vom Server laden')")
        seite.wait_for_timeout(300)
        # Ein Entwurf ist jetzt da: die Karte bleibt offen, ohne erneuten Klick.
        assert seite.locator("details.manual").get_attribute("open") is not None
        assert seite.locator("[data-albumartist]").input_value() == "Vom Handy getippt"
        assert seite.locator("#result-step").is_hidden()
    finally:
        seite.close()


def test_artist_lookup_trifft_nur_das_angeklickte_feld(browser, server, monkeypatch):
    """``hx-trigger="click from:.lookup-button"`` (ohne ``find``) hörte auf
    JEDEN Klick auf JEDEN Lookup-Button der Seite, weil ``from:`` ohne
    ``find``/``closest`` global im ganzen Dokument sucht -- ein Klick auf den
    Track-Künstler-Button hat also nebenbei auch den Albumkünstler erneut
    (mit dessen aktuellem Feldwert) gegen MusicBrainz gesucht und dessen
    Treffer überschrieben. Ein Klick darf nur das eigene Feld auslösen.
    """
    from backend import artist_ids

    def stub(name, **kwargs):
        return (
            artist_ids.ArtistMatch(name=name, mbid="deadbeef-0000-0000-0000-000000000000", exact=True),
        )

    monkeypatch.setattr(artist_ids, "search", stub)

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        seite.fill("[data-artist-field=albumartist]", "Windsbacher Knabenchor")
        seite.fill("[data-artist-field=artist]", "Irgendwer")

        # Nur den Track-Künstler-Button klicken -- der Albumkünstler bleibt
        # unangetastet.
        seite.locator("[data-artist-field=artist]").locator(
            "xpath=ancestor::div[contains(@class,'lookup-field')]"
        ).locator(".lookup-button").click()

        artist_ergebnis = seite.locator("[data-artist-results=artist]")
        albumartist_ergebnis = seite.locator("[data-artist-results=albumartist]")

        artist_ergebnis.locator(".artist-match-item").wait_for(timeout=5000)
        seite.wait_for_timeout(200)

        assert artist_ergebnis.locator(".artist-match-item").count() == 1
        assert albumartist_ergebnis.locator(".artist-match-item").count() == 0
        assert "Noch kein MusicBrainz-Match" in albumartist_ergebnis.inner_text()

        # Die Umstellung von "from:input" auf "from:find input" darf das
        # Enter-zum-Suchen im eigenen Textfeld nicht miträumen.
        seite.locator("[data-artist-field=albumartist]").press("Enter")
        albumartist_ergebnis.locator(".artist-match-item").wait_for(timeout=5000)
        assert albumartist_ergebnis.locator(".artist-match-item").count() == 1
    finally:
        seite.close()


def test_track_kuenstler_lupe_zeigt_treffer_nur_fuer_diese_zeile(browser, server, monkeypatch):
    """Die MusicBrainz-Zuordnung für Track-Künstler lief bisher nur still im
    Hintergrund beim Schreiben mit -- ohne dass der Nutzer sie je sah oder
    hätte korrigieren können. Die Lupe je Zeile in "Titel je Track" macht sie
    jetzt sichtbar: Suche, Auswahl und geschriebene Datei-Tags nur für die
    angeklickte Zeile."""
    from backend import artist_ids

    def stub(name, **kwargs):
        return (
            artist_ids.ArtistMatch(name=name, mbid="cafebabe-0000-0000-0000-000000000000", exact=True),
        )

    monkeypatch.setattr(artist_ids, "search", stub)

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [
                {"name": "01 Erstes.flac", "mimeType": "audio/flac",
                 "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34},
                {"name": "02 Zweites.flac", "mimeType": "audio/flac",
                 "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34},
            ],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        zeilen = seite.locator(".je-track tbody tr")
        erste_zeile = zeilen.nth(0)
        zweite_zeile = zeilen.nth(1)
        erste_zeile.locator('[data-artist-field^="interpret:"]').fill("Bill Evans")
        zweite_zeile.locator('[data-artist-field^="interpret:"]').fill("Bill Evans")

        # Nur die erste Zeile abgleichen -- die zweite bleibt unberührt.
        erste_zeile.locator(".lookup-button").click()

        erstes_ergebnis = erste_zeile.locator(".artist-match")
        zweites_ergebnis = zweite_zeile.locator(".artist-match")
        erstes_ergebnis.locator(".artist-match-item").wait_for(timeout=5000)
        assert erstes_ergebnis.locator(".artist-match-item").count() == 1
        assert zweites_ergebnis.locator(".artist-match-item").count() == 0

        erstes_ergebnis.get_by_role("button", name="Übernehmen").click()
        erste_mbid = erste_zeile.locator('[data-artist-mbid^="interpret:"]')
        zweite_mbid = zweite_zeile.locator('[data-artist-mbid^="interpret:"]')
        assert erste_mbid.input_value() == "cafebabe-0000-0000-0000-000000000000"
        assert zweite_mbid.input_value() == ""

        # Name danach geändert: die Bestätigung passt nicht mehr und wird
        # verworfen, statt an der falschen ID kleben zu bleiben.
        erste_zeile.locator('[data-artist-field^="interpret:"]').fill("Jemand anders")
        seite.wait_for_timeout(150)
        assert erste_mbid.input_value() == ""
    finally:
        seite.close()


def test_vom_server_geladene_artist_id_wird_bei_namensaenderung_verworfen(browser, server):
    """Anders als eine frisch in dieser Seite ausgewählte Zuordnung (siehe
    oben) kommt eine über einen Entwurf geladene Artist-ID nie durch den
    "Übernehmen"-Klick, der ``dataset.selectedName`` setzt -- ohne dass das
    Template denselben Namen beim Rendern in ``data-selected-name`` einträgt,
    bemerkt das Browser-Skript eine spätere Namensänderung gar nicht und die
    alte ID bliebe unbemerkt kleben."""
    import requests

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        session_id = seite.locator("#files-inner").get_attribute("data-session")

        # "Anderes Gerät" hat hier schon einen Track-Künstler samt Artist-ID
        # bestätigt und im Entwurf gesichert.
        requests.post(
            f"{server}/entwurf/{session_id}",
            data={
                "interpret:01 Stück.flac": "Bill Evans",
                "mbinterpret:01 Stück.flac": "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5",
            },
            timeout=5,
        ).raise_for_status()

        # Ein Entwurf liegt schon vor dem Öffnen vor -- die Karte klappt sich
        # deshalb gleich auf (kein zusätzlicher Klick auf die Summary nötig,
        # der sie hier sonst wieder zuklappen würde).
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual[open] > summary")
        seite.click("button:has-text('Entwurf vom Server laden')")
        seite.wait_for_timeout(300)

        zeile = seite.locator(".je-track tbody tr").first
        interpret_feld = zeile.locator('[data-artist-field^="interpret:"]')
        mbid_feld = zeile.locator('[data-artist-mbid^="interpret:"]')
        assert interpret_feld.input_value() == "Bill Evans"
        assert mbid_feld.input_value() == "5b689d33-aca8-4c64-9a6d-c3e7f9f7d9e5"

        interpret_feld.fill("Bill Evans Trio")
        seite.wait_for_timeout(150)
        assert mbid_feld.input_value() == ""
        assert "bitte neu prüfen" in zeile.locator(".artist-match").inner_text()
    finally:
        seite.close()


def test_mehrfach_kuenstler_werden_getrennt_gewaehlt_und_zusammengesetzt(browser, server, monkeypatch):
    """Chor + Dirigent (oder jede andere ``A / B``-Kollaboration): jeder Name
    bekommt eine eigene Trefferliste, und erst wenn für BEIDE ein Treffer
    gewählt wurde, landet eine kombinierte Artist-ID im versteckten Feld --
    eine halbe Auswahl wäre mehrdeutig, welcher Name zu welcher ID gehört.
    """
    from backend import artist_ids

    def stub(name, **kwargs):
        return (
            artist_ids.ArtistMatch(name=name, mbid=f"mbid-{name}", exact=True),
        )

    monkeypatch.setattr(artist_ids, "search", stub)

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        seite.fill("[data-artist-field=albumartist]", "Windsbacher Knabenchor / Karl-Friedrich Beringer")
        seite.locator("[data-artist-field=albumartist]").locator(
            "xpath=ancestor::div[contains(@class,'lookup-field')]"
        ).locator(".lookup-button").click()

        gruppen = seite.locator("[data-artist-results=albumartist] .artist-match-group")
        gruppen.first.wait_for(timeout=5000)
        assert gruppen.count() == 2

        mbid_feld = seite.locator("[data-artist-mbid=albumartist]")

        # Nur den ersten Namen wählen -- solange der zweite offen ist, darf
        # noch keine (halbe, mehrdeutige) ID geschrieben werden.
        seite.locator('[data-name-slot="0"] [data-artist-choose]').click()
        seite.wait_for_timeout(150)
        assert mbid_feld.input_value() == ""

        # Jetzt auch den zweiten -- erst jetzt ist die Auswahl vollständig.
        seite.locator('[data-name-slot="1"] [data-artist-choose]').click()
        seite.wait_for_timeout(150)

        assert seite.locator("[data-artist-field=albumartist]").input_value() == (
            "Windsbacher Knabenchor / Karl-Friedrich Beringer"
        )
        erwartete_id = "mbid-Windsbacher Knabenchor; mbid-Karl-Friedrich Beringer"
        assert mbid_feld.input_value() == erwartete_id
    finally:
        seite.close()


def test_fortsetzen_zeigt_ladezustand_und_schliesst_dropdown(browser, server, monkeypatch):
    """``Fortsetzen`` lädt eine Sitzung neu über ``audio.inspect_file`` je Datei --
    auf dem knappen Zielserver spürbar langsam, und bis eben ganz ohne
    Rückmeldung: der Klick verschwand für Sekunden im Nichts, und das
    Sitzungen-Dropdown blieb offen über der neu geladenen Ansicht liegen.
    """
    from backend import routes as routes_mod

    original = routes_mod._files_fragment

    def verzoegert(request, session):
        time.sleep(1.5)
        return original(request, session)

    monkeypatch.setattr(routes_mod, "_files_fragment", verzoegert)

    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)

        # Neu laden, damit die Sitzung als "offen" auftaucht -- über den
        # echten Weg (Dropdown -> Fortsetzen), nicht per Rohnavigation.
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.click("details.sessions-dropdown > summary")

        dropdown = seite.locator("details.sessions-dropdown")
        fortsetzen = seite.get_by_role("button", name="Fortsetzen")
        spinner = seite.locator(".sessions-panel .spinner").first

        assert dropdown.get_attribute("open") is not None
        fortsetzen.click()

        # Mitten in der künstlichen Verzögerung: Rückmeldung muss sofort da
        # sein, nicht erst wenn die Antwort eintrifft.
        seite.wait_for_timeout(300)
        assert spinner.is_visible()
        assert fortsetzen.is_disabled()

        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.wait_for_timeout(100)
        assert dropdown.get_attribute("open") is None
    finally:
        seite.close()


def test_offenes_sitzungen_dropdown_sprengt_die_seite_nicht(browser, server):
    """Fünf Spalten (Auswahl, Dateien, Größe, Zuletzt, Aktionen) passten nicht
    nebeneinander in das 22-28rem schmale Panel -- die "Fortsetzen"/"Verwerfen"-
    Knöpfe blieben ungewrappt breiter als das Panel, und weil nichts das
    abschnitt, blähte die absolut positionierte Tabelle die ganze Seite
    horizontal auf. Das trat gerade NICHT auf dem 360px-Handy-Breakpoint auf
    (dort greift schon die allgemeine ".stapelbar"-Stapelung), sondern in der
    Mitte -- z. B. bei einem schmaleren Desktop-Fenster.
    """
    seite = browser.new_page(viewport={"width": 1000, "height": 700})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Track 1.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)

        # Neu laden, damit die Sitzung als "offen" im Dropdown auftaucht.
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.click("details.sessions-dropdown > summary")
        seite.wait_for_timeout(200)

        overflow = seite.evaluate(
            "document.documentElement.scrollWidth - window.innerWidth"
        )
        assert overflow <= 0, f"Seite läuft mit offenem Dropdown um {overflow}px über"

        fortsetzen = seite.get_by_role("button", name="Fortsetzen")
        verwerfen = seite.get_by_role("button", name="Verwerfen")
        assert fortsetzen.is_visible() and verwerfen.is_visible()
    finally:
        seite.close()


def test_genre_vorschlaege_werden_beim_tippen_aktualisiert(browser, server):
    """Tippen filtert die Vorschläge, ein Klick übernimmt sie als Chip -- auch
    nach einem ersten ausgewählten Genre soll das noch klappen."""
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.set_input_files(
            "#upload-files",
            [{"name": "01 Stück.flac", "mimeType": "audio/flac",
              "buffer": b"fLaC\x00\x00\x00\x22" + b"\x00" * 34}],
        )
        seite.click("#upload-submit")
        seite.wait_for_selector("#files-inner", timeout=20000)
        seite.click("button:has-text('Ohne MusicBrainz von Hand taggen')")
        seite.wait_for_selector("details.manual > summary")
        seite.click("details.manual > summary")

        suche = seite.locator("[data-genre-suche]")
        suche.fill("ja")
        seite.locator("[data-genre-vorschlaege-liste] li", has_text="Jazz").first.click()
        assert seite.locator("[data-genre-chips] .genre-chip").first.inner_text().strip().startswith("Jazz")

        suche.fill("cl")
        seite.locator("[data-genre-vorschlaege-liste] li", has_text="Classical").first.click()

        wert = seite.locator("[data-genre-wert]")
        assert wert.input_value() == "Jazz; Classical"
    finally:
        seite.close()


# --------------------------------------------------- Benachrichtigungen ---
#
# Getestet wird die Verzweigung in notify.js, nicht das Erlaubnismodell von
# Chromium. Der Umweg über gestubbte Werte ist hier der einzige gangbare:
# headless Chromium meldet ``Notification.permission === "denied"``, auch nach
# ``grant_permissions`` -- nachgemessen -- und der Testserver auf 127.0.0.1
# gilt als sicherer Kontext, sodass der HTTPS-Hinweis sonst unerreichbar wäre.


def _zustand_setzen(seite, *, sicher=True, erlaubnis="default", api=True):
    teile = [
        (
            f"Object.defineProperty(window, 'isSecureContext', "
            f"{{get: () => {str(sicher).lower()}}});"
        )
    ]
    if api:
        teile.append(
            f"Object.defineProperty(Notification, 'permission', "
            f"{{configurable: true, get: () => '{erlaubnis}'}});"
        )
    else:
        teile.append("delete window.Notification;")
    seite.add_init_script("\n".join(teile))


def _kasten_text(browser, server, **zustand):
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        _zustand_setzen(seite, **zustand)
        seite.goto(server + "/musik", wait_until="networkidle")
        kasten = seite.locator("#benachrichtigung")
        assert kasten.is_visible(), "Ein unsichtbarer Hinweis erklärt nichts"
        return kasten.inner_text(), kasten.locator("button").count()
    finally:
        seite.close()


def test_benachrichtigung_ist_ueberhaupt_auffindbar(browser, server):
    """Ein Feature, das man nicht sieht, gibt es nicht.

    Der Auslöser für den ganzen Kasten: „ich hab da jetzt noch nirgends was
    gefunden wie funktioniert das aus clientsicht."
    """
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        seite.goto(server + "/musik", wait_until="networkidle")
        assert seite.locator("#benachrichtigung").is_visible()
        # Auf beiden Hauptseiten, nicht nur auf einer.
        seite.goto(server + "/hoerbuch", wait_until="networkidle")
        assert seite.locator("#benachrichtigung").is_visible()
    finally:
        seite.close()


def test_noch_nicht_gefragt_bietet_den_knopf(browser, server):
    text, knoepfe = _kasten_text(browser, server, erlaubnis="default")
    assert knoepfe == 1, "Ohne Knopf käme man nie zur Erlaubnis"
    assert "Erlaubnis" in text
    # Was auch ohne Erlaubnis passiert, muss dabeistehen.
    assert "Tab-Titel" in text


def test_erteilte_erlaubnis_wird_bestaetigt(browser, server):
    text, knoepfe = _kasten_text(browser, server, erlaubnis="granted")
    assert "Benachrichtigung an" in text
    assert knoepfe == 0, "Nochmal fragen ginge ohnehin nicht"


def test_abgelehnte_erlaubnis_nennt_den_ausweg(browser, server):
    text, knoepfe = _kasten_text(browser, server, erlaubnis="denied")
    assert "Browsereinstellungen" in text, text
    assert "Tab-Titel" in text
    # Ein Knopf wäre eine Lüge: nach einer Ablehnung fragt der Browser nicht
    # noch einmal, das geht nur über die Einstellungen.
    assert knoepfe == 0


def test_ohne_https_wird_die_grenze_erklaert(browser, server):
    """Der Regelfall auf dem Server: http://musicserver:8000."""
    text, knoepfe = _kasten_text(browser, server, sicher=False)
    assert "HTTPS" in text, text
    assert "Tab-Titel" in text
    assert knoepfe == 0


def test_ohne_notifications_api_bleibt_der_rest(browser, server):
    text, knoepfe = _kasten_text(browser, server, api=False)
    assert "Tab-Titel" in text
    assert knoepfe == 0


def test_knopf_fragt_genau_einmal_und_zieht_den_kasten_nach(browser, server):
    """Der Klick darf nicht zwei Anfragen auslösen.

    Der Knopf im Kasten ist ein <button> -- und damit greift auch der
    allgemeine Klick-Handler, der die Erlaubnis beiläufig anfragt. Zwei Aufrufe
    im selben Tick lehnt der Browser ab, und der Kasten bliebe stumm stehen:
    genau die Ratlosigkeit, gegen die er gebaut wurde.
    """
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        _zustand_setzen(seite, erlaubnis="default")
        seite.add_init_script(
            """
            // Wie im echten Browser: die Antwort kommt später, und bis
            // dahin steht permission weiter auf "default". Ein Stub, der sie
            // sofort setzt, würde den zweiten Aufruf von allein abfangen und
            // den Fehler verdecken -- nachgeprüft, der Test blieb dann auch
            // ohne die Sperre grün.
            window.__anfragen = 0;
            Notification.requestPermission = () => {
              window.__anfragen += 1;
              return new Promise((fertig) => setTimeout(() => {
                Object.defineProperty(Notification, 'permission',
                  {configurable: true, get: () => 'granted'});
                fertig('granted');
              }, 50));
            };
            """
        )
        seite.goto(server + "/musik", wait_until="networkidle")
        seite.locator("#benachrichtigung button").click()
        kasten = seite.locator("#benachrichtigung")
        kasten.get_by_text("Benachrichtigung an").wait_for(timeout=3000)
        assert seite.evaluate("window.__anfragen") == 1
        assert kasten.locator("button").count() == 0
    finally:
        seite.close()


def test_beilaeufige_erlaubnis_aktualisiert_den_kasten(browser, server):
    """Auch wer die Erlaubnis über einen Start-Knopf erteilt, sieht es im Kasten."""
    seite = browser.new_page(viewport={"width": 900, "height": 800})
    try:
        _zustand_setzen(seite, erlaubnis="default")
        seite.add_init_script(
            """
            Notification.requestPermission = () => new Promise((fertig) =>
              setTimeout(() => {
                Object.defineProperty(Notification, 'permission',
                  {configurable: true, get: () => 'denied'});
                fertig('denied');
              }, 50));
            """
        )
        seite.goto(server + "/musik", wait_until="networkidle")
        # Ein anderer Knopf auf der Seite, nicht der im Kasten -- der
        # Daten-CD-Reiter lädt seinen Inhalt erst beim Öffnen nach.
        seite.get_by_role("button", name="Daten-CD").click()
        seite.get_by_role("button", name="Neu einlesen").click()
        seite.locator("#benachrichtigung").get_by_text(
            "Browsereinstellungen"
        ).wait_for(timeout=3000)
    finally:
        seite.close()
