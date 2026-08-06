"""Das Rippen einer Audio-CD.

Ohne Laufwerk lässt sich das Lesen selbst nicht prüfen -- wohl aber alles
darum herum: das Inhaltsverzeichnis, die Vorbedingungen, der Ablauf und das
Aufräumen im Fehlerfall. ``cdparanoia`` und ``flac`` werden dafür ersetzt.
"""

from __future__ import annotations

import subprocess

import pytest

from backend import discid, rip, sessions
from tests.test_discid import CDPARANOIA_AUSGABE, VEKTOR_ID


@pytest.fixture(autouse=True)
def kein_alter_auftrag():
    """Der Auftrag ist global -- es gibt nur ein Laufwerk."""
    rip._job = None
    yield
    rip._job = None


@pytest.fixture
def toc():
    return discid.parse_cdparanoia_toc(CDPARANOIA_AUSGABE)


class TestReadToc:
    def test_liest_das_inhaltsverzeichnis(self, monkeypatch):
        def fake(command, **kwargs):
            # cdparanoia schreibt das Inhaltsverzeichnis nach stderr.
            return subprocess.CompletedProcess(command, 0, "", CDPARANOIA_AUSGABE)

        monkeypatch.setattr(rip, "_run", fake)
        toc = rip.read_toc()
        assert toc.track_count == 6
        assert discid.calculate(toc) == VEKTOR_ID

    def test_fehlendes_programm(self, monkeypatch):
        def fake(command, **kwargs):
            raise FileNotFoundError(command[0])

        monkeypatch.setattr(rip, "_run", fake)
        with pytest.raises(rip.RipError, match="cdparanoia"):
            rip.read_toc()

    def test_laufwerk_antwortet_nicht(self, monkeypatch):
        def fake(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 60)

        monkeypatch.setattr(rip, "_run", fake)
        with pytest.raises(rip.RipError, match="nicht geantwortet"):
            rip.read_toc()

    def test_datencd_wird_erkannt(self, monkeypatch):
        def fake(command, **kwargs):
            return subprocess.CompletedProcess(command, 1, "", "Unable to open disc.")

        monkeypatch.setattr(rip, "_run", fake)
        with pytest.raises(rip.RipError, match="keine Audio-CD"):
            rip.read_toc()


class TestStart:
    def test_zweiter_rip_wird_abgelehnt(self, monkeypatch, toc):
        """Ein Laufwerk, ein Auftrag."""
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        rip.start(allowance=10**12)
        with pytest.raises(rip.RipError, match="bereits ein Rip"):
            rip.start(allowance=10**12)

    def test_zu_wenig_platz(self, monkeypatch, toc):
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        # Die CD braucht unkomprimiert gut 200 MB.
        with pytest.raises(rip.RipError, match="zu wenig Platz"):
            rip.start(allowance=1024)

    def test_discid_wird_gesetzt(self, monkeypatch, toc):
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        job = rip.start(allowance=10**12)
        assert job.disc_id == VEKTOR_ID
        assert job.tracks_gesamt == 6
        assert job.session_id


class _FakeThread:
    """Startet nichts -- der Ablauf wird separat und synchron geprüft."""

    def __init__(self, *args, **kwargs):
        self.args = kwargs.get("args", args)

    def start(self):
        pass


class TestAblauf:
    """``_arbeite`` synchron, damit der Ablauf deterministisch prüfbar ist."""

    def test_erfolgreicher_rip(self, monkeypatch, toc):
        gelesen = []

        def fake_track(nummer, ziel):
            gelesen.append(nummer)
            ziel.write_bytes(b"fLaC\x00\x00\x00\x22")

        monkeypatch.setattr(rip, "_rip_track", fake_track)
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [])

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(job, toc, session)

        assert job.zustand == "fertig"
        assert gelesen == [1, 2, 3, 4, 5, 6]
        assert len(session.audio_paths) == 6
        assert job.prozent == 100

    def test_releases_werden_uebernommen(self, monkeypatch, toc):
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )
        hinweis = discid.ReleaseHint(mbid="x" * 36, title="Album", date="", country="")
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [hinweis])

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(job, toc, session)

        assert job.releases == [hinweis]

    def test_musicbrainz_ausfall_verwirft_den_rip_nicht(self, monkeypatch, toc):
        """Die Tracks sind gelesen -- die Suche geht notfalls von Hand."""
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )

        def kaputt(*args, **kwargs):
            raise discid.DiscIdError("kein Netz")

        monkeypatch.setattr(rip.discid, "lookup", kaputt)

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(job, toc, session)

        assert job.zustand == "fertig"
        assert job.releases == []

    def test_lesefehler_raeumt_die_session_weg(
        self, monkeypatch, toc, isoliertes_staging
    ):
        def kaputt(nummer, ziel):
            if nummer == 3:
                raise rip.RipError("Track 3 ließ sich nicht lesen.")
            ziel.write_bytes(b"fLaC\x00\x00\x00\x22")

        monkeypatch.setattr(rip, "_rip_track", kaputt)

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(job, toc, session)

        assert job.zustand == "fehler"
        assert "Track 3" in (job.fehler or "")
        # Kein halbes Album im Staging.
        assert list(isoliertes_staging.iterdir()) == []
        assert job.session_id is None

    def test_unerwarteter_fehler_bleibt_nicht_stumm(
        self, monkeypatch, toc, isoliertes_staging
    ):
        """Ein Thread, der still stirbt, hinterlässt eine hängende Anzeige."""

        def platzt(nummer, ziel):
            raise ValueError("etwas ganz anderes")

        monkeypatch.setattr(rip, "_rip_track", platzt)

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(job, toc, session)

        assert job.zustand == "fehler"
        assert "Unerwarteter Fehler" in (job.fehler or "")
        assert list(isoliertes_staging.iterdir()) == []


class TestRipTrack:
    def test_wav_wird_nach_dem_packen_entfernt(self, monkeypatch, tmp_path):
        """Das WAV belegt das Vierfache des FLAC und ist nur Zwischenprodukt."""
        ziel = tmp_path / "01 Track 1.flac"
        wav = ziel.with_suffix(".wav")

        def fake(command, **kwargs):
            if "cdparanoia" in command[0]:
                wav.write_bytes(b"RIFF....WAVE")
            else:
                ziel.write_bytes(b"fLaC\x00\x00\x00\x22")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(rip, "_run", fake)
        rip._rip_track(1, ziel)

        assert ziel.exists()
        assert not wav.exists()

    def test_wav_wird_auch_bei_fehler_entfernt(self, monkeypatch, tmp_path):
        ziel = tmp_path / "01 Track 1.flac"
        wav = ziel.with_suffix(".wav")

        def fake(command, **kwargs):
            wav.write_bytes(b"RIFF....WAVE")
            return subprocess.CompletedProcess(command, 1, "", "Leseschaden")

        monkeypatch.setattr(rip, "_run", fake)
        with pytest.raises(rip.RipError):
            rip._rip_track(1, ziel)

        assert not wav.exists()


class TestReset:
    def test_laufender_auftrag_wird_nicht_verworfen(self, monkeypatch, toc):
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)
        rip.start(allowance=10**12)

        with pytest.raises(rip.RipError, match="laufende"):
            rip.reset()

    def test_abgeschlossener_auftrag_gibt_das_laufwerk_frei(self, monkeypatch, toc):
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)
        job = rip.start(allowance=10**12)
        job.zustand = "fertig"

        rip.reset()
        assert rip.current() is None
