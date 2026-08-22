"""Ein abfotografiertes Cover entgegennehmen.

Das Bild kommt aus einem Formular und ist damit beliebig -- die Endung sagt
nichts, entschieden wird an den ersten Bytes.
"""

from __future__ import annotations

import pytest

from backend import cover

JPEG = b"\xff\xd8\xff\xe0" + b"x" * 100
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100


class TestFormatErkennen:
    def test_jpeg(self):
        assert cover.format_erkennen(JPEG) == "jpeg"

    def test_png(self):
        assert cover.format_erkennen(PNG) == "png"

    @pytest.mark.parametrize(
        "daten", [b"", b"kein bild", b"GIF89a", b"<html>", b"%PDF-1.4"]
    )
    def test_alles_andere_wird_abgelehnt(self, daten):
        with pytest.raises(cover.CoverError):
            cover.format_erkennen(daten)


class TestSpeichern:
    def test_landet_als_cover_jpg(self, tmp_path):
        """Der Name ist keine Willkür -- beets und ABS suchen genau danach."""
        ziel = cover.speichern(tmp_path, JPEG)
        assert ziel.name == "cover.jpg"
        assert ziel.read_bytes() == JPEG

    def test_ersetzt_ein_vorhandenes(self, tmp_path):
        cover.speichern(tmp_path, JPEG)
        neu = b"\xff\xd8\xff\xe0" + b"neu" * 50
        cover.speichern(tmp_path, neu)
        assert (tmp_path / "cover.jpg").read_bytes() == neu

    def test_kein_zwischenprodukt_bleibt_liegen(self, tmp_path):
        cover.speichern(tmp_path, JPEG)
        assert sorted(p.name for p in tmp_path.iterdir()) == ["cover.jpg"]

    def test_leeres_bild(self, tmp_path):
        with pytest.raises(cover.CoverError, match="kein Bild"):
            cover.speichern(tmp_path, b"")

    def test_zu_gross(self, tmp_path):
        riesig = b"\xff\xd8\xff" + b"x" * cover.MAX_BYTES
        with pytest.raises(cover.CoverError, match="zu groß"):
            cover.speichern(tmp_path, riesig)

    def test_fremdes_format(self, tmp_path):
        with pytest.raises(cover.CoverError, match="nicht nach einem Bild"):
            cover.speichern(tmp_path, b"<?php echo 1; ?>")
        assert not (tmp_path / "cover.jpg").exists()

    def test_ohne_zielordner(self, tmp_path):
        with pytest.raises(cover.CoverError, match="gibt es nicht"):
            cover.speichern(tmp_path / "weg", JPEG)

    def test_vorhanden(self, tmp_path):
        assert cover.vorhanden(tmp_path) is False
        cover.speichern(tmp_path, JPEG)
        assert cover.vorhanden(tmp_path) is True

    def test_raeumt_kein_cover_anderer_erweiterung_von_sich_aus_weg(self, tmp_path):
        """speichern() räumt NICHT selbst auf -- es schreibt auch in die
        Upload-Session, wo niemals ein fetchart-'cover.png' liegt. Das
        Aufräumen ist Sache des Aufrufers, siehe TestAndereErweiterungenEntfernen."""
        (tmp_path / "cover.png").write_bytes(b"bleibt hier stehen")
        cover.speichern(tmp_path, JPEG)
        namen = sorted(p.name for p in tmp_path.iterdir())
        assert namen == ["cover.jpg", "cover.png"]


class TestAndereErweiterungenEntfernen:
    def test_entfernt_png_neben_frischem_jpg(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(JPEG)
        (tmp_path / "cover.png").write_bytes(b"altes fetchart-cover")
        cover.andere_erweiterungen_entfernen(tmp_path)
        namen = sorted(p.name for p in tmp_path.iterdir())
        assert namen == ["cover.jpg"]

    def test_ohne_andere_erweiterung_passiert_nichts(self, tmp_path):
        (tmp_path / "cover.jpg").write_bytes(JPEG)
        cover.andere_erweiterungen_entfernen(tmp_path)
        assert [p.name for p in tmp_path.iterdir()] == ["cover.jpg"]


class TestGefunden:
    """Ein von fetchart heruntergeladenes Cover landet unter der Erweiterung
    des Content-Type der Quelle (z. B. 'cover.png'), nicht zwingend als
    'cover.jpg' -- siehe das Docstring von _ERWEITERUNGEN."""

    def test_kein_cover(self, tmp_path):
        assert cover.gefunden(tmp_path) is None

    def test_cover_jpg(self, tmp_path):
        pfad = tmp_path / "cover.jpg"
        pfad.write_bytes(b"x")
        assert cover.gefunden(tmp_path) == pfad

    def test_von_fetchart_heruntergeladenes_png(self, tmp_path):
        pfad = tmp_path / "cover.png"
        pfad.write_bytes(b"x")
        assert cover.gefunden(tmp_path) == pfad
        assert cover.vorhanden(tmp_path) is True

    def test_webp(self, tmp_path):
        pfad = tmp_path / "cover.webp"
        pfad.write_bytes(b"x")
        assert cover.gefunden(tmp_path) == pfad

    def test_cover_jpg_hat_vorrang_vor_png(self, tmp_path):
        """Ein frisches Foto (immer .jpg) soll vor einem älteren, von
        fetchart heruntergeladenen Bild gewinnen -- kommt in der Praxis durch
        das Aufräumen in speichern() eigentlich nicht mehr vor, aber
        gefunden() soll auch robust sein, falls doch mal beides da liegt."""
        (tmp_path / "cover.png").write_bytes(b"alt")
        pfad = tmp_path / "cover.jpg"
        pfad.write_bytes(b"neu")
        assert cover.gefunden(tmp_path) == pfad


class TestVonUrlHolen:
    """Für Discogs-Kandidaten: kein automatischer fetchart-Weg wie bei
    MusicBrainz, siehe das Docstring von ``von_url_holen``."""

    def test_landet_als_cover_jpg(self, tmp_path, monkeypatch):
        class FakeResponse:
            content = JPEG

            def raise_for_status(self) -> None:
                pass

        monkeypatch.setattr(
            cover.requests, "get", lambda url, **kw: FakeResponse()
        )
        ziel = cover.von_url_holen(tmp_path, "https://example.invalid/cover.jpg")
        assert ziel is not None
        assert ziel.name == "cover.jpg"
        assert ziel.read_bytes() == JPEG

    def test_netzfehler_liefert_none_statt_zu_werfen(self, tmp_path, monkeypatch):
        def wirft(url, **kw):
            raise cover.requests.RequestException("kein Netz")

        monkeypatch.setattr(cover.requests, "get", wirft)
        assert cover.von_url_holen(tmp_path, "https://example.invalid/cover.jpg") is None
        assert not cover.vorhanden(tmp_path)

    def test_http_fehlerstatus_liefert_none(self, tmp_path, monkeypatch):
        class FakeResponse:
            content = JPEG

            def raise_for_status(self) -> None:
                raise cover.requests.HTTPError("500")

        monkeypatch.setattr(
            cover.requests, "get", lambda url, **kw: FakeResponse()
        )
        assert cover.von_url_holen(tmp_path, "https://example.invalid/cover.jpg") is None
        assert not cover.vorhanden(tmp_path)

    def test_unbrauchbarer_inhalt_liefert_none(self, tmp_path, monkeypatch):
        """Ein Discogs-Link kann auch mal auf ein PDF oder eine HTML-Fehlerseite
        zeigen -- dieselbe Formaterkennung wie beim fotografierten Cover."""
        class FakeResponse:
            content = b"<html>nicht gefunden</html>"

            def raise_for_status(self) -> None:
                pass

        monkeypatch.setattr(
            cover.requests, "get", lambda url, **kw: FakeResponse()
        )
        assert cover.von_url_holen(tmp_path, "https://example.invalid/cover.jpg") is None
        assert not cover.vorhanden(tmp_path)

    def test_vorhandenes_foto_wuerde_ueberschrieben_wenn_aufgerufen(
        self, tmp_path, monkeypatch
    ):
        """von_url_holen() selbst prüft das nicht -- das Nicht-Überschreiben
        eines fotografierten Covers ist Sache des Aufrufers (routes.choose()),
        nicht dieser Funktion. Dokumentiert das bewusst als Vertrag."""
        cover.speichern(tmp_path, JPEG)
        neu = b"\xff\xd8\xff\xe0" + b"neu" * 50

        class FakeResponse:
            content = neu

            def raise_for_status(self) -> None:
                pass

        monkeypatch.setattr(
            cover.requests, "get", lambda url, **kw: FakeResponse()
        )
        cover.von_url_holen(tmp_path, "https://example.invalid/cover.jpg")
        assert (tmp_path / cover.COVER_DATEI).read_bytes() == neu
