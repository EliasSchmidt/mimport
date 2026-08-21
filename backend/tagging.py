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
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import artist_ids, beets_env, tag_catalog

log = logging.getLogger(__name__)


@dataclass
class TagWriteResult:
    """Ergebnis des Tag-Schreibens."""

    written: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.written) and not self.failed


#: Felder, die beets' ``AlbumInfo``/``TrackInfo`` quellenunabhängig unter
#: MusicBrainz-Namen ablegt (``MEDIA_FIELD_MAP`` in
#: ``beets/autotag/hooks.py``): ``album_id`` etwa landet immer in
#: ``mb_albumid``, ob die Quelle nun MusicBrainz oder Discogs war. Für Discogs
#: trägt das Discogs' eigene numerische Release-ID ein -- keine MB-UUID, aber
#: unter einem Tag-Namen, den ``mediafile`` (und jeder Player, der ihn liest)
#: als "MusicBrainz Release Id" o. Ä. beschriftet. Ohne Korrektur würde das
#: also nicht nur in der Datei falsch beschriftete IDs hinterlassen, sondern
#: auch ``retry_missing_cover`` (siehe ``albums.py``) dazu bringen, für ein
#: Discogs-Album einen MusicBrainz-Cover-Nachschlag zu versuchen, der nie
#: treffen kann.
_MB_TEXTFELDER = (
    "mb_albumid",
    "mb_releasegroupid",
    "mb_trackid",
    "mb_releasetrackid",
    "mb_workid",
)
_MB_LISTENFELDER = ("mb_artistids", "mb_albumartistids")


def _mb_ids_ohne_musicbrainz_entfernen(items: Any) -> None:
    for item in items:
        for feld in _MB_TEXTFELDER:
            item[feld] = ""
        for feld in _MB_LISTENFELDER:
            item[feld] = []


def apply_album_match(match: Any, *, from_scratch: bool = False) -> TagWriteResult:
    """Schreibt die Metadaten eines gewählten ``AlbumMatch`` in die Dateien.

    ``apply_metadata`` aktualisiert die ``Item``-Objekte im Speicher,
    ``try_write`` legt die Tags dann in der Datei ab.
    """
    beets_env.ensure_loaded()
    result = TagWriteResult()

    match.apply_metadata(from_scratch=from_scratch)

    if match.info.data_source != "MusicBrainz":
        _mb_ids_ohne_musicbrainz_entfernen(match.mapping)

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


#: Felder, die beets mehrwertig führt. Ein einzelner String landet dort sonst
#: als flexibles Attribut und **wird nicht in die Datei geschrieben** -- genau
#: das passierte mit "genre", das in beets 2.x "genres" heißt.
#:
#: Aus ``tag_catalog`` abgeleitet, nicht mehr hier von Hand gepflegt --
#: dieselbe Liste, die auch ``albums.py`` fürs nachträgliche Bearbeiten
#: benutzt. Das fügt gegenüber der früheren Handliste ein paar Felder hinzu
#: (z. B. Texter, Arrangeur), die das Manuell-Formular heute noch gar nicht
#: anbietet -- die Einträge liegen dann einfach ungenutzt bereit, bis sie
#: gebraucht werden, statt beim nächsten neuen Feld erneut von Hand
#: nachgetragen werden zu müssen.
_ALLE_KATALOG_FELDER = tag_catalog.ALBUM_FELDER + tag_catalog.TRACK_FELDER
_MEHRWERTIG = {
    f.key: f.einzelform
    for f in _ALLE_KATALOG_FELDER
    if f.mehrwertig_art in ("kuenstler", "genre") and f.einzelform
}
_ID_MEHRWERTIG = {
    f.key: f.einzelform
    for f in _ALLE_KATALOG_FELDER
    if f.mehrwertig_art == "liste" and f.einzelform
}

#: Womit mehrere Namen in einem Feld getrennt werden. Genau die Zeichenfolgen,
#: die auch Navidrome kennt: " / ", " feat. ", " feat ", " ft. ", " ft ", "; ".
#:
#: Die Leerzeichen sind wesentlich, nicht Kosmetik. Ohne sie zerlegt der
#: Ausdruck "AC/DC" in zwei Künstler -- nachgeprüft, zusammen mit
#: "Simon & Garfunkel" und "Crosby, Stills & Nash", die ebenfalls
#: zusammenbleiben müssen.
_KUENSTLER_TRENNER = re.compile(
    r"\s+/\s+|\s+feat\b\.?\s+|\s+ft\b\.?\s+|;\s*", re.IGNORECASE
)

#: Für Genres ist das absichtlich strenger als bei Künstlern: nur Semikolon.
#: So bleiben Einträge wie "R&B/Soul" oder "Folk, World, & Country" ganz.
_GENRE_TRENNER = re.compile(r";\s*")


def sampler_name() -> str:
    """Wie ein Sampler-Albumkünstler heißt.

    Aus der beets-Konfiguration (``va_name``), nicht fest verdrahtet -- wer
    dort etwas anderes einstellt, soll es auch in den Dateien wiederfinden.
    """
    beets_env.ensure_loaded()
    from beets import config

    try:
        return str(config["va_name"].get()) or "Various Artists"
    except Exception:  # noqa: BLE001 -- ohne gültige Konfiguration gilt der Standardname
        return "Various Artists"


def _kuenstlerwerte(value: object) -> list[str]:
    """Zerlegt eine Eingabe wie ``A feat. B`` in einzelne Namen."""
    return [teil.strip() for teil in _KUENSTLER_TRENNER.split(str(value)) if teil.strip()]


def kuenstlerliste(value: object) -> list[str]:
    """Öffentliche Fassung von :func:`_kuenstlerwerte`.

    Die Mehrfach-Suche im UI (``routes.artist_match``) muss dieselbe Zerlegung
    verwenden wie das spätere Schreiben, sonst würde die MusicBrainz-Suche
    andere Namen sehen als am Ende in die Datei wandern.
    """
    return _kuenstlerwerte(value)


def _genrewerte(value: object) -> list[str]:
    """Zerlegt Genres über Semikolon, sonst nichts."""
    return [teil.strip() for teil in _GENRE_TRENNER.split(str(value)) if teil.strip()]


def _listenwerte(value: object) -> list[str]:
    """Mehrwertige Felder aus Liste/Tupel oder ``;``-getrenntem String lesen."""
    if isinstance(value, (list, tuple)):
        return [str(teil).strip() for teil in value if str(teil).strip()]
    return [teil.strip() for teil in str(value).split(";") if teil.strip()]


def _kuenstler_mbids(value: object) -> list[str] | None:
    """Löst einen Künstlerwert vollständig zu Artist-MBIDs auf.

    Gibt nur dann IDs zurück, wenn **alle** beteiligten Künstler eindeutig
    gefunden wurden. Teiltreffer wären mehrdeutig und würden Name und ID aus dem
    Tritt bringen.
    """
    namen = _kuenstlerwerte(value)
    if not namen:
        return None

    ids: list[str] = []
    for name in namen:
        mbid = artist_ids.lookup_exact(name)
        if not mbid:
            return None
        ids.append(mbid)
    return ids


def _inhalt(value: object) -> bool:
    if isinstance(value, (list, tuple)):
        return any(str(teil).strip() for teil in value)
    return bool(str(value).strip())


def _ergänze_kuenstler_ids(fields: Mapping[str, object]) -> dict[str, object]:
    """Schreibt MusicBrainz-Artist-IDs ergänzend zu manuellen Namen dazu.

    Es werden bewusst nur Künstler-IDs ergänzt, keine Release-ID: für ein lokal
    zusammengestelltes oder inoffizielles Album wäre eine erfundene Album-MBID
    schlicht falsch.
    """
    ergänzt = dict(fields)

    if _inhalt(ergänzt.get("artists", "")) and not _inhalt(ergänzt.get("mb_artistids", "")):
        artist_ids_wert = _kuenstler_mbids(ergänzt["artists"])
        if artist_ids_wert:
            ergänzt["mb_artistids"] = artist_ids_wert

    if _inhalt(ergänzt.get("albumartist", "")) and not _inhalt(
        ergänzt.get("mb_albumartistids", "")
    ):
        albumartist_ids = _kuenstler_mbids(ergänzt["albumartist"])
        if albumartist_ids:
            ergänzt["mb_albumartistids"] = albumartist_ids

    return ergänzt


def _setzen(item: Any, key: str, value: object) -> None:
    """Setzt ein Feld so, dass es tatsächlich in der Datei landet."""
    if key in ("year", "track"):
        try:
            setattr(item, key, int(str(value).strip()))
        except ValueError:
            pass
        return
    if key == "comp":
        item.comp = bool(value)
        return
    if key in _MEHRWERTIG:
        teile = _genrewerte(value) if key == "genres" else _kuenstlerwerte(value)
        if not teile:
            return
        item[key] = teile
        # Das einwertige Gegenstück mitschreiben: ältere Abspieler und Scanner
        # lesen nur dieses.
        item[_MEHRWERTIG[key]] = "; ".join(teile)
        return
    if key in _ID_MEHRWERTIG:
        teile = _listenwerte(value)
        if not teile:
            return
        item[key] = teile
        item[_ID_MEHRWERTIG[key]] = "; ".join(teile)
        return
    item[key] = str(value).strip()


def apply_manual_tags(
    paths: list[Path],
    fields: dict[str, object],
    *,
    je_track: Mapping[str, Mapping[str, object]] | None = None,
    relative_to: Path | None = None,
) -> TagWriteResult:
    """Schreibt handgepflegte Tags.

    ``fields`` gilt für alle Dateien -- Album, Albumkünstler, Jahr, Genre.
    ``je_track`` trägt zusätzlich für einzelne Dateien Titel und Künstler ein,
    und genau das braucht eine Sampler-CD: dort hat jeder Track einen anderen
    Interpreten, während der Albumkünstler „Various Artists" bleibt.

    ``je_track`` darf mit Basenamen (``Track01.flac``) oder mit Pfaden relativ
    zu ``relative_to`` (``CD1/Track01.flac``) adressieren.

    Leere Werte werden übersprungen, damit ein leeres Formularfeld nichts
    überschreibt.
    """
    beets_env.ensure_loaded()
    from beets.library import Item

    fields = dict(fields)
    # Ein Sampler ohne Albumkünstler hätte in der Datei keinen -- und genau die
    # liest Audiobookshelf, Navidrome oder sonst ein Abspieler. beets trägt
    # „Various Artists" zwar in seine Library ein, schreibt es aber nicht in
    # die Datei zurück; nachgemessen nach einem Import. Ohne den Eintrag
    # gruppiert Navidrome die Stücke nicht zu einem Album, weil dort je Track
    # ein anderer Interpret steht.
    if fields.get("comp") is True and not str(fields.get("albumartist", "")).strip():
        fields["albumartist"] = sampler_name()

    # Kein Sampler, aber der Track-Künstler blieb leer: ohne Fallback bekäme
    # die Datei gar keinen ARTIST-Tag (nicht einmal einen leeren) -- Navidrome
    # & Co. zeigen dann "Unknown Artist", obwohl der Albumkünstler ja bekannt
    # ist. Bei einem Sampler dagegen wäre "Various Artists" auf jedem Track
    # falsch, deshalb nur außerhalb von "comp". ``albumartist`` ist in diesem
    # Zweig serverseitig Pflicht (siehe ``routes.manual``), kann hier also
    # nicht leer sein. Die MBID gleich mitnehmen erspart ``_ergänze_kuenstler_ids``
    # unten den doppelten Nachschlag für denselben Namen.
    if fields.get("comp") is not True and not str(fields.get("artists", "")).strip():
        if str(fields.get("albumartist", "")).strip():
            fields["artists"] = fields["albumartist"]
            if str(fields.get("mb_albumartistids", "")).strip():
                fields["mb_artistids"] = fields["mb_albumartistids"]

    fields = _ergänze_kuenstler_ids(fields)
    je_track = {key: _ergänze_kuenstler_ids(value) for key, value in (je_track or {}).items()}

    result = TagWriteResult()
    # Ein nicht gesetztes Häkchen ist keine Eingabe. Ohne die ausdrückliche
    # Prüfung auf ``False`` zählte es als solche -- ``str(False)`` ist nicht
    # leer -- und die Rückmeldung „kein Feld ausgefüllt" wäre nie erschienen.
    usable = {
        key: value
        for key, value in fields.items()
        if value is not False and (value is True or str(value).strip())
    }
    if not usable and not je_track:
        return result

    # Sobald irgendeine Nummer von Hand korrigiert wurde, sagt "Anzahl der
    # Dateien in dieser Sitzung" nichts mehr verlässlich über die Gesamtzahl
    # der Tracks aus -- die korrigierten Nummern können ja gerade bedeuten,
    # dass hier nur ein Ausschnitt eines größeren Albums liegt. Ohne diese
    # Ausnahme entstünde sonst ein in sich widersprüchliches Tag-Paar wie
    # "Track 5 von 2".
    manuelle_nummern = any("track" in werte for werte in je_track.values())

    for nummer, path in enumerate(paths, start=1):
        relative = ""
        if relative_to is not None:
            try:
                relative = str(path.relative_to(relative_to))
            except Exception:  # noqa: BLE001 -- ohne relativen Pfad zählt nur der Basename
                relative = ""

        eigene_quelle = je_track.get(relative) or je_track.get(path.name) or {}
        eigene = {
            key: wert
            for key, wert in eigene_quelle.items()
            if str(wert).strip()
        }
        if not usable and not eigene:
            continue
        try:
            item = Item.from_path(str(path))
        except Exception as exc:  # noqa: BLE001 -- eine unlesbare Datei soll die übrigen nicht stoppen
            result.failed.append((path.name, f"nicht lesbar: {exc}"))
            continue

        for key, value in {**usable, **eigene}.items():
            _setzen(item, key, value)

        # Ohne Tracknummer benennt beets jede Datei zu "00 <Titel>" -- bei
        # einem Sampler mit vierzehn Stücken vierzehnmal dieselbe Null, und
        # die Reihenfolge im Album ist dahin. Die Position in der sortierten
        # Liste ist die beste verfügbare Auskunft; eine vorhandene Nummer
        # (etwa vom Rip, oder von Hand in "Nr." korrigiert) bleibt unangetastet
        # -- Letzteres steckt schon in "eigene" und wurde oben gesetzt.
        if not item.track:
            item.track = nummer
        if not item.tracktotal and not manuelle_nummern:
            item.tracktotal = len(paths)

        try:
            if item.try_write():
                result.written.append(path.name)
            else:
                result.failed.append((path.name, "Tags konnten nicht geschrieben werden"))
        except Exception as exc:  # noqa: BLE001 -- ein Schreibfehler soll die übrigen Dateien nicht stoppen
            result.failed.append((path.name, str(exc)))
    return result


def _display(item: Any) -> str:
    path = item.path
    if isinstance(path, bytes):
        return Path(path.decode("utf-8", "replace")).name
    return Path(str(path)).name
