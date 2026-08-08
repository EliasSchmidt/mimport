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
