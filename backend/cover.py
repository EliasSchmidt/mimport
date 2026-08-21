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

#: Der Name, unter dem beets und Audiobookshelf ein Cover erwarten.
COVER_DATEI = "cover.jpg"

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


def vorhanden(ordner: Path) -> bool:
    return (ordner / COVER_DATEI).is_file()


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
