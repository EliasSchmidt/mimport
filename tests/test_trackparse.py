"""Parser für OCR-Tracklisten.

Der Zweck ist nicht semantisches Raten, sondern klare Regeln -- die Tests
prüfen deshalb jeden Schalter einzeln und in Kombination, sowie die Toleranz
gegenüber den häufigsten OCR-Verwechslungen bei Dauern.
"""

from __future__ import annotations

from backend import trackparse


class TestDauerErkennung:
    def test_normale_dauer(self):
        [track] = trackparse.parse_text(
            "Titel - 3:45", trackparse.ParseFlags(tracknummer=False, dauer=True)
        )
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

    def test_dauer_schalter_aus_laesst_endziffern_im_titel(self):
        [track] = trackparse.parse_text(
            "Titel - 3:45", trackparse.ParseFlags(tracknummer=False, dauer=False)
        )
        assert track.duration == ""
        assert track.title == "Titel - 3:45"


class TestParseText:
    def test_nur_titel_ohne_schalter(self):
        flags = trackparse.ParseFlags(tracknummer=False, interpret=False, dauer=False)
        [t1, t2] = trackparse.parse_text("Erster Track\nZweiter Track", flags)
        assert t1.title == "Erster Track"
        assert t2.title == "Zweiter Track"
        assert t1.number == t1.artist == t1.duration == ""

    def test_tracknummer_ohne_trenner(self):
        flags = trackparse.ParseFlags(tracknummer=True, interpret=False, dauer=False)
        [track] = trackparse.parse_text("01 Titel", flags)
        assert track.number == "01"
        assert track.title == "Titel"

    def test_tracknummer_mit_bindestrich(self):
        flags = trackparse.ParseFlags(tracknummer=True, interpret=False, dauer=False)
        [track] = trackparse.parse_text("3 - Ein Titel", flags)
        assert track.number == "3"
        assert track.title == "Ein Titel"

    def test_tracknummer_und_dauer(self):
        flags = trackparse.ParseFlags(tracknummer=True, interpret=False, dauer=True)
        [track] = trackparse.parse_text("2 Ein Titel 3:21", flags)
        assert track.number == "2"
        assert track.title == "Ein Titel"
        assert track.duration == "3:21"

    def test_nur_interpret_trennen(self):
        flags = trackparse.ParseFlags(tracknummer=False, interpret=True, dauer=False)
        [track] = trackparse.parse_text("Interpret - Titel", flags)
        assert track.artist == "Interpret"
        assert track.title == "Titel"

    def test_tracknummer_und_interpret(self):
        flags = trackparse.ParseFlags(tracknummer=True, interpret=True, dauer=False)
        [track] = trackparse.parse_text("01 Interpret - Titel", flags)
        assert track.number == "01"
        assert track.artist == "Interpret"
        assert track.title == "Titel"

    def test_alle_drei_schalter_mit_ocr_tippfehler(self):
        """Der gemeldete Fall vollständig durch den Parser."""
        zeilen = (
            "1 ZIGGY MARLEY - LOVE IS MY RELIGION - 3:51\n"
            "2 NATTALI RIZE - ONE PEOPLE - 4:O2\n"
            "3 SARAH LESCH - TESTAMENT - 5:41"
        )
        flags = trackparse.ParseFlags(tracknummer=True, interpret=True, dauer=True)
        tracks = trackparse.parse_text(zeilen, flags)
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
        flags = trackparse.ParseFlags(tracknummer=False, interpret=False, dauer=False)
        tracks = trackparse.parse_text("Titel 1\n\n  \nTitel 2", flags)
        assert len(tracks) == 2

    def test_vorgabe_ist_tracknummer_und_dauer_ohne_interpret(self):
        [track] = trackparse.parse_text("2 Ein Titel 3:21", trackparse.ParseFlags())
        assert track.number == "2"
        assert track.title == "Ein Titel"
        assert track.duration == "3:21"
