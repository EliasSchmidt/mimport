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
        assert job.neue_session is True
        assert job.disc_ordner is None

    def test_weitere_disc_haengt_an_bestehende_session_an(self, monkeypatch, toc):
        """Eine zweite physische CD desselben Albums, nicht ein neues Album."""
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(b"fLaC")

        job = rip.start(allowance=10**12, session_id=session.session_id)

        assert job.session_id == session.session_id
        assert job.neue_session is False
        assert job.disc_ordner == str(session.directory / "CD 2")
        # Die erste Disc ist vorher nach "CD 1" umgezogen, sonst kollidiert
        # ihr "01 Track 1.flac" mit dem der zweiten.
        assert (session.directory / "CD 1" / "01 Track 1.flac").is_file()
        assert not (session.directory / "01 Track 1.flac").exists()

    def test_gescheiterter_toc_read_bei_weiterer_disc_behaelt_session_bezug(
        self, monkeypatch
    ):
        """Der Kern des Bugs: Laufwerk leer/verkratzt beim zweiten Rip-Versuch
        darf den Bezug zur Session nicht verlieren -- sonst zeigt die
        Oberfläche den Fehler einer *neuen* Session an, und "Nochmal
        versuchen" würde beim Zurücksetzen fälschlich die ganze Session löschen
        wollen (nur dass sie mangels ``session_id`` dann gar nicht gefunden
        würde -- der Nutzer verliert trotzdem jeden Zugriff darüber)."""

        def kaputt():
            raise rip.RipError("Im Laufwerk wurde keine Audio-CD erkannt.")

        monkeypatch.setattr(rip, "read_toc", kaputt)

        session = sessions.create_session()
        (session.directory / "01 Track 1.flac").write_bytes(b"fLaC")

        with pytest.raises(rip.RipError):
            rip.start(allowance=10**12, session_id=session.session_id)

        job = rip.current()
        assert job is not None
        assert job.zustand == "fehler"
        assert job.session_id == session.session_id
        assert job.neue_session is False
        # Die Session selbst ist unangetastet -- nichts wurde umgeräumt oder
        # gelöscht, nur weil das Laufwerk nicht mitspielte.
        assert (session.directory / "01 Track 1.flac").is_file()

    def test_weitere_disc_uebernimmt_releases_der_vorherigen(self, monkeypatch, toc):
        """Erste Disc kannte MusicBrainz bereits -- der Treffer darf nicht
        verschwinden, nur weil jetzt eine zweite Disc gelesen wird."""
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        session = sessions.create_session()
        hinweis = discid.ReleaseHint(mbid="a" * 36, title="Album", date="", country="")
        rip._job = rip.RipJob(
            session_id=session.session_id, disc_id=VEKTOR_ID, releases=[hinweis],
            zustand="fertig",
        )

        job = rip.start(allowance=10**12, session_id=session.session_id)

        assert job.fruehere_releases == [hinweis]

    def test_gescheiterte_weitere_disc_behaelt_release_der_ersten(self, monkeypatch):
        """Der eigentliche Fall aus dem Bugreport: zweite Disc kaputt, aber
        die erste hatte schon einen MusicBrainz-Treffer -- damit lässt sich
        trotzdem taggen, ohne die kaputte Disc erst reparieren zu müssen."""

        def kaputt():
            raise rip.RipError("Im Laufwerk wurde keine Audio-CD erkannt.")

        monkeypatch.setattr(rip, "read_toc", kaputt)

        session = sessions.create_session()
        hinweis = discid.ReleaseHint(mbid="a" * 36, title="Album", date="", country="")
        rip._job = rip.RipJob(
            session_id=session.session_id, disc_id=VEKTOR_ID, releases=[hinweis],
            zustand="fertig",
        )

        with pytest.raises(rip.RipError):
            rip.start(allowance=10**12, session_id=session.session_id)

        job = rip.current()
        assert job is not None
        assert job.zustand == "fehler"
        assert job.fruehere_releases == [hinweis]

    def test_weitere_disc_mit_unbekannter_session(self, monkeypatch, toc):
        monkeypatch.setattr(rip, "read_toc", lambda: toc)
        monkeypatch.setattr(rip.threading, "Thread", _FakeThread)

        with pytest.raises(rip.RipError):
            rip.start(allowance=10**12, session_id="x" * 20)
        # Der Auftrag bleibt sichtbar, damit die Oberfläche den Fehler zeigen
        # kann -- er wird nur nicht als "läuft" markiert.
        job = rip.current()
        assert job is not None
        assert job.zustand == "fehler"


class TestAlleReleases:
    A = discid.ReleaseHint(mbid="a" * 36, title="A", date="", country="")
    B = discid.ReleaseHint(mbid="b" * 36, title="B", date="", country="")

    def test_eigene_treffer_zuerst_dann_fruehere_ohne_duplikate(self):
        job = rip.RipJob(releases=[self.A], fruehere_releases=[self.A, self.B])
        assert job.alle_releases == [self.A, self.B]

    def test_ohne_eigenen_treffer_nur_fruehere(self):
        job = rip.RipJob(fruehere_releases=[self.B])
        assert job.alle_releases == [self.B]

    def test_ohne_beides_leer(self):
        assert rip.RipJob().alle_releases == []


class TestNaechsteDisc:
    def test_erste_disc_zieht_nach_cd1_um_zweite_wird_cd2(self, tmp_path):
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / "01 Track 1.flac").write_bytes(b"x")

        ziel = rip._naechste_disc(session_dir)

        assert ziel == session_dir / "CD 2"
        assert ziel.is_dir()
        assert (session_dir / "CD 1" / "01 Track 1.flac").is_file()

    def test_dritte_disc_zaehlt_weiter(self, tmp_path):
        session_dir = tmp_path / "sess"
        session_dir.mkdir()
        (session_dir / "CD 1").mkdir()
        (session_dir / "CD 2").mkdir()

        ziel = rip._naechste_disc(session_dir)

        assert ziel == session_dir / "CD 3"

    def test_leere_session_bekommt_cd1(self, tmp_path):
        session_dir = tmp_path / "sess"
        session_dir.mkdir()

        ziel = rip._naechste_disc(session_dir)

        assert ziel == session_dir / "CD 1"


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

        def fake_track(nummer, ziel, **kwargs):
            gelesen.append(nummer)
            ziel.write_bytes(b"fLaC\x00\x00\x00\x22")

        monkeypatch.setattr(rip, "_rip_track", fake_track)
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [])

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(job),
        )

        assert job.zustand == "fertig"
        assert gelesen == [1, 2, 3, 4, 5, 6]
        assert len(session.audio_paths) == 6
        assert job.prozent == 100

    def test_zweite_disc_ueberschreibt_die_erste_nicht(self, monkeypatch, toc):
        """Beide Discs vergeben dieselben Dateinamen -- die dürfen sich nicht
        gegenseitig überschreiben, wenn sie in derselben Session landen."""
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(f"a{n}".encode())
        )
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [])

        session = sessions.create_session()
        erste = rip.RipJob(disc_id=VEKTOR_ID, session_id=session.session_id)
        rip._arbeite(
            erste, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(erste),
        )
        assert erste.zustand == "fertig"
        assert len(session.audio_paths) == 6

        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(f"b{n}".encode())
        )
        zielordner = rip._naechste_disc(session.directory)
        zweite = rip.RipJob(
            disc_id=VEKTOR_ID, session_id=session.session_id, neue_session=False
        )
        rip._arbeite(zweite, toc, zielordner, bei_fehler=lambda: None)

        assert zweite.zustand == "fertig"
        assert len(session.audio_paths) == 12
        assert (session.directory / "CD 1" / "01 Track 1.flac").read_bytes() == b"a1"
        assert (session.directory / "CD 2" / "01 Track 1.flac").read_bytes() == b"b1"

    def test_releases_werden_uebernommen(self, monkeypatch, toc):
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )
        hinweis = discid.ReleaseHint(mbid="x" * 36, title="Album", date="", country="")
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [hinweis])

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(job),
        )

        assert job.releases == [hinweis]

    def test_musicbrainz_ausfall_verwirft_den_rip_nicht(self, monkeypatch, toc):
        """Die Tracks sind gelesen -- die Suche geht notfalls von Hand."""
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )

        def kaputt(*args, **kwargs):
            raise discid.DiscIdError("kein Netz")

        monkeypatch.setattr(rip.discid, "lookup", kaputt)

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(job),
        )

        assert job.zustand == "fertig"
        assert job.releases == []

    def test_lesefehler_raeumt_die_session_weg(
        self, monkeypatch, toc, isoliertes_staging
    ):
        def kaputt(nummer, ziel, **kwargs):
            if nummer == 3:
                raise rip.RipError("Track 3 ließ sich nicht lesen.")
            ziel.write_bytes(b"fLaC\x00\x00\x00\x22")

        monkeypatch.setattr(rip, "_rip_track", kaputt)

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(job),
        )

        assert job.zustand == "fehler"
        assert "Track 3" in (job.fehler or "")
        # Kein halbes Album im Staging.
        assert list(isoliertes_staging.iterdir()) == []
        assert job.session_id is None

    def test_unerwarteter_fehler_bleibt_nicht_stumm(
        self, monkeypatch, toc, isoliertes_staging
    ):
        """Ein Thread, der still stirbt, hinterlässt eine hängende Anzeige."""

        def platzt(nummer, ziel, **kwargs):
            raise ValueError("etwas ganz anderes")

        monkeypatch.setattr(rip, "_rip_track", platzt)

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(job),
        )

        assert job.zustand == "fehler"
        assert "Unerwarteter Fehler" in (job.fehler or "")
        assert list(isoliertes_staging.iterdir()) == []


class TestAuswerfen:
    def test_ohne_eject_bleibt_es_beim_versuch(self, monkeypatch):
        """Fehlt das Programm im Image, soll das nicht auffallen."""
        monkeypatch.setattr(rip.shutil, "which", lambda _: None)

        def fake_run(command, **kwargs):
            raise AssertionError("eject hätte nicht aufgerufen werden dürfen")

        monkeypatch.setattr(rip, "_run", fake_run)
        rip._auswerfen()

    def test_ruft_eject_mit_dem_laufwerk_auf(self, monkeypatch):
        monkeypatch.setattr(rip.shutil, "which", lambda _: "/usr/bin/eject")
        aufrufe = []

        def fake_run(command, **kwargs):
            aufrufe.append(command)
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(rip, "_run", fake_run)
        rip._auswerfen()

        assert aufrufe == [[rip.settings.eject_bin, rip.settings.cdrom_device]]

    def test_fehlschlag_wird_nur_geloggt(self, monkeypatch, caplog):
        """Der Rip ist zu diesem Zeitpunkt schon erfolgreich -- kein Grund,
        deswegen den Auftrag als Fehler zu markieren."""
        monkeypatch.setattr(rip.shutil, "which", lambda _: "/usr/bin/eject")
        monkeypatch.setattr(
            rip, "_run",
            lambda command, **kw: subprocess.CompletedProcess(
                command, 1, "", "device is busy"
            ),
        )

        with caplog.at_level("WARNING"):
            rip._auswerfen()

        assert "konnte die CD nicht auswerfen" in caplog.text

    def test_ausnahme_wird_nur_geloggt(self, monkeypatch, caplog):
        monkeypatch.setattr(rip.shutil, "which", lambda _: "/usr/bin/eject")

        def fake_run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, rip.EJECT_TIMEOUT)

        monkeypatch.setattr(rip, "_run", fake_run)

        with caplog.at_level("WARNING"):
            rip._auswerfen()

        assert "CD-Auswurf fehlgeschlagen" in caplog.text

    def test_erfolgreicher_rip_ruft_auswerfen_auf(self, monkeypatch, toc):
        """``_arbeite`` löst den Auswurf am Ende jeder erfolgreich gelesenen
        Disc aus -- auch bei einem Hörbuch mit mehreren CDs soll das nach
        jeder einzelnen passieren, nicht nur ganz am Ende."""
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [])
        aufgerufen = []
        monkeypatch.setattr(rip, "_auswerfen", lambda: aufgerufen.append(True))

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory,
            bei_fehler=lambda: rip._session_verwerfen(job),
        )

        assert aufgerufen == [True]


class TestRipTrack:
    def test_wav_wird_nach_dem_packen_entfernt(self, monkeypatch, tmp_path):
        """Das WAV belegt das Vierfache des FLAC und ist nur Zwischenprodukt."""
        ziel = tmp_path / "01 Track 1.flac"
        wav = ziel.with_suffix(".wav")

        def fake_lesen(nummer, w, fortschritt):
            w.write_bytes(b"RIFF....WAVE")
            return 0

        def fake_run(command, **kwargs):
            ziel.write_bytes(b"fLaC\x00\x00\x00\x22")
            return subprocess.CompletedProcess(command, 0, "", "")

        monkeypatch.setattr(rip, "_lesen", fake_lesen)
        monkeypatch.setattr(rip, "_run", fake_run)
        rip._rip_track(1, ziel)

        assert ziel.exists()
        assert not wav.exists()

    def test_wav_wird_auch_bei_fehler_entfernt(self, monkeypatch, tmp_path):
        ziel = tmp_path / "01 Track 1.flac"
        wav = ziel.with_suffix(".wav")

        def fake_lesen(nummer, w, fortschritt):
            w.write_bytes(b"RIFF....WAVE")
            return 1  # Leseschaden

        monkeypatch.setattr(rip, "_lesen", fake_lesen)
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


class TestGeraeteschalter:
    def test_ohne_laufwerk_kein_rip_bereich(self, monkeypatch):
        """Beide Dienste laufen dasselbe Image -- das Gerät entscheidet.

        Ohne diese Prüfung böte der Upload-Dienst einen Knopf an, der nur
        scheitern kann.
        """
        monkeypatch.setattr(rip.settings, "cdrom_device", "/gibt/es/nicht")
        assert rip.tools_available()["device"] is False

    def test_mit_laufwerk(self, monkeypatch):
        monkeypatch.setattr(rip.settings, "cdrom_device", "/dev/null")
        assert rip.tools_available()["device"] is True


class TestMatchNachDemRip:
    """Die zentrale Behauptung dieses Features, gegen echte Dateien geprüft.

    Eine gerippte CD hat keine Tags -- die Zuordnung Datei→Track hängt damit
    allein an den Spieldauern. Dass das trifft, ist die Begründung für den
    ganzen DiscID-Umweg und gehört belegt, nicht behauptet.
    """

    #: Spieldauern der Testvektor-CD in Sekunden (Sektoren aus dem TOC / 75).
    LAENGEN = (202.84, 226.01, 190.37, 224.29, 227.67, 199.64)

    #: Der Release, den MusicBrainz zu dieser DiscID nennt.
    MBID = "d3dc4be9-9749-4959-99e5-133d0cb467fe"

    @pytest.mark.network
    def test_release_id_ordnet_die_tracks_richtig_zu(self, tmp_path):
        # So sieht ein frischer Rip aus: richtige Längen, keine Titel -- aber
        # die Tracknummer, die flac beim Packen mitbekommt.
        import mediafile

        from backend import matching
        from tests.flacfixture import write_flac

        pfade = []
        for i, laenge in enumerate(self.LAENGEN, start=1):
            pfad = write_flac(tmp_path / f"{i:02d} Track {i}.flac", seconds=laenge)
            medien = mediafile.MediaFile(pfad)
            medien.track = i
            medien.save()
            pfade.append(pfad)

        match = matching.find_candidate_by_id(pfade, self.MBID, mbid=self.MBID)
        assert match is not None, "Zu dieser Release-ID muss ein Match kommen"

        kandidat = matching.serialize_candidate(match, 0)
        assert len(kandidat.pairings) == len(self.LAENGEN)

        # Der Kern: Datei n muss zu Track n gehören. Die Paare zählen, nicht
        # die Trackliste -- die ist ohnehin nach Nummer sortiert.
        zuordnung = {p.filename: p.new_track for p in kandidat.pairings}
        erwartet = {f"{i:02d} Track {i}.flac": i for i in range(1, 7)}
        assert zuordnung == erwartet, f"falsch zugeordnet: {zuordnung}"
        # Passt die Zuordnung, stimmen auch die Spieldauern auf die Sekunde.
        assert all(
            p.length_delta is not None and abs(p.length_delta) < 1
            for p in kandidat.pairings
        )

    @pytest.mark.network
    def test_ohne_tracknummer_geht_die_zuordnung_daneben(self, tmp_path):
        """Die Gegenprobe -- sie begründet, warum beim Rippen getaggt wird."""
        from backend import matching
        from tests.flacfixture import write_flac

        pfade = [
            write_flac(tmp_path / f"{i:02d} Track {i}.flac", seconds=laenge)
            for i, laenge in enumerate(self.LAENGEN, start=1)
        ]

        match = matching.find_candidate_by_id(pfade, self.MBID, mbid=self.MBID)
        kandidat = matching.serialize_candidate(match, 0)
        zuordnung = {p.filename: p.new_track for p in kandidat.pairings}
        erwartet = {f"{i:02d} Track {i}.flac": i for i in range(1, 7)}
        assert zuordnung != erwartet, (
            "Ohne Tracknummer trifft die Zuordnung zufällig -- dann wäre die "
            "Begründung für das Tagging beim Rippen hinfällig"
        )


class TestHoerbuchZiel:
    """Der Fehlerpfad darf niemals das ganze Buch mitnehmen."""

    @pytest.fixture
    def buch(self, tmp_path, monkeypatch):
        from backend import audiobook

        monkeypatch.setattr(
            audiobook.settings, "audiobook_root", tmp_path / "audiobooks"
        )
        ziel = audiobook.book_dir("Frank Herbert", "Der Wüstenplanet")
        # Zwei Discs sind schon eingelesen -- Stunden Arbeit.
        for cd in ("CD 1", "CD 2"):
            (ziel / cd).mkdir(parents=True)
            (ziel / cd / "01 Track 1.flac").write_bytes(b"fLaC\x00\x00\x00\x22")
        return ziel

    def test_fehlgeschlagene_disc_laesst_die_vorherigen_stehen(
        self, buch, toc, monkeypatch
    ):
        from backend import audiobook

        dritte = audiobook.next_disc_dir(buch)
        assert dritte.name == "CD 3"
        dritte.mkdir(parents=True)

        def kaputt(nummer, ziel, **kwargs):
            raise rip.RipError("Track 1 ließ sich nicht lesen.")

        monkeypatch.setattr(rip, "_rip_track", kaputt)

        job = rip.RipJob(modus="hoerbuch", buch=str(buch))
        rip._arbeite(
            job,
            toc,
            dritte,
            bei_fehler=lambda: __import__("shutil").rmtree(dritte, ignore_errors=True),
            mit_lookup=False,
        )

        assert job.zustand == "fehler"
        # Die angefangene Disc ist weg ...
        assert not dritte.exists()
        # ... aber das Buch und die fertigen Discs stehen.
        assert buch.is_dir()
        assert (buch / "CD 1" / "01 Track 1.flac").is_file()
        assert (buch / "CD 2" / "01 Track 1.flac").is_file()

    def test_hoerbuch_rip_fragt_musicbrainz_nicht(self, buch, toc, monkeypatch):
        """MusicBrainz kennt Hörbücher kaum -- Audiobookshelf macht das Matching."""
        gefragt = []
        monkeypatch.setattr(
            rip.discid, "lookup", lambda *a, **k: gefragt.append(1) or []
        )
        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )

        from backend import audiobook

        dritte = audiobook.next_disc_dir(buch)
        dritte.mkdir(parents=True)
        job = rip.RipJob(modus="hoerbuch")
        rip._arbeite(job, toc, dritte, bei_fehler=lambda: None, mit_lookup=False)

        assert job.zustand == "fertig"
        assert gefragt == [], "im Hörbuch-Modus darf nicht angefragt werden"


class TestAnzeigeDesBuchs:
    """Nach „Nächste CD" muss sichtbar sein, wohin die Disc geht."""

    def test_buch_und_disc_werden_lesbar(self):
        job = rip.RipJob(
            modus="hoerbuch",
            buch="/audiobooks/Astrid Lindgren/Ronja",
            disc_ordner="/audiobooks/Astrid Lindgren/Ronja/CD 3",
        )
        assert job.buch_anzeige == "Astrid Lindgren – Ronja"
        assert job.disc_anzeige == "CD 3"

    def test_ohne_buch_leer_statt_fehler(self):
        job = rip.RipJob()
        assert job.buch_anzeige == ""
        assert job.disc_anzeige == ""


class TestFortschrittImTrack:
    """Das echte Ausgabeformat von ``cdparanoia -e``.

    Aus einem tatsächlichen Rip mitgeschnitten -- die Zahl hinter dem ``@``
    steht in Samples, nicht in Sektoren und nicht in Bytes. Nur geteilt durch
    588 ergeben die Werte glatte Sektornummern.
    """

    AUSGABE = """\
Sending all callbacks to stderr for wrapper script
cdparanoia III release 10.2 (September 11, 2008)

Ripping from sector       0 (track  1 [0:00.00])
          to sector   29461 (track  1 [6:32.61])

outputting to track1.wav

##: 0 [read] @ 24696
##: 0 [read] @ 56448
##: 0 [read] @ 1009008
"""

    def test_samples_werden_zu_sektoren(self):
        assert rip.parse_progress("##: 0 [read] @ 24696") == ("read", 42)
        assert rip.parse_progress("##: 0 [read] @ 56448") == ("read", 96)
        assert rip.parse_progress("##: 0 [read] @ 1009008") == ("read", 1716)

    def test_kopfzeilen_sind_kein_fortschritt(self):
        for zeile in self.AUSGABE.splitlines():
            if not zeile.startswith("##:"):
                assert rip.parse_progress(zeile) is None, zeile

    def test_alle_fortschrittszeilen_der_echten_ausgabe(self):
        erkannt = [
            rip.parse_progress(z)
            for z in self.AUSGABE.splitlines()
            if rip.parse_progress(z) is not None
        ]
        assert erkannt == [("read", 42), ("read", 96), ("read", 1716)]

    def test_leseprobleme_werden_erkannt(self):
        """Bei einer zerkratzten CD meldet cdparanoia andere Zustände."""
        assert rip.parse_progress("##: 4 [scratch] @ 588") == ("scratch", 1)
        assert rip.parse_progress("##: -1 [readerr] @ 1176") == ("readerr", 2)
        # Und die haben einen deutschen Text.
        assert rip._MUEHSAM["scratch"] == "Kratzer erkannt"
        assert rip._MUEHSAM["readerr"] == "Lesefehler"


class TestTrackLaenge:
    def test_laenge_aus_dem_toc(self, toc):
        # Testvektor: Offsets 150, 15363, ... Leadout 95462.
        assert toc.track_sectors(0) == 15363 - 150
        assert toc.track_sectors(1) == 32314 - 15363
        # Der letzte Track reicht bis zum Leadout.
        assert toc.track_sectors(5) == 95462 - 80489

    def test_ausserhalb_gibt_null(self, toc):
        assert toc.track_sectors(99) == 0
        assert toc.track_sectors(-1) == 0


class TestAnteiligerFortschritt:
    """Ein zäher Track soll nicht wie ein Stillstand aussehen."""

    def test_halber_track_zaehlt_halb(self):
        job = rip.RipJob(tracks_gesamt=10, track=2, track_anteil=0.5)
        assert job.prozent == 25  # 2,5 von 10

    def test_ohne_anteil_wie_vorher(self):
        job = rip.RipJob(tracks_gesamt=10, track=2)
        assert job.prozent == 20

    def test_anteil_wird_begrenzt(self):
        """cdparanoia liest bei Überlappung auch mal über das Trackende."""
        job = rip.RipJob(tracks_gesamt=10, track=9, track_anteil=1.8)
        assert job.prozent == 100


class TestRipDauer:
    """Die Dauer soll ablesbar sein -- „10 bis 40 Minuten" war geschätzt."""

    def test_laufender_auftrag_zaehlt_mit(self):
        job = rip.RipJob()
        job.gestartet = rip.time.monotonic() - 125
        assert job.dauer >= 125
        assert job.dauer_text == "2:05"

    def test_fertiger_auftrag_bleibt_stehen(self):
        job = rip.RipJob()
        job.gestartet = 0.0
        job.beendet = 761.0
        assert job.dauer_text == "12:41"

    def test_lange_dauer_mit_stunden(self):
        job = rip.RipJob()
        job.gestartet = 0.0
        job.beendet = 3 * 3600 + 5 * 60 + 9
        assert job.dauer_text == "3:05:09"

    def test_dauer_steht_in_der_schlussmeldung(self, monkeypatch, toc):
        from backend import sessions

        monkeypatch.setattr(
            rip, "_rip_track", lambda n, z, **kw: z.write_bytes(b"fLaC\x00\x00\x00\x22")
        )
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [])

        job = rip.RipJob()
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(
            job, toc, session.directory, bei_fehler=lambda: None
        )

        assert job.zustand == "fertig"
        assert "gelesen in" in job.meldung
        assert job.beendet is not None


class TestFortschrittUeberDieGanzeCD:
    """Der Fortschritt muss über alle Tracks stimmen, nicht nur über den ersten.

    cdparanoia meldet die Position auf der **ganzen CD**. Bei Track 1 fällt
    das mit der Position im Track zusammen -- und genau daran war der Fehler
    an einer echten Beispielausgabe nicht zu erkennen: ab Track 2 stand der
    Balken sofort bei hundert Prozent.
    """

    def test_startsektor_je_track(self, toc):
        # Offsets 150, 15363, 32314 ... -- cdparanoia zählt ohne den Vorlauf.
        assert toc.track_start(0) == 0
        assert toc.track_start(1) == 15363 - 150
        assert toc.track_start(5) == 80489 - 150

    def _lauf(self, monkeypatch, toc, meldungen_je_track):
        """Rippt und zeichnet auf, welche Prozentwerte angezeigt würden."""
        from backend import sessions

        verlauf = []

        def fake(nummer, ziel, fortschritt=None):
            for zustand, sektor in meldungen_je_track(nummer):
                if fortschritt:
                    fortschritt(zustand, sektor)
                verlauf.append(job.prozent)
            ziel.write_bytes(b"fLaC\x00\x00\x00\x22")

        monkeypatch.setattr(rip, "_rip_track", fake)
        monkeypatch.setattr(rip.discid, "lookup", lambda *a, **k: [])

        job = rip.RipJob(disc_id=VEKTOR_ID)
        session = sessions.create_session()
        job.session_id = session.session_id
        rip._arbeite(job, toc, session.directory, bei_fehler=lambda: None)
        return verlauf

    def test_absolute_sektoren_ergeben_einen_echten_verlauf(self, monkeypatch, toc):
        """So meldet cdparanoia: Position auf der CD, nicht im Track."""

        def meldungen(nummer):
            start = toc.track_start(nummer - 1)
            laenge = toc.track_sectors(nummer - 1)
            return [("read", start + int(laenge * anteil))
                    for anteil in (0.25, 0.5, 0.75, 1.0)]

        verlauf = self._lauf(monkeypatch, toc, meldungen)

        # Vorher klebte alles ab Track 2 bei 100 -- jetzt steigt es gleichmäßig.
        assert verlauf[0] < 10, f"Anfang zu hoch: {verlauf[:4]}"
        assert verlauf[-1] == 100
        assert verlauf == sorted(verlauf), f"nicht monoton: {verlauf}"
        # Und es steht nicht die halbe Zeit auf 100.
        assert verlauf.count(100) <= 2, f"zu lange bei 100: {verlauf}"

    def test_rueckwaertslesen_laesst_den_balken_stehen(self, monkeypatch, toc):
        """Bei einer schwierigen Stelle liest cdparanoia zurück und neu."""

        def meldungen(nummer):
            start = toc.track_start(nummer - 1)
            laenge = toc.track_sectors(nummer - 1)
            return [
                ("read", start + int(laenge * 0.5)),
                ("backoff", start + int(laenge * 0.3)),   # springt zurück
                ("overlap", start + int(laenge * 0.35)),
                ("read", start + laenge),
            ]

        verlauf = self._lauf(monkeypatch, toc, meldungen)
        assert verlauf == sorted(verlauf), f"Balken springt zurück: {verlauf}"

    def test_spaeterer_track_zeigt_seinen_eigenen_stand(self, monkeypatch, toc):
        """Halb durch Track 3 heißt (2 + 0.5) / 6, nicht „irgendwas über 33".

        Der monotone Deckel liegt auf dem Job, die Tracklänge dagegen im
        Aufruf je Track -- ohne Zurücksetzen bei Trackbeginn schleppte Track 3
        die 1.0 von Track 2 mit und stünde sofort am nächsten Trackende.
        """
        def meldungen(nummer):
            start = toc.track_start(nummer - 1)
            laenge = toc.track_sectors(nummer - 1)
            if nummer < 3:
                return [("read", start + laenge)]
            return [("read", start + laenge // 2)]

        verlauf = self._lauf(monkeypatch, toc, meldungen)
        # Erste Meldung von Track 3 ist der dritte Eintrag im Verlauf.
        assert verlauf[2] == round((2 + 0.5) / 6 * 100), verlauf

    def test_ohne_toc_kein_absturz(self, monkeypatch, toc):
        """Eine Position vor dem Trackanfang darf nichts kaputt machen."""

        def meldungen(nummer):
            return [("read", 5), ("read", 10)]

        verlauf = self._lauf(monkeypatch, toc, meldungen)
        assert all(0 <= p <= 100 for p in verlauf)
