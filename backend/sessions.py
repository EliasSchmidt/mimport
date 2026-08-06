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


def _staging_root() -> Path:
    root = settings.staging_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def create_session() -> StagingSession:
    """Legt eine neue Session mit einem zufälligen Namen an."""
    session_id = secrets.token_urlsafe(18)
    directory = _staging_root() / session_id
    directory.mkdir(parents=True, exist_ok=False)
    return StagingSession(session_id=session_id, directory=directory)


def get_session(session_id: str) -> StagingSession:
    """Löst eine Session-ID auf und stellt sicher, dass sie im Staging liegt."""
    if not SESSION_ID_RE.match(session_id or ""):
        raise SessionError("Ungültige Session-ID")

    root = _staging_root().resolve()
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


def cleanup_if_empty(session: StagingSession) -> None:
    """Entfernt den Session-Ordner, wenn beets alle Dateien verschoben hat."""
    if not session.directory.is_dir():
        return
    remaining = [p for p in session.directory.rglob("*") if p.is_file()]
    if not remaining:
        shutil.rmtree(session.directory, ignore_errors=True)
        log.info("Leere Session aufgeräumt: %s", session.session_id)
