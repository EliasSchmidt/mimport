"""DiscID-Berechnung und Auflösung bei MusicBrainz.

Der Kern ist reine Rechnerei und deshalb ohne Laufwerk prüfbar: MusicBrainz
veröffentlicht einen Testvektor, und an dem hängt hier alles.
"""

from __future__ import annotations

import pytest

from backend import discid

# Der Testvektor aus der MusicBrainz-Dokumentation zur DiscID-Berechnung.
# Sechs Tracks, bekannte Offsets, bekanntes Ergebnis.
VEKTOR_TOC = discid.Toc(
    first_track=1,
    last_track=6,
    leadout=95462,
    offsets=(150, 15363, 32314, 46592, 63414, 80489),
)
VEKTOR_ID = "49HHV7Eb8UKF3aQiNmu1GR8vKTY-"

# Dieselbe CD, wie cdparanoia -Q sie ausgibt. Die Startsektoren stehen hier
# ohne den Vorlauf von 150, den MusicBrainz mitzählt -- genau die Umrechnung,
# die der Parser leisten muss.
CDPARANOIA_AUSGABE = """\
cdparanoia III release 10.2 (September 11, 2008)

Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    15213 [03:22.63]        0 [00:00.00]    no   no  2
  2.    16951 [03:46.01]    15213 [03:22.63]    no   no  2
  3.    14278 [03:10.28]    32164 [07:08.64]    no   no  2
  4.    16822 [03:44.22]    46442 [10:19.17]    no   no  2
  5.    17075 [03:47.50]    63264 [14:03.39]    no   no  2
  6.    14973 [03:19.48]    80339 [17:51.14]    no   no  2
TOTAL   95312 [21:10.62]    (audio only)
"""


class TestCalculate:
    def test_testvektor_von_musicbrainz(self):
        """Wenn dieser Test fällt, ist die Berechnung falsch -- nichts anderes."""
        assert discid.calculate(VEKTOR_TOC) == VEKTOR_ID

    def test_leerer_toc_wird_abgelehnt(self):
        leer = discid.Toc(first_track=1, last_track=1, leadout=0, offsets=())
        with pytest.raises(discid.DiscIdError):
            discid.calculate(leer)

    def test_ergebnis_ist_url_tauglich(self):
        kennung = discid.calculate(VEKTOR_TOC)
        # Die ersetzten Zeichen dürfen nicht mehr vorkommen.
        assert not set(kennung) & {"+", "/", "="}
        assert len(kennung) == 28


class TestParseCdparanoiaToc:
    def test_ausgabe_ergibt_denselben_toc(self):
        toc = discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        assert toc.first_track == 1
        assert toc.last_track == 6
        assert toc.offsets == VEKTOR_TOC.offsets
        assert toc.leadout == VEKTOR_TOC.leadout

    def test_von_der_ausgabe_bis_zur_discid(self):
        """Die eigentliche Probe: Parser und Berechnung zusammen."""
        toc = discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        assert discid.calculate(toc) == VEKTOR_ID

    def test_ohne_audio_tracks(self):
        """Eine Daten-CD gehört in den Ordner-Auswähler, nicht hierher."""
        with pytest.raises(discid.DiscIdError, match="keine Audio-CD"):
            discid.parse_cdparanoia_toc("Unable to open disc.\n")

    def test_spieldauer_und_rohgroesse(self):
        toc = discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)
        assert toc.track_count == 6
        # 95312 Sektoren zu 1/75 s sind gut 21 Minuten.
        assert 21 * 60 < toc.total_seconds < 22 * 60
        # Unkomprimiert etwa 214 MB -- die Obergrenze für die Platzprüfung.
        assert 200 * 1024**2 < toc.raw_bytes < 230 * 1024**2


class TestLookup:
    def test_unbekannte_disc_gibt_leere_liste(self, monkeypatch):
        class Antwort:
            status_code = 404
            ok = False

        monkeypatch.setattr(discid.requests, "get", lambda *a, **k: Antwort())
        assert discid.lookup("egal") == []

    def test_netzfehler_wird_gemeldet(self, monkeypatch):
        def kaputt(*args, **kwargs):
            raise discid.requests.RequestException("kein Netz")

        monkeypatch.setattr(discid.requests, "get", kaputt)
        with pytest.raises(discid.DiscIdError, match="nicht erreichbar"):
            discid.lookup("egal")

    def test_releases_werden_uebernommen(self, monkeypatch):
        class Antwort:
            status_code = 200
            ok = True

            def json(self):
                return {
                    "releases": [
                        {
                            "id": "d3dc4be9-9749-4959-99e5-133d0cb467fe",
                            "title": "Ettella Diamant",
                            "date": "2004",
                            "country": "SK",
                        },
                        {"title": "ohne ID wird übersprungen"},
                    ]
                }

        monkeypatch.setattr(discid.requests, "get", lambda *a, **k: Antwort())
        treffer = discid.lookup("egal")
        assert len(treffer) == 1
        assert treffer[0].mbid == "d3dc4be9-9749-4959-99e5-133d0cb467fe"
        assert "2004" in treffer[0].label
        assert "SK" in treffer[0].label

    @pytest.mark.network
    def test_echte_abfrage(self):
        """Gegen MusicBrainz selbst -- prüft die Antwortstruktur.

        Ein Ausfall dort ist kein Fehler hier: MusicBrainz antwortet unter
        Last mit 503, und daran ist an diesem Code nichts zu reparieren.
        Übersprungen wird deshalb, statt die Suite rot zu färben.
        """
        try:
            treffer = discid.lookup(VEKTOR_ID)
        except discid.DiscIdError as exc:
            pytest.skip(f"MusicBrainz nicht verfügbar: {exc}")
        assert treffer, "Der Testvektor ist eine real eingetragene CD"
        assert all(len(t.mbid) == 36 for t in treffer)
