"""Hörbücher: Discs sammeln und zur m4b bündeln.

Der wichtigste Teil hier sind die beiden Bremsen: nicht löschen, wenn die
Laufzeit nicht passt, und gar nicht erst umwandeln, wenn die Quelle schon
verlustbehaftet ist.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from backend import audiobook

HAT_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
braucht_ffmpeg = pytest.mark.skipif(HAT_FFMPEG is False, reason="ffmpeg fehlt")


@pytest.fixture(autouse=True)
def bibliothek(tmp_path, monkeypatch):
    wurzel = tmp_path / "audiobooks"
    monkeypatch.setattr(audiobook.settings, "audiobook_root", wurzel)
    audiobook._m4b_job = None
    yield wurzel
    audiobook._m4b_job = None


def toene(ordner, laengen, *, praefix="Kapitel"):
    """Echte FLAC-Dateien mit bekannter Spieldauer."""
    ordner.mkdir(parents=True, exist_ok=True)
    pfade = []
    for i, sek in enumerate(laengen, start=1):
        ziel = ordner / f"{i:02d} Track {i}.flac"
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                "-i", f"sine=frequency={200 + 50 * i}:duration={sek}",
                "-metadata", f"title={praefix} {i}", str(ziel), "-y",
            ],
            check=True,
        )
        pfade.append(ziel)
    return pfade


class TestBookDir:
    """Autor und Titel kommen aus einem Formular und sind damit feindlich."""

    def test_normaler_pfad(self, bibliothek):
        buch = audiobook.book_dir("Frank Herbert", "Der Wüstenplanet")
        assert buch == (bibliothek / "Frank Herbert" / "Der Wüstenplanet").resolve()

    def test_aufstieg_wird_entschaerft(self, bibliothek):
        buch = audiobook.book_dir("../../etc", "passwd")
        assert buch.is_relative_to(bibliothek.resolve())
        assert ".." not in buch.parts

    def test_schraegstrich_bleibt_eine_ebene(self, bibliothek):
        buch = audiobook.book_dir("a/b", "c/d")
        # Aus zwei Segmenten dürfen keine vier werden.
        assert buch.relative_to(bibliothek.resolve()).parts == ("a_b", "c_d")

    @pytest.mark.parametrize("autor,titel", [("", "Titel"), ("Autor", "  ")])
    def test_leere_angaben(self, autor, titel):
        with pytest.raises(audiobook.AudiobookError):
            audiobook.book_dir(autor, titel)


class TestNextDiscDir:
    def test_zaehlt_hoch(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        assert audiobook.next_disc_dir(buch).name == "CD 1"
        (buch / "CD 1").mkdir(parents=True)
        assert audiobook.next_disc_dir(buch).name == "CD 2"
        (buch / "CD 2").mkdir()
        assert audiobook.next_disc_dir(buch).name == "CD 3"

    def test_erste_datencd_bleibt_flach(self, bibliothek):
        """Eine MP3-CD trägt meist das ganze Buch -- kein einsames 'CD 1'."""
        buch = audiobook.book_dir("A", "B")
        assert audiobook.next_disc_dir(buch, ist_datencd=True) == buch

    def test_zweite_datencd_bekommt_doch_eine_nummer(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "01.mp3").write_bytes(b"x")
        assert audiobook.next_disc_dir(buch, ist_datencd=True).name == "CD 1"


class TestNatuerlicheSortierung:
    def test_cd10_kommt_nach_cd2(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        for cd in (1, 2, 10):
            ordner = buch / f"CD {cd}"
            ordner.mkdir(parents=True)
            (ordner / "01 Track 1.flac").write_bytes(b"x")

        namen = [str(p.parent.name) for p in audiobook.audio_files(buch)]
        assert namen == ["CD 1", "CD 2", "CD 10"]

    def test_track10_kommt_nach_track2(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        for n in (1, 2, 10):
            (buch / f"{n} Track.flac").write_bytes(b"x")

        namen = [p.name for p in audiobook.audio_files(buch)]
        assert namen == ["1 Track.flac", "2 Track.flac", "10 Track.flac"]


class TestBuchliste:
    def test_zuletzt_geaendertes_buch_steht_oben(self, bibliothek):
        import os

        alt = audiobook.book_dir("Autor", "Alt")
        neu = audiobook.book_dir("Autor", "Neu")
        alt.mkdir(parents=True)
        neu.mkdir(parents=True)
        (alt / "01.mp3").write_bytes(b"\xff\xfb")
        (neu / "01.mp3").write_bytes(b"\xff\xfb")

        os.utime(alt / "01.mp3", (1, 1))
        os.utime(alt, (1, 1))
        os.utime(neu / "01.mp3", (2, 2))
        os.utime(neu, (2, 2))

        buecher = audiobook.list_books()
        assert [(b.autor, b.titel) for b in buecher[:2]] == [
            ("Autor", "Neu"),
            ("Autor", "Alt"),
        ]


class TestQuellenAufraeumen:
    """Die Bremse vor dem Löschen -- die CD ist das Archiv, aber ein
    misslungener Encode wäre trotzdem Datenverlust."""

    def test_passende_laufzeit_loescht(self, bibliothek, monkeypatch):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        quellen = [buch / "01.flac", buch / "02.flac"]
        for q in quellen:
            q.write_bytes(b"x")
        ziel = buch / "B.m4b"
        ziel.write_bytes(b"x")

        job = audiobook.M4bJob(buch=str(buch))
        # m4b und Quellen sind gleich lang.
        monkeypatch.setattr(audiobook, "_probe_duration", lambda p: 3600.0)
        audiobook._laufzeit_pruefen(job, ziel, 3600.0)
        audiobook._quellen_loeschen(job, quellen, ziel)

        assert job.geloescht == 2
        assert not any(q.exists() for q in quellen)

    def test_abweichende_laufzeit_loescht_nichts(self, bibliothek, monkeypatch):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        quellen = [buch / "01.flac", buch / "02.flac"]
        for q in quellen:
            q.write_bytes(b"x")
        ziel = buch / "B.m4b"
        ziel.write_bytes(b"x")

        # Der m4b fehlt eine Viertelstunde -- ein ganzer Track.
        monkeypatch.setattr(audiobook, "_probe_duration", lambda p: 2700.0)

        job = audiobook.M4bJob(buch=str(buch))
        with pytest.raises(audiobook.AudiobookError, match="passen nicht zusammen"):
            audiobook._laufzeit_pruefen(job, ziel, 3600.0)

        assert job.geloescht == 0
        assert all(q.exists() for q in quellen), "Quellen müssen unangetastet bleiben"

    def test_toleranz_deckt_encoder_padding(self, bibliothek, monkeypatch):
        """Millisekunden sind normal, Minuten nicht."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        quelle = buch / "01.flac"
        quelle.write_bytes(b"x")
        ziel = buch / "B.m4b"
        ziel.write_bytes(b"x")

        monkeypatch.setattr(audiobook, "_probe_duration", lambda p: 3600.9)
        job = audiobook.M4bJob(buch=str(buch))
        audiobook._laufzeit_pruefen(job, ziel, 3600.0)
        audiobook._quellen_loeschen(job, [quelle], ziel)
        assert job.geloescht == 1


class TestConcatEscaping:
    def test_apostroph_im_pfad(self, tmp_path):
        pfad = tmp_path / "Rock'n'Roll.flac"
        zeile = audiobook._concat_line(pfad)
        # Der String darf nicht vorzeitig enden.
        assert zeile.startswith("file '")
        assert zeile.rstrip().endswith("'")
        assert "'\\''" in zeile


@braucht_ffmpeg
class TestBauen:
    """Gegen echtes ffmpeg -- ohne das ist über den Encode nichts zu sagen."""

    def test_zwei_discs_werden_eine_m4b_mit_kapiteln(self, bibliothek):
        import time

        buch = audiobook.book_dir("Frank Herbert", "Der Wüstenplanet")
        toene(buch / "CD 1", [2, 3], praefix="Erste CD")
        toene(buch / "CD 2", [2], praefix="Zweite CD")

        job = audiobook.build(buch)
        for _ in range(300):
            if not job.laeuft:
                break
            time.sleep(0.2)

        assert job.zustand == "fertig", job.fehler
        ergebnis = buch / "Der Wüstenplanet.m4b"
        assert ergebnis.is_file()

        # Kapitel in der Reihenfolge der Discs.
        roh = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(ergebnis)],
            capture_output=True, text=True, check=True,
        ).stdout
        import json

        kapitel = [k["tags"]["title"] for k in json.loads(roh)["chapters"]]
        assert kapitel == ["Erste CD 1", "Erste CD 2", "Zweite CD 1"]

        # Quellen weg, leere Disc-Ordner auch.
        assert job.geloescht == 3
        assert sorted(p.name for p in buch.iterdir()) == ["Der Wüstenplanet.m4b"]

    def test_verlustbehaftete_quelle_wird_abgelehnt(self, bibliothek):
        """lossy auf lossy bringt nichts außer Verlust."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
             "-i", "sine=frequency=300:duration=2", "-b:a", "48k",
             str(buch / "01.mp3"), "-y"],
            check=True,
        )

        with pytest.raises(audiobook.AudiobookError, match="verlustbehaftet"):
            audiobook.build(buch)

    def test_ohne_quellen(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        with pytest.raises(audiobook.AudiobookError, match="keine Quelldateien"):
            audiobook.build(buch)


class TestUnstimmigerZustand:
    """Nach einem abgebrochenen Bündeln liegen m4b und Quellen nebeneinander.

    Audiobookshelf zeigt das Buch dann doppelt an -- die Oberfläche darf das
    keinesfalls als „fertig" ausgeben.
    """

    def test_m4b_neben_quellen_ist_unstimmig(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "01.flac").write_bytes(b"x")
        (buch / "B.m4b").write_bytes(b"x")

        zustand = audiobook.state(buch)
        assert zustand.has_m4b
        assert zustand.file_count == 1
        assert zustand.unstimmig is True

    def test_nur_m4b_ist_fertig(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "B.m4b").write_bytes(b"x")

        zustand = audiobook.state(buch)
        assert zustand.has_m4b
        assert zustand.unstimmig is False

    def test_nur_quellen_ist_nicht_unstimmig(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "01.flac").write_bytes(b"x")

        assert audiobook.state(buch).unstimmig is False

    @braucht_ffmpeg
    def test_abgebrochener_bau_laesst_das_buch_sauber(self, bibliothek, monkeypatch):
        """Seit die m4b im Staging entsteht, gibt es diesen Zustand nicht mehr.

        Früher hatte ffmpeg schon in den Buchordner geschrieben, bevor die
        Laufzeitprüfung durchfiel -- m4b und Quellen lagen nebeneinander, und
        Audiobookshelf zeigte das Buch doppelt. Jetzt kommt die m4b gar nicht
        erst dorthin.
        """
        import time

        buch = audiobook.book_dir("A", "B")
        toene(buch, [2, 2])

        echt = audiobook._probe_duration
        monkeypatch.setattr(
            audiobook,
            "_probe_duration",
            lambda p: 1.0 if str(p).endswith(".m4b") else echt(p),
        )

        job = audiobook.build(buch)
        for _ in range(300):
            if not job.laeuft:
                break
            time.sleep(0.2)

        assert job.zustand == "fehler"
        assert job.geloescht == 0, "Quellen müssen stehen bleiben"
        zustand = audiobook.state(buch)
        assert zustand.unstimmig is False
        assert zustand.has_m4b is False, "die m4b darf nicht im Buch liegen"
        assert zustand.file_count == 2, "die Quellen aber schon"


class TestKapitelnamen:
    """Nach einem Rip gibt es keine Tags -- und der Dateiname wiederholt sich."""

    @braucht_ffmpeg
    def test_rip_ohne_tags_wird_durchgezaehlt(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        # So benennt der Rip: jede Disc fängt wieder bei 01 an.
        for cd in (1, 2):
            ordner = buch / f"CD {cd}"
            ordner.mkdir(parents=True)
            for i in (1, 2):
                subprocess.run(
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", "sine=frequency=300:duration=1",
                     str(ordner / f"{i:02d} Track {i}.flac"), "-y"],
                    check=True,
                )

        namen = audiobook.chapter_titles(audiobook.audio_files(buch))
        assert namen == ["Kapitel 1", "Kapitel 2", "Kapitel 3", "Kapitel 4"]
        assert len(set(namen)) == 4, "Kapitelnamen dürfen sich nicht wiederholen"

    @braucht_ffmpeg
    def test_vorhandene_titel_werden_uebernommen(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        toene(buch, [1, 1], praefix="Die Reise")

        namen = audiobook.chapter_titles(audiobook.audio_files(buch))
        assert namen == ["Die Reise 1", "Die Reise 2"]

    @braucht_ffmpeg
    def test_doppelte_titel_werden_durchgezaehlt(self, bibliothek):
        """Zwei gleich benannte Tracks sind im Player nicht auseinanderzuhalten."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        for i in (1, 2):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "sine=frequency=300:duration=1",
                 "-metadata", "title=Intro", str(buch / f"{i:02d}.flac"), "-y"],
                check=True,
            )

        namen = audiobook.chapter_titles(audiobook.audio_files(buch))
        # Der Name bleibt erhalten, die Nummer macht ihn unterscheidbar.
        assert namen == ["1. Intro", "2. Intro"]
        assert len(set(namen)) == 2

    @braucht_ffmpeg
    def test_eigene_namen_landen_in_der_m4b(self, bibliothek):
        import json
        import time

        buch = audiobook.book_dir("A", "B")
        toene(buch, [1, 1])

        job = audiobook.build(buch, titel=["Vorwort", "Erstes Kapitel"])
        for _ in range(300):
            if not job.laeuft:
                break
            time.sleep(0.2)
        assert job.zustand == "fertig", job.fehler

        roh = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json",
             str(buch / "B.m4b")],
            capture_output=True, text=True, check=True,
        ).stdout
        assert [k["tags"]["title"] for k in json.loads(roh)["chapters"]] == [
            "Vorwort",
            "Erstes Kapitel",
        ]

    def test_zeilenumbruch_zerlegt_die_metadatei_nicht(self, bibliothek, monkeypatch):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        quelle = buch / "01.flac"
        quelle.write_bytes(b"x")
        monkeypatch.setattr(audiobook, "_probe_duration", lambda p: 1.0)

        meta = buch / "meta.txt"
        audiobook._kapitel_schreiben(buch, [quelle], meta, ["Kapitel\nmit Umbruch"])
        titel_zeilen = [z for z in meta.read_text().splitlines() if z.startswith("title=")]
        # Buchtitel plus genau ein Kapitel -- kein zusätzlicher Eintrag.
        assert len(titel_zeilen) == 2


@braucht_ffmpeg
class TestZweitesBuendeln:
    """Der Fall, der stillschweigend Discs verlor.

    Nach einem erfolgreichen Bündeln sind die Quellen gelöscht. Kommt danach
    eine weitere Disc dazu, kennt ein neuer Bau nur noch diese -- und würde
    die m4b mit allen früheren Discs überschreiben.
    """

    def _bauen(self, buch, **kwargs):
        import time

        audiobook._m4b_job = None
        job = audiobook.build(buch, **kwargs)
        for _ in range(300):
            if not job.laeuft:
                break
            time.sleep(0.2)
        return job

    def test_zweiter_bau_wird_abgelehnt(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        toene(buch / "CD 1", [2, 2])
        assert self._bauen(buch).zustand == "fertig"

        m4b = buch / "B.m4b"
        vorher = audiobook._probe_duration(m4b)
        assert vorher > 3

        # Zweite Disc kommt dazu.
        toene(buch / "CD 2", [2])
        audiobook._m4b_job = None
        with pytest.raises(audiobook.AudiobookError, match="bereits eine m4b"):
            audiobook.build(buch)

        # Entscheidend: die m4b ist unangetastet.
        assert abs(audiobook._probe_duration(m4b) - vorher) < 0.5

    def test_mit_ersetzen_geht_es_bewusst_doch(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        toene(buch / "CD 1", [2])
        assert self._bauen(buch).zustand == "fertig"

        toene(buch / "CD 2", [3])
        job = self._bauen(buch, ersetzen=True)
        assert job.zustand == "fertig"
        # Jetzt enthält sie nur noch CD 2 -- so gewollt und bestätigt.
        assert abs(audiobook._probe_duration(buch / "B.m4b") - 3) < 0.5

    def test_alle_discs_zuerst_ist_der_richtige_weg(self, bibliothek):
        """Die Reihenfolge, zu der die Oberfläche rät."""
        buch = audiobook.book_dir("A", "B")
        toene(buch / "CD 1", [2, 2])
        toene(buch / "CD 2", [3])

        assert self._bauen(buch).zustand == "fertig"
        # 2+2+3 Sekunden, alles drin.
        assert abs(audiobook._probe_duration(buch / "B.m4b") - 7) < 0.5


class TestVonVornEinlesen:
    """Ein fertiges Buch darf keine Sackgasse sein."""

    def test_m4b_wird_beiseite_gelegt_nicht_geloescht(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        m4b = buch / "B.m4b"
        m4b.write_bytes(b"alte fassung")

        beiseite = audiobook.m4b_beiseite_legen(buch)

        assert beiseite is not None
        assert not m4b.exists()
        # Der Inhalt ist erhalten -- scheitert der neue Versuch, ist das alles,
        # was noch da ist.
        assert beiseite.read_bytes() == b"alte fassung"

    def test_beiseite_gelegtes_gilt_nicht_mehr_als_hoerbuch(self, bibliothek):
        """Sonst zeigte Audiobookshelf das Buch doppelt an."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "B.m4b").write_bytes(b"x")
        audiobook.m4b_beiseite_legen(buch)

        zustand = audiobook.state(buch)
        assert zustand.has_m4b is False
        assert zustand.file_count == 0
        assert audiobook.audio_files(buch) == []

    def test_ohne_m4b_passiert_nichts(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        assert audiobook.m4b_beiseite_legen(buch) is None

    def test_mehrmals_von_vorn_ueberschreibt_nichts(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)

        (buch / "B.m4b").write_bytes(b"erste")
        erste = audiobook.m4b_beiseite_legen(buch)
        (buch / "B.m4b").write_bytes(b"zweite")
        zweite = audiobook.m4b_beiseite_legen(buch)

        assert erste != zweite
        assert erste.read_bytes() == b"erste"
        assert zweite.read_bytes() == b"zweite"

    def test_danach_baut_es_ohne_ersetzen(self, bibliothek, monkeypatch):
        """Weil keine m4b mehr im Weg liegt, greift die Rückfrage nicht."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "B.m4b").write_bytes(b"alt")
        audiobook.m4b_beiseite_legen(buch)
        (buch / "01.flac").write_bytes(b"x")

        monkeypatch.setattr(audiobook, "_probe_duration", lambda p: 1.0)
        monkeypatch.setattr(audiobook, "_probe_kbps", lambda p: 0)
        monkeypatch.setattr(audiobook.threading, "Thread", _FakeThread)

        # Kein AudiobookError über eine schon vorhandene m4b.
        job = audiobook.build(buch)
        assert job.fehler is None


class _FakeThread:
    def __init__(self, *args, **kwargs):
        pass

    def start(self):
        pass


class TestGroesseUndDauer:
    """Was in der Liste steht -- und was der Nutzer ablesen soll."""

    def test_fertiges_buch_zeigt_die_m4b_groesse(self, bibliothek):
        """Stand vorher auf „0 MB": die Quellen sind ja gelöscht."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "B.m4b").write_bytes(b"x" * 5 * 1024**2)

        zustand = audiobook.state(buch)
        assert zustand.total_bytes == 0, "die m4b ist keine Quelle"
        assert zustand.m4b_bytes == 5 * 1024**2
        assert zustand.size_label == "5 MB"

    def test_angefangenes_buch_zeigt_die_quellen(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.flac").write_bytes(b"x" * 3 * 1024**2)

        assert audiobook.state(buch).size_label == "3 MB"

    def test_unstimmiges_buch_zaehlt_beides(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "01.flac").write_bytes(b"x" * 2 * 1024**2)
        (buch / "B.m4b").write_bytes(b"x" * 1024**2)

        assert audiobook.state(buch).size_label == "3 MB"

    def test_kleine_dateien_werden_nicht_zu_null(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "B.m4b").write_bytes(b"x" * 4096)
        assert audiobook.state(buch).size_label == "4 KB"

    def test_dauer_und_faktor(self):
        # Ein abgeschlossener Bau: fertig und gesamt fallen zusammen. Der Test
        # setzte vorher nur ``sekunden_gesamt`` und hielt damit ausgerechnet
        # die Rechnung fest, die während des Laufs falsch war.
        job = audiobook.M4bJob(
            buch="/x", sekunden_gesamt=3600.0, sekunden_fertig=3600.0
        )
        job.gestartet = 0.0
        job.beendet = 450.0
        assert job.dauer_text == "7:30"
        # Eine Stunde Audio in 7,5 Minuten sind 8× Echtzeit.
        assert job.faktor_text == "8.0× Echtzeit"

    def test_faktor_bleibt_leer_ohne_messwerte(self):
        job = audiobook.M4bJob(buch="/x")
        assert job.faktor_text == ""


class TestStagingInDerBibliothek:
    """Unfertiges entsteht neben der Bibliothek, nicht darin.

    Audiobookshelf scannt den Buchordner; eine halb gelesene Disc oder eine
    wachsende m4b würde es als unvollständiges Buch einlesen. Das Staging liegt
    trotzdem unter ``audiobook_root`` -- nur so ist das Verschieben ein
    Umbenennen und kein Kopiervorgang über Dateisystemgrenzen.
    """

    def test_staging_liegt_in_der_bibliothek(self, bibliothek):
        assert audiobook.staging_dir().parent == bibliothek.resolve()
        assert audiobook.staging_dir().name.startswith(".")

    def test_arbeitsordner_sind_eindeutig(self, bibliothek):
        a = audiobook.neuer_arbeitsordner("disc")
        b = audiobook.neuer_arbeitsordner("disc")
        assert a != b and a.is_dir() and b.is_dir()

    def test_staging_taucht_nicht_als_buch_auf(self, bibliothek):
        arbeit = audiobook.neuer_arbeitsordner("disc")
        (arbeit / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")

        assert audiobook.list_books() == [], "Unfertiges ist kein Buch"

    def test_fertigstellen_ist_ein_umbenennen(self, bibliothek):
        arbeit = audiobook.neuer_arbeitsordner("disc")
        (arbeit / "01.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        buch = audiobook.book_dir("A", "B")

        ziel = audiobook.fertigstellen(arbeit, audiobook.next_disc_dir(buch))

        assert ziel.name == "CD 1"
        assert (ziel / "01.flac").is_file()
        assert not arbeit.exists()

    def test_belegtes_ziel_gibt_einen_klaren_fehler(self, bibliothek):
        """Nach Stunden Arbeit darf nichts stillschweigend danebenlanden."""
        buch = audiobook.book_dir("A", "B")
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "schon da.flac").write_bytes(b"x")

        arbeit = audiobook.neuer_arbeitsordner("disc")
        (arbeit / "neu.flac").write_bytes(b"x")

        with pytest.raises(audiobook.AudiobookError, match="nicht verloren"):
            audiobook.fertigstellen(arbeit, buch / "CD 1")

        # Das Ergebnis liegt noch im Staging, nichts ist weg.
        assert (arbeit / "neu.flac").is_file()
        assert (buch / "CD 1" / "schon da.flac").is_file()

    def test_aufraeumen_entfernt_reste(self, bibliothek):
        arbeit = audiobook.neuer_arbeitsordner("disc")
        (arbeit / "halb.flac").write_bytes(b"x" * 1000)

        assert audiobook.staging_aufraeumen() == 1
        assert not arbeit.exists()
        # Der Staging-Ordner selbst bleibt.
        assert audiobook.staging_dir().is_dir()

    @braucht_ffmpeg
    def test_m4b_entsteht_im_staging_und_wird_verschoben(self, bibliothek):
        import time

        buch = audiobook.book_dir("A", "B")
        toene(buch, [1, 1])

        job = audiobook.build(buch)
        for _ in range(300):
            if not job.laeuft:
                break
            time.sleep(0.2)

        assert job.zustand == "fertig", job.fehler
        assert (buch / "B.m4b").is_file()
        # Im Staging bleibt nichts zurück.
        assert list(audiobook.staging_dir().iterdir()) == []


class TestDiscsNormalisieren:
    """Die erste Daten-CD liegt flach -- kommt eine zweite, muss sie umziehen."""

    def test_flache_dateien_wandern_nach_cd1(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "01.mp3").write_bytes(b"\xff\xfb")
        (buch / "02.mp3").write_bytes(b"\xff\xfb")

        audiobook.discs_normalisieren(buch)

        assert (buch / "CD 1" / "01.mp3").is_file()
        assert not (buch / "01.mp3").exists()

    def test_unterordner_wandern_mit(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        (buch / "Disc 1").mkdir(parents=True)
        (buch / "Disc 1" / "01.mp3").write_bytes(b"\xff\xfb")

        audiobook.discs_normalisieren(buch)

        assert (buch / "CD 1" / "Disc 1" / "01.mp3").is_file()

    def test_reihenfolge_stimmt_danach(self, bibliothek):
        """Der eigentliche Zweck: sonst käme Disc 2 vor Disc 1."""
        buch = audiobook.book_dir("A", "B")
        (buch / "Disc 1").mkdir(parents=True)
        (buch / "Disc 1" / "01.mp3").write_bytes(b"\xff\xfb")

        audiobook.discs_normalisieren(buch)
        (buch / "CD 2").mkdir()
        (buch / "CD 2" / "01.mp3").write_bytes(b"\xff\xfb")

        reihenfolge = [str(p.relative_to(buch)) for p in audiobook.audio_files(buch)]
        assert reihenfolge == ["CD 1/Disc 1/01.mp3", "CD 2/01.mp3"]

    def test_bereits_geordnetes_bleibt_unberuehrt(self, bibliothek):
        buch = audiobook.book_dir("A", "B")
        (buch / "CD 1").mkdir(parents=True)
        (buch / "CD 1" / "01.mp3").write_bytes(b"\xff\xfb")

        assert audiobook.discs_normalisieren(buch) is None
        assert (buch / "CD 1" / "01.mp3").is_file()

    def test_die_m4b_bleibt_liegen(self, bibliothek):
        """Sie ist das Ergebnis, keine Quelle -- sie gehört nicht in CD 1."""
        buch = audiobook.book_dir("A", "B")
        buch.mkdir(parents=True)
        (buch / "B.m4b").write_bytes(b"x")
        (buch / "01.mp3").write_bytes(b"\xff\xfb")

        audiobook.discs_normalisieren(buch)

        assert (buch / "B.m4b").is_file()
        assert (buch / "CD 1" / "01.mp3").is_file()


@braucht_ffmpeg
class TestHaengenderEncode:
    """Was passiert, wenn ffmpeg nicht mehr weiterkommt.

    Vorher: gar nichts Gutes. Die Leseschleife über ``prozess.stdout``
    blockierte unbegrenzt, das Zeitlimit stand dahinter und konnte deshalb nie
    greifen, und ``reset_m4b`` verweigerte die Arbeit, solange der Auftrag auf
    „läuft" stand -- was er dann für immer tat. Der einzige Ausweg war, den
    Container neu zu starten.
    """

    def test_viel_stderr_blockiert_nicht(self, tmp_path):
        """Der Deadlock, der von allein eintrat -- nach gut fünf Minuten.

        Eine ungelesene Pipe fasst 64 KiB. ffmpeg schreibt dorthin rund
        210 Byte je Sekunde Laufzeit, auch ohne ``-loglevel debug`` -- gemessen.
        Ein Hörbuch-Encode läuft Stunden, also lief er zwangsläufig hinein.

        Hier wird dieselbe Menge in Sekunden erzeugt, statt sie abzuwarten.
        Entscheidend ist, dass der Weg der ausgelieferte ist: stderr landet in
        einer Datei, nicht in einer Pipe.
        """
        job = audiobook.M4bJob(buch=str(tmp_path))
        ziel = tmp_path / "out.m4a"
        # -loglevel debug erzeugt die 64 KiB sofort; der Punkt ist nicht der
        # Loglevel, sondern dass beliebig viel stderr folgenlos bleibt.
        befehl = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "debug",
            "-f", "lavfi", "-i", "sine=f=440:d=20", "-c:a", "aac",
            "-progress", "pipe:1", str(ziel),
        ]
        audiobook._encodieren(job, befehl, tmp_path)

        protokoll = tmp_path / "ffmpeg-stderr.log"
        assert protokoll.stat().st_size > 64 * 1024, (
            "Der Test prüft nichts, wenn weniger als eine Pipe voll anfällt: "
            f"{protokoll.stat().st_size} Byte"
        )
        assert ziel.is_file()
        assert job.sekunden_fertig > 19

    def test_stillstand_wird_beendet(self, tmp_path, monkeypatch):
        """Ein ffmpeg, der nichts mehr meldet, wird abgeräumt statt abgewartet."""
        monkeypatch.setattr(audiobook.settings, "m4b_stillstand", 1)
        job = audiobook.M4bJob(buch=str(tmp_path))
        # -re bremst auf Echtzeit; ohne Fortschrittsausgabe (kein -progress)
        # sieht die Wache nie eine Regung und greift ein.
        befehl = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "quiet",
            "-re", "-f", "lavfi", "-i", "sine=f=440:d=120", "-c:a", "aac",
            str(tmp_path / "out.m4a"),
        ]
        with pytest.raises(audiobook.AudiobookError, match="keinen Fortschritt"):
            audiobook._encodieren(job, befehl, tmp_path)
        assert job.prozess is None, "Das Prozesshandle muss wieder frei sein"

    def test_zeitlimit_greift_auch_bei_fortschritt(self, tmp_path, monkeypatch):
        """Die Wanduhr als zweite Bremse -- vorher war sie toter Code.

        Sie stand hinter der Leseschleife, die bei einem hängenden ffmpeg nie
        endet. Das Zeitlimit, für das Messwerte vom Server angefordert wurden,
        konnte damit nie zuschlagen.
        """
        monkeypatch.setattr(audiobook.settings, "m4b_timeout", 1)
        monkeypatch.setattr(audiobook.settings, "m4b_stillstand", 3600)
        job = audiobook.M4bJob(buch=str(tmp_path))
        befehl = [
            "ffmpeg", "-hide_banner", "-nostdin", "-y", "-loglevel", "quiet",
            "-re", "-f", "lavfi", "-i", "sine=f=440:d=120", "-c:a", "aac",
            "-progress", "pipe:1", str(tmp_path / "out.m4a"),
        ]
        with pytest.raises(audiobook.AudiobookError, match="Zeitlimit"):
            audiobook._encodieren(job, befehl, tmp_path)

    def test_abbruch_von_hand(self, tmp_path, monkeypatch):
        """Der Knopf, den es nicht gab."""
        import threading

        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = tmp_path / "Autor" / "Titel"
        (buch / "Disc 1").mkdir(parents=True)
        for nummer in range(1, 4):
            subprocess.run(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                 "-i", "sine=f=440:d=30", "-c:a", "flac",
                 str(buch / "Disc 1" / f"{nummer:02d}.flac")],
                check=True,
            )

        audiobook.reset_m4b()
        job = audiobook.build(buch)
        try:
            # Warten, bis ffmpeg wirklich läuft -- vorher gäbe es nichts zu
            # beenden, und der Test prüfte den falschen Zweig.
            for _ in range(200):
                if job.prozess is not None:
                    break
                threading.Event().wait(0.05)
            assert job.prozess is not None, "ffmpeg kam nie in Gang"

            audiobook.abbrechen_m4b()
            for _ in range(200):
                if not job.laeuft:
                    break
                threading.Event().wait(0.05)

            assert not job.laeuft
            assert job.zustand == "fehler"
            assert "abgebrochen" in (job.fehler or "").lower()
            # Und das Wichtigste: die Quellen sind noch da.
            assert len(list((buch / "Disc 1").glob("*.flac"))) == 3
            assert not (buch / f"{buch.name}.m4b").exists()
        finally:
            audiobook.reset_m4b()

    def test_verwerfen_waehrend_es_laeuft_wird_abgelehnt(self, tmp_path):
        """Verwerfen ist nicht abbrechen -- der Unterschied muss deutlich sein."""
        audiobook.reset_m4b()
        job = audiobook.M4bJob(buch=str(tmp_path))
        job.zustand = "encodiert"
        audiobook._m4b_job = job
        try:
            with pytest.raises(audiobook.AudiobookError, match="erst abbrechen"):
                audiobook.reset_m4b()
        finally:
            audiobook._m4b_job = None


class TestFfprobeHaengt:
    """Auch die kleinen Abfragen brauchen ein Limit.

    Sie laufen in der Vorbereitungsphase -- dort gibt es noch keinen ffmpeg,
    den man abbrechen könnte, also hinge der Bau-Thread ohne Ausweg fest.
    """

    def test_timeout_gibt_ersatzwert(self, monkeypatch, tmp_path):
        def haengt(*args, **kwargs):
            assert kwargs.get("timeout"), "ohne Zeitlimit hinge es hier für immer"
            raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=kwargs["timeout"])

        monkeypatch.setattr(audiobook.subprocess, "run", haengt)
        assert audiobook._probe_duration(tmp_path / "x.flac") == 0.0
        assert audiobook._probe_title(tmp_path / "x.flac") == ""
        assert audiobook._probe_kbps(tmp_path / "x.flac") == 0

    def test_fehlendes_ffprobe_reisst_nichts_mit(self, monkeypatch, tmp_path):
        def fehlt(*args, **kwargs):
            raise FileNotFoundError("ffprobe")

        monkeypatch.setattr(audiobook.subprocess, "run", fehlt)
        assert audiobook._probe_duration(tmp_path / "x.flac") == 0.0


class TestAbbruchNachDemEncode:
    """Ein Abbruch, den niemand mehr liest, ist schlimmer als eine Absage.

    In der Prüfphase ist ffmpeg schon durch: das Prozesshandle ist wieder frei,
    der Auftrag läuft aber noch. Wer hier „Abbruch vorgemerkt" antwortet, sagt
    die Unwahrheit -- der Bau läuft weiter, besteht die Laufzeitprüfung und
    löscht die Quelldateien.
    """

    def test_in_der_pruefphase_wird_abgelehnt(self, tmp_path):
        audiobook.reset_m4b()
        job = audiobook.M4bJob(buch=str(tmp_path))
        job.zustand = "pruefen"
        audiobook._m4b_job = job
        try:
            with pytest.raises(audiobook.AudiobookError, match="verhindert jetzt nichts"):
                audiobook.abbrechen_m4b()
            assert job.abbruchgrund is None, (
                "Eine abgelehnte Bitte darf keine Spur hinterlassen -- sonst "
                "stünde sie später in der Meldung eines erfolgreichen Baus"
            )
        finally:
            audiobook._m4b_job = None

    def test_beim_vorbereiten_wird_vorgemerkt(self, tmp_path):
        audiobook.reset_m4b()
        job = audiobook.M4bJob(buch=str(tmp_path))
        job.zustand = "vorbereiten"
        audiobook._m4b_job = job
        try:
            antwort = audiobook.abbrechen_m4b()
            assert "vorgemerkt" in antwort
            assert job.abbruchgrund
        finally:
            audiobook._m4b_job = None

    def test_encode_gerade_beendet_wird_abgelehnt(self, tmp_path):
        """Das Rennen: ffmpeg endet zwischen Anzeige und Klick."""
        audiobook.reset_m4b()
        job = audiobook.M4bJob(buch=str(tmp_path))
        job.zustand = "encodiert"
        job.prozess = None
        audiobook._m4b_job = job
        try:
            with pytest.raises(audiobook.AudiobookError, match="verhindert jetzt nichts"):
                audiobook.abbrechen_m4b()
        finally:
            audiobook._m4b_job = None


class TestFaktor:
    """Gegen echte Zahlen vom Server, nicht gegen ausgedachte.

    Gemeldeter Zwischenstand: „Wandle um … 2:23:59 von 7:21:00", 33 %, läuft
    seit 3:07, angezeigt „141.0× Echtzeit" -- und die Beobachtung, dass der
    Faktor immer kleiner wird, je weiter der Bau kommt.
    """

    def _stand(self, fertig: float, gesamt: float, dauer: float):
        job = audiobook.M4bJob(buch="/x/Autor/Titel")
        job.sekunden_fertig = fertig
        job.sekunden_gesamt = gesamt
        job.gestartet = 0.0
        job.beendet = dauer
        return job

    def test_gemeldeter_zwischenstand(self):
        job = self._stand(2 * 3600 + 23 * 60 + 59, 7 * 3600 + 21 * 60, 3 * 60 + 7)
        assert job.prozent == 33  # wie angezeigt -- der Teil stimmte
        assert 46.0 < job.faktor < 46.5, job.faktor_text
        # Der alte Wert zum Vergleich: 26460 / 187 = 141,5.
        assert abs(job.sekunden_gesamt / job.dauer - 141.5) < 0.5

    def test_gleichmaessige_geschwindigkeit_bleibt_gleichmaessig(self):
        """Der Kern der Fehlmeldung: „wird langsamer, je weiter er kommt."

        Hier encodiert die Maschine mit exakt konstanten 46×. Der alte Wert
        fällt dabei trotzdem auf ein Sechstel -- ohne dass irgendetwas langsamer
        geworden wäre.
        """
        gesamt = 7 * 3600 + 21 * 60
        neu, alt = [], []
        for anteil in (0.05, 0.1, 0.2, 0.33, 0.6, 1.0):
            fertig = gesamt * anteil
            job = self._stand(fertig, gesamt, fertig / 46.0)
            neu.append(round(job.faktor, 1))
            alt.append(round(gesamt / job.dauer, 1))

        assert neu == [46.0] * 6, neu
        assert alt[0] / alt[-1] > 15, alt
        assert alt == sorted(alt, reverse=True), alt

    def test_restzeit_statt_kopfrechnen(self):
        job = self._stand(2 * 3600 + 23 * 60 + 59, 7 * 3600 + 21 * 60, 3 * 60 + 7)
        # Offen sind 4:57:01, bei 46,2× also gut sechs Minuten.
        assert job.rest_text.startswith("6:"), job.rest_text

    def test_am_ziel_keine_restzeit(self):
        job = self._stand(3600, 3600, 60)
        assert job.rest_text == ""
        assert job.faktor_text == "60.0× Echtzeit"

    def test_ohne_fortschritt_kein_faktor(self):
        assert self._stand(0, 3600, 10).faktor_text == ""


@braucht_ffmpeg
class TestCoverNachtraeglich:
    """Cover setzen, wenn die m4b schon fertig ist.

    Vorher gab es den Knopf nur, solange noch Quelldateien lagen -- danach war
    das Cover nicht mehr zu ändern, obwohl gerade dann nichts anderes mehr da
    ist als die m4b.
    """

    def _buch(self, tmp_path, kapitel=2):
        buch = tmp_path / "Rebecca Gablé" / "Die Siedler von Catan"
        buch.mkdir(parents=True)
        meta = tmp_path / "meta.txt"
        zeilen = [";FFMETADATA1", "title=Die Siedler von Catan",
                  "artist=Rebecca Gablé"]
        for i in range(kapitel):
            zeilen += ["[CHAPTER]", "TIMEBASE=1/1000",
                       f"START={i * 5000}", f"END={(i + 1) * 5000}",
                       f"title=Kapitel {i + 1}"]
        meta.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", f"sine=f=440:d={kapitel * 5}",
             "-i", str(meta), "-map", "0:a", "-map_metadata", "1",
             "-map_chapters", "1", "-c:a", "aac", "-movflags", "+faststart",
             str(audiobook.m4b_pfad(buch))],
            check=True,
        )
        return buch

    def _bild(self, ordner, farbe="blue", groesse="600x600"):
        pfad = ordner / f"cover-{farbe}.jpg"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
             "-i", f"color=c={farbe}:s={groesse}:d=1", "-frames:v", "1", str(pfad)],
            check=True,
        )
        return pfad

    def test_kapitel_und_dauer_bleiben(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path, kapitel=3)
        m4b = audiobook.m4b_pfad(buch)
        vorher = audiobook._probe_eckdaten(m4b)
        assert vorher[1] == 3 and vorher[2] == 0

        meldung = audiobook.cover_einbetten(buch, self._bild(tmp_path))

        dauer, kapitel, bilder = audiobook._probe_eckdaten(m4b)
        assert kapitel == 3, "Die Kapitel sind das Wertvollste an einer m4b"
        assert abs(dauer - vorher[0]) < 0.05
        assert bilder == 1
        assert "3 Kapitel" in meldung

    def test_titel_und_autor_ueberleben(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path)
        audiobook.cover_einbetten(buch, self._bild(tmp_path))
        tags = audiobook._ffprobe(
            ["-show_entries", "format_tags=title,artist", "-of", "default=nw=1",
             str(audiobook.m4b_pfad(buch))]
        )
        assert "Die Siedler von Catan" in tags
        assert "Rebecca Gablé" in tags

    def test_ersetzen_stapelt_nicht(self, tmp_path, monkeypatch):
        """Zweimal fotografieren darf nicht zwei Bilder in der Datei ergeben."""
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path)
        audiobook.cover_einbetten(buch, self._bild(tmp_path, "blue", "600x600"))
        audiobook.cover_einbetten(buch, self._bild(tmp_path, "red", "400x400"))

        _, _, bilder = audiobook._probe_eckdaten(audiobook.m4b_pfad(buch))
        assert bilder == 1
        breite = audiobook._ffprobe(
            ["-select_streams", "v:0", "-show_entries", "stream=width",
             "-of", "csv=p=0", str(audiobook.m4b_pfad(buch))]
        )
        assert breite == "400", f"Das zweite Bild hat nicht gewonnen: {breite}"

    def test_das_staging_bleibt_leer(self, tmp_path, monkeypatch):
        """Die halbfertige Kopie darf nicht im Buchordner entstehen.

        Audiobookshelf liest jede Audiodatei im Buchordner als Track desselben
        Buchs -- eine zweite m4b daneben, und sei es für Sekunden, kann bei
        einem Scan als zweites Hörbuch landen. Gearbeitet wird deshalb im
        Staging, und danach ist auch dort nichts mehr.
        """
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path)
        # Das Bild liegt hier bewusst außerhalb des Buchordners. Über die
        # Route landet es als cover.jpg *im* Buch -- gewollt, damit has_cover
        # stimmt und Audiobookshelf auch ein Ordnerbild hat. Der Routentest
        # deckt genau das ab; hier geht es allein um die Arbeitskopie.
        audiobook.cover_einbetten(buch, self._bild(tmp_path))

        assert sorted(p.name for p in buch.iterdir()) == [
            "Die Siedler von Catan.m4b"
        ]
        staging = tmp_path / audiobook.STAGING_NAME
        assert not staging.is_dir() or not list(staging.iterdir())

    def test_ohne_m4b_wird_abgelehnt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = tmp_path / "A" / "B"
        buch.mkdir(parents=True)
        with pytest.raises(audiobook.AudiobookError, match="noch keine m4b"):
            audiobook.cover_einbetten(buch, self._bild(tmp_path))

    def test_kaputtes_bild_laesst_die_m4b_unversehrt(self, tmp_path, monkeypatch):
        """Der Kern: die m4b ist die einzige Kopie des Buchs."""
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path)
        m4b = audiobook.m4b_pfad(buch)
        vorher = m4b.read_bytes()

        kaputt = tmp_path / "kaputt.jpg"
        kaputt.write_bytes(b"\xff\xd8\xff\xe0 das ist kein Bild")
        with pytest.raises(audiobook.AudiobookError):
            audiobook.cover_einbetten(buch, kaputt)

        assert m4b.read_bytes() == vorher, "Die m4b wurde angetastet"
        assert audiobook._probe_eckdaten(m4b)[1] == 2

    def test_zeitlimit_laesst_die_m4b_unversehrt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path)
        m4b = audiobook.m4b_pfad(buch)
        vorher = m4b.read_bytes()
        # Das Bild vor dem Patch erzeugen -- es entsteht selbst über
        # subprocess.run und wäre sonst der erste Aufruf, den es abfängt.
        bild = self._bild(tmp_path)

        echt = audiobook.subprocess.run

        def langsam(befehl, *args, **kwargs):
            # Nur ffmpeg aufhalten: die ffprobe-Vorabprüfung muss echt laufen,
            # sonst scheitert es an ihr statt am Zeitlimit.
            if audiobook.settings.ffprobe_bin in befehl[0]:
                return echt(befehl, *args, **kwargs)
            assert kwargs.get("timeout"), "ohne Zeitlimit hinge es an der m4b"
            raise audiobook.subprocess.TimeoutExpired(cmd="ffmpeg", timeout=1)

        monkeypatch.setattr(audiobook.subprocess, "run", langsam)
        with pytest.raises(audiobook.AudiobookError, match="vorgesehenen Zeit"):
            audiobook.cover_einbetten(buch, bild)
        monkeypatch.undo()

        assert m4b.read_bytes() == vorher


    def test_abweichende_kapitel_verhindern_das_ersetzen(self, tmp_path, monkeypatch):
        """Das Sicherheitsnetz vor dem Überschreiben der einzigen Kopie.

        Im Normalbetrieb schlägt es nie zu -- Remuxen ändert an Dauer, Kapiteln
        und Coverzahl nichts, nachgemessen. Genau deshalb braucht es einen
        eigenen Test: ohne ihn wäre die Prüfung unbelegt, und ein Wegfall
        fiele erst auf, wenn ein Hörbuch dabei kaputtginge.
        """
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path, kapitel=3)
        m4b = audiobook.m4b_pfad(buch)
        vorher = m4b.read_bytes()
        bild = self._bild(tmp_path)

        echt = audiobook._probe_eckdaten

        def verliert_kapitel(datei):
            dauer, kapitel, bilder = echt(datei)
            # Die frisch geschriebene Datei im Staging meldet weniger Kapitel.
            if audiobook.STAGING_NAME in str(datei):
                return dauer, kapitel - 1, bilder
            return dauer, kapitel, bilder

        monkeypatch.setattr(audiobook, "_probe_eckdaten", verliert_kapitel)
        with pytest.raises(audiobook.AudiobookError, match="Kapitel hätten sich"):
            audiobook.cover_einbetten(buch, bild)

        assert m4b.read_bytes() == vorher, "Die m4b wurde trotzdem ersetzt"

    def test_abweichende_dauer_verhindert_das_ersetzen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._buch(tmp_path)
        m4b = audiobook.m4b_pfad(buch)
        vorher = m4b.read_bytes()
        bild = self._bild(tmp_path)

        echt = audiobook._probe_eckdaten

        def verliert_zeit(datei):
            dauer, kapitel, bilder = echt(datei)
            if audiobook.STAGING_NAME in str(datei):
                return dauer - 30, kapitel, bilder
            return dauer, kapitel, bilder

        monkeypatch.setattr(audiobook, "_probe_eckdaten", verliert_zeit)
        with pytest.raises(audiobook.AudiobookError, match="Spieldauer hätte sich"):
            audiobook.cover_einbetten(buch, bild)

        assert m4b.read_bytes() == vorher


class TestRelativerPfadUeberSymlink:
    """Der Buchpfad muss auch dann stimmen, wenn ein Symlink im Weg liegt.

    Aufgefallen beim Anzeigen der Cover: die Bilder blieben leer, weil in der
    Adresse kein Buchpfad stand. Aufgelöst wurde nur die Wurzel, nicht der
    Buchpfad -- passten die beiden nicht zusammen, kam der leere String heraus.
    Getroffen hätte es nicht nur die Cover: „Nächste CD" und der Cover-Knopf
    bauen ihre Adresse aus demselben Wert.
    """

    def test_symlink_in_der_wurzel(self, tmp_path, monkeypatch):
        echt = tmp_path / "echt"
        (echt / "Autor" / "Buch").mkdir(parents=True)
        verweis = tmp_path / "verweis"
        verweis.symlink_to(echt, target_is_directory=True)

        monkeypatch.setattr(audiobook.settings, "audiobook_root", verweis)
        zustand = audiobook.state(verweis / "Autor" / "Buch")
        assert zustand.relative == "Autor/Buch"

    def test_ohne_symlink_unveraendert(self, tmp_path, monkeypatch):
        (tmp_path / "Autor" / "Buch").mkdir(parents=True)
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        assert audiobook.state(tmp_path / "Autor" / "Buch").relative == "Autor/Buch"

    def test_ausserhalb_bleibt_leer(self, tmp_path, monkeypatch):
        """Ein Pfad außerhalb der Bibliothek darf keinen Knopf ergeben."""
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path / "lib")
        (tmp_path / "lib").mkdir()
        fremd = tmp_path / "woanders" / "Buch"
        fremd.mkdir(parents=True)
        assert audiobook.state(fremd).relative == ""


@braucht_ffmpeg
class TestCoverAusM4bHolen:
    """Cover von Büchern, die nicht über mimport kamen.

    Dort steckt das Bild oft nur in der m4b, und die Liste zeigte einen
    Platzhalter, obwohl ein Cover vorhanden ist -- has_cover sieht nur den
    Ordner. Einmal beim Start herausgeholt, ist die Frage danach wieder eine
    reine Dateisystemabfrage.
    """

    def _m4b(self, buch: Path, *, bild: str | None = "attached", video: bool = False):
        buch.mkdir(parents=True, exist_ok=True)
        befehl = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "lavfi", "-i", "sine=f=440:d=3"]
        if bild or video:
            befehl += ["-f", "lavfi", "-i", "testsrc=s=200x200:d=1"]
        befehl += ["-map", "0:a"]
        if video:
            # Eine echte Videospur, kein Titelbild.
            befehl += ["-map", "1:v", "-c:v", "mpeg4", "-frames:v", "10"]
        elif bild:
            befehl += ["-map", "1:v", "-frames:v", "1", "-c:v", "mjpeg",
                       "-disposition:v:0", "attached_pic"]
        befehl += ["-c:a", "aac", str(audiobook.m4b_pfad(buch))]
        subprocess.run(befehl, check=True, capture_output=True)
        return buch

    def _mp3(self, buch: Path, *, bild: bool = True):
        buch.mkdir(parents=True, exist_ok=True)
        ziel = buch / "01 Kapitel.mp3"
        befehl = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                  "-f", "lavfi", "-i", "sine=f=440:d=3"]
        if bild:
            befehl += ["-f", "lavfi", "-i", "testsrc=s=200x200:d=1",
                       "-map", "0:a", "-map", "1:v", "-c:v", "mjpeg",
                       "-id3v2_version", "3", "-metadata:s:v", "title=Album cover",
                       "-metadata:s:v", "comment=Cover (front)"]
        else:
            befehl += ["-map", "0:a"]
        befehl += ["-c:a", "libmp3lame", str(ziel)]
        subprocess.run(befehl, check=True, capture_output=True)
        return buch

    def test_eingebettetes_cover_wird_zur_datei(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._m4b(tmp_path / "Autor" / "Fremdes Buch")
        assert not audiobook.state(buch).has_cover

        assert audiobook.cover_aus_m4b_holen(buch) is True

        bild = buch / "cover.jpg"
        assert bild.is_file() and bild.stat().st_size > 500
        # Wirklich ein JPEG, nicht bloß so benannt.
        from backend import cover as cover_modul

        assert cover_modul.format_erkennen(bild.read_bytes())
        assert audiobook.state(buch).has_cover

    def test_ohne_bild_bleibt_der_ordner_unberuehrt(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._m4b(tmp_path / "Autor" / "Ohne Bild", bild=None)

        assert audiobook.cover_aus_m4b_holen(buch) is False
        assert sorted(p.name for p in buch.iterdir()) == ["Ohne Bild.m4b"]

    def test_echte_videospur_ist_kein_cover(self, tmp_path, monkeypatch):
        """Manche Hörbücher bringen ein Video mit -- daraus kein Bild schneiden."""
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._m4b(tmp_path / "Autor" / "Mit Video", bild=None, video=True)

        assert audiobook.cover_aus_m4b_holen(buch) is False
        assert not (buch / "cover.jpg").exists()

    def test_vorhandenes_ordnerbild_wird_nicht_ueberschrieben(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._m4b(tmp_path / "Autor" / "Hat schon eins")
        (buch / "cover.jpg").write_bytes(b"das hier soll bleiben")

        assert audiobook.cover_aus_m4b_holen(buch) is False
        assert (buch / "cover.jpg").read_bytes() == b"das hier soll bleiben"

    def test_zweiter_lauf_tut_nichts_mehr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        self._m4b(tmp_path / "Autor" / "Eins")
        self._m4b(tmp_path / "Autor" / "Zwei", bild=None)

        assert audiobook.cover_nachziehen() == 1
        assert audiobook.cover_nachziehen() == 0

    def test_eingebettetes_cover_aus_mp3_wird_zur_datei(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._mp3(tmp_path / "Autor" / "MP3-Buch")

        assert audiobook.cover_aus_audio_holen(buch) is True
        assert (buch / "cover.jpg").is_file()
        assert audiobook.state(buch).has_cover

    def test_cover_nachziehen_nimmt_auch_quellen(self, tmp_path, monkeypatch):
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        self._mp3(tmp_path / "Autor" / "Mit MP3-Cover")
        self._mp3(tmp_path / "Autor" / "Ohne MP3-Cover", bild=False)

        assert audiobook.cover_nachziehen() == 1
        assert audiobook.cover_nachziehen() == 0

    def test_kein_rest_bei_fehlschlag(self, tmp_path, monkeypatch):
        """Eine halbe Datei ginge als „hat Cover" durch und zeigte ein Loch."""
        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._m4b(tmp_path / "Autor" / "Buch")

        echt = audiobook.subprocess.run

        def scheitert(befehl, *args, **kwargs):
            if audiobook.settings.ffprobe_bin in befehl[0]:
                return echt(befehl, *args, **kwargs)
            # ffmpeg legt eine leere Datei an und bricht ab.
            import pathlib

            pathlib.Path(befehl[-1]).write_bytes(b"")
            return subprocess.CompletedProcess(befehl, 1, "", "kaputt")

        monkeypatch.setattr(audiobook.subprocess, "run", scheitert)
        assert audiobook.cover_aus_m4b_holen(buch) is False
        monkeypatch.undo()

        assert not (buch / "cover.jpg").exists()
        assert not list(buch.glob(".cover*"))

    def test_leere_datei_trotz_erfolg_wird_verworfen(self, tmp_path, monkeypatch):
        """ffmpeg meldet Erfolg, hat aber nichts geschrieben.

        Eigener Test, weil der Fehlschlag oben schon am Rückgabewert hängt --
        die Größenprüfung wäre sonst unbelegt. Eine leere cover.jpg machte
        has_cover wahr und ergäbe in der Liste ein kaputtes Bild.
        """
        import pathlib

        monkeypatch.setattr(audiobook.settings, "audiobook_root", tmp_path)
        buch = self._m4b(tmp_path / "Autor" / "Buch")

        echt = audiobook.subprocess.run

        def leer(befehl, *args, **kwargs):
            if audiobook.settings.ffprobe_bin in befehl[0]:
                return echt(befehl, *args, **kwargs)
            pathlib.Path(befehl[-1]).write_bytes(b"")
            return subprocess.CompletedProcess(befehl, 0, "", "")

        monkeypatch.setattr(audiobook.subprocess, "run", leer)
        assert audiobook.cover_aus_m4b_holen(buch) is False
        monkeypatch.undo()

        assert not (buch / "cover.jpg").exists()
        assert not audiobook.state(buch).has_cover
