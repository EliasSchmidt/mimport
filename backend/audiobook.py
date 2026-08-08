"""Hörbücher: Discs sammeln und zu einer m4b mit Kapiteln bündeln.

Warum das nicht über beets läuft: MusicBrainz kennt Hörbücher praktisch nicht,
und der ganze Match-Dialog von mimport hätte nichts zu zeigen. Die Metadaten
holt sich Audiobookshelf später selbst über Audible. Hier endet der Weg also
beim fertigen Buchordner -- kein Match, kein beets-Import.

Aufbau der Bibliothek, wie Audiobookshelf ihn erwartet::

    /audiobooks/<Autor>/<Titel>/CD 1/01 Track 1.flac
                               /CD 2/...
                               /cover.jpg
                               /<Titel>.m4b        (nach dem Bündeln)

Der Buchordner *ist* der Zustand: welche Discs schon eingelesen sind, steht
nicht in einer Datenbank, sondern im Dateisystem. Ein Neustart mitten in einem
mehrteiligen Hörbuch verliert deshalb nichts.

Zum Bündeln: eine Lesung braucht keine Musikqualität, 64 kbit/s in Mono sind
reichlich. Aus zwölf CDs FLAC (gut 4 GB) wird so eine Datei von etwa 300 MB.
Verlustbehaftete Quellen werden **nicht** umgewandelt -- das wäre lossy auf
lossy und bringt nichts außer Verlust.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend import sessions
from backend.config import AUDIO_EXTENSIONS, LOSSY_EXTENSIONS, settings

log = logging.getLogger(__name__)

#: Wie die Disc-Ordner heißen. Zweistellig, damit die einfache Sortierung
#: schon stimmt und "CD 10" nicht vor "CD 2" landet.
_DISC_RE = re.compile(r"^CD (\d+)$")

#: Beim Bündeln erlaubte Abweichung zwischen der Summe der Quellen und der
#: fertigen m4b. Bewusst ein fester Wert und kein Prozentsatz: das Padding des
#: Encoders liegt unabhängig von der Gesamtlänge im Millisekundenbereich, ein
#: fehlender Track dagegen sind immer Minuten.
TOLERANZ_MS = 1500

COVER_NAMES = ("cover.jpg", "cover.jpeg", "folder.jpg", "cover.png", "folder.png")


class AudiobookError(Exception):
    """Ungültiger Buchpfad oder fehlgeschlagener Bau."""


def library_root() -> Path:
    root = settings.audiobook_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def book_dir(autor: str, titel: str) -> Path:
    """Der Ordner eines Buchs -- aus Formulareingaben, also feindlich.

    Autor und Titel tippt jemand in ein Feld; ohne Prüfung stünde hier ein
    Pfadwechsel offen. Beide Teile werden deshalb entschärft und der fertige
    Pfad muss innerhalb der Bibliothek liegen.
    """
    autor_teil = sessions.sanitize_component(autor or "")
    titel_teil = sessions.sanitize_component(titel or "")
    if not autor.strip() or not titel.strip():
        raise AudiobookError("Autor und Titel werden beide gebraucht.")

    root = library_root().resolve()
    ziel = (root / autor_teil / titel_teil).resolve()
    if not ziel.is_relative_to(root) or ziel == root:
        raise AudiobookError("Dieser Autor-Titel-Pfad ist nicht zulässig.")
    return ziel


def audio_files(directory: Path) -> list[Path]:
    """Alle Audiodateien eines Buchs, in natürlicher Reihenfolge.

    ``sorted`` allein sortiert stumpf nach Zeichen und stellte "CD 10" vor
    "CD 2" sowie "10.flac" vor "2.flac". Zahlen werden deshalb als Zahlen
    verglichen -- sonst stünden die Kapitel später in falscher Folge.
    """
    if not directory.is_dir():
        return []
    gefunden = [
        p
        for p in directory.rglob("*")
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return sorted(gefunden, key=_natural_key)


def _natural_key(pfad: Path) -> list[object]:
    teile = re.split(r"(\d+)", str(pfad).lower())
    return [int(t) if t.isdigit() else t for t in teile]


def next_disc_dir(buch: Path, *, ist_datencd: bool = False) -> Path:
    """Wohin die nächste Disc kommt.

    Eine MP3-Daten-CD trägt meistens das ganze Buch. Ist sie die erste Disc,
    landet ihr Inhalt deshalb direkt im Buchordner statt in einem "CD 1", das
    für immer allein bliebe. Audio-CDs bekommen dagegen immer ihre Nummer --
    dort ist die Fortsetzung der Normalfall.
    """
    belegt = {
        int(treffer.group(1))
        for kind in (buch.iterdir() if buch.is_dir() else [])
        if kind.is_dir() and (treffer := _DISC_RE.match(kind.name))
    }
    if ist_datencd and not belegt and not audio_files(buch):
        return buch

    nummer = 1
    while nummer in belegt:
        nummer += 1
    return buch / f"CD {nummer}"


@dataclass
class BookState:
    """Was von einem Buch schon auf der Platte liegt."""

    autor: str
    titel: str
    path: Path
    discs: list[str] = field(default_factory=list)
    file_count: int = 0
    total_bytes: int = 0
    has_m4b: bool = False
    has_cover: bool = False
    lossy: bool = False

    #: Größe der fertigen m4b. Getrennt von ``total_bytes``, das absichtlich
    #: nur die Quelldateien zählt -- sonst ginge in der Platzrechnung das
    #: Ergebnis als Quelle durch.
    m4b_bytes: int = 0

    @property
    def size_label(self) -> str:
        """Was das Buch auf der Platte belegt.

        Die m4b muss mitzählen: nach dem Bündeln sind die Quellen gelöscht, und
        ein fertiges Buch stand deshalb mit „0 MB" in der Liste.
        """
        gesamt = self.total_bytes + self.m4b_bytes
        if gesamt >= 1024**3:
            return f"{gesamt / 1024**3:.1f} GB"
        if gesamt >= 1024**2:
            return f"{gesamt / 1024**2:.0f} MB"
        return f"{max(1, gesamt // 1024)} KB"

    @property
    def unstimmig(self) -> bool:
        """m4b *und* Quelldateien liegen nebeneinander.

        Der Zustand nach einem abgebrochenen Bündeln: ffmpeg hatte schon
        geschrieben, dann fiel die Laufzeitprüfung durch und die Quellen
        blieben absichtlich stehen. Beides zusammen im Buchordner ist genau
        das, was Audiobookshelf als zwei Bücher anzeigt -- das darf die
        Oberfläche nicht als „fertig" ausgeben.
        """
        return self.has_m4b and self.file_count > 0

    @property
    def relative(self) -> str:
        """Pfad relativ zur Bibliothek -- so kommt er ins Formular zurück."""
        try:
            return str(self.path.relative_to(settings.audiobook_root.resolve()))
        except ValueError:
            return ""


def state(buch: Path) -> BookState:
    """Liest den Stand eines Buchs aus dem Dateisystem."""
    dateien = audio_files(buch)
    m4b = buch / f"{buch.name}.m4b"
    quellen = [p for p in dateien if p != m4b]
    gesamt = 0
    for p in quellen:
        try:
            gesamt += p.stat().st_size
        except OSError:
            continue
    discs = sorted(
        (k.name for k in (buch.iterdir() if buch.is_dir() else []) if _DISC_RE.match(k.name)),
        key=_natural_key,
    )
    try:
        m4b_groesse = m4b.stat().st_size if m4b.is_file() else 0
    except OSError:
        m4b_groesse = 0

    return BookState(
        autor=buch.parent.name,
        titel=buch.name,
        path=buch,
        discs=discs,
        file_count=len(quellen),
        total_bytes=gesamt,
        m4b_bytes=m4b_groesse,
        has_m4b=m4b.is_file(),
        has_cover=any((buch / n).is_file() for n in COVER_NAMES),
        lossy=any(p.suffix.lower() in LOSSY_EXTENSIONS for p in quellen),
    )


def list_books() -> list[BookState]:
    """Alle angefangenen Bücher der Bibliothek."""
    root = settings.audiobook_root
    if not root.is_dir():
        return []
    buecher = []
    for autor in sorted(p for p in root.iterdir() if p.is_dir()):
        for buch in sorted(p for p in autor.iterdir() if p.is_dir()):
            zustand = state(buch)
            if zustand.file_count or zustand.has_m4b:
                buecher.append(zustand)
    return buecher


# --------------------------------------------------------------- m4b bauen


@dataclass
class M4bJob:
    """Ein laufender oder abgeschlossener Bündelvorgang."""

    buch: str
    zustand: str = "vorbereiten"
    sekunden_fertig: float = 0.0
    sekunden_gesamt: float = 0.0
    meldung: str = "Lese die Quelldateien …"
    ergebnis: str | None = None
    geloescht: int = 0
    fehler: str | None = None

    #: Wie lange der Encode gebraucht hat. Der einzige Weg, das Zeitlimit und
    #: die Bitrate zu belegen statt zu schätzen.
    gestartet: float = field(default_factory=time.monotonic)
    beendet: float | None = None

    @property
    def laeuft(self) -> bool:
        return self.zustand not in ("fertig", "fehler")

    @property
    def prozent(self) -> int:
        if self.sekunden_gesamt <= 0:
            return 0
        return min(100, round(100 * self.sekunden_fertig / self.sekunden_gesamt))

    @property
    def dauer(self) -> float:
        ende = self.beendet if self.beendet is not None else time.monotonic()
        return max(0.0, ende - self.gestartet)

    @property
    def dauer_text(self) -> str:
        gesamt = int(self.dauer)
        stunden, rest = divmod(gesamt, 3600)
        minuten, sekunden = divmod(rest, 60)
        if stunden:
            return f"{stunden}:{minuten:02d}:{sekunden:02d}"
        return f"{minuten}:{sekunden:02d}"

    @property
    def faktor_text(self) -> str:
        """Wie viel schneller als Echtzeit encodiert wurde.

        Der Wert, mit dem man das Zeitlimit für längere Bücher abschätzen kann:
        bei Faktor 8 braucht ein 15-Stunden-Hörbuch knapp zwei Stunden.
        """
        if self.dauer <= 0 or self.sekunden_gesamt <= 0:
            return ""
        return f"{self.sekunden_gesamt / self.dauer:.1f}× Echtzeit"


_m4b_job: M4bJob | None = None
_m4b_lock = threading.Lock()


def current_m4b() -> M4bJob | None:
    return _m4b_job


def _probe_duration(pfad: Path) -> float:
    """Spieldauer einer Datei in Sekunden."""
    ergebnis = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(pfad),
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    try:
        return float((ergebnis.stdout or "0").strip())
    except ValueError:
        return 0.0


def _probe_title(pfad: Path) -> str:
    ergebnis = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-show_entries",
            "format_tags=title",
            "-of",
            "csv=p=0",
            str(pfad),
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    return (ergebnis.stdout or "").strip()


def _probe_kbps(pfad: Path) -> int:
    ergebnis = subprocess.run(
        [
            settings.ffprobe_bin,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=bit_rate",
            "-of",
            "csv=p=0",
            str(pfad),
        ],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    roh = (ergebnis.stdout or "").strip()
    try:
        return int(roh) // 1000
    except ValueError:
        return 0


def _concat_line(pfad: Path) -> str:
    """Eine Zeile für den concat-Demuxer.

    Einfache Anführungszeichen im Pfad müssen verdoppelt-escapt werden, sonst
    endet die Zeichenkette mitten im Dateinamen -- getestet mit einer Datei
    namens ``Rock'n'Roll``.
    """
    escaped = str(pfad).replace("'", "'\\''")
    return f"file '{escaped}'\n"


def _has_libfdk() -> bool:
    """libfdk_aac klingt bei niedrigen Bitraten besser, fehlt aber meist.

    Aus Lizenzgründen ist es in den wenigsten Distributions-Builds enthalten;
    der native Encoder ist der Normalfall.
    """
    try:
        ergebnis = subprocess.run(
            [settings.ffmpeg_bin, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return "libfdk_aac" in (ergebnis.stdout or "")


def tools_available() -> dict[str, bool]:
    return {
        "ffmpeg": shutil.which(settings.ffmpeg_bin) is not None,
        "ffprobe": shutil.which(settings.ffprobe_bin) is not None,
    }


def free_bytes() -> int:
    """Freier Platz auf dem Dateisystem der Hörbücher.

    Eigene Abfrage, nicht die des Stagings: Hörbücher werden direkt in ihre
    Bibliothek geschrieben, und die liegt auf einem anderen Volume.
    """
    try:
        return shutil.disk_usage(library_root()).free
    except OSError:
        log.warning("Freier Platz unter %s nicht ermittelbar.", settings.audiobook_root)
        return 0


#: Endung für eine beiseite gelegte m4b. Bewusst nichts, was in
#: ``AUDIO_EXTENSIONS`` steht -- weder mimport noch Audiobookshelf sollen die
#: Datei danach noch als Hörbuch sehen.
ERSETZT_SUFFIX = ".ersetzt"


def m4b_beiseite_legen(buch: Path) -> Path | None:
    """Benennt eine vorhandene m4b um, statt sie zu löschen.

    Für „von vorn einlesen": Solange die alte m4b im Buchordner liegt und
    daneben neue Quelldateien entstehen, zeigt Audiobookshelf das Buch doppelt
    an -- und ein Rip dauert Stunden, das Fenster ist also real. Gelöscht wird
    sie aber auch nicht: scheitert der neue Versuch, ist die alte Fassung das
    Einzige, was noch da ist.
    """
    m4b = buch / f"{buch.name}.m4b"
    if not m4b.is_file():
        return None

    ziel = m4b.with_suffix(m4b.suffix + ERSETZT_SUFFIX)
    zaehler = 2
    while ziel.exists():
        ziel = m4b.with_suffix(f"{m4b.suffix}{ERSETZT_SUFFIX}{zaehler}")
        zaehler += 1
    m4b.rename(ziel)
    log.info("Alte m4b beiseite gelegt: %s", ziel.name)
    return ziel


def resolve_book(relativ: str) -> Path:
    """Löst einen Buchpfad aus einem Formular auf, ohne die Bibliothek zu verlassen."""
    root = library_root().resolve()
    kandidat = (root / (relativ or "")).resolve()
    if not kandidat.is_relative_to(root) or kandidat == root:
        raise AudiobookError("Dieser Buchpfad gehört nicht zur Bibliothek.")
    if not kandidat.is_dir():
        raise AudiobookError("Diesen Buchordner gibt es nicht.")
    return kandidat


def chapter_titles(quellen: list[Path]) -> list[str]:
    """Vorgeschlagene Kapitelnamen, einer je Quelldatei.

    Erste Wahl ist das Titel-Tag -- MP3-Hörbuch-CDs bringen meist brauchbare
    mit. Eine gerippte Audio-CD hat dagegen gar keine Tags, und der Dateiname
    taugt nicht: der Rip zählt je Disc wieder von vorn, "01 Track 1" stünde bei
    zwölf CDs zwölfmal in der Kapitelliste. Deshalb wird in diesem Fall
    durchgezählt.
    """
    getaggt = [_probe_title(p) for p in quellen]
    if all(getaggt) and len(set(getaggt)) == len(getaggt):
        return getaggt

    # Sobald Namen fehlen oder sich wiederholen, muss die laufende Nummer
    # dazu -- zwei Kapitel namens "Intro" sind im Player nicht auseinander-
    # zuhalten. Ein vorhandener Name bleibt dabei erhalten, er trägt ja
    # Information; nur eindeutig wird er erst durch die Nummer.
    return [
        f"{nummer}. {vorhanden}" if vorhanden else f"Kapitel {nummer}"
        for nummer, vorhanden in enumerate(getaggt, start=1)
    ]


def _kapitel_schreiben(
    buch: Path, quellen: list[Path], ziel: Path, titel: list[str] | None = None
) -> float:
    """Schreibt die FFMETADATA-Datei und liefert die Gesamtdauer in Sekunden.

    Ein Kapitel je Track -- die Grenzen sind die Trackgrenzen.
    """
    namen = titel or chapter_titles(quellen)
    zeilen = [
        ";FFMETADATA1",
        f"title={buch.name}",
        f"artist={buch.parent.name}",
        "genre=Audiobook",
    ]
    start_ms = 0
    for nummer, pfad in enumerate(quellen):
        dauer_ms = int(_probe_duration(pfad) * 1000)
        ende_ms = start_ms + dauer_ms
        name = namen[nummer] if nummer < len(namen) else f"Kapitel {nummer + 1}"
        zeilen += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={start_ms}",
            f"END={ende_ms}",
            # Zeilenumbrüche würden die FFMETADATA-Datei zerlegen.
            f"title={name.replace(chr(10), ' ').replace(chr(13), ' ')}",
        ]
        start_ms = ende_ms
    ziel.write_text("\n".join(zeilen) + "\n", encoding="utf-8")
    return start_ms / 1000


def _cover_von(buch: Path) -> Path | None:
    for name in COVER_NAMES:
        kandidat = buch / name
        if kandidat.is_file():
            return kandidat
    return None


def _bauen(
    job: M4bJob,
    buch: Path,
    quellen: list[Path],
    arbeit: Path,
    titel: list[str] | None = None,
) -> None:
    """Der Encode. Läuft im Hintergrund-Thread."""
    ziel = buch / f"{buch.name}.m4b"
    try:
        job.zustand = "vorbereiten"
        job.meldung = "Lese Spieldauern und baue die Kapitel …"
        meta = arbeit / "meta.txt"
        liste = arbeit / "list.txt"
        gesamt = _kapitel_schreiben(buch, quellen, meta, titel)
        liste.write_text("".join(_concat_line(p) for p in quellen), encoding="utf-8")
        job.sekunden_gesamt = gesamt

        befehl = [
            settings.ffmpeg_bin,
            "-hide_banner",
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(liste),
            "-i",
            str(meta),
        ]
        cover = _cover_von(buch)
        if cover:
            befehl += ["-i", str(cover)]

        befehl += ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
        if cover:
            befehl += ["-map", "2:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
        else:
            befehl += ["-vn"]

        if _has_libfdk():
            befehl += ["-c:a", "libfdk_aac", "-b:a", settings.audiobook_bitrate,
                       "-profile:a", "aac_he"]
        else:
            befehl += ["-c:a", "aac", "-b:a", settings.audiobook_bitrate]
        if settings.audiobook_mono:
            befehl += ["-ac", "1"]

        befehl += ["-movflags", "+faststart", "-progress", "pipe:1", str(ziel)]

        job.zustand = "encodiert"
        job.meldung = "Wandle um …"
        log.info("Baue m4b: %s", ziel)
        _encodieren(job, befehl)

        if not ziel.is_file():
            raise AudiobookError("ffmpeg lief durch, hat aber nichts geschrieben.")

        job.zustand = "pruefen"
        job.meldung = "Vergleiche die Laufzeiten …"
        _quellen_aufraeumen(job, quellen, ziel, gesamt)

        job.beendet = time.monotonic()
        job.zustand = "fertig"
        job.ergebnis = str(ziel)
        job.meldung += f" Gebaut in {job.dauer_text}"
        if job.faktor_text:
            job.meldung += f" ({job.faktor_text})"
        job.meldung += "."

    except AudiobookError as exc:
        job.beendet = time.monotonic()
        job.zustand = "fehler"
        job.fehler = str(exc)
        job.meldung = f"Der m4b-Bau ist nach {job.dauer_text} fehlgeschlagen."
        log.warning("m4b-Bau abgebrochen: %s", exc)
    except Exception as exc:  # noqa: BLE001 -- der Thread darf nie still sterben
        job.beendet = time.monotonic()
        job.zustand = "fehler"
        job.fehler = f"Unerwarteter Fehler: {exc}"
        job.meldung = f"Der m4b-Bau ist nach {job.dauer_text} fehlgeschlagen."
        log.exception("m4b-Bau mit unerwartetem Fehler abgebrochen")
    finally:
        shutil.rmtree(arbeit, ignore_errors=True)


def _encodieren(job: M4bJob, befehl: list[str]) -> None:
    """Führt ffmpeg aus und verfolgt den Fortschritt.

    ``-progress`` liefert Schlüssel-Wert-Zeilen. Achtung bei ``out_time_ms``:
    der Schlüssel heißt zwar "ms", der Wert steht aber in **Mikrosekunden**
    (nachgemessen: 9000000 bei neun Sekunden Audio). Deshalb ``out_time_us``,
    das ist wenigstens ehrlich benannt.
    """
    try:
        prozess = subprocess.Popen(
            befehl,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise AudiobookError(f"{settings.ffmpeg_bin} wurde nicht gefunden.") from exc

    assert prozess.stdout is not None
    for zeile in prozess.stdout:
        schluessel, _, wert = zeile.strip().partition("=")
        if schluessel == "out_time_us" and wert.isdigit():
            job.sekunden_fertig = int(wert) / 1_000_000
            job.meldung = (
                f"Wandle um … {_hms(job.sekunden_fertig)} von "
                f"{_hms(job.sekunden_gesamt)}"
            )

    prozess.wait(timeout=settings.m4b_timeout)
    if prozess.returncode != 0:
        rest = (prozess.stderr.read() if prozess.stderr else "") or ""
        raise AudiobookError(f"ffmpeg endete mit {prozess.returncode}. {rest.strip()[-300:]}")


def _hms(sekunden: float) -> str:
    gesamt = int(sekunden)
    return f"{gesamt // 3600}:{gesamt // 60 % 60:02d}:{gesamt % 60:02d}"


def _quellen_aufraeumen(
    job: M4bJob, quellen: list[Path], ziel: Path, quell_sekunden: float
) -> None:
    """Löscht die Quelldateien -- aber nur, wenn die m4b vollständig ist.

    Audiobookshelf liest *alle* Audiodateien eines Buchordners als Tracks
    desselben Buchs. Bleiben die FLACs liegen, steht das Buch doppelt in der
    Bibliothek. Die CD ist das Archiv, ein misslungener Encode wäre trotzdem
    Datenverlust -- deshalb wird vorher die Laufzeit verglichen.
    """
    m4b_sekunden = _probe_duration(ziel)
    abweichung_ms = abs(int(m4b_sekunden * 1000) - int(quell_sekunden * 1000))
    job.meldung = (
        f"Quellen {_hms(quell_sekunden)}, m4b {_hms(m4b_sekunden)} "
        f"(Abweichung {abweichung_ms} ms)"
    )

    if abweichung_ms > TOLERANZ_MS:
        raise AudiobookError(
            f"Die Laufzeiten passen nicht zusammen: Quellen {_hms(quell_sekunden)}, "
            f"m4b {_hms(m4b_sekunden)}, Abweichung {abweichung_ms} ms. Die m4b ist "
            "womöglich unvollständig. Es wurde nichts gelöscht -- bitte hineinhören "
            "und dann selbst entscheiden."
        )

    # Nur die eingesammelte Liste, niemals "alles außer der m4b": ein Fehler
    # in der Suche würde sonst Fremdes mitnehmen.
    for pfad in quellen:
        try:
            pfad.unlink()
            job.geloescht += 1
        except OSError as exc:
            log.warning("Quelldatei %s nicht löschbar: %s", pfad, exc)

    # Leere Disc-Ordner hinterher wegräumen.
    for kind in sorted(ziel.parent.iterdir(), reverse=True):
        if kind.is_dir() and _DISC_RE.match(kind.name) and not any(kind.iterdir()):
            kind.rmdir()

    job.meldung += f" -- {job.geloescht} Quelldateien gelöscht."


def build(
    buch: Path,
    *,
    force: bool = False,
    ersetzen: bool = False,
    titel: list[str] | None = None,
) -> M4bJob:
    """Startet das Bündeln eines Buchs zur m4b.

    ``ersetzen`` ist nötig, sobald schon eine m4b vorliegt -- siehe unten,
    warum das keine Formsache ist.
    """
    global _m4b_job

    with _m4b_lock:
        if _m4b_job is not None and _m4b_job.laeuft:
            raise AudiobookError("Es läuft bereits ein m4b-Bau.")
        job = M4bJob(buch=str(buch))
        _m4b_job = job

    ziel = buch / f"{buch.name}.m4b"
    quellen = [p for p in audio_files(buch) if p != ziel]
    if not quellen:
        job.zustand = "fehler"
        job.fehler = "In diesem Buch liegen keine Quelldateien."
        raise AudiobookError(job.fehler)

    # Eine vorhandene m4b niemals stillschweigend ersetzen. Der gefährliche
    # Fall: Disc 1 wurde gebündelt, ihre Quellen sind dabei gelöscht worden,
    # danach kommt Disc 2 dazu. Ein neuer Bau kennt nur noch Disc 2 und würde
    # die m4b mit Disc 1 überschreiben -- deren Inhalt liegt dann nirgends mehr
    # vor. Die Laufzeiten machen den Unterschied sichtbar.
    if ziel.is_file() and not ersetzen:
        job.zustand = "fehler"
        vorhanden = _probe_duration(ziel)
        neu = sum(_probe_duration(p) for p in quellen)
        job.fehler = (
            f"Es gibt bereits eine m4b für dieses Buch ({_hms(vorhanden)}), "
            f"die vorliegenden Quelldateien ergeben aber nur {_hms(neu)}. "
            "Wurde schon einmal gebündelt, sind die Quellen der früheren Discs "
            "gelöscht -- ein neuer Bau enthielte dann nur noch die jetzigen. "
            "Richtig ist: erst alle Discs einlesen, dann einmal bündeln."
        )
        raise AudiobookError(job.fehler)

    # Verlustbehaftete Quellen nicht noch einmal umwandeln. Was fehlt, kommt
    # nicht zurück, und Audiobookshelf spielt einen Ordner mit MP3s ohnehin
    # ohne Murren.
    schlechteste = min(
        (_probe_kbps(p) for p in quellen if p.suffix.lower() in LOSSY_EXTENSIONS),
        default=0,
    )
    if schlechteste and schlechteste <= settings.audiobook_min_kbps and not force:
        job.zustand = "fehler"
        job.fehler = (
            f"Die Quelle ist bereits verlustbehaftet ({schlechteste} kbit/s). "
            "Noch einmal umzuwandeln hieße lossy auf lossy und bringt nichts. "
            "Audiobookshelf spielt den Ordner so, wie er ist."
        )
        raise AudiobookError(job.fehler)

    arbeit = Path(str(buch)) / ".mimport-m4b"
    arbeit.mkdir(parents=True, exist_ok=True)

    thread = threading.Thread(
        target=_bauen,
        args=(job, buch, quellen, arbeit, titel),
        name="mimport-m4b",
        daemon=True,
    )
    thread.start()
    return job


def reset_m4b() -> None:
    """Vergisst einen abgeschlossenen Bauauftrag."""
    global _m4b_job

    with _m4b_lock:
        if _m4b_job is not None and _m4b_job.laeuft:
            raise AudiobookError("Der laufende m4b-Bau lässt sich nicht verwerfen.")
        _m4b_job = None
