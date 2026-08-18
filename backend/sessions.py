"""Staging-Bereich für Uploads.

Jeder Upload landet in einem eigenen Ordner unterhalb von
``settings.staging_root``. Erst der Import übergibt diesen Ordner an beets.

Dateinamen sind hier grundsätzlich feindlich: sowohl ``UploadFile.filename`` als
auch der Unterordner-Pfad aus ``webkitRelativePath`` kommen unverändert vom
Browser. Beides wird zerlegt, von allem Gefährlichen befreit und darf am Ende
nur innerhalb des Session-Ordners landen.
"""

from __future__ import annotations

import json
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


@dataclass
class OpenSession:
    """Eine Sitzung, die im Staging liegt und noch nicht importiert wurde."""

    session_id: str
    label: str
    file_count: int
    total_bytes: int
    age_seconds: float

    @property
    def size_label(self) -> str:
        if self.total_bytes >= 1024**3:
            return f"{self.total_bytes / 1024**3:.1f} GB"
        if self.total_bytes >= 1024**2:
            return f"{self.total_bytes / 1024**2:.0f} MB"
        return f"{max(1, self.total_bytes // 1024)} KB"

    @property
    def age_label(self) -> str:
        minuten = int(self.age_seconds // 60)
        if minuten < 1:
            return "gerade eben"
        if minuten < 60:
            return f"vor {minuten} Min"
        stunden = minuten // 60
        return f"vor {stunden} Std" if stunden < 24 else "vor über einem Tag"


def _label_for(session: StagingSession, dateien: list[Path]) -> str:
    """Ein wiedererkennbarer Name für eine Sitzung.

    Der Ordner, in dem die Dateien liegen, ist die beste Auskunft, die das
    Dateisystem hergibt -- beim Upload die gewählte Albumstruktur, bei der
    Daten-CD der übernommene Ordner. Liegt alles flach (ein Rip etwa), muss
    der erste Dateiname genügen.
    """
    ordner = {
        d.parent.relative_to(session.directory).parts[0]
        for d in dateien
        if d.parent != session.directory
    }
    if len(ordner) == 1:
        return ordner.pop()
    if ordner:
        return f"{len(ordner)} Ordner"
    return dateien[0].name if dateien else "leer"


def list_open() -> list[OpenSession]:
    """Alle Sitzungen im Staging, neueste zuerst.

    Die Session-ID steht sonst nur im ausgelieferten HTML: Wer den Tab
    schließt oder dessen Gerät ausgeht, käme an seine bereits hochgeladenen
    Dateien nicht mehr heran, obwohl sie noch da sind. Diese Liste ist der Weg
    zurück -- und weil sie serverseitig entsteht, funktioniert sie auch von
    einem anderen Gerät aus.
    """
    root = settings.staging_root
    if not root.is_dir():
        return []

    jetzt = time.time()
    offen: list[OpenSession] = []
    for directory in root.iterdir():
        if not directory.is_dir() or not SESSION_ID_RE.match(directory.name):
            continue
        session = StagingSession(session_id=directory.name, directory=directory)
        dateien = session.audio_paths
        if not dateien:
            continue
        gesamt = 0
        for datei in dateien:
            try:
                gesamt += datei.stat().st_size
            except OSError:
                continue
        try:
            alter = max(0.0, jetzt - _last_touched(directory))
        except OSError:
            alter = 0.0
        offen.append(
            OpenSession(
                session_id=directory.name,
                label=_label_for(session, dateien),
                file_count=len(dateien),
                total_bytes=gesamt,
                age_seconds=alter,
            )
        )
    offen.sort(key=lambda s: s.age_seconds)
    return offen


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


#: Zwischenstand des Handtagging-Formulars -- kein Datei-Tag, nur Text, damit
#: eine unterbrochene Sitzung (Browser zu, Gerät gewechselt) nicht auch noch
#: die halb ausgefüllten Felder kostet. Führender Punkt: taucht dadurch nicht
#: unter den Audiodateien auf (``audio_paths`` filtert ohnehin nach Endung).
_DRAFT_DATEINAME = ".mimport-entwurf.json"


def _draft_path(session: StagingSession) -> Path:
    return session.directory / _DRAFT_DATEINAME


def save_draft(session: StagingSession, felder: dict[str, str]) -> None:
    """Sichert die aktuellen Formularwerte. Überschreibt den letzten Stand
    komplett -- der Aufrufer schickt immer das ganze Formular, nicht nur ein
    geändertes Feld."""
    try:
        _draft_path(session).write_text(json.dumps(felder, ensure_ascii=False), encoding="utf-8")
    except OSError:
        log.warning("Entwurf konnte nicht gespeichert werden: %s", session.session_id)


def load_draft(session: StagingSession) -> dict[str, str]:
    """Der zuletzt gesicherte Formularstand, oder leer, wenn es keinen gibt."""
    try:
        roh = _draft_path(session).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        daten = json.loads(roh)
    except ValueError:
        return {}
    return daten if isinstance(daten, dict) else {}


def delete_draft(session: StagingSession) -> None:
    _draft_path(session).unlink(missing_ok=True)


def cleanup_if_empty(session: StagingSession) -> None:
    """Entfernt den Session-Ordner, wenn beets alle Dateien verschoben hat."""
    if not session.directory.is_dir():
        return
    # Zuerst den Entwurf weg -- sonst zählt er unten als "noch was da" und der
    # sonst leere Ordner bliebe für immer liegen.
    delete_draft(session)
    remaining = [p for p in session.directory.rglob("*") if p.is_file()]
    if not remaining:
        shutil.rmtree(session.directory, ignore_errors=True)
        log.info("Leere Session aufgeräumt: %s", session.session_id)
