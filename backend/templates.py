"""Jinja2-Umgebung mit ein paar Filtern für die Anzeige."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATE_DIR))


def human_bytes(value: float | int | None) -> str:
    """Byte-Angabe in etwas Lesbares umwandeln."""
    if not value:
        return "0 B"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def duration(seconds: float | int | None) -> str:
    """Sekunden als ``m:ss``."""
    if not seconds:
        return "–"
    total = int(seconds)
    return f"{total // 60}:{total % 60:02d}"


def signed(value: float | int | None) -> str:
    """Vorzeichenbehaftete Zahl, für Längenabweichungen."""
    if value is None:
        return ""
    return f"+{value:g}" if value > 0 else f"{value:g}"


templates.env.filters["human_bytes"] = human_bytes
templates.env.filters["duration"] = duration
templates.env.filters["signed"] = signed
