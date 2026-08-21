"""Ein abfotografiertes Cover entgegennehmen.

Das Zuschneiden und Entzerren passiert im Browser -- das Handy hat das Foto
ohnehin schon und rechnet für sich allein, statt den Server zu belasten. Hier
kommt nur das fertige Bild an.

Wohin es gehört, ist an beiden Wegen schon vorbereitet:

* **Hörbücher** -- ``cover.jpg`` im Buchordner, ``backend.audiobook`` bettet es
  beim Bündeln in die m4b ein.
* **Musik** -- ``cover.jpg`` in der Upload-Session. beets übernimmt sie beim
  Import über ``fetchart``, sofern dort ``cautious`` und ``cover_names: cover``
  stehen (tut es in ``beets/config.yaml``).

Deshalb heißt die Datei in beiden Fällen genau so und nicht anders.
"""

from __future__ import annotations

import logging
from pathlib import Path

import requests

from backend.artist_ids import USER_AGENT

log = logging.getLogger(__name__)

#: Der Name, unter dem beets und Audiobookshelf ein Cover erwarten -- und
#: unter dem mimport selbst jedes Cover ablegt, das es schreibt (siehe
#: speichern()).
COVER_DATEI = "cover.jpg"

#: Erweiterungen, unter denen im Album-Ordner ein Cover liegen kann,
#: absteigend nach Vorrang. 'jpg' zuerst: das ist der Name, den mimport selbst
#: vergibt (abfotografiertes Cover, per URL geholtes Discogs-Bild) -- ein
#: frisches Foto soll immer vor einem älteren, von fetchart heruntergeladenen
#: Bild gewinnen. Die übrigen sind die Formate, die beets' fetchart-Plugin je
#: nach Content-Type der Quelle selbst wählt (``Album.art_destination``
#: übernimmt die Erweiterung der heruntergeladenen Datei) -- bei der Cover Art
#: Archive ist PNG keine Seltenheit. Ohne diese Liste prüfte mimport
#: ausschließlich auf 'cover.jpg' und hielt ein von fetchart erfolgreich
#: geladenes PNG-Cover für nicht vorhanden, obwohl es im Ordner lag (Navidrome
#: fand es trotzdem, weil es nicht auf einen festen Dateinamen besteht).
_ERWEITERUNGEN = ("jpg", "jpeg", "png", "webp")

#: Obergrenze für ein einzelnes Bild. Ein entzerrtes Cover liegt bei ein paar
#: hundert Kilobyte; alles darüber ist kein Cover mehr.
MAX_BYTES = 12 * 1024 * 1024

#: Kennungen der Formate, die wir annehmen. Die Endung sagt nichts -- ein
#: Browser darf schicken, was er will, und hier landet eine Datei aus einem
#: Formular.
_MAGIC = {
    b"\xff\xd8\xff": "jpeg",
    b"\x89PNG\r\n\x1a\n": "png",
}


class CoverError(Exception):
    """Das Bild ist unbrauchbar oder das Ziel ungültig."""


def format_erkennen(daten: bytes) -> str:
    """Welches Bildformat liegt vor? Entschieden wird an den ersten Bytes."""
    for magic, name in _MAGIC.items():
        if daten.startswith(magic):
            return name
    raise CoverError(
        "Das sieht nicht nach einem Bild aus. Erwartet wird JPEG oder PNG."
    )


def speichern(ordner: Path, daten: bytes) -> Path:
    """Legt das Cover als ``cover.jpg`` in einen Ordner.

    Ein vorhandenes Cover wird ersetzt -- wer neu fotografiert, will das alte
    loswerden. Anders als bei Audiodateien ist dabei nichts zu verlieren: das
    Bild lässt sich jederzeit neu aufnehmen.
    """
    if not daten:
        raise CoverError("Es kam kein Bild an.")
    if len(daten) > MAX_BYTES:
        raise CoverError(
            f"Das Bild ist zu groß ({len(daten) / 1024**2:.1f} MB). "
            f"Erlaubt sind {MAX_BYTES // 1024**2} MB."
        )
    format_erkennen(daten)

    if not ordner.is_dir():
        raise CoverError("Das Ziel für das Cover gibt es nicht.")

    ziel = ordner / COVER_DATEI
    # Erst danebenschreiben, dann umbenennen: bricht die Übertragung ab, bleibt
    # das bisherige Cover unversehrt statt halb überschrieben.
    vorlaeufig = ordner / f".{COVER_DATEI}.neu"
    try:
        vorlaeufig.write_bytes(daten)
        vorlaeufig.replace(ziel)
    except OSError as exc:
        vorlaeufig.unlink(missing_ok=True)
        raise CoverError(f"Das Cover ließ sich nicht speichern: {exc}") from exc

    log.info("Cover gespeichert: %s (%d KB)", ziel, len(daten) // 1024)
    return ziel


def andere_erweiterungen_entfernen(ordner: Path) -> None:
    """Räumt ein älteres Cover unter anderer Erweiterung weg, nachdem
    ``cover.jpg`` gerade neu geschrieben wurde.

    Bewusst NICHT Teil von ``speichern()`` selbst: das speichert auch in die
    Upload-Session (und über ``von_url_holen`` einen Discogs-Kandidaten),
    beides Orte, an denen niemals ein ``cover.png`` von fetchart liegt und ein
    unbedingtes Aufräumen dort ein unerwarteter Seiteneffekt eines
    "speichern"-Aufrufs wäre. Sinn ergibt das Aufräumen nur beim Ersetzen des
    Covers eines *schon importierten* Albums (siehe
    ``routes.update_album_cover``) -- dort läge sonst neben dem neuen Foto ein
    verwaistes ``cover.png`` von fetchart, und welches der beiden Bilder ein
    Player dann anzeigt, ist Zufall. Best effort: ein einzelnes
    Aufräumproblem soll das eigentliche Speichern des neuen Covers nicht zu
    Fall bringen.
    """
    for ext in _ERWEITERUNGEN:
        if ext == "jpg":
            continue
        pfad = ordner / f"cover.{ext}"
        if pfad.is_file():
            try:
                pfad.unlink()
            except OSError as exc:
                log.warning("Altes Cover %s ließ sich nicht entfernen: %s", pfad, exc)


def gefunden(ordner: Path) -> Path | None:
    """Das Cover im Ordner, unabhängig davon, welche Erweiterung es trägt.

    Siehe ``_ERWEITERUNGEN`` für die Reihenfolge und die Begründung.
    """
    for ext in _ERWEITERUNGEN:
        kandidat = ordner / f"cover.{ext}"
        if kandidat.is_file():
            return kandidat
    return None


def vorhanden(ordner: Path) -> bool:
    return gefunden(ordner) is not None


def von_url_holen(ordner: Path, url: str, *, timeout: float = 10.0) -> Path | None:
    """Lädt ein Cover von einer URL herunter und legt es als ``cover.jpg`` ab.

    Für Discogs-Kandidaten gedacht: ``AlbumInfo.cover_art_url`` zeigt auf ein
    Bild von der Release-Seite, aber beets' ``fetchart``-Quelle dafür
    (``cover_art_url``) braucht ein Feld, das erst beim *selben* Importlauf
    gesetzt wird -- mimport trennt Tag-Schreiben und ``beet import -A`` aber in
    zwei Prozesse, dazwischen geht das Feld verloren (es ist kein einbettbarer
    Tag). Landet das Bild stattdessen hier als ``cover.jpg`` in der Session,
    greift beim Import ganz normal die ``filesystem``-Quelle -- dieselbe, die
    auch ein abfotografiertes Cover übernimmt.

    Best-effort mit Absicht: Ein Netzfehler oder ein unbrauchbares Bild soll
    die Auswahl des Kandidaten nicht zu Fall bringen. ``None`` bei jedem
    Fehlschlag, der Rückgabewert der zugrunde liegenden Speicherung sonst --
    fotografieren bleibt in jedem Fall weiterhin möglich.
    """
    try:
        antwort = requests.get(
            url, headers={"User-Agent": USER_AGENT}, timeout=timeout
        )
        antwort.raise_for_status()
    except requests.RequestException as exc:
        log.info("Cover-Download von %s fehlgeschlagen: %s", url, exc)
        return None

    try:
        return speichern(ordner, antwort.content)
    except CoverError as exc:
        log.info("Heruntergeladenes Cover von %s unbrauchbar: %s", url, exc)
        return None
