"""Zugriff auf die beets-Installation des Servers.

Zwei Dinge sind hier wichtig:

1. Wir lesen die **bestehende** beets-Konfiguration des Servers (``config.read()``
   berücksichtigt ``BEETSDIR`` bzw. ``~/.config/beets/config.yaml``). Damit
   stimmen die Kandidaten, die wir in der Oberfläche zeigen, mit dem überein,
   was das System-beets später beim Import sieht.

2. Wir öffnen dabei **keine** Library. ``tag_album`` braucht keine Datenbank --
   nur geladene Metadaten-Plugins. So kann mimport parallel zu einem laufenden
   ``beet``-Prozess matchen, ohne sich um Datenbank-Locks oder
   Schema-Migrationen zu sorgen. Die Library berührt ausschließlich der
   ``beet import``-Subprozess.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from typing import TypedDict

log = logging.getLogger(__name__)

# Reentrant, damit ein verschachtelter Aufruf (etwa aus einer Hilfsfunktion,
# die selbst ensure_loaded aufruft) nicht in einen Deadlock läuft.
_lock = threading.RLock()
_loaded = False

# beet_cli_version() startet einen eigenen Python-Interpreter und ist damit
# der teuerste Teil von health() -- und health() hängt an praktisch jeder
# Seiten- und Fragment-Antwort. Ungecached kostete jede Navigation einen
# Subprozessstart, auf schwacher Hardware spürbar. Das Binary wechselt
# zur Laufzeit nicht, ein Neustart genügt als Invalidierung.
_cli_version_cache: dict[str, str | None] = {}


def ensure_loaded() -> None:
    """Lädt Konfiguration und Plugins genau einmal pro Prozess.

    Ohne ``plugins.load_plugins()`` greift der MusicBrainz-Hook nicht und
    ``tag_album`` liefert schlicht null Kandidaten -- MusicBrainz ist in
    beets 2.x ein Metadaten-Plugin, kein fest eingebauter Teil.
    """
    global _loaded
    with _lock:
        if _loaded:
            return

        from beets import config, metadata_plugins, plugins

        config.read()
        plugins.load_plugins()
        _loaded = True

        active = [p.name for p in plugins.find_plugins()]
        log.info("beets-Plugins geladen: %s", ", ".join(active) or "keine")
        # Direkt abfragen statt über metadata_sources() -- das würde erneut
        # ensure_loaded aufrufen.
        if not metadata_plugins.find_metadata_source_plugins():
            log.warning(
                "Kein Metadaten-Plugin aktiv -- es wird keine Match-Kandidaten "
                "geben. In der beets-Konfiguration muss unter 'plugins' "
                "mindestens 'musicbrainz' stehen."
            )


def metadata_sources() -> list[str]:
    """Namen der aktiven Metadaten-Plugins (MusicBrainz, Discogs, ...)."""
    ensure_loaded()
    from beets import metadata_plugins

    return [p.name for p in metadata_plugins.find_metadata_source_plugins()]


def library_version() -> str:
    """Version des mitgelieferten beets-Pakets."""
    import beets

    return beets.__version__


def beet_cli_version(beet_bin: str) -> str | None:
    """Version des ``beet``-Executables, an das wir den Import übergeben.

    ``None``, wenn das Binary nicht aufrufbar ist. Gecached pro Prozess --
    siehe ``_cli_version_cache``.
    """
    with _lock:
        if beet_bin in _cli_version_cache:
            return _cli_version_cache[beet_bin]

    version = _beet_cli_version_uncached(beet_bin)

    with _lock:
        _cli_version_cache[beet_bin] = version
    return version


def _beet_cli_version_uncached(beet_bin: str) -> str | None:
    try:
        proc = subprocess.run(
            [beet_bin, "version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None

    # Gesucht ist die Zeile "beets version 2.13.1" -- aber nicht unbedingt die
    # erste: beets schreibt Hinweise davor, etwa beim Migrieren des
    # Datenbankschemas ("Created database backup at ..."). Nahm man stumpf
    # Zeile eins, galt dieser Hinweis als Versionsnummer, der Vergleich mit der
    # eigenen Version schlug fehl und der Import wurde gesperrt -- beim ersten
    # Start mit einer neuen Library also zuverlässig.
    zeilen = (proc.stdout or "").splitlines() + (proc.stderr or "").splitlines()
    for zeile in zeilen:
        if "version" not in zeile.lower():
            continue
        for token in zeile.split():
            # Eine Versionsnummer beginnt mit einer Ziffer und enthält Punkte.
            if token[:1].isdigit():
                return token.rstrip(".,;")

    # Nichts Versionsförmiges gefunden. Lieber nichts melden als einen
    # Hinweistext, der später als Versionsunterschied gelesen würde.
    return None


class Health(TypedDict):
    """Startzustand für das Hinweisbanner in der Oberfläche."""

    beets_version: str
    beet_cli_version: str | None
    metadata_sources: list[str]
    fingerprint: bool
    problems: list[str]
    import_ready: bool


def health() -> Health:
    """Startzustand für das Hinweisbanner in der Oberfläche."""
    from backend.config import settings

    ensure_loaded()
    own = library_version()
    cli = beet_cli_version(settings.beet_bin)
    sources = metadata_sources()

    problems: list[str] = []
    if cli is None:
        problems.append(
            f"'{settings.beet_bin}' ist nicht aufrufbar -- der Import ist "
            "deaktiviert. Pfad über MIMPORT_BEET_BIN setzen."
        )
    elif cli != own:
        # Ein Versionsunterschied ist kein Schönheitsfehler: das neuere beets
        # migriert beim Öffnen das Schema der library.db, und das ist für die
        # ältere Installation nicht mehr lesbar.
        problems.append(
            f"Versionsunterschied: mimport nutzt beets {own}, "
            f"'{settings.beet_bin}' meldet {cli}. Im Container kann das nicht "
            "vorkommen, dort ist beides dasselbe venv -- tritt es auf, zeigt "
            "MIMPORT_BEET_BIN auf eine fremde Installation."
        )
    if not sources:
        problems.append(
            "Kein Metadaten-Plugin aktiv -- es gibt keine Match-Kandidaten. "
            "In der beets-Konfiguration 'plugins: [musicbrainz]' ergänzen."
        )

    return {
        "beets_version": own,
        "beet_cli_version": cli,
        "metadata_sources": sources,
        "fingerprint": settings.fingerprint_available(),
        "problems": problems,
        #: Import nur erlauben, wenn das CLI erreichbar und versionsgleich ist.
        "import_ready": cli is not None and cli == own,
    }
