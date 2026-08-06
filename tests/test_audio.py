"""Formaterkennung: verlustfrei oder nicht."""

from __future__ import annotations

from backend.audio import AudioInfo, classify_format, summarize


class TestClassifyFormat:
    def test_bekannte_verlustfreie_formate(self):
        for fmt in ("FLAC", "ALAC", "WAVE", "AIFF", "APE", "WavPack"):
            assert classify_format(fmt) == "lossless", fmt

    def test_bekannte_verlustbehaftete_formate(self):
        for fmt in ("MP3", "AAC", "OGG", "Opus", "Musepack", "Windows Media"):
            assert classify_format(fmt) == "lossy", fmt

    def test_alac_und_aac_werden_unterschieden(self):
        # Beide stecken in .m4a -- die Endung sagt nichts, das Format schon.
        assert classify_format("ALAC") == "lossless"
        assert classify_format("AAC") == "lossy"

    def test_bittiefe_als_rueckfallebene(self):
        # Unbekanntes Format mit Bittiefe: verlustbehaftete Codecs melden hier 0.
        assert classify_format("Irgendwas", bitdepth=24) == "lossless"
        assert classify_format("Irgendwas", bitdepth=0) == "unknown"

    def test_bekanntes_format_schlaegt_bittiefe(self):
        # Manche MP3-Dateien melden eine Bittiefe; der Formatname gewinnt.
        assert classify_format("MP3", bitdepth=16) == "lossy"


class TestSummarize:
    def _info(self, name: str, quality: str, error: str = "") -> AudioInfo:
        from pathlib import Path

        return AudioInfo(
            path=Path(name), display_name=name, quality=quality, error=error
        )

    def test_warnung_nur_bei_verlustbehafteten_dateien(self):
        alles_gut = summarize([self._info("a.flac", "lossless")])
        assert alles_gut["warn"] is False

        gemischt = summarize(
            [self._info("a.flac", "lossless"), self._info("b.mp3", "lossy")]
        )
        assert gemischt["warn"] is True
        assert gemischt["lossy"] == 1
        assert gemischt["lossy_names"] == ["b.mp3"]

    def test_unklare_dateien_loesen_keine_warnung_aus(self):
        # .m4a klärt erst der Server -- das ist kein Grund zu warnen.
        result = summarize([self._info("a.m4a", "unknown")])
        assert result["warn"] is False
        assert result["unknown"] == 1

    def test_unlesbare_dateien_werden_getrennt_gezaehlt(self):
        result = summarize([self._info("kaputt.flac", "unknown", error="defekt")])
        assert result["unreadable"] == 1
        assert result["unreadable_names"] == ["kaputt.flac"]
        # Unlesbar heißt nicht "unklare Qualität" -- sonst doppelte Zählung.
        assert result["unknown"] == 0
