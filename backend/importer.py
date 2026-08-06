"""Übergabe an das beets des Servers.

Der Import selbst bleibt bewusst beim System-``beet``: dort steckt die
konfigurierte Library, das Umbenennungsschema und die aktivierten Plugins.
mimport ruft es als Subprozess mit ``-A`` auf, also **ohne** Autotagging --
die Tags stehen zu diesem Zeitpunkt schon in den Dateien (siehe
``backend.tagging``), und beets soll sie nicht erneut überschreiben, sondern nur
noch die Dateien an ihren Platz in der Library bringen.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from backend.config import settings

log = logging.getLogger(__name__)


@dataclass
class ImportResult:
    """Ausgang eines Importlaufs."""

    command: list[str] = field(default_factory=list)
    returncode: int = -1
    stdout: str = ""
    stderr: str = ""
    #: Nur angesehen, nichts verschoben.
    pretend: bool = False
    timed_out: bool = False
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.error and not self.timed_out

    @property
    def command_line(self) -> str:
        """Der Aufruf als lesbare Zeile, damit man ihn nachvollziehen kann."""
        return " ".join(self.command)

    @property
    def output(self) -> str:
        parts = [self.stdout.strip(), self.stderr.strip()]
        return "\n".join(p for p in parts if p)


def build_command(directory: Path, *, pretend: bool = False) -> list[str]:
    """Setzt die ``beet import``-Kommandozeile zusammen.

    * ``-q`` unterdrückt jede Rückfrage im Terminal -- die Entscheidungen sind
      bereits in der Oberfläche gefallen.
    * ``-A`` schaltet das Autotagging ab. Damit läuft beets über
      ``import_asis``, greift MusicBrainz nicht erneut ab und übernimmt die Tags
      wie sie in den Dateien stehen.
    * ``-m`` verschiebt statt zu kopieren, sonst bleiben die Uploads im Staging
      liegen und tauchen beim nächsten Mal wieder auf.
    """
    command = [settings.beet_bin, "import", "-q", "-A"]
    if pretend:
        command.append("--pretend")
    elif settings.move_on_import:
        command.append("-m")
    else:
        command.append("-c")
    command.append(str(directory))
    return command


def run_import(directory: Path, *, pretend: bool = False) -> ImportResult:
    """Führt den Import aus und sammelt die Ausgabe ein.

    Ohne Autotagging gibt es keine Netzabfragen, der Lauf ist deshalb kurz --
    Streaming der Ausgabe ist hier nicht nötig.
    """
    command = build_command(directory, pretend=pretend)
    result = ImportResult(command=command, pretend=pretend)
    log.info("Starte %s", " ".join(command))

    try:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=settings.import_timeout,
            check=False,
            # Kein shell=True: die Argumente gehen unverändert an das Programm,
            # damit aus Dateinamen keine Shell-Befehle werden können.
            shell=False,
        )
    except FileNotFoundError:
        result.error = (
            f"'{settings.beet_bin}' wurde nicht gefunden. Pfad zum beets des "
            "Servers über MIMPORT_BEET_BIN setzen."
        )
        return result
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.error = (
            f"Import nach {settings.import_timeout}s abgebrochen. "
            "MIMPORT_IMPORT_TIMEOUT erhöhen, falls das zu knapp ist."
        )
        return result
    except OSError as exc:
        result.error = f"Import konnte nicht gestartet werden: {exc}"
        return result

    result.returncode = proc.returncode
    result.stdout = proc.stdout or ""
    result.stderr = proc.stderr or ""
    if result.returncode != 0 and not result.error:
        result.error = f"beets endete mit Rückgabewert {result.returncode}"
    return result
