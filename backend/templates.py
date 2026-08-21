"""Jinja2-Umgebung mit ein paar Filtern für die Anzeige."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

#: Cache-Buster für /static/*.js und .css: Anders als Cover-Bilder (die ihr
#: eigenes ?v= tragen) hatten die <script>-Tags keinen -- ein Deploy landete
#: dadurch erst nach hartem Neuladen wirklich im Browser. Der Prozessstart
#: reicht als Version, weil ein Deploy hier immer den Container neu startet.
ASSET_VERSION = str(int(time.time()))
templates.env.globals["asset_version"] = ASSET_VERSION


def human_bytes(value: float | None) -> str:
    """Byte-Angabe in etwas Lesbares umwandeln."""
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def duration(seconds: float | None) -> str:
    """Sekunden als ``m:ss``."""
    if not seconds:
        return "–"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def signed(value: float | None) -> str:
    """Vorzeichenbehaftete Zahl, für Längenabweichungen."""
    if value is None:
        return ""
    return f"+{value:g}" if value > 0 else f"{value:g}"


templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["duration"] = duration
templates.env.filters["signed"] = signed
