"""Der Aufruf von ``beet import``.

Entscheidend ist ``-A``: ohne dieses Flag würde beets erneut taggen und die
Auswahl des Nutzers überschreiben oder -- bei schwachem Match und ``-q`` --
das Album still überspringen.
"""

from __future__ import annotations

from pathlib import Path

from backend import importer


class TestBuildCommand:
    def test_kein_autotagging(self):
        command = importer.build_command(Path("/staging/abc"))
        assert "-A" in command, "ohne -A würde beets erneut taggen"

    def test_keine_rueckfragen(self):
        command = importer.build_command(Path("/staging/abc"))
        assert "-q" in command

    def test_zielordner_kommt_zuletzt(self):
        command = importer.build_command(Path("/staging/abc"))
        assert command[-1] == "/staging/abc"

    def test_import_unterbefehl(self):
        command = importer.build_command(Path("/staging/abc"))
        assert command[1] == "import"

    def test_verschieben_ist_standard(self, monkeypatch):
        monkeypatch.setattr(importer.settings, "move_on_import", True)
        assert "-m" in importer.build_command(Path("/x"))

    def test_kopieren_wenn_so_eingestellt(self, monkeypatch):
        monkeypatch.setattr(importer.settings, "move_on_import", False)
        command = importer.build_command(Path("/x"))
        assert "-c" in command
        assert "-m" not in command

    def test_probelauf_verschiebt_nichts(self, monkeypatch):
        monkeypatch.setattr(importer.settings, "move_on_import", True)
        command = importer.build_command(Path("/x"), pretend=True)
        assert "--pretend" in command
        # Beim Probelauf darf kein Verschieben angefordert werden.
        assert "-m" not in command
        assert "-c" not in command

    def test_binary_aus_der_konfiguration(self, monkeypatch):
        monkeypatch.setattr(importer.settings, "beet_bin", "/opt/beets/bin/beet")
        assert importer.build_command(Path("/x"))[0] == "/opt/beets/bin/beet"

    def test_pfade_bleiben_einzelne_argumente(self):
        """Kein shell=True: Sonderzeichen im Namen dürfen nichts auslösen."""
        command = importer.build_command(Path("/staging/a b; rm -rf tmp"))
        # Der Pfad bleibt ein einziges Argument, wird also nicht an Semikolon
        # oder Leerzeichen zerlegt.
        assert command[-1] == "/staging/a b; rm -rf tmp"
        assert len([arg for arg in command if "rm" in arg]) == 1


class TestRunImport:
    def test_fehlendes_binary_wird_gemeldet(self, monkeypatch, tmp_path):
        monkeypatch.setattr(importer.settings, "beet_bin", "/gibt/es/nicht/beet")
        result = importer.run_import(tmp_path)
        assert not result.ok
        assert "nicht gefunden" in result.error
        assert "MIMPORT_BEET_BIN" in result.error

    def test_erfolgreicher_lauf(self, monkeypatch, tmp_path):
        # 'true' beendet sich mit 0, ohne etwas zu tun.
        monkeypatch.setattr(importer.settings, "beet_bin", "true")
        result = importer.run_import(tmp_path)
        assert result.ok
        assert result.returncode == 0

    def test_fehlerhafter_lauf(self, monkeypatch, tmp_path):
        monkeypatch.setattr(importer.settings, "beet_bin", "false")
        result = importer.run_import(tmp_path)
        assert not result.ok
        assert "Rückgabewert" in result.error

    def test_zeitueberschreitung(self, monkeypatch, tmp_path):
        monkeypatch.setattr(importer.settings, "beet_bin", "sleep")
        monkeypatch.setattr(importer.settings, "import_timeout", 1)

        # sleep bekommt den Ordner als Argument und versteht ihn nicht --
        # deshalb hier ein Aufruf, der wirklich wartet.
        def command(directory, *, pretend=False):
            return ["sleep", "5"]

        monkeypatch.setattr(importer, "build_command", command)
        result = importer.run_import(tmp_path)
        assert result.timed_out
        assert not result.ok

    def test_kommandozeile_ist_nachvollziehbar(self, monkeypatch, tmp_path):
        monkeypatch.setattr(importer.settings, "beet_bin", "true")
        result = importer.run_import(tmp_path, pretend=True)
        assert "--pretend" in result.command_line
        assert result.pretend


class TestLibraryLock:
    """Zwei Dienste, eine library.db -- Importe müssen sich abwechseln."""

    def test_lock_liegt_neben_der_library(self):
        pfad = importer._lock_path()
        # Nicht im Staging: der Lock gehört zu der Datenbank, die er schützt,
        # damit alle Prozesse mit derselben Library dieselbe Datei nehmen.
        assert pfad.suffix == ".lock"
        assert pfad.stem == "library"

    def test_zweiter_import_wartet_auf_den_ersten(self):
        """Der eigentliche Zweck: keine zwei beet-Importe gleichzeitig."""
        import threading

        verlauf = []
        zweiter_drin = threading.Event()

        def zweiter():
            with importer.library_lock():
                verlauf.append("zweiter")
                zweiter_drin.set()

        with importer.library_lock():
            verlauf.append("erster")
            thread = threading.Thread(target=zweiter)
            thread.start()
            # Solange der erste den Lock hält, darf der zweite nicht durch.
            assert not zweiter_drin.wait(timeout=0.5)
            assert verlauf == ["erster"]

        # Nach dem Freigeben kommt er dran.
        assert zweiter_drin.wait(timeout=5)
        thread.join(timeout=5)
        assert verlauf == ["erster", "zweiter"]

    def test_ohne_schreibbaren_ort_laeuft_der_import_trotzdem(self, monkeypatch):
        """Ein nicht anlegbarer Lock darf den Import nicht verhindern."""
        monkeypatch.setattr(
            importer, "_lock_path", lambda: Path("/nicht/anlegbar/x.lock")
        )
        with importer.library_lock():
            pass  # kein Fehler
