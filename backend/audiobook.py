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
import secrets
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


#: Hier entsteht alles Unfertige. Der Punkt am Anfang hält Audiobookshelf
#: davon ab, den Ordner als Buch zu lesen.
STAGING_NAME = ".mimport-unfertig"


def staging_dir() -> Path:
    """Wo Discs und m4b entstehen, bevor sie im Buchordner auftauchen.

    Bewusst *innerhalb* der Hörbuch-Bibliothek und nicht im ``/staging``-Volume
    der Uploads: das ist ein Named Volume, die Bibliothek ein Bind-Mount vom
    Host. Ein Verschieben dorthin wäre ein Kopiervorgang über
    Dateisystemgrenzen -- bei zwölf CDs mehrere Gigabyte, die zweimal
    geschrieben würden. Von hier aus ist es ein ``rename`` auf demselben
    Dateisystem: sofort und ohne zusätzlichen Platz.

    Auch nicht über ``backend.sessions``: dessen Staging hängt an
    ``staging_root``, an Session-IDs und an der TTL-Aufräumung für Uploads --
    drei Lebenszyklen, die mit Hörbüchern nichts zu tun haben.
    """
    ordner = library_root() / STAGING_NAME
    ordner.mkdir(parents=True, exist_ok=True)
    return ordner


def neuer_arbeitsordner(zweck: str) -> Path:
    """Ein leerer Ordner für einen Vorgang, der noch nicht fertig ist."""
    ordner = staging_dir() / f"{zweck}-{secrets.token_urlsafe(8)}"
    ordner.mkdir(parents=True, exist_ok=False)
    return ordner


def staging_aufraeumen() -> int:
    """Entfernt Reste abgebrochener Vorgänge.

    Ein Absturz mitten im Rip lässt mehrere Gigabyte liegen, die sonst niemand
    je wieder anfasst. Läuft beim Start -- da kann nichts in Arbeit sein.
    """
    ordner = settings.audiobook_root / STAGING_NAME
    if not ordner.is_dir():
        return 0
    entfernt = 0
    for kind in ordner.iterdir():
        shutil.rmtree(kind, ignore_errors=True) if kind.is_dir() else kind.unlink(
            missing_ok=True
        )
        entfernt += 1
    if entfernt:
        log.info("%d unfertige(r) Hörbuch-Vorgang aufgeräumt.", entfernt)
    return entfernt


def fertigstellen(arbeit: Path, ziel: Path) -> Path:
    """Schiebt ein fertiges Ergebnis an seinen Platz.

    Ein ``rename`` auf ein vorhandenes, nicht leeres Verzeichnis scheitert --
    und das nach Stunden Arbeit. Deshalb wird der Zielname erst hier bestimmt,
    unmittelbar davor, und ein Fehlschlag bleibt ein Fehlschlag: still
    danebenlegen wäre schlimmer als eine klare Meldung.
    """
    ziel.parent.mkdir(parents=True, exist_ok=True)
    try:
        arbeit.rename(ziel)
    except OSError as exc:
        raise AudiobookError(
            f"„{ziel.name}“ ließ sich nicht an seinen Platz schieben: {exc}. "
            "Das Ergebnis liegt noch unter "
            f"{STAGING_NAME}/{arbeit.name} und ist nicht verloren."
        ) from exc
    return ziel


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


def discs_normalisieren(buch: Path) -> Path | None:
    """Räumt flach liegende Dateien nach ``CD 1``, bevor eine Disc dazukommt.

    Die erste Daten-CD landet absichtlich flach im Buchordner -- eine MP3-CD
    trägt meist das ganze Buch, ein einsames „CD 1" wäre albern. Kommt aber
    doch eine zweite Disc, ist die Struktur uneinheitlich, und das verdreht die
    Kapitelreihenfolge: die natürliche Sortierung stellt ``CD 1/…`` vor
    ``Disc 1/…``, die zweite Disc käme also vor der ersten.

    Deshalb wandert das Flache vorher nach ``CD 1``. Auf demselben Dateisystem
    kostet das nichts.
    """
    if not buch.is_dir():
        return None
    flach = [
        p
        for p in buch.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS
        and p.name != f"{buch.name}.m4b"
    ]
    ordner_flach = [
        p
        for p in buch.iterdir()
        if p.is_dir() and not _DISC_RE.match(p.name) and audio_files(p)
    ]
    if not flach and not ordner_flach:
        return None

    ziel = buch / "CD 1"
    if ziel.exists():
        return None

    ziel.mkdir()
    for pfad in flach + ordner_flach:
        pfad.rename(ziel / pfad.name)
    log.info("Bisherigen Inhalt von %s nach „CD 1“ geräumt.", buch.name)
    return ziel


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

    #: Änderungszeit des Coverbilds, als Cache-Schlüssel in der Bildadresse.
    #: Damit darf der Browser das Bild dauerhaft behalten -- und sieht ein neu
    #: fotografiertes trotzdem sofort, weil sich die Adresse mitändert.
    cover_mtime: int = 0

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
        """Pfad relativ zur Bibliothek -- so kommt er ins Formular zurück.

        Beide Seiten werden aufgelöst. Vorher wurde nur die Wurzel aufgelöst,
        der Buchpfad aber nicht: Führt irgendein Stück des Wegs über einen
        Symlink, passten die beiden nicht mehr zusammen, und heraus kam der
        leere String -- „Nächste CD" und der Cover-Knopf zeigten dann ins
        Nichts. Aufgefallen an einem Bibliotheksordner unter ``/var/folders``,
        das auf macOS ein Symlink auf ``/private/var/folders`` ist; ein
        eingehängtes ``/srv/audiobooks`` kann genauso beschaffen sein.
        """
        try:
            return str(self.path.resolve().relative_to(settings.audiobook_root.resolve()))
        except (ValueError, OSError):
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
        has_cover=cover_pfad(buch) is not None,
        cover_mtime=_cover_mtime(buch),
        lossy=any(p.suffix.lower() in LOSSY_EXTENSIONS for p in quellen),
    )


def list_books() -> list[BookState]:
    """Alle angefangenen Bücher der Bibliothek."""
    root = settings.audiobook_root
    if not root.is_dir():
        return []
    buecher = []
    for autor in sorted(
        p for p in root.iterdir() if p.is_dir() and p.name != STAGING_NAME
    ):
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

    #: Der laufende ffmpeg, damit ein hängender Bau überhaupt zu beenden ist.
    #: Ohne diesen Griff blieb nur, den Container neu zu starten.
    prozess: Any = None

    #: Wann zuletzt etwas passiert ist. Grundlage der Stillstandsüberwachung.
    letzte_regung: float = field(default_factory=time.monotonic)

    #: Gesetzt, wenn nicht ffmpeg selbst aufgehört hat, sondern jemand ihn
    #: beendet hat -- der Nutzer oder die Überwachung. Sonst wäre die Meldung
    #: „ffmpeg endete mit -9" alles, was davon zu sehen wäre.
    abbruchgrund: str | None = None

    @property
    def laeuft(self) -> bool:
        return self.zustand not in ("fertig", "fehler")

    def regung(self) -> None:
        self.letzte_regung = time.monotonic()

    @property
    def stiller_moment(self) -> float:
        return max(0.0, time.monotonic() - self.letzte_regung)

    @property
    def prozent(self) -> int:
        if self.sekunden_gesamt <= 0:
            return 0
        return min(100, round(100 * self.sekunden_fertig / self.sekunden_gesamt))

    @property
    def buch_anzeige(self) -> str:
        """Autor und Titel -- nötig, seit zwei Aufträge nebeneinander laufen."""
        pfad = Path(self.buch)
        return f"{pfad.parent.name} – {pfad.name}"

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
    def faktor(self) -> float:
        """Wie viel schneller als Echtzeit encodiert wird.

        Maßgeblich ist, was **bisher** fertig ist, nicht die Gesamtlänge des
        Buchs. Mit der Gesamtlänge im Zähler und der wachsenden Laufzeit im
        Nenner fällt der Wert wie 1/t -- auch bei völlig gleichmäßiger
        Geschwindigkeit sähe das nach stetiger Verlangsamung aus, und genau so
        wurde es gemeldet. Bei 2:23:59 von 7:21:00 nach 3:07 Laufzeit standen
        dort 141× statt der tatsächlichen 46×; erst am Ziel fielen beide
        Rechnungen zusammen.
        """
        if self.dauer <= 0 or self.sekunden_fertig <= 0:
            return 0.0
        return self.sekunden_fertig / self.dauer

    @property
    def faktor_text(self) -> str:
        """Der Wert, mit dem sich das Zeitlimit für längere Bücher abschätzen
        lässt: bei Faktor 46 braucht ein 15-Stunden-Hörbuch knapp 20 Minuten.
        """
        return f"{self.faktor:.1f}× Echtzeit" if self.faktor > 0 else ""

    @property
    def rest_text(self) -> str:
        """Was aus dem Faktor folgt: die geschätzte Restzeit.

        Die eigentlich interessante Zahl -- „46× Echtzeit" muss man erst in
        Kopfrechnen übersetzen, „noch etwa 6:19" nicht.
        """
        if self.faktor <= 0 or self.sekunden_gesamt <= 0:
            return ""
        offen = self.sekunden_gesamt - self.sekunden_fertig
        if offen <= 0:
            return ""
        sekunden = int(offen / self.faktor)
        stunden, rest = divmod(sekunden, 3600)
        minuten, sek = divmod(rest, 60)
        if stunden:
            return f"{stunden}:{minuten:02d}:{sek:02d}"
        return f"{minuten}:{sek:02d}"


_m4b_job: M4bJob | None = None
_m4b_lock = threading.Lock()


def current_m4b() -> M4bJob | None:
    return _m4b_job


#: Zeitlimit für die kleinen ffprobe-Abfragen. Sie lesen nur den Kopf einer
#: lokalen Datei und sind in Millisekunden fertig; braucht eine länger, stimmt
#: etwas nicht -- eine kaputte Datei, ein Laufwerk, das nicht antwortet. Ohne
#: Limit hinge daran der ganze Bau-Thread, und zwar in der Vorbereitungsphase,
#: in der es noch keinen ffmpeg zum Abbrechen gibt.
PROBE_TIMEOUT = 60


def _ffprobe(args: list[str]) -> str:
    """Eine ffprobe-Abfrage. Gibt bei jedem Fehler den leeren String zurück.

    Die Aufrufer haben alle einen brauchbaren Ersatzwert; ein hängendes oder
    fehlendes ffprobe soll den Bau nicht mitreißen, sondern eine Stufe später
    an der Laufzeitprüfung auffallen.
    """
    try:
        ergebnis = subprocess.run(
            [settings.ffprobe_bin, "-v", "error", *args],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log.warning("ffprobe antwortet nicht: %s", " ".join(args[-1:]))
        return ""
    except FileNotFoundError:
        return ""
    return (ergebnis.stdout or "").strip()


def _probe_duration(pfad: Path) -> float:
    """Spieldauer einer Datei in Sekunden."""
    roh = _ffprobe(["-show_entries", "format=duration", "-of", "csv=p=0", str(pfad)])
    try:
        return float(roh or "0")
    except ValueError:
        return 0.0


def _probe_title(pfad: Path) -> str:
    return _ffprobe(
        ["-show_entries", "format_tags=title", "-of", "csv=p=0", str(pfad)]
    )


def _probe_kbps(pfad: Path) -> int:
    roh = _ffprobe(
        ["-select_streams", "a:0", "-show_entries", "stream=bit_rate",
         "-of", "csv=p=0", str(pfad)]
    )
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


def cover_pfad(buch: Path) -> Path | None:
    """Das Coverbild eines Buchs, falls eines danebenliegt.

    Fünf mögliche Namen, weil Bücher nicht nur aus mimport kommen -- eine von
    Hand kopierte Sammlung bringt oft ``folder.jpg`` mit. Wer hier nur
    ``cover.jpg`` sucht, zeigt bei genau diesen Büchern ein kaputtes Bild an,
    obwohl die Liste sie als „hat Cover" führt.
    """
    for name in COVER_NAMES:
        kandidat = buch / name
        if kandidat.is_file():
            return kandidat
    return None


def _cover_mtime(buch: Path) -> int:
    pfad = cover_pfad(buch)
    if pfad is None:
        return 0
    try:
        return int(pfad.stat().st_mtime)
    except OSError:
        return 0


def _bauen(
    job: M4bJob,
    buch: Path,
    quellen: list[Path],
    arbeit: Path,
    titel: list[str] | None = None,
) -> None:
    """Der Encode. Läuft im Hintergrund-Thread.

    Geschrieben wird in ``arbeit`` und erst am Ende in den Buchordner
    geschoben. Während des Encodierens wächst die Datei stundenlang und ist
    unabspielbar -- läge sie im Buchordner, würde Audiobookshelf sie bei einem
    Scan als Hörbuch einlesen.
    """
    endziel = buch / f"{buch.name}.m4b"
    ziel = arbeit / f"{buch.name}.m4b"
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
        cover = cover_pfad(buch)
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

        # Wer während des Vorbereitens abbricht, wird hier erhört -- ffmpeg
        # erst zu starten, um ihn gleich wieder zu beenden, wäre unsinnig.
        if job.abbruchgrund:
            raise AudiobookError(job.abbruchgrund)

        job.zustand = "encodiert"
        job.meldung = "Wandle um …"
        log.info("Baue m4b: %s", ziel)
        _encodieren(job, befehl, arbeit)

        if not ziel.is_file():
            raise AudiobookError("ffmpeg lief durch, hat aber nichts geschrieben.")

        job.zustand = "pruefen"
        job.meldung = "Vergleiche die Laufzeiten …"
        # Erst prüfen, dann verschieben, dann löschen. Andersherum stünde man
        # nach einem gescheiterten Verschieben ohne Quellen und ohne m4b da.
        _laufzeit_pruefen(job, ziel, gesamt)
        ziel = fertigstellen(ziel, endziel)
        _quellen_loeschen(job, quellen, ziel)

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


def _wache(job: M4bJob, prozess: Any) -> None:
    """Beendet ffmpeg, wenn er stehenbleibt oder das Zeitlimit reißt.

    Läuft neben dem Lesen der Fortschrittszeilen, weil das Lesen selbst
    blockiert -- ein Zeitlimit *hinter* der Leseschleife kann nie greifen.
    Genau so stand es hier, und damit war ``m4b_timeout`` toter Code.
    """
    beginn = time.monotonic()
    while prozess.poll() is None:
        if job.stiller_moment > settings.m4b_stillstand:
            job.abbruchgrund = (
                f"ffmpeg hat {int(job.stiller_moment) // 60} Minuten lang keinen "
                "Fortschritt mehr gemeldet und wurde beendet."
            )
        elif time.monotonic() - beginn > settings.m4b_timeout:
            job.abbruchgrund = (
                f"Das Zeitlimit von {settings.m4b_timeout // 3600} Stunden für den "
                "m4b-Bau ist abgelaufen."
            )
        else:
            time.sleep(2)
            continue
        log.warning("Beende ffmpeg: %s", job.abbruchgrund)
        _beenden(prozess)
        return


def _beenden(prozess: Any) -> None:
    """SIGTERM, und wenn das nicht reicht, SIGKILL.

    ffmpeg räumt bei SIGTERM seine Ausgabedatei ordentlich ab. Hängt er aber
    im Kernel fest -- ein CD-Laufwerk, das nicht antwortet --, kommt er dort
    nicht heraus, und dann muss es der härtere Weg sein.
    """
    try:
        prozess.terminate()
        prozess.wait(timeout=10)
    except subprocess.TimeoutExpired:
        prozess.kill()
        try:
            prozess.wait(timeout=10)
        except subprocess.TimeoutExpired:
            log.error("ffmpeg reagiert auch auf SIGKILL nicht.")
    except Exception:  # noqa: BLE001 -- der Prozess war schon fort
        pass


def _encodieren(job: M4bJob, befehl: list[str], arbeit: Path) -> None:
    """Führt ffmpeg aus und verfolgt den Fortschritt.

    ``-progress`` liefert Schlüssel-Wert-Zeilen. Achtung bei ``out_time_ms``:
    der Schlüssel heißt zwar "ms", der Wert steht aber in **Mikrosekunden**
    (nachgemessen: 9000000 bei neun Sekunden Audio). Deshalb ``out_time_us``,
    das ist wenigstens ehrlich benannt.

    stderr geht in eine Datei, nicht in eine Pipe. Eine ungelesene Pipe fasst
    64 KiB, und ffmpeg schreibt dorthin unabhängig von ``-progress`` etwa
    210 Byte je Sekunde Laufzeit -- nachgemessen. Nach gut fünf Minuten wäre
    sie voll, ffmpeg blockierte beim Schreiben, mimport wartete auf stdout, und
    beide stünden für immer. Nachgestellt und bestätigt.
    """
    protokoll = arbeit / "ffmpeg-stderr.log"
    try:
        with protokoll.open("wb") as fehlerstrom:
            prozess = subprocess.Popen(
                befehl,
                stdout=subprocess.PIPE,
                stderr=fehlerstrom,
                text=True,
                stdin=subprocess.DEVNULL,
            )
    except FileNotFoundError as exc:
        raise AudiobookError(f"{settings.ffmpeg_bin} wurde nicht gefunden.") from exc

    job.prozess = prozess
    job.regung()
    wache = threading.Thread(target=_wache, args=(job, prozess), daemon=True)
    wache.start()

    try:
        assert prozess.stdout is not None
        for zeile in prozess.stdout:
            schluessel, _, wert = zeile.strip().partition("=")
            if schluessel == "out_time_us" and wert.isdigit():
                job.regung()
                job.sekunden_fertig = int(wert) / 1_000_000
                job.meldung = (
                    f"Wandle um … {_hms(job.sekunden_fertig)} von "
                    f"{_hms(job.sekunden_gesamt)}"
                )
        prozess.wait()
    finally:
        job.prozess = None
        wache.join(timeout=30)

    if prozess.returncode != 0:
        if job.abbruchgrund:
            raise AudiobookError(
                f"{job.abbruchgrund} Es wurde nichts gelöscht -- die Quelldateien "
                "liegen unverändert im Buchordner."
            )
        rest = _letzte_zeilen(protokoll)
        raise AudiobookError(f"ffmpeg endete mit {prozess.returncode}. {rest}")


def _letzte_zeilen(protokoll: Path, zeichen: int = 300) -> str:
    """Der Schluss des ffmpeg-Protokolls -- dort steht die eigentliche Ursache."""
    try:
        text = protokoll.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    # ffmpeg trennt seine Statuszeilen mit \r; ohne Ersetzung stünde in der
    # Oberfläche eine einzige unleserliche Zeile.
    return text.replace("\r", "\n").strip()[-zeichen:]


def _hms(sekunden: float) -> str:
    gesamt = int(sekunden)
    return f"{gesamt // 3600}:{gesamt // 60 % 60:02d}:{gesamt % 60:02d}"


def _laufzeit_pruefen(job: M4bJob, ziel: Path, quell_sekunden: float) -> None:
    """Ist die m4b so lang wie die Summe ihrer Quellen?

    Die Bremse vor dem Löschen: die CD ist das Archiv, ein misslungener Encode
    wäre trotzdem Datenverlust.
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


def _quellen_loeschen(job: M4bJob, quellen: list[Path], ziel: Path) -> None:
    """Räumt die Quelldateien weg, nachdem die m4b an ihrem Platz liegt.

    Audiobookshelf liest *alle* Audiodateien eines Buchordners als Tracks
    desselben Buchs. Bleiben die FLACs neben der m4b liegen, steht das Buch
    doppelt in der Bibliothek.
    """
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


#: Zeitlimit fürs Einbetten eines Covers. Das ist reines Umkopieren ohne
#: Neuencode -- gemessen 230 MB/s, eine m4b von 212 MB also in etwa einer
#: Sekunde. Auf einer alten Platte und neben einem laufenden Rip dauert es
#: länger, aber nicht Minuten. Fünf sind großzügig und verhindern trotzdem,
#: dass ein hängendes ffmpeg diesmal an der einzigen Kopie des fertigen Buchs
#: sitzt.
COVER_EINBETTEN_TIMEOUT = 300


def m4b_pfad(buch: Path) -> Path:
    """Wo die fertige m4b eines Buchs liegt."""
    return buch / f"{buch.name}.m4b"


def _probe_eckdaten(datei: Path) -> tuple[float, int, int]:
    """Spieldauer, Anzahl Kapitel und Anzahl eingebetteter Bilder."""
    roh = _ffprobe(
        ["-print_format", "json", "-show_format", "-show_streams",
         "-show_chapters", str(datei)]
    )
    try:
        daten = json.loads(roh or "{}")
    except ValueError:
        return 0.0, 0, 0
    try:
        dauer = float(daten.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        dauer = 0.0
    bilder = sum(
        1
        for s in daten.get("streams", [])
        if s.get("codec_type") == "video"
        and s.get("disposition", {}).get("attached_pic")
    )
    return dauer, len(daten.get("chapters", [])), bilder


def cover_einbetten(buch: Path, bild: Path) -> str:
    """Setzt das Cover einer **fertigen** m4b nachträglich.

    Vor dem Bündeln genügt eine Bilddatei im Buchordner -- der Encode nimmt sie
    mit. Danach sind die Quelldateien gelöscht und die m4b ist alles, was es
    noch gibt; das Bild muss also in die Datei selbst.

    Kein Neuencode: ffmpeg schreibt nur den Container neu (``-c copy``).
    Nachgemessen bleiben dabei Spieldauer, Kapitel und Tags unverändert, und
    ein schon vorhandenes Cover wird ersetzt statt gestapelt -- deshalb wird
    hier auf Gleichheit geprüft und nicht auf eine Toleranz.

    Gearbeitet wird im Staging, nicht neben der m4b. Eine zweite Datei im
    Buchordner, und sei es für zehn Sekunden, liest Audiobookshelf bei einem
    Scan als zweites Hörbuch ein -- dieselbe Falle, wegen der die Quelldateien
    nach dem Bündeln verschwinden.
    """
    ziel = m4b_pfad(buch)
    if not ziel.is_file():
        raise AudiobookError("Für dieses Buch gibt es noch keine m4b.")
    if not bild.is_file():
        raise AudiobookError("Das Coverbild ist nicht auffindbar.")

    vorher_dauer, vorher_kapitel, _ = _probe_eckdaten(ziel)
    if vorher_dauer <= 0:
        raise AudiobookError(
            "Die vorhandene m4b ließ sich nicht lesen. Es wurde nichts verändert."
        )

    arbeit = neuer_arbeitsordner("cover")
    neu = arbeit / ziel.name
    try:
        befehl = [
            settings.ffmpeg_bin, "-hide_banner", "-nostdin", "-y",
            "-i", str(ziel),
            "-i", str(bild),
            # Nur die Tonspur des Originals: ein bereits eingebettetes Cover
            # bleibt damit außen vor, statt sich mit dem neuen zu stapeln.
            "-map", "0:a", "-map", "1:v",
            # Beides ausdrücklich, obwohl ffmpeg Metadaten und Kapitel beim
            # Remuxen ohnehin vom ersten Input übernimmt -- nachgeprüft, ohne
            # die Angaben bleiben sie erhalten. Sie stehen hier als Absicherung
            # gegen eine spätere ffmpeg-Version, die das anders handhabt: die
            # Kapitel sind das Wertvollste an einer m4b.
            "-map_metadata", "0", "-map_chapters", "0",
            "-c", "copy", "-disposition:v:0", "attached_pic",
            "-movflags", "+faststart", str(neu),
        ]
        try:
            ergebnis = subprocess.run(
                befehl,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=COVER_EINBETTEN_TIMEOUT,
            )
        except subprocess.TimeoutExpired as exc:
            raise AudiobookError(
                "ffmpeg hat das Cover nicht in der vorgesehenen Zeit einbetten "
                "können und wurde beendet. Die m4b ist unverändert."
            ) from exc
        except FileNotFoundError as exc:
            raise AudiobookError(
                f"{settings.ffmpeg_bin} wurde nicht gefunden."
            ) from exc

        if ergebnis.returncode != 0:
            rest = (ergebnis.stderr or "").strip()[-300:]
            raise AudiobookError(
                f"ffmpeg endete mit {ergebnis.returncode}. Die m4b ist "
                f"unverändert. {rest}"
            )

        dauer, kapitel, bilder = _probe_eckdaten(neu)
        # Umkopieren ohne Neuencode ändert an diesen drei Zahlen nichts --
        # nachgemessen. Weicht doch etwas ab, bleibt das Original stehen: es
        # ist die einzige Kopie des Buchs.
        if abs(int(dauer * 1000) - int(vorher_dauer * 1000)) > TOLERANZ_MS:
            raise AudiobookError(
                f"Die Spieldauer hätte sich geändert ({_hms(vorher_dauer)} → "
                f"{_hms(dauer)}). Die m4b ist unverändert geblieben."
            )
        if kapitel != vorher_kapitel:
            raise AudiobookError(
                f"Die Kapitel hätten sich geändert ({vorher_kapitel} → "
                f"{kapitel}). Die m4b ist unverändert geblieben."
            )
        if bilder != 1:
            raise AudiobookError(
                f"Statt eines Covers wären {bilder} Bilder in der Datei. Die "
                "m4b ist unverändert geblieben."
            )

        fertigstellen(neu, ziel)
    finally:
        shutil.rmtree(arbeit, ignore_errors=True)

    log.info("Cover in %s eingebettet", ziel)
    return (
        f"Cover in die m4b übernommen – {kapitel} Kapitel und "
        f"{_hms(dauer)} Spielzeit unverändert."
    )


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

    arbeit = neuer_arbeitsordner("m4b")

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
            raise AudiobookError(
                "Der m4b-Bau läuft noch. Zum Verwerfen erst abbrechen."
            )
        _m4b_job = None


def abbrechen_m4b() -> str:
    """Beendet einen laufenden m4b-Bau auf Wunsch des Nutzers.

    Der Ausweg, den es vorher nicht gab: hing ffmpeg, blieb der Auftrag für
    immer auf „läuft", das Buch war gesperrt, und nur ein Neustart des
    Containers half. Gelöscht wird dabei nichts -- die Quelldateien werden erst
    nach bestandener Laufzeitprüfung angefasst, und dorthin kommt ein
    abgebrochener Bau nicht.
    """
    with _m4b_lock:
        job = _m4b_job
        if job is None or not job.laeuft:
            raise AudiobookError("Es läuft gerade kein m4b-Bau.")

        # Entschieden wird am Zustand, nicht daran, ob gerade ein Prozess
        # läuft. Beides fällt nur beim Encode zusammen: danach ist das
        # Prozesshandle wieder frei, der Auftrag aber noch nicht fertig -- und
        # eine Antwort „Abbruch vorgemerkt", auf die niemand mehr hört, wäre
        # schlimmer als eine Absage. Die Quellen wären dann trotzdem gelöscht.
        if job.zustand == "vorbereiten":
            job.abbruchgrund = "Der m4b-Bau wurde von Hand abgebrochen."
            # Der Zustand wird hier bewusst nicht gesetzt: das täte gleichzeitig
            # der Bau-Thread, und wessen Wert am Ende stünde, wäre Zufall. Der
            # Thread liest die Bitte, bevor er ffmpeg startet.
            return (
                "Der Abbruch ist vorgemerkt. Die Spieldauern werden gerade "
                "gelesen; danach endet der Auftrag, ohne dass etwas gelöscht "
                "wird."
            )

        prozess = job.prozess
        if job.zustand == "encodiert" and prozess is not None:
            job.abbruchgrund = "Der m4b-Bau wurde von Hand abgebrochen."
        else:
            raise AudiobookError(
                "Der Encode ist durch, der Bau wird gerade abgeschlossen. Ein "
                "Abbruch verhindert jetzt nichts mehr -- bitte das Ergebnis "
                "abwarten."
            )

    _beenden(prozess)
    return job.abbruchgrund
