"""Die Entscheidung des Nutzers in die Dateien schreiben.

mimport lässt beets beim Import ausdrücklich *nicht* nochmal taggen. Die
Zuordnung ist ja schon gefallen -- der Nutzer hat in der Oberfläche einen
Kandidaten gewählt. Also wenden wir dessen Metadaten hier direkt auf die
Dateien an, und der anschließende Import läuft mit ``-A`` (kein Autotagging).

Damit gilt genau das, was der Nutzer gesehen und bestätigt hat. Würde man
stattdessen ``beet import -q`` mit einer Release-ID aufrufen, wendet beets den
Match nur bei ``Recommendation.strong`` an und überspringt ihn sonst still --
gerade bei unvollständigen Uploads wäre die Auswahl also wirkungslos.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import beets_env

log = logging.getLogger(__name__)


@dataclass
class TagWriteResult:
    """Ergebnis des Tag-Schreibens."""

    written: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.written) and not self.failed


def apply_album_match(match: Any, *, from_scratch: bool = False) -> TagWriteResult:
    """Schreibt die Metadaten eines gewählten ``AlbumMatch`` in die Dateien.

    ``apply_metadata`` aktualisiert die ``Item``-Objekte im Speicher,
    ``try_write`` legt die Tags dann in der Datei ab.
    """
    beets_env.ensure_loaded()
    result = TagWriteResult()

    match.apply_metadata(from_scratch=from_scratch)

    for item in match.mapping:
        name = _display(item)
        try:
            if item.try_write():
                result.written.append(name)
            else:
                result.failed.append((name, "Tags konnten nicht geschrieben werden"))
        except Exception as exc:
            log.exception("Tags schreiben fehlgeschlagen: %s", name)
            result.failed.append((name, str(exc)))

    # Dateien, die zu keinem Track des Releases passen, bleiben unangetastet --
    # sie behalten ihre vorhandenen Tags und werden as-is mit importiert.
    for item in getattr(match, "extra_items", []) or []:
        log.info("Ohne Zuordnung, bleibt unverändert: %s", _display(item))

    return result


def apply_manual_tags(paths: list[Path], fields: dict[str, str]) -> TagWriteResult:
    """Schreibt handgepflegte Tags auf mehrere Dateien.

    Gedacht für den Fall, dass es keinen brauchbaren Match gibt und der Nutzer
    Künstler, Album und Jahr selbst setzt. Leere Werte werden übersprungen,
    damit ein leeres Formularfeld nichts überschreibt.
    """
    beets_env.ensure_loaded()
    from beets.library import Item

    result = TagWriteResult()
    usable = {key: value for key, value in fields.items() if str(value).strip()}
    if not usable:
        return result

    for path in paths:
        try:
            item = Item.from_path(str(path))
        except Exception as exc:
            result.failed.append((path.name, f"nicht lesbar: {exc}"))
            continue
        for key, value in usable.items():
            if key == "year":
                try:
                    item.year = int(str(value).strip())
                except ValueError:
                    continue
            else:
                item[key] = str(value).strip()
        try:
            if item.try_write():
                result.written.append(path.name)
            else:
                result.failed.append((path.name, "Tags konnten nicht geschrieben werden"))
        except Exception as exc:
            result.failed.append((path.name, str(exc)))
    return result


def _display(item: Any) -> str:
    path = item.path
    if isinstance(path, bytes):
        return Path(path.decode("utf-8", "replace")).name
    return Path(str(path)).name
