"""Staging-Bereich für Uploads.

Jeder Upload landet in einem eigenen Ordner unterhalb von
``settings.staging_root``. Erst der Import übergibt diesen Ordner an beets.

Dateinamen sind hier grundsätzlich feindlich: sowohl ``UploadFile.filename`` als
auch der Unterordner-Pfad aus ``webkitRelativePath`` kommen unverändert vom
Browser. Beides wird zerlegt, von allem Gefährlichen befreit und darf am Ende
nur innerhalb des Session-Ordners landen.
"""

from __future__ import annotations

import logging
import re
import secrets
import shutil
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from backend.config import AUDIO_EXTENSIONS, settings

log = logging.getLogger(__name__)

#: Session-IDs erzeugen wir selbst und akzeptieren nur genau dieses Format --
#: damit kann keine ID aus einer Anfrage zu einem Pfadwechsel führen.
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")

#: Zeichen, die in Dateinamen nichts zu suchen haben.
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f<>:"\\|?*]')


class SessionError(Exception):
    """Ungültige Session oder unzulässiger Pfad."""


def sanitize_component(name: str) -> str:
    """Macht einen einzelnen Pfadbestandteil harmlos."""
    cleaned = _UNSAFE_CHARS.sub("_", name).replace("/", "_").strip()
    # Führende Punkte entfernen, damit weder ".." noch versteckte Dateien
    # entstehen.
    cleaned = cleaned.lstrip(".").strip()
    if not cleaned:
        cleaned = "unbenannt"
    return cleaned[:200]


def sanitize_relative_path(raw: str) -> Path:
    """Baut aus einer Browser-Pfadangabe einen sicheren relativen Pfad.

    Absolute Pfade, ``..`` und Laufwerksbuchstaben werden verworfen; die
    Ordnerstruktur des Uploads bleibt aber erhalten, weil das Album-Matching den
    Ordner als zusammengehörige Menge braucht.
    """
    raw = (raw or "").replace("\\", "/")
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        if part in ("", ".", "/"):
            continue
        if part == "..":
            # Aufstiegsversuche einfach fallen lassen statt den Upload zu
            # verweigern -- der Rest des Pfads bleibt nutzbar.
            continue
        if re.fullmatch(r"[A-Za-z]:", part):
            continue
        parts.append(sanitize_component(part))
    if not parts:
        parts = ["unbenannt"]
    # Nur die letzten Ebenen behalten, sonst legt ein Upload beliebig tiefe
    # Baumstrukturen an.
    return Path(*parts[-4:])


@dataclass
class StagingSession:
    """Ein Upload-Vorgang und sein Ordner auf der Platte."""

    session_id: str
    directory: Path

    @property
    def audio_paths(self) -> list[Path]:
        """Alle Audiodateien der Session, stabil sortiert."""
        found = [
            path
            for path in sorted(self.directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
        ]
        return found

    @property
    def is_empty(self) -> bool:
        return not self.audio_paths


def ensure_root() -> Path:
    """Die Staging-Wurzel, notfalls frisch angelegt.

    Muss aufgerufen werden, bevor irgendetwas den Platz vermisst: ohne den
    Ordner scheitert ``disk_usage`` und mimport würde jeden Upload mit einer
    Meldung über zu wenig Speicherplatz abweisen, statt den Ordner einfach
    wieder anzulegen.
    """
    root = settings.staging_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_session() -> StagingSession:
    """Legt eine neue Session mit einem zufälligen Namen an."""
    session_id = secrets.token_urlsafe(18)
    directory = ensure_root() / session_id
    directory.mkdir(parents=True, exist_ok=False)
    return StagingSession(session_id=session_id, directory=directory)


def get_session(session_id: str) -> StagingSession:
    """Löst eine Session-ID auf und stellt sicher, dass sie im Staging liegt."""
    if not SESSION_ID_RE.match(session_id or ""):
        raise SessionError("Ungültige Session-ID")

    root = ensure_root().resolve()
    directory = (root / session_id).resolve()
    # Doppelt geprüft: Format oben, tatsächlicher Pfad hier.
    if not directory.is_relative_to(root):
        raise SessionError("Ungültige Session-ID")
    if not directory.is_dir():
        raise SessionError("Diese Upload-Sitzung gibt es nicht (mehr)")
    return StagingSession(session_id=session_id, directory=directory)


def target_path(session: StagingSession, relative: Path) -> Path:
    """Zielpfad einer hochgeladenen Datei, garantiert innerhalb der Session."""
    root = session.directory.resolve()
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise SessionError(f"Pfad außerhalb der Sitzung abgewiesen: {relative}")
    return candidate


def delete_session(session_id: str) -> None:
    """Räumt eine Session restlos auf."""
    try:
        session = get_session(session_id)
    except SessionError:
        return
    shutil.rmtree(session.directory, ignore_errors=True)


def usage_bytes() -> int:
    """Belegter Platz im Staging, über alle Sessions zusammen.

    Grundlage für das Gesamtbudget: ``max_upload_bytes`` begrenzt nur den
    einzelnen Upload, nicht die Summe vieler.
    """
    root = settings.staging_root
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except OSError:
            # Verschwindet eine Datei während der Zählung, ist sie eben nicht
            # mehr da -- das Ergebnis ist ohnehin nur eine Momentaufnahme.
            continue
    return total


def _last_touched(directory: Path) -> float:
    """Jüngste Änderungszeit innerhalb eines Session-Ordners.

    Bewusst nicht die mtime des Ordners allein: die bleibt stehen, während in
    einem bereits angelegten Unterordner noch Dateien geschrieben werden.
    """
    newest = directory.stat().st_mtime
    for path in directory.rglob("*"):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def sweep_expired(max_age_hours: int, keep: str | None = None) -> int:
    """Entfernt Sessions, die seit Stunden niemand mehr angefasst hat.

    Abgebrochene Uploads und Sitzungen, in denen nie ein Import ausgelöst wurde,
    blieben sonst für immer liegen und füllen mit der Zeit das Dateisystem.

    Aufgerufen wird das beim Start und vor jedem neuen Upload -- das genügt und
    erspart einen Hintergrunddienst. Die Frist ist absichtlich großzügig:
    zwischen Upload und Entscheidung darf eine lange Pause liegen.

    Dass der laufende Upload nicht selbst weggeräumt wird, sichert die
    Reihenfolge: der Sweep läuft, *bevor* die neue Session angelegt wird.
    ``keep`` ist nur die Rückfalloption für Aufrufer, die das nicht einhalten
    können -- die eigentliche Garantie ist die Reihenfolge.
    """
    root = settings.staging_root
    if max_age_hours <= 0 or not root.is_dir():
        return 0

    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    for directory in sorted(root.iterdir()):
        # Nur was wie eine von uns angelegte Session aussieht -- fremde Ordner
        # unter der Staging-Wurzel bleiben unangetastet.
        if not directory.is_dir() or directory.name == keep:
            continue
        if not SESSION_ID_RE.match(directory.name):
            continue
        try:
            if _last_touched(directory) >= cutoff:
                continue
        except OSError:
            continue
        shutil.rmtree(directory, ignore_errors=True)
        removed += 1
        log.info(
            "Verwaiste Session entfernt (älter als %s h): %s",
            max_age_hours,
            directory.name,
        )
    return removed


def cleanup_if_empty(session: StagingSession) -> None:
    """Entfernt den Session-Ordner, wenn beets alle Dateien verschoben hat."""
    if not session.directory.is_dir():
        return
    remaining = [p for p in session.directory.rglob("*") if p.is_file()]
    if not remaining:
        shutil.rmtree(session.directory, ignore_errors=True)
        log.info("Leere Session aufgeräumt: %s", session.session_id)
