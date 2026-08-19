"""Die Rechenteile von ``static/cover.js``.

Cover-Zuschnitt und Entzerrung laufen im Browser -- das Handy hat das Foto
ohnehin. Getestet wird trotzdem, und zwar mit Node: die Homographie und die
Eckensuche sind reine Funktionen und brauchen kein DOM.

Ohne diese Prüfung wäre die Mathematik das einzige Stück im Projekt, das nur
aus Anschauung stimmt -- und genau dort ist in dieser Sitzung schon zweimal
eine Annahme falsch gewesen.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
braucht_node = pytest.mark.skipif(NODE is None, reason="node fehlt")
SKRIPT = Path(__file__).parent / "js" / "cover.mjs"


@braucht_node
def test_mathematik_und_eckensuche():
    """Läuft die Prüfung in Node und übernimmt deren Urteil."""
    assert NODE is not None
    ergebnis = subprocess.run(
        [NODE, str(SKRIPT)],
        cwd=Path(__file__).parent.parent,
        capture_output=True,
        text=True,
        check=False,
    )
    print(ergebnis.stdout)
    assert ergebnis.returncode == 0, ergebnis.stdout + ergebnis.stderr
    # Die Homographie muss dieselben Werte liefern wie die Gegenrechnung in
    # Python -- steht als Kommentar in der Prüfdatei.
    assert "FEHL" not in ergebnis.stdout
