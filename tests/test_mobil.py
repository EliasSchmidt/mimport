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

        # Auch mit aufgeklapptem Formular für das Taggen von Hand -- die
        # Tabelle "Titel je Track" steht darin als eigene Karte, ohne
        # weiteres Aufklappen.
        seite.click("details.manual > summary")
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
        seite.click("details.manual > summary")

        seite.fill("[data-albumartist]", "Deutsche Grammophon")
        seite.check("[data-sampler]")
        assert seite.locator("[data-albumartist]").input_value() == "Deutsche Grammophon"
        seite.uncheck("[data-sampler]")
        assert seite.locator("[data-albumartist]").input_value() == "Deutsche Grammophon"
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


def test_genre_vorschlaege_werden_beim_tippen_aktualisiert(browser, server):
    """Auch nach einem ersten Genre sollen sinnvolle Vorschläge übrig bleiben."""
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
        seite.click("details.manual > summary")

        genre = seite.locator("[data-genre-input]")
        genre.fill("ja")
        vorschlaege = seite.evaluate(
            "Array.from(document.querySelector('[data-genre-vorschlaege]').options).map((o) => o.value)",
        )
        assert "Jazz" in vorschlaege

        genre.fill("Jazz; cl")
        vorschlaege = seite.evaluate(
            "Array.from(document.querySelector('[data-genre-vorschlaege]').options).map((o) => o.value)",
        )
        assert "Jazz; Classical" in vorschlaege
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
        f"Object.defineProperty(window, 'isSecureContext', "
        f"{{get: () => {str(sicher).lower()}}});"
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
