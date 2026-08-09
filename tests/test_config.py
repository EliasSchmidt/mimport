

class TestM4bZeitschranken:
    """Die Werte sind gemessen, nicht geraten -- und das soll so bleiben.

    Auf dem Zielrechner: 46× Echtzeit, ein 7:21:00-Hörbuch in knapp zehn
    Minuten. Wer die Vorgaben ändert, soll an diesem Test merken, dass dahinter
    eine Messung steht.
    """

    #: Was gemessen wurde. Grundlage beider Schranken.
    FAKTOR = 46.0

    def test_zeitlimit_deckt_ein_sehr_langes_buch(self):
        from backend.config import Settings

        s = Settings()
        # 30 Stunden Hörbuch, und die Maschine sei nur halb so schnell wie
        # gemessen -- etwa weil parallel gerippt wird.
        noetig = 30 * 3600 / (self.FAKTOR / 2)
        assert s.m4b_timeout > noetig, (
            f"{s.m4b_timeout} s reichen nicht für {noetig:.0f} s Bedarf"
        )
        # Aber auch nicht ins Absurde: sechs Stunden entsprächen 276 Stunden Buch.
        assert s.m4b_timeout <= 3 * 3600

    def test_stillstand_ist_kuerzer_als_ein_ganzer_bau(self):
        from backend.config import Settings

        s = Settings()
        ein_bau = 7.35 * 3600 / self.FAKTOR  # das gemessene Buch: ~575 s
        assert s.m4b_stillstand < ein_bau, (
            "Eine Überwachung, die länger wartet als ein kompletter Bau dauert, "
            "ist keine"
        )
        # Und lang genug für die einzige legitime Stille: faststart schreibt die
        # fertige Datei um, nachgemessen unter einer Minute.
        assert s.m4b_stillstand >= 120
