"""Parser für OCR-Tracklisten.

Der Zweck ist nicht semantisches Raten, sondern klare Regeln -- die Tests
prüfen deshalb genau die Layouts aus ``_MODE_LABELS`` sowie die Toleranz
gegenüber den häufigsten OCR-Verwechslungen bei Dauern.
"""

from __future__ import annotations

from backend import trackparse


class TestDauerErkennung:
    def test_normale_dauer(self):
        [track] = trackparse.parse_text("Titel - 3:45", "artist_dash_title")
        assert track.duration == "3:45"
        assert track.title == "Titel"

    def test_ocr_verwechslung_i_statt_eins(self):
        text, dauer = trackparse._extract_duration("Titel - I:23")
        assert dauer == "1:23"
        assert text == "Titel"

    def test_ocr_verwechslung_o_direkt(self):
        text, dauer = trackparse._extract_duration("Titel - 4:O2")
        assert dauer == "4:02"
        assert text == "Titel"

    def test_ocr_verwechslung_gemischt(self):
        text, dauer = trackparse._extract_duration("Titel - l:Oo")
        assert dauer == "1:00"
        assert text == "Titel"

    def test_ungueltige_dauer_bleibt_im_titel(self):
        """Kein Ziffern-Ersatz für alles -- nur die engen OCR-Fälle."""
        text, dauer = trackparse._extract_duration("Titel - 4:XY")
        assert dauer == ""
        assert text == "Titel - 4:XY"

    def test_ohne_dauer(self):
        text, dauer = trackparse._extract_duration("Nur ein Titel")
        assert dauer == ""
        assert text == "Nur ein Titel"

    def test_stunden_minuten_sekunden(self):
        text, dauer = trackparse._extract_duration("Titel - 1:02:O3")
        assert dauer == "1:02:03"
        assert text == "Titel"


class TestParseText:
    def test_plain_title(self):
        [t1, t2] = trackparse.parse_text("Erster Track\nZweiter Track", "plain_title")
        assert t1.title == "Erster Track"
        assert t2.title == "Zweiter Track"
        assert t1.number == t1.artist == t1.duration == ""

    def test_track_title(self):
        [track] = trackparse.parse_text("01 Titel", "track_title")
        assert track.number == "01"
        assert track.title == "Titel"

    def test_track_dash_title(self):
        [track] = trackparse.parse_text("3 - Ein Titel", "track_dash_title")
        assert track.number == "3"
        assert track.title == "Ein Titel"

    def test_track_title_duration(self):
        [track] = trackparse.parse_text("2 Ein Titel 3:21", "track_title_duration")
        assert track.number == "2"
        assert track.title == "Ein Titel"
        assert track.duration == "3:21"

    def test_artist_dash_title(self):
        [track] = trackparse.parse_text("Interpret - Titel", "artist_dash_title")
        assert track.artist == "Interpret"
        assert track.title == "Titel"

    def test_track_artist_dash_title(self):
        [track] = trackparse.parse_text(
            "01 Interpret - Titel", "track_artist_dash_title"
        )
        assert track.number == "01"
        assert track.artist == "Interpret"
        assert track.title == "Titel"

    def test_track_artist_dash_title_duration_mit_ocr_tippfehler(self):
        """Der gemeldete Fall vollständig durch den Parser."""
        zeilen = (
            "1 ZIGGY MARLEY - LOVE IS MY RELIGION - 3:51\n"
            "2 NATTALI RIZE - ONE PEOPLE - 4:O2\n"
            "3 SARAH LESCH - TESTAMENT - 5:41"
        )
        tracks = trackparse.parse_text(zeilen, "track_artist_dash_title_duration")
        assert [t.number for t in tracks] == ["1", "2", "3"]
        assert [t.artist for t in tracks] == [
            "ZIGGY MARLEY",
            "NATTALI RIZE",
            "SARAH LESCH",
        ]
        assert [t.title for t in tracks] == [
            "LOVE IS MY RELIGION",
            "ONE PEOPLE",
            "TESTAMENT",
        ]
        assert [t.duration for t in tracks] == ["3:51", "4:02", "5:41"]

    def test_leere_zeilen_werden_uebersprungen(self):
        tracks = trackparse.parse_text("Titel 1\n\n  \nTitel 2", "plain_title")
        assert len(tracks) == 2

    def test_unbekannter_modus_faellt_auf_titel_zurueck(self):
        [track] = trackparse.parse_text("Irgendein Text", "hoppla")
        assert track.title == "Irgendein Text"


class TestModes:
    def test_alle_modi_sind_gelabelt(self):
        werte = {eintrag["value"] for eintrag in trackparse.modes()}
        assert werte == set(trackparse._MODE_LABELS)
