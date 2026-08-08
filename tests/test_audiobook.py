"""Hörbücher: Discs sammeln und zur m4b bündeln.

Der wichtigste Teil hier sind die beiden Bremsen: nicht löschen, wenn die
Laufzeit nicht passt, und gar nicht erst umwandeln, wenn die Quelle schon
verlustbehaftet ist.
"""

from __future__ import annotations

import shutil
import subprocess

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
        audiobook._quellen_aufraeumen(job, quellen, ziel, 3600.0)

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
            audiobook._quellen_aufraeumen(job, quellen, ziel, 3600.0)

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
        audiobook._quellen_aufraeumen(job, [quelle], ziel, 3600.0)
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
    def test_abgebrochener_bau_hinterlaesst_genau_diesen_zustand(
        self, bibliothek, monkeypatch
    ):
        """Der Weg dorthin -- damit der Zustand nicht bloß theoretisch ist."""
        import time

        buch = audiobook.book_dir("A", "B")
        toene(buch, [2, 2])

        # Die m4b fällt kürzer aus, als sie darf.
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
        assert zustand.unstimmig is True


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
        job = audiobook.M4bJob(buch="/x", sekunden_gesamt=3600.0)
        job.gestartet = 0.0
        job.beendet = 450.0
        assert job.dauer_text == "7:30"
        # Eine Stunde Audio in 7,5 Minuten sind 8× Echtzeit.
        assert job.faktor_text == "8.0× Echtzeit"

    def test_faktor_bleibt_leer_ohne_messwerte(self):
        job = audiobook.M4bJob(buch="/x")
        assert job.faktor_text == ""
