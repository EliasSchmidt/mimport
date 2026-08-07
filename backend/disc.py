"""Import von einer eingelegten Daten-CD.

Gemeint ist die CD mit einem Dateisystem darauf -- typischerweise eine
MP3-Sammlung. Die muss man nicht rippen, sondern nur kopieren. Eine Audio-CD
(CDDA) hat kein Dateisystem und ist hier nicht gemeint.

Der Zuschnitt ist bewusst schmal: dieses Modul bringt die Dateien in eine
Staging-Session, und ab da läuft exakt derselbe Weg wie beim Upload --
Kandidaten anzeigen, auswählen, taggen, importieren. Nichts in ``matching``,
``tagging`` oder ``importer`` weiß, woher die Dateien kamen.

Warum ein Ordner nach dem anderen: ``tag_album`` behandelt eine Session als
*ein* Album. Eine MP3-CD trägt oft zwölf Alben in Unterordnern; alle zusammen
zu matchen ergäbe Unsinn. Deshalb listet ``list_albums()`` die Ordner auf und
der Nutzer wählt einen davon.

Der Inhalt einer fremden CD ist nicht vertrauenswürdiger als ein Upload:
Verzeichnisnamen kommen aus dem Dateisystem der CD, und über Rock Ridge kann
sie Symlinks enthalten, die aus dem Mount herauszeigen. Beides wird hier
abgefangen.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from backend import sessions
from backend.config import AUDIO_EXTENSIONS, settings

log = logging.getLogger(__name__)


class DiscError(Exception):
    """Keine CD da, oder ein unzulässiger Pfad darauf."""


@dataclass
class AlbumFolder:
    """Ein Ordner auf der CD, der nach einem Album aussieht."""

    #: Pfad relativ zur Disc-Wurzel. Leer bedeutet: die Wurzel selbst.
    relative: str
    #: Was in der Oberfläche steht.
    display: str
    track_count: int
    total_bytes: int

    @property
    def size_label(self) -> str:
        """Größe in einer Einheit, die auch bei kleinen Ordnern etwas aussagt."""
        if self.total_bytes >= 1024**3:
            return f"{self.total_bytes / 1024**3:.1f} GB"
        if self.total_bytes >= 1024**2:
            return f"{self.total_bytes / 1024**2:.0f} MB"
        return f"{max(1, self.total_bytes // 1024)} KB"


def is_available() -> bool:
    """Liegt eine CD ein?

    Es gibt keinen Schalter für dieses Feature -- ein vorhandener, nicht leerer
    Mount *ist* der Schalter.
    """
    root = settings.disc_root
    try:
        return root.is_dir() and any(root.iterdir())
    except OSError:
        # Laufwerk leer, ausgeworfen oder Medium unlesbar.
        return False


def _audio_files(directory: Path) -> list[Path]:
    """Audiodateien direkt in diesem Ordner, ohne Unterordner.

    Symlinks werden übergangen: eine CD kann über Rock Ridge welche enthalten,
    und ein Link auf ``/etc/passwd`` hätte im Staging nichts verloren.
    """
    found = []
    try:
        entries = sorted(directory.iterdir())
    except OSError as exc:
        log.warning("Ordner %s nicht lesbar: %s", directory, exc)
        return []
    for path in entries:
        try:
            if not path.is_file() or path.is_symlink():
                continue
            if path.suffix.lower() in AUDIO_EXTENSIONS:
                found.append(path)
        except OSError:
            continue
    return found


def list_albums() -> list[AlbumFolder]:
    """Alle Ordner der CD, die Audiodateien enthalten.

    Ein Eintrag je Ordner, nicht je Datei -- die Auswahl ist ein Album. Ordner
    ohne Audiodateien tauchen nicht auf, Unterordner zählen jeweils für sich.
    """
    root = settings.disc_root
    if not is_available():
        return []

    gefunden: list[AlbumFolder] = []
    # Die Wurzel selbst zuerst, dann alle Unterordner alphabetisch.
    kandidaten = [root]
    try:
        kandidaten += sorted(p for p in root.rglob("*") if p.is_dir() and not p.is_symlink())
    except OSError as exc:
        log.warning("CD nicht vollständig lesbar: %s", exc)

    for directory in kandidaten:
        dateien = _audio_files(directory)
        if not dateien:
            continue
        try:
            relative = directory.relative_to(root)
        except ValueError:
            continue
        relativ_str = "" if directory == root else str(relative)
        gesamt = 0
        for datei in dateien:
            try:
                gesamt += datei.stat().st_size
            except OSError:
                continue
        gefunden.append(
            AlbumFolder(
                relative=relativ_str,
                display=relativ_str or "(Hauptverzeichnis der CD)",
                track_count=len(dateien),
                total_bytes=gesamt,
            )
        )
    return gefunden


def resolve_folder(relative: str) -> Path:
    """Löst eine Ordnerangabe der Oberfläche auf, ohne die CD zu verlassen.

    Die Angabe kommt aus einem Formular und ist damit beliebig manipulierbar;
    geprüft wird deshalb der tatsächlich aufgelöste Pfad, nicht die Zeichenkette.
    """
    root = settings.disc_root.resolve()
    if not is_available():
        raise DiscError("Es liegt keine CD ein.")

    candidate = (root / (relative or "")).resolve()
    if not candidate.is_relative_to(root):
        raise DiscError("Dieser Ordner gehört nicht zur eingelegten CD.")
    if not candidate.is_dir():
        raise DiscError("Diesen Ordner gibt es auf der CD nicht (mehr).")
    return candidate


def folder_size(directory: Path) -> tuple[int, int]:
    """Anzahl und Gesamtgröße der Audiodateien eines Ordners.

    Anders als beim Upload steht beides *vor* dem Kopieren fest -- die Grenzen
    lassen sich deshalb einmal vorab prüfen statt häppchenweise.
    """
    dateien = _audio_files(directory)
    gesamt = 0
    for datei in dateien:
        try:
            gesamt += datei.stat().st_size
        except OSError:
            continue
    return len(dateien), gesamt


def copy_to_session(directory: Path) -> sessions.StagingSession:
    """Kopiert die Audiodateien eines CD-Ordners in eine neue Session.

    Nur reguläre Dateien dieses einen Ordners, keine Unterordner: die Auswahl
    ist genau ein Album.

    Zerkratzte CDs sind der Normalfall, nicht der Sonderfall. Scheitert das
    Lesen mittendrin, wird die halbfertige Session verworfen -- ein
    unvollständiges Album gegen MusicBrainz zu matchen wäre schlimmer als ein
    klarer Fehler.
    """
    dateien = _audio_files(directory)
    if not dateien:
        raise DiscError("In diesem Ordner liegen keine Audiodateien.")

    # Den Ordnernamen der CD mitnehmen: er ist später die einzige Auskunft
    # darüber, was in dieser Sitzung liegt -- etwa in der Liste offener
    # Sitzungen. Im Hauptverzeichnis der CD gibt es keinen, dann bleibt es flach.
    wurzel = settings.disc_root.resolve()
    unterordner = "" if directory == wurzel else f"{directory.name}/"

    session = sessions.create_session()
    try:
        for quelle in dateien:
            ziel = sessions.target_path(
                session,
                sessions.sanitize_relative_path(f"{unterordner}{quelle.name}"),
            )
            ziel.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(quelle, ziel)
    except (OSError, sessions.SessionError) as exc:
        sessions.delete_session(session.session_id)
        name = getattr(exc, "filename", None) or directory.name
        raise DiscError(
            f"Die CD ließ sich nicht vollständig lesen "
            f"(bei „{Path(str(name)).name}“). "
            "Meist hilft es, sie zu säubern und es erneut zu versuchen."
        ) from exc

    log.info(
        "%d Datei(en) von der CD nach %s kopiert.", len(dateien), session.session_id
    )
    return session
