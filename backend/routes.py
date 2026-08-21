"""HTTP-Endpunkte.

Die Antworten sind größtenteils HTML-Fragmente, die HTMX an die passende Stelle
der Seite hängt -- deshalb gibt es hier kein JSON-Schema und kein Frontend-
Framework.

Zur Nebenläufigkeit: alles, was beets aufruft, ist als ``def`` deklariert und
nicht als ``async def``. MusicBrainz-Abfragen sind synchron und brauchen
Sekunden; in einer ``async``-Funktion würden sie den gesamten Server anhalten.
FastAPI führt ``def``-Endpunkte dagegen in einem Threadpool aus.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import shutil
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse

from backend import (
    albums,
    artist_ids,
    audio,
    audiobook,
    beets_env,
    cover,
    disc,
    genres,
    importer,
    matching,
    ocr,
    rip,
    sessions,
    tag_catalog,
    tagging,
    trackparse,
)
from backend.config import AUDIO_EXTENSIONS, settings
from backend.templates import templates

log = logging.getLogger(__name__)

router = APIRouter()

#: Uploads in Häppchen auf die Platte schreiben, damit ein Album mit
#: verlustfreien Dateien nicht komplett im Speicher landet.
CHUNK_SIZE = 1024 * 1024


#: Katalogfelder, die das manuelle Tagging zusätzlich zu den schon
#: benannten Basis-Feldern (Albumkünstler/Album/Jahr/Genre/Sampler, eigene
#: Form-Namen aus historischen Gründen) generisch anbietet. Künstler-Felder
#: laufen über die MusicBrainz-Lupe, nicht hier drüber; eine Album- oder
#: Release-ID setzen wir beim Handtaggen bewusst nicht (siehe Docstring von
#: ``manual`` unten) -- die ganze "musicbrainz"-Gruppe bleibt deshalb außen
#: vor. Track-seitige "erweitert"-Felder (ISRC, BPM, Tonart, ...) fehlen hier
#: bewusst: die Tabelle "Titel je Track" wäre mit einer Spalte je Feld
#: sofort wieder so breit, wie die /albums-Detailseite es gerade nicht mehr
#: sein soll -- nachträglich sind sie dort im "Weitere Felder"-Dialog pro
#: Titel erreichbar.
_MANUAL_ALBUM_BASIS_ZUSATZ = tuple(
    f
    for f in tag_catalog.ALBUM_FELDER
    if f.gruppe == "basis" and not f.kuenstler_link
    and f.key not in ("album", "year", "genres", "comp")
)
_MANUAL_ALBUM_ERWEITERT = tuple(
    f for f in tag_catalog.ALBUM_FELDER if f.gruppe == "erweitert" and not f.kuenstler_link
)

_FELD_WERTE = {"titel", "interpret", "komponist"}


def _felder(*rohe: str) -> tuple[trackparse.Feld, ...]:
    """Baut die Feld-Reihenfolge aus den drei Positions-Auswahlfeldern.

    Leere Positionen (nichts ausgewählt) und Duplikate (dasselbe Feld
    zweimal gewählt) fallen einfach raus, statt einen Fehler zu werfen --
    bei einer Handvoll Dropdowns ist das plausibler als eine strenge
    Validierung. Ganz leer (nichts gewählt) heißt "nur Titel", die Vorgabe.
    """
    ergebnis: list[trackparse.Feld] = []
    for wert in rohe:
        wert = wert.strip()
        if wert in _FELD_WERTE and wert not in ergebnis:
            ergebnis.append(wert)  # type: ignore[arg-type]
    return tuple(ergebnis) or ("titel",)


def _track_zahl_warnung(erkannt: int, erwartet: int, flags: trackparse.ParseFlags) -> str:
    """Bei "zeilenweise" halbiert (o. ä.) sich die Trackzahl gegenüber den
    OCR-Zeilen -- "Zeile(n)" wäre dann eine falsche Einheit."""
    zeilenweise = flags.zeilenweise and len(flags.felder) > 1
    einheit = "Track(s)" if zeilenweise else "Zeile(n)"
    return f"Parser hat {erkannt} {einheit} erkannt, Session enthält {erwartet} Datei(en)."


def _parse_flags(draft: dict[str, str]) -> trackparse.ParseFlags:
    """Die zuletzt gewählten Parser-Schalter aus dem Entwurf, sonst die Vorgabe.

    Ein Kästchen taucht im Entwurf nur auf, wenn es beim letzten Autosave
    gesetzt war -- Abwesenheit heißt "abgewählt", nicht "noch nie gesetzt".
    Für einen frischen Entwurf (noch gar nichts gespeichert) gelten
    stattdessen die Vorgaben aus ``ParseFlags``.
    """
    if not draft:
        return trackparse.ParseFlags()
    return trackparse.ParseFlags(
        tracknummer="tracknummer" in draft,
        dauer="dauer" in draft,
        felder=_felder(
            str(draft.get("feld1") or ""),
            str(draft.get("feld2") or ""),
            str(draft.get("feld3") or ""),
        ),
        zeilenweise="zeilenweise" in draft,
        trenner=str(draft.get("trenner") or " - "),
    )


def _entwurf_nach_ocr_lauf(
    session: sessions.StagingSession,
    ocr_text: str,
    flags: trackparse.ParseFlags,
    rows: list[dict[str, str]],
) -> None:
    """Sichert Texterkennung/Parser-Lauf sofort im Entwurf.

    Das normale Autosave reagiert nur auf Tippen im Formular (``input``-
    Events); ein htmx-Swap nach "Text erkennen" oder "Parser anwenden" löst
    das nicht aus. Ohne das hier ging ein erkannter Text verloren, wenn die
    Sitzung danach ohne weiteren Tastendruck verlassen wurde.
    """
    draft = sessions.load_draft(session)
    if ocr_text:
        draft["ocr_text"] = ocr_text
    else:
        draft.pop("ocr_text", None)
    for name, gesetzt in (
        ("tracknummer", flags.tracknummer),
        ("dauer", flags.dauer),
        ("zeilenweise", flags.zeilenweise),
    ):
        if gesetzt:
            draft[name] = "true"
        else:
            draft.pop(name, None)
    for index in range(3):
        name = f"feld{index + 1}"
        wert = flags.felder[index] if index < len(flags.felder) else ""
        if wert:
            draft[name] = wert
        else:
            draft.pop(name, None)
    draft["trenner"] = flags.trenner
    for row in rows:
        for praefix, wert in (
            ("titel", row["title"]),
            ("interpret", row["artist"]),
            ("komponist", row["composer"]),
            ("nr", row["track"]),
        ):
            key = f"{praefix}:{row['key']}"
            if praefix == "interpret" and str(draft.get(key) or "") != wert:
                # Der Parser überschreibt den Track-Künstler hier mit einem
                # anderen Namen als dem, für den zuvor per Lupe eine
                # Artist-ID bestätigt wurde -- die galt fürs alte Wort und
                # bliebe sonst unbemerkt am neuen kleben.
                draft.pop(f"mbinterpret:{row['key']}", None)
            if wert:
                draft[key] = wert
            else:
                draft.pop(key, None)
    sessions.save_draft(session, draft)


def _storage_allowance(was: str = "Upload") -> tuple[int, str]:
    """Wie viele Bytes noch ins Staging dürfen -- und die Meldung dazu.

    ``was`` benennt die Quelle für den Fall, dass die Grenze je Vorgang greift;
    Upload und CD teilen sich die Rechnung, aber nicht den Wortlaut.

    Drei Grenzen, die kleinste gewinnt: das Limit je Upload, der freie Platz
    abzüglich Sicherheitsabstand und der Rest des Staging-Gesamtbudgets.

    Der freie Platz ist die einzige Grenze, die wirklich schützt: eine
    konfigurierte Obergrenze nützt nichts, wenn das Dateisystem aus anderen
    Gründen schon voll ist. Die beiden anderen sind Politik darüber.

    Bewusst ohne Reservierung: zwei gleichzeitige Uploads sehen denselben
    Stand und dürfen beide los, zusammen also mehr als das Budget. Das ist
    hingenommen, nicht übersehen -- mimport bedient einen Nutzer auf
    ``127.0.0.1``, und der Sicherheitsabstand zum vollen Dateisystem federt
    den Überhang ab.
    """
    sessions.ensure_root()
    frei = settings.staging_free_bytes() - settings.min_free_bytes
    budget = settings.max_staging_bytes - sessions.usage_bytes()
    grenzen = [
        (
            settings.max_upload_bytes,
            (
                f"{was} überschreitet das Limit von "
                f"{settings.max_upload_bytes / 1024**3:.1f} GB."
            ),
        ),
        (
            frei,
            (
                "Auf dem Server ist nicht genug Speicherplatz frei. Bitte zuerst "
                "laufende Importe abschließen."
            ),
        ),
        (
            budget,
            (
                "Der Staging-Bereich ist ausgelastet. Bitte zuerst laufende "
                "Importe abschließen oder nicht mehr benötigte Uploads verwerfen."
            ),
        ),
    ]
    return min(grenzen, key=lambda grenze: grenze[0])


def _session_or_404(session_id: str) -> sessions.StagingSession:
    try:
        return sessions.get_session(session_id)
    except sessions.SessionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


def _fragment(request: Request, name: str, **context: object) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


#: Wie oft /rip/events und /audiobook/events den Auftragsstand nachsehen.
#: Reines Lesen eines Moduls-Globals, kein Grund für ein längeres Intervall.
_SSE_INTERVALL = 1.0


def _sse_event(event: str, html: str = "") -> str:
    """Formatiert ein Server-Sent-Event.

    Jede Zeile des HTML-Fragments braucht ihre eigene "data:"-Zeile -- ein
    rohes Newline im data-Feld würde SSE als Ende der Nachricht lesen und den
    Rest verschlucken.
    """
    zeilen = html.splitlines() or [""]
    daten = "\n".join(f"data: {zeile}" for zeile in zeilen)
    return f"event: {event}\n{daten}\n\n"


def _track_files(session: sessions.StagingSession) -> list[dict[str, str]]:
    """Dateien einer Session mit stabilem Formularschlüssel."""
    files: list[dict[str, str]] = []
    for path in session.audio_paths:
        relative = str(path.relative_to(session.directory))
        files.append({"key": relative, "display": relative})
    return files


def _session_cover_path(session: sessions.StagingSession) -> Path:
    return session.directory / cover.COVER_DATEI


def _session_cover_version(session: sessions.StagingSession) -> str:
    bild = _session_cover_path(session)
    if not bild.is_file():
        return ""
    try:
        return str(int(bild.stat().st_mtime_ns))
    except OSError:
        return ""


def _mb_albumid_der_session(session: sessions.StagingSession) -> str | None:
    """Liest die MusicBrainz-Release-ID aus den Dateien der Session.

    Genau die ID, die ``choose()`` über ``apply_metadata()`` in die Dateien
    geschrieben hat -- vor dem Import gelesen, weil danach nichts mehr an
    diesem Ort liegt (verschoben oder kopiert). Dient ``albums.retry_missing_cover``
    als eindeutiger Ankerpunkt, statt über Album-/Künstlername zu suchen.
    """
    import mediafile

    for path in session.audio_paths:
        try:
            media = mediafile.MediaFile(path)
        except Exception:  # noqa: BLE001 -- eine unlesbare Datei soll die Suche nicht abbrechen
            continue
        if media.mb_albumid:
            return media.mb_albumid
    return None


def _ocr_overlay(result: ocr.OcrResult, image_url: str) -> dict[str, object]:
    detections: list[dict[str, object]] = []
    for detection in result.detections:
        points = " ".join(f"{x:.2f},{y:.2f}" for x, y in detection.box)
        detections.append(
            {
                "points": points,
                "text": detection.text,
                "score": f"{detection.score * 100:.0f}",
                "x": min(point[0] for point in detection.box),
                "y": min(point[1] for point in detection.box),
            }
        )
    return {
        "image_url": image_url,
        "image_width": result.image_width,
        "image_height": result.image_height,
        "detections": detections,
    }


def _field_label(field: str) -> str:
    # "interpret:<Dateiname>" adressiert den Track-Künstler einer einzelnen
    # Zeile in "Titel je Track" -- fachlich derselbe Künstler-Lookup wie das
    # albumweite Feld, nur je Track statt einmal fürs ganze Album.
    if field.startswith("interpret:"):
        return "Track-Künstler"
    return {
        "albumartist": "Albumkünstler",
        "artist": "Track-Künstler",
    }.get(field, field)


def _mbid_field(field: str) -> str:
    if field.startswith("interpret:"):
        return "mbinterpret:" + field.partition(":")[2]
    return {
        "albumartist": "mb_albumartistids",
        "artist": "mb_artistids",
    }.get(field, "")


def _track_inputs(
    session: sessions.StagingSession,
    parsed: list[trackparse.ParsedTrack] | None = None,
    draft: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Track-Eingaben fürs manuelle Formular, optional aus Parser vorbefüllt.

    Ein frischer Parser-Lauf (``parsed``) geht immer vor -- ein gesicherter
    Entwurf (``draft``) füllt nur, was sonst leer bliebe. So gewinnt "Text
    erkennen" gegen einen älteren Entwurf, statt von ihm überschrieben zu
    werden.
    """
    parsed = parsed or []
    draft = draft or {}
    rows: list[dict[str, str]] = []
    for index, file in enumerate(_track_files(session)):
        row = parsed[index] if index < len(parsed) else None
        title = (row.title if row else "").strip()
        artist = (row.artist if row else "").strip()
        track = (row.number if row else "").strip()
        if not artist:
            artist = str(draft.get(f"interpret:{file['key']}") or "").strip()
        if not title:
            title = str(draft.get(f"titel:{file['key']}") or "").strip()
        if not track:
            track = str(draft.get(f"nr:{file['key']}") or "").strip()
        # Passt zum Namen aus genau demselben Entwurf-Feld -- kein separater
        # Abgleich nötig: ``data-selected-name`` im Template merkt sich beim
        # Rendern, zu welchem Namen diese ID gehört, und das Browser-Skript
        # verwirft sie selbst, sobald der Name danach abweicht (bevor der
        # nächste Autosave den veralteten Stand sichern könnte).
        mbid = str(draft.get(f"mbinterpret:{file['key']}") or "").strip()
        composer = (row.composer if row else "").strip()
        if not composer:
            composer = str(draft.get(f"komponist:{file['key']}") or "").strip()
        rows.append(
            {
                "key": file["key"],
                "display": file["display"],
                "title": title,
                "mbid": mbid,
                "artist": artist,
                "track": track,
                "composer": composer,
            }
        )
    return rows


def _files_fragment(request: Request, session: sessions.StagingSession) -> HTMLResponse:
    """Die geprüfte Dateiliste einer Session.

    Der Übergang, an dem sich Upload und CD wieder treffen: ab hier ist nicht
    mehr zu erkennen, woher die Dateien kamen, und Schritt 2 bis 4 laufen für
    beide gleich.
    """
    infos = [
        audio.inspect_file(path, display_name=str(path.relative_to(session.directory)))
        for path in session.audio_paths
    ]
    draft = sessions.load_draft(session)
    return _fragment(
        request,
        "_files.html",
        session_id=session.session_id,
        infos=infos,
        summary=audio.summarize(infos),
        health=beets_env.health(),
        parse_flags=_parse_flags(draft),
        ocr_text=str(draft.get("ocr_text") or ""),
        ocr_warnings=[],
        track_inputs=_track_inputs(session, draft=draft),
        draft=draft,
        genre_vorschlaege=genres.katalog(),
        manual_basis_zusatz=_MANUAL_ALBUM_BASIS_ZUSATZ,
        manual_erweitert=_MANUAL_ALBUM_ERWEITERT,
        cover_present=cover.vorhanden(session.directory),
        cover_version=_session_cover_version(session),
        ocr_overlay=None,
    )


def _seite(request: Request, name: str, selbst: str, **kontext: object) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        name,
        {
            "health": beets_env.health(),
            "settings": settings,
            "selbst": selbst,
            **kontext,
        },
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Die Startseite stellt nur eine Frage: Musik oder Hörbuch?

    Die beiden Wege haben fast nichts gemeinsam -- Musik geht über beets mit
    Match-Dialog, Hörbücher über keines von beidem. Auf einer gemeinsamen Seite
    war jeweils die Hälfte der Bedienelemente Ballast.
    """
    return _seite(
        request,
        "index.html",
        "start",
        musik_ziel=settings.staging_root,
        hoerbuch_ziel=settings.audiobook_root,
    )


@router.get("/musik", response_class=HTMLResponse)
def musik(request: Request) -> HTMLResponse:
    """Hochladen, Daten-CD, Audio-CD rippen -- alles mit Match und beets."""
    return _seite(request, "musik.html", "musik")


@router.get("/hoerbuch", response_class=HTMLResponse)
def hoerbuch(request: Request) -> HTMLResponse:
    """Discs sammeln und bündeln, ohne beets."""
    return _seite(request, "hoerbuch.html", "hoerbuch")


@router.post("/upload", response_class=HTMLResponse)
async def upload(request: Request, files: list[UploadFile]) -> HTMLResponse:
    """Nimmt Dateien an, legt sie im Staging ab und prüft ihr Format.

    Die Prüfung im Browser ist nur ein Hinweis vorab; hier entscheidet mediafile
    verbindlich -- und klärt insbesondere ``.m4a``, wo ALAC und AAC dieselbe
    Endung haben.
    """
    if not files:
        return _fragment(request, "_error.html", message="Es wurden keine Dateien gesendet.")

    audio_files = [
        f for f in files if f.filename and Path(f.filename).suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not audio_files:
        return _fragment(
            request,
            "_error.html",
            message="Keine Audiodateien in der Auswahl. Unterstützt werden u. a. "
            "FLAC, WAV, AIFF, ALAC, MP3 und AAC.",
        )
    if len(audio_files) > settings.max_files:
        return _fragment(
            request,
            "_error.html",
            message=f"Zu viele Dateien ({len(audio_files)}), erlaubt sind "
            f"{settings.max_files}.",
        )

    # Verwaistes zuerst wegräumen: der Platz soll diesem Upload wieder zugute
    # kommen, bevor das Budget berechnet wird.
    sessions.sweep_expired(settings.session_ttl_hours)

    erlaubt, grenzmeldung = _storage_allowance()
    if erlaubt <= 0:
        return _fragment(request, "_error.html", message=grenzmeldung)

    session = sessions.create_session()
    written = 0
    total_bytes = 0

    try:
        for upload_file in audio_files:
            # Der Browser schickt die Ordnerstruktur in einem Zusatzfeld; der
            # Dateiname allein enthält sie nicht.
            relative = sessions.sanitize_relative_path(
                upload_file.filename or "unbenannt"
            )
            try:
                destination = sessions.target_path(session, relative)
            except sessions.SessionError as exc:
                log.warning("Upload abgewiesen: %s", exc)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with destination.open("wb") as sink:
                    while chunk := await upload_file.read(CHUNK_SIZE):
                        total_bytes += len(chunk)
                        if total_bytes > erlaubt:
                            sink.close()
                            sessions.delete_session(session.session_id)
                            return _fragment(
                                request, "_error.html", message=grenzmeldung
                            )
                        sink.write(chunk)
                written += 1
            finally:
                await upload_file.close()
    except BaseException:
        # Bricht der Browser die Verbindung ab, fliegt die Exception hier
        # vorbei (bei abgebrochenen Anfragen ein CancelledError, deshalb
        # BaseException). Ohne Aufräumen bliebe der halbe Upload für immer
        # liegen -- ein volles Staging braucht keine Absicht, ein
        # geschlossener Tab genügt. Eine Antwort erübrigt sich, es hört
        # niemand mehr zu.
        sessions.delete_session(session.session_id)
        raise

    if not written:
        sessions.delete_session(session.session_id)
        return _fragment(request, "_error.html", message="Keine Datei konnte gespeichert werden.")

    return _files_fragment(request, session)


def _sessions_fragment(request: Request) -> HTMLResponse:
    """Die Liste offener Sitzungen, samt Auskunft, ob sich eine weitere Disc
    dazu rippen lässt.

    Bewusst nicht an den Ursprung der Sitzung geknüpft (Upload, Daten-CD oder
    Rip): auch wer Disc 1 eines Albums hochgeladen hat, kann für Disc 2 nur
    noch die physische CD haben. "Weitere Disc rippen" hängt deshalb an der
    Sitzung, nicht am flüchtigen Rip-Auftrag -- der wäre nach einem
    fehlgeschlagenen Versuch oder einfach beim Verlassen der Seite wieder weg,
    die Sitzung nicht.
    """
    return _fragment(
        request,
        "_sessions.html",
        offen=sessions.list_open(),
        ttl=settings.session_ttl_hours,
        tools=rip.tools_available(),
    )


@router.get("/sessions", response_class=HTMLResponse)
def open_sessions(request: Request) -> HTMLResponse:
    """Was im Staging liegt und noch nicht importiert ist.

    Die Session-ID steht sonst nur im ausgelieferten HTML -- ein geschlossener
    Tab oder ein leerer Akku kostete sonst den ganzen Upload, obwohl die
    Dateien noch da sind.
    """
    return _sessions_fragment(request)


@router.delete("/sessions/{session_id}", response_class=HTMLResponse)
def discard_from_list(request: Request, session_id: str) -> HTMLResponse:
    """Verwirft eine Sitzung aus der Übersicht heraus.

    Eigene Route neben ``DELETE /session/{id}``: dort wird der laufende Upload
    verworfen und ein leerer Bereich zurückgegeben, hier bleibt die Liste
    stehen und zeigt danach den neuen Stand.
    """
    sessions.delete_session(session_id)
    return _sessions_fragment(request)


@router.get("/session/{session_id}", response_class=HTMLResponse)
def resume_session(request: Request, session_id: str) -> HTMLResponse:
    """Nimmt eine unterbrochene Sitzung wieder auf."""
    session = _session_or_404(session_id)
    if session.is_empty:
        return _fragment(
            request,
            "_error.html",
            message="In dieser Sitzung liegen keine Audiodateien mehr.",
        )
    return _files_fragment(request, session)


@router.get("/disc", response_class=HTMLResponse)
def disc_albums(request: Request) -> HTMLResponse:
    """Listet die Alben der eingelegten CD.

    Absichtlich jederzeit neu abrufbar: nach einem Import ist das nächste Album
    derselben CD der erwartete nächste Schritt.

    Als ``def``, nicht ``async def`` -- ein optisches Laufwerk zu durchsuchen
    dauert Sekunden und gehört deshalb in den Threadpool.
    """
    return _fragment(
        request,
        "_disc.html",
        available=disc.is_available(),
        albums=disc.list_albums(),
        disc_root=settings.disc_root,
    )


@router.post("/disc", response_class=HTMLResponse)
def disc_copy(request: Request, folder: str = Form(default="")) -> HTMLResponse:
    """Kopiert einen Ordner der CD ins Staging.

    Danach ist die Antwort dieselbe wie nach einem Upload, und der restliche
    Weg -- Kandidaten, Auswahl, Import -- unterscheidet sich nicht.
    """
    try:
        directory = disc.resolve_folder(folder)
    except disc.DiscError as exc:
        return _fragment(request, "_error.html", message=str(exc))

    # Wie beim Upload: erst aufräumen, dann rechnen.
    sessions.sweep_expired(settings.session_ttl_hours)

    anzahl, groesse = disc.folder_size(directory)
    if not anzahl:
        return _fragment(
            request,
            "_error.html",
            message="In diesem Ordner liegen keine Audiodateien.",
        )
    if anzahl > settings.max_files:
        return _fragment(
            request,
            "_error.html",
            message=f"Zu viele Dateien ({anzahl}), erlaubt sind "
            f"{settings.max_files}.",
        )

    # Anders als beim Upload steht die Größe vorher fest -- einmal prüfen
    # genügt, es muss nicht häppchenweise mitgezählt werden.
    erlaubt, grenzmeldung = _storage_allowance("Dieser Ordner")
    if groesse > erlaubt:
        return _fragment(request, "_error.html", message=grenzmeldung)

    try:
        session = disc.copy_to_session(directory)
    except disc.DiscError as exc:
        return _fragment(request, "_error.html", message=str(exc))

    return _files_fragment(request, session)


def _rip_fragment(request: Request) -> HTMLResponse:
    """Der Stand des Rips. Solange er läuft, fragt die Seite ihn selbst ab.

    Es gibt ein Laufwerk und damit einen Auftrag, aber zwei Seiten, die ihn
    anzeigen könnten. Ein Hörbuch-Rip gehört nicht hierher: die Musikseite böte
    danach "Dateien" an, die in einer Session liegen sollen -- ein Hörbuch
    schreibt aber direkt in seine Bibliothek.
    """
    job = rip.current()
    return _fragment(
        request,
        "_rip.html",
        job=job if job is not None and job.modus == "musik" else None,
        fremder_auftrag=job is not None and job.modus != "musik" and job.laeuft,
        tools=rip.tools_available(),
        device=settings.cdrom_device,
    )


@router.get("/rip", response_class=HTMLResponse)
def rip_status(request: Request) -> HTMLResponse:
    """Fortschritt eines laufenden Rips -- Ziel des einen vollen Refresh, den
    /rip/events per "sse:fertig" anstößt, sowie des Erst-Ladens."""
    return _rip_fragment(request)


async def _rip_events_strom(request: Request) -> AsyncIterator[str]:
    """Pusht den Rip-Fortschritt, statt dass die Seite ihn abholt.

    Deckt beide Fälle ab, die _rip.html bisher per Polling nachlud: den
    eigenen laufenden Auftrag (Balken alle ~1s) und ein fremdes Laufwerk, das
    gerade für ein Hörbuch belegt ist (nur warten, bis es frei wird). Sobald
    keiner von beidem mehr zutrifft, kommt genau ein "fertig" und die
    Verbindung endet -- den Rest erledigt der eine volle Refresh im Browser.
    """
    while True:
        if await request.is_disconnected():
            return
        job = rip.current()
        # Dieselbe Fallunterscheidung wie in _rip_fragment(): fremder_auftrag
        # blockiert nur das Laufwerk, laeuft-mit-modus-musik ist unser eigener.
        fremder_auftrag = job is not None and job.modus != "musik" and job.laeuft
        eigener_lauf = job is not None and job.modus == "musik" and job.laeuft
        if fremder_auftrag:
            await asyncio.sleep(_SSE_INTERVALL)
            continue
        if eigener_lauf:
            html = templates.get_template("_rip_fortschritt.html").render(job=job)
            yield _sse_event("fortschritt", html)
            await asyncio.sleep(_SSE_INTERVALL)
            continue
        yield _sse_event("fertig")
        return


@router.get("/rip/events")
async def rip_events(request: Request) -> StreamingResponse:
    return StreamingResponse(_rip_events_strom(request), media_type="text/event-stream")


@router.post("/rip", response_class=HTMLResponse)
def rip_start(request: Request, session_id: str = Form(default="")) -> HTMLResponse:
    """Startet den Rip der eingelegten Audio-CD.

    Kehrt sofort zurück; gelesen wird im Hintergrund. Ein Rip dauert 10 bis 40
    Minuten, so lange darf keine Anfrage offen stehen.

    Mit ``session_id`` (vom "Weitere Disc rippen"-Knopf) hängt sich der Rip an
    ein Mehrfach-CD-Album an, statt eine neue Session zu beginnen.
    """
    sessions.sweep_expired(settings.session_ttl_hours)
    erlaubt, grenzmeldung = _storage_allowance("Diese CD")
    if erlaubt <= 0:
        return _fragment(request, "_error.html", message=grenzmeldung)

    try:
        rip.start(allowance=erlaubt, session_id=session_id or None)
    except rip.RipError as exc:
        log.warning("Rip nicht gestartet: %s", exc)
        # Der Fehler steht im Auftrag und wird mit angezeigt.
        return _rip_fragment(request)
    return _rip_fragment(request)


@router.delete("/rip", response_class=HTMLResponse)
def rip_reset(request: Request, sitzung_loeschen: bool = True) -> HTMLResponse:
    """Verwirft einen abgeschlossenen Auftrag, damit die nächste CD kann.

    Normalerweise verschwindet mit dem Auftrag auch die Session -- das ist das
    "Verwerfen" in der Oberfläche. Zwei Fälle sollen das aber *nicht* auslösen:
    eine fehlgeschlagene weitere Disc eines Mehrfach-CD-Albums (die zuvor
    erfolgreich gelesenen Discs sollen nicht mit in den Abfluss gehen, nur weil
    die neueste nicht lesbar war) und ein fertiger Rip, der für ein anderes
    Album zurückgestellt wird, statt gleich weiter gematcht zu werden -- sonst
    gäbe es keinen Weg, das Laufwerk für ein zweites Album freizugeben, ohne
    das erste zu verwerfen. Für beide Fälle schicken "Laufwerk freigeben" und
    "Zurückstellen, Laufwerk freigeben" in ``_rip.html`` ``sitzung_loeschen=false``,
    und die Session bleibt als offene Sitzung stehen.
    """
    job = rip.current()
    try:
        rip.reset()
    except rip.RipError as exc:
        return _fragment(request, "_error.html", message=str(exc))
    if job is not None and job.session_id and sitzung_loeschen:
        sessions.delete_session(job.session_id)
    return _rip_fragment(request)


@router.get("/rip/files", response_class=HTMLResponse)
def rip_files(request: Request) -> HTMLResponse:
    """Die Dateiliste des fertigen Rips -- ab hier wie Upload und Daten-CD."""
    job = rip.current()
    if job is None or not job.session_id:
        return _fragment(
            request, "_error.html", message="Es liegt kein fertiger Rip vor."
        )
    session = _session_or_404(job.session_id)
    return _files_fragment(request, session)


def _buch_belegt(buch: Path) -> str:
    """Arbeitet an diesem Buch schon etwas anderes?

    Zwei Bücher gleichzeitig sind kein Problem -- eines bündeln, das nächste
    rippen, das ist der ganze Sinn. Dasselbe Buch gleichzeitig zerstört sich
    aber gegenseitig: der m4b-Bau räumt am Ende leere Disc-Ordner weg und
    erwischt dabei den Ordner, den der Rip gerade angelegt hat. Nachgestellt --
    der Rip endete mit „No such file or directory".

    Die Prüfung sitzt hier und nicht in den Modulen: nur diese Schicht kennt
    beide Aufträge, und ein Modul, das das andere importiert, wäre eine
    Abhängigkeit, die es sonst nicht braucht.

    Bekannte Lücke: Ein laufendes Cover-Einbetten meldet sich hier nicht an --
    es dauert Sekunden und hat keinen Auftrag, den man abfragen könnte. Zwei
    gleichzeitige Einbettungen lesen beide dieselbe m4b, schreiben je eine
    gültige Datei und die zweite gewinnt; verloren geht dabei nichts. Erst
    wenn daraus ein längerer Vorgang würde, wäre ein eigener Auftrag fällig.
    """
    laufender_bau = audiobook.current_m4b()
    if (
        laufender_bau is not None
        and laufender_bau.laeuft
        and Path(laufender_bau.buch) == buch
    ):
        return (
            "Für dieses Buch läuft gerade der m4b-Bau. Ein zweites Buch "
            "einzulesen ist kein Problem, dasselbe gleichzeitig schon."
        )

    laufender_rip = rip.current()
    if (
        laufender_rip is not None
        and laufender_rip.laeuft
        and laufender_rip.buch
        and Path(laufender_rip.buch) == buch
    ):
        return "Für dieses Buch wird gerade eine Disc eingelesen."
    return ""


def _hoerbuch_job() -> rip.RipJob | None:
    """Der laufende Rip-Auftrag, sofern er zu einem Hörbuch gehört.

    Derselbe Auftragsspeicher wie auf der Musikseite (``rip.current()``) --
    ein Laufwerk, ein Auftrag, nur der Modus entscheidet, wo er hingehört.
    """
    job = rip.current()
    return job if job is not None and job.modus == "hoerbuch" else None


def _audiobook_fragment(
    request: Request, *, meldung: str = "", fehler: str = ""
) -> HTMLResponse:
    """Der Hörbuch-Bereich: Formular, laufende Aufträge, angefangene Bücher."""
    return _fragment(
        request,
        "_audiobook.html",
        job=_hoerbuch_job(),
        m4b=audiobook.current_m4b(),
        buecher=audiobook.list_books(),
        tools={**rip.tools_available(), **audiobook.tools_available()},
        stillstand_minuten=settings.m4b_stillstand // 60,
        root=settings.audiobook_root,
        disc_da=disc.is_available(),
        meldung=meldung,
        fehler=fehler,
    )


@router.get("/audiobook", response_class=HTMLResponse)
def audiobook_status(request: Request) -> HTMLResponse:
    """Stand des Hörbuch-Bereichs -- Ziel des einen vollen Refresh, den
    /audiobook/events per "sse:fertig" anstößt, sowie des Erst-Ladens."""
    return _audiobook_fragment(request)


async def _audiobook_events_strom(request: Request) -> AsyncIterator[str]:
    """Pusht Rip- und m4b-Fortschritt für die Hörbuchseite, statt dass sie
    gepollt wird.

    Rip und Bündeln laufen absichtlich nebeneinander (siehe _audiobook.html)
    -- eine Verbindung bedient deshalb beide, mit eigenen Event-Namen, damit
    jeder Balken für sich nachlädt. list_books() (Dateisystem-Scan) läuft
    hier bewusst nicht mit: das gehört in den einen vollen Refresh nach dem
    "fertig", nicht in jeden Tick.
    """
    while True:
        if await request.is_disconnected():
            return
        job = _hoerbuch_job()
        m4b = audiobook.current_m4b()
        job_laeuft = job is not None and job.laeuft
        m4b_laeuft = m4b is not None and m4b.laeuft
        if not job_laeuft and not m4b_laeuft:
            yield _sse_event("fertig")
            return
        if job_laeuft:
            html = templates.get_template("_audiobook_rip_fortschritt.html").render(
                job=job
            )
            yield _sse_event("fortschritt-rip", html)
        if m4b_laeuft:
            html = templates.get_template("_audiobook_m4b_fortschritt.html").render(
                m4b=m4b, stillstand_minuten=settings.m4b_stillstand // 60
            )
            yield _sse_event("fortschritt-m4b", html)
        await asyncio.sleep(_SSE_INTERVALL)


@router.get("/audiobook/events")
async def audiobook_events(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _audiobook_events_strom(request), media_type="text/event-stream"
    )


@router.post("/audiobook/upload", response_class=HTMLResponse)
async def audiobook_upload(
    request: Request,
    files: list[UploadFile],
    autor: str = Form(default=""),
    titel: str = Form(default=""),
    buch: str = Form(default=""),
) -> HTMLResponse:
    """Übernimmt hochgeladene Hörbuch-Dateien direkt in die Bibliothek.

    Anders als bei Musik gibt es danach weder Match noch beets-Import. Die
    Dateien landen sofort im Buchordner; eine vorhandene Unterordnerstruktur
    bleibt erhalten, damit mehrteilige MP3-Hörbücher in ihrer Reihenfolge
    zusammenbleiben.
    """
    if not files:
        return _audiobook_fragment(request, fehler="Es wurden keine Dateien gesendet.")

    dateien = [
        f for f in files if f.filename and Path(f.filename).suffix.lower() in AUDIO_EXTENSIONS
    ]
    if not dateien:
        return _audiobook_fragment(
            request,
            fehler="Keine Audiodateien in der Auswahl. Unterstützt werden u. a. "
            "FLAC, WAV, AIFF, ALAC, MP3 und AAC.",
        )
    if len(dateien) > settings.max_files:
        return _audiobook_fragment(
            request,
            fehler=f"Zu viele Dateien ({len(dateien)}), erlaubt sind {settings.max_files}.",
        )

    try:
        buchpfad = audiobook.resolve_book(buch) if buch.strip() else audiobook.book_dir(autor, titel)
    except audiobook.AudiobookError as exc:
        return _audiobook_fragment(request, fehler=str(exc))

    belegt = _buch_belegt(buchpfad)
    if belegt:
        return _audiobook_fragment(request, fehler=belegt)

    frei = audiobook.free_bytes() - settings.min_free_bytes
    upload_limit = min(settings.max_upload_bytes, frei)
    if upload_limit <= 0:
        meldung = (
            "Auf dem Hörbuch-Volume ist nicht genug Platz frei."
            if frei <= settings.max_upload_bytes
            else f"Upload überschreitet das Limit von {settings.max_upload_bytes / 1024**3:.1f} GB."
        )
        return _audiobook_fragment(request, fehler=meldung)

    arbeit = audiobook.neuer_arbeitsordner("upload")
    wurzel = arbeit.resolve()
    geschrieben = 0
    gesamtgroesse = 0

    try:
        for upload_datei in dateien:
            relativ = sessions.sanitize_relative_path(upload_datei.filename or "unbenannt")
            ziel = (wurzel / relativ).resolve()
            if not ziel.is_relative_to(wurzel):
                continue

            ziel.parent.mkdir(parents=True, exist_ok=True)
            try:
                with ziel.open("wb") as senke:
                    while chunk := await upload_datei.read(CHUNK_SIZE):
                        gesamtgroesse += len(chunk)
                        if gesamtgroesse > upload_limit:
                            senke.close()
                            shutil.rmtree(arbeit, ignore_errors=True)
                            meldung = (
                                "Auf dem Hörbuch-Volume ist nicht genug Platz frei."
                                if frei <= settings.max_upload_bytes
                                else f"Upload überschreitet das Limit von {settings.max_upload_bytes / 1024**3:.1f} GB."
                            )
                            return _audiobook_fragment(request, fehler=meldung)
                        senke.write(chunk)
                geschrieben += 1
            finally:
                await upload_datei.close()

        if not geschrieben:
            shutil.rmtree(arbeit, ignore_errors=True)
            return _audiobook_fragment(request, fehler="Keine Datei konnte gespeichert werden.")

        audiobook.discs_normalisieren(buchpfad)
        ordner = audiobook.next_disc_dir(buchpfad, ist_datencd=True)
        if ordner == buchpfad and not buchpfad.exists():
            ordner = audiobook.fertigstellen(arbeit, buchpfad)
        else:
            ordner = _einsortieren(arbeit, ordner)
        audiobook.cover_nachziehen_buch(buchpfad)
    except BaseException:
        shutil.rmtree(arbeit, ignore_errors=True)
        raise

    wohin = "ins Buch" if ordner == buchpfad else f"nach {ordner.name}"
    return _audiobook_fragment(
        request,
        meldung=f"{geschrieben} Datei(en) {wohin} kopiert.",
    )


@router.post("/audiobook/rip", response_class=HTMLResponse)
def audiobook_rip(
    request: Request,
    autor: str = Form(default=""),
    titel: str = Form(default=""),
    buch: str = Form(default=""),
    von_vorn: bool = Form(default=False),
) -> HTMLResponse:
    """Liest eine Hörbuch-CD in den Ordner eines Buchs.

    Audio-CD oder Daten-CD entscheidet sich hier: liegt unter ``/disc`` etwas,
    ist es eine Daten-CD und wird kopiert; sonst wird gerippt.

    ``buch`` ist der Weg für Fortsetzungen -- der Pfad eines bereits
    angefangenen Buchs. Autor und Titel müssen dann nicht erneut eingegeben
    werden, und sie werden auch nicht ein zweites Mal entschärft: aus einem
    schon bereinigten Namen könnte sonst ein abweichender Ordner entstehen.
    """
    try:
        buchpfad = (
            audiobook.resolve_book(buch) if buch.strip()
            else audiobook.book_dir(autor, titel)
        )
    except audiobook.AudiobookError as exc:
        return _audiobook_fragment(request, fehler=str(exc))

    belegt = _buch_belegt(buchpfad)
    if belegt:
        return _audiobook_fragment(request, fehler=belegt)

    # Der Platz muss auf dem Dateisystem der Hörbücher frei sein, nicht auf dem
    # des Stagings -- hierhin wird direkt geschrieben.
    frei = audiobook.free_bytes() - settings.min_free_bytes
    if frei <= 0:
        return _audiobook_fragment(
            request, fehler="Auf dem Hörbuch-Volume ist nicht genug Platz frei."
        )

    # „Von vorn" legt die fertige m4b beiseite -- aber erst, wenn das Einlesen
    # wirklich angelaufen ist. Sonst wäre sie nach einem Fehlstart (kein
    # Laufwerk, keine CD) umbenannt, ohne dass etwas passiert ist, und die
    # Fehlermeldung verschluckte den Hinweis darauf.
    def beiseite_legen() -> str:
        if not von_vorn:
            return ""
        weggelegt = audiobook.m4b_beiseite_legen(buchpfad)
        if weggelegt is None:
            return ""
        return (
            f"Die bisherige m4b liegt jetzt als „{weggelegt.name}“ daneben und "
            "wird nicht mehr als Hörbuch gelesen. "
        )

    if disc.is_available():
        return _audiobook_datencd(request, buchpfad, beiseite_legen=beiseite_legen)

    try:
        ordner = audiobook.next_disc_dir(buchpfad)
        rip.start_audiobook(allowance=frei, buch=buchpfad, disc_ordner=ordner)
    except (rip.RipError, audiobook.AudiobookError) as exc:
        log.warning("Hörbuch-Rip nicht gestartet: %s", exc)
        # Nichts umbenannt -- das Buch ist unverändert.
        return _audiobook_fragment(request, fehler=str(exc))
    return _audiobook_fragment(request, meldung=beiseite_legen())


def _einsortieren(arbeit: Path, ziel: Path) -> Path:
    """Schiebt kopierte Dateien an ihren Platz.

    Ist der Zielordner schon da (weitere Disc eines angefangenen Buchs), kann
    der Arbeitsordner nicht einfach umbenannt werden -- dann wandern die
    Dateien einzeln, was auf demselben Dateisystem ebenfalls nur Metadaten
    kostet.
    """
    if not ziel.exists():
        return audiobook.fertigstellen(arbeit, ziel)

    ziel.mkdir(parents=True, exist_ok=True)
    for quelle in sorted(arbeit.rglob("*")):
        if not quelle.is_file():
            continue
        neu = ziel / quelle.relative_to(arbeit)
        neu.parent.mkdir(parents=True, exist_ok=True)
        quelle.rename(neu)
    shutil.rmtree(arbeit, ignore_errors=True)
    return ziel


def _audiobook_datencd(
    request: Request, buch: Path, *, beiseite_legen=None
) -> HTMLResponse:
    """Eine Daten-CD ins Buch kopieren -- kein Rippen nötig.

    ``beiseite_legen`` wird erst nach dem Kopieren aufgerufen: scheitert es,
    soll das Buch unverändert bleiben.
    """
    arbeit = audiobook.neuer_arbeitsordner("datencd")
    try:
        quelle = disc.resolve_folder("")
        # Erst neben die Bibliothek kopieren: 700 MB dauern Minuten, und ein
        # ABS-Scan in dieser Zeit läse ein halbes Buch ein.
        #
        # Rekursiv: Hörbuch-CDs legen ihre Kapitel oft in einen Unterordner
        # ("Disc 1", "CD1", nach dem Titel benannt). Anders als bei Musik gibt
        # es hier keinen Ordner-Auswähler -- eine Hörbuch-CD ist ein Buch.
        anzahl = disc.copy_into(quelle, arbeit, rekursiv=True)

        # Zielnamen erst jetzt bestimmen, unmittelbar vor dem Umbenennen.
        # Vorher aufräumen: liegt schon eine Disc flach im Buchordner, muss sie
        # nach „CD 1", sonst stünde die neue Disc beim Bündeln davor.
        audiobook.discs_normalisieren(buch)
        ordner = audiobook.next_disc_dir(buch, ist_datencd=True)
        if ordner == buch and not buch.exists():
            # Die erste Daten-CD landet flach im Buchordner -- dann ist das
            # Umbenennen des Arbeitsordners genau der Buchordner.
            ordner = audiobook.fertigstellen(arbeit, buch)
        else:
            ordner = _einsortieren(arbeit, ordner)
        audiobook.cover_nachziehen_buch(buch)
    except (disc.DiscError, audiobook.AudiobookError) as exc:
        shutil.rmtree(arbeit, ignore_errors=True)
        return _audiobook_fragment(request, fehler=str(exc))
    hinweis = beiseite_legen() if beiseite_legen else ""
    wohin = "ins Buch" if ordner == buch else f"nach {ordner.name}"
    return _audiobook_fragment(
        request,
        meldung=f"{hinweis}{anzahl} Dateien von der Daten-CD {wohin} kopiert.",
    )


@router.post("/audiobook/m4b", response_class=HTMLResponse)
def audiobook_m4b(
    request: Request,
    buch: str = Form(default=""),
    force: bool = Form(default=False),
    ersetzen: bool = Form(default=False),
    kapitel: str = Form(default=""),
) -> HTMLResponse:
    """Bündelt ein Buch zu einer m4b mit Kapiteln.

    ``kapitel`` ist optional: eine Zeile je Kapitel. Bleibt das Feld leer,
    kommen die Namen aus den Titel-Tags oder werden durchgezählt.
    """
    namen = [z.strip() for z in kapitel.splitlines() if z.strip()] or None
    try:
        pfad = audiobook.resolve_book(buch)
        belegt = _buch_belegt(pfad)
        if belegt:
            return _audiobook_fragment(request, fehler=belegt)
        audiobook.build(pfad, force=force, ersetzen=ersetzen, titel=namen)
    except audiobook.AudiobookError as exc:
        return _audiobook_fragment(request, fehler=str(exc))
    return _audiobook_fragment(request)


@router.delete("/audiobook/rip", response_class=HTMLResponse)
def audiobook_rip_reset(request: Request) -> HTMLResponse:
    """Gibt das Laufwerk nach einem abgeschlossenen Auftrag wieder frei."""
    try:
        rip.reset()
    except rip.RipError as exc:
        return _audiobook_fragment(request, fehler=str(exc))
    return _audiobook_fragment(request)


@router.delete("/audiobook/m4b", response_class=HTMLResponse)
def audiobook_m4b_reset(request: Request) -> HTMLResponse:
    try:
        audiobook.reset_m4b()
    except audiobook.AudiobookError as exc:
        return _audiobook_fragment(request, fehler=str(exc))
    return _audiobook_fragment(request)


@router.post("/audiobook/m4b/abbruch", response_class=HTMLResponse)
def audiobook_m4b_abbruch(request: Request) -> HTMLResponse:
    """Beendet einen laufenden m4b-Bau.

    Der Ausweg für den Fall, dass ffmpeg steht: vorher blieb nur, den Container
    neu zu starten, weil ein Auftrag auf „läuft" das Buch dauerhaft sperrte.
    """
    try:
        hinweis = audiobook.abbrechen_m4b()
    except audiobook.AudiobookError as exc:
        return _audiobook_fragment(request, fehler=str(exc))
    return _audiobook_fragment(request, meldung=hinweis)


@router.post("/cover/audiobook", response_class=HTMLResponse)
async def cover_audiobook(
    request: Request, buch: str = "", bild: UploadFile = File(...)
) -> HTMLResponse:
    """Nimmt ein abfotografiertes Cover für ein Hörbuch entgegen.

    ``buch`` kommt als Abfrageparameter, nicht als Formularfeld: der Knopf in
    der Buchliste baut damit die Ziel-Adresse, und das Formular trägt nur das
    Bild.
    """
    try:
        pfad = audiobook.resolve_book(buch)
        bild_pfad = cover.speichern(pfad, await bild.read())
    except (audiobook.AudiobookError, cover.CoverError) as exc:
        return _audiobook_fragment(request, fehler=str(exc))

    # Vor dem Bündeln reicht die Bilddatei im Ordner -- der Encode nimmt sie
    # mit. Ist die m4b schon gebaut, sind die Quellen gelöscht und die Datei
    # ist alles, was es noch gibt: dann muss das Bild hinein.
    if not audiobook.m4b_pfad(pfad).is_file():
        return _audiobook_fragment(request, meldung="Cover übernommen.")

    # Dieselbe Sperre wie beim Rippen und Bündeln. Ohne sie könnte ein „Neu
    # bauen" dieselbe Datei unter den Händen wegziehen -- und ein doppelter
    # Klick zwei Läufe auf derselben m4b starten.
    if belegt := _buch_belegt(pfad):
        return _audiobook_fragment(
            request,
            fehler=f"{belegt} Das Bild liegt im Buchordner und geht nicht "
            "verloren; bitte danach noch einmal übernehmen.",
        )

    try:
        meldung = audiobook.cover_einbetten(pfad, bild_pfad)
    except audiobook.AudiobookError as exc:
        return _audiobook_fragment(request, fehler=str(exc))
    return _audiobook_fragment(request, meldung=meldung)


@router.get("/audiobook/cover")
def audiobook_cover(buch: str = "", v: str = "") -> FileResponse:
    """Liefert das Coverbild eines Buchs für die Liste aus.

    ``v`` trägt die Änderungszeit und wird nicht ausgewertet -- es steht nur in
    der Adresse, damit ein neu fotografiertes Cover eine andere ergibt. Deshalb
    darf das Bild hier als unveränderlich gelten: Der Browser holt es einmal
    und danach nie wieder, zeigt aber trotzdem sofort das neue.

    Der Pfad kommt aus dem Formular, also durch ``resolve_book`` -- ein
    Endpunkt, der eine Datei zu einem übergebenen Pfad ausliefert, ist sonst
    die Einladung, damit aus der Bibliothek herauszulaufen.
    """
    try:
        pfad = audiobook.resolve_book(buch)
    except audiobook.AudiobookError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    bild = audiobook.cover_pfad(pfad)
    if bild is None:
        raise HTTPException(status_code=404, detail="Für dieses Buch gibt es kein Cover.")

    return FileResponse(
        bild,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get("/cover/session/{session_id}")
def session_cover(session_id: str, v: str = "") -> FileResponse:
    """Liefert das Coverbild einer Upload-Session aus.

    ``v`` trägt die Änderungszeit in der Adresse, damit der Browser ein neu
    fotografiertes Cover sofort neu lädt und das alte sonst aggressiv cachen
    darf.
    """
    session = _session_or_404(session_id)
    bild = _session_cover_path(session)
    if not bild.is_file():
        raise HTTPException(status_code=404, detail="Für diese Sitzung gibt es kein Cover.")
    return FileResponse(
        bild,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/cover/session/{session_id}", response_class=HTMLResponse)
async def cover_session(
    request: Request,
    session_id: str,
    bild: UploadFile = File(...),
    box_id: str = Query(default="files"),
) -> HTMLResponse:
    """Nimmt ein Cover für eine laufende Musik-Session entgegen.

    Es landet als ``cover.jpg`` neben den Audiodateien; beim Import zieht beets
    es über ``fetchart`` heran, statt selbst eines zu suchen.

    ``box_id`` unterscheidet, von wo aus aufgenommen wurde: der Standardfall
    ``"files"`` (Schritt 2) tauscht die ganze Dateiliste neu ein, jeder andere
    Wert (z. B. aus Schritt 4, wo ohne MusicBrainz-Release sonst kein Cover
    nachgeladen würde) nur die kleine Cover-Box selbst -- ``coverAufnehmen``
    im Browser ersetzt ohnehin nur das Element mit genau dieser ID.
    """
    session = _session_or_404(session_id)
    try:
        cover.speichern(session.directory, await bild.read())
    except cover.CoverError as exc:
        return _fragment(request, "_error.html", message=str(exc))
    if box_id != "files":
        return _fragment(
            request,
            "_cover_status.html",
            session_id=session_id,
            box_id=box_id,
            cover_titel="Cover für dieses Album",
            cover_present=True,
            cover_version=_session_cover_version(session),
        )
    return _files_fragment(request, session)


@router.post("/match/{session_id}", response_class=HTMLResponse)
def match(
    request: Request,
    session_id: str,
    mbid: str = Form(default=""),
    artist: str = Form(default=""),
    album: str = Form(default=""),
) -> HTMLResponse:
    """Sucht Match-Kandidaten und zeigt sie mit Sicherheit und Lücken an."""
    session = _session_or_404(session_id)
    paths = session.audio_paths
    if not paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")

    resolved_mbid = matching.extract_mbid(mbid) if mbid.strip() else None
    if mbid.strip() and not resolved_mbid:
        return _fragment(
            request,
            "_error.html",
            message="Das sieht nicht nach einer MusicBrainz-ID aus. Erwartet wird "
            "eine Release-ID oder eine musicbrainz.org-Adresse.",
        )

    result = matching.find_candidates(
        paths,
        mbid=resolved_mbid,
        artist=artist.strip() or None,
        album=album.strip() or None,
    )
    # _candidates.html bindet _manual.html mit ein -- ohne den Entwurf hier
    # würde ein Klick auf "Matches suchen" die schon eingetippten Handtagging-
    # Felder unsichtbar machen (der nächste Autosave-Tick hätte sie dann auch
    # im Entwurf überschrieben).
    draft = sessions.load_draft(session)
    return _fragment(
        request,
        "_candidates.html",
        session_id=session_id,
        result=result,
        file_count=len(paths),
        parse_flags=_parse_flags(draft),
        ocr_text=str(draft.get("ocr_text") or ""),
        ocr_warnings=[],
        track_inputs=_track_inputs(session, draft=draft),
        draft=draft,
        genre_vorschlaege=genres.katalog(),
        manual_basis_zusatz=_MANUAL_ALBUM_BASIS_ZUSATZ,
        manual_erweitert=_MANUAL_ALBUM_ERWEITERT,
        ocr_overlay=None,
    )


@router.post("/choose/{session_id}", response_class=HTMLResponse)
def choose(
    request: Request,
    session_id: str,
    album_id: str = Form(...),
    from_scratch: str = Form(default=""),
) -> HTMLResponse:
    """Übernimmt einen Kandidaten: schreibt dessen Tags in die Dateien.

    Danach ist der Import nur noch ein Verschieben -- beets taggt nicht erneut.
    """
    session = _session_or_404(session_id)
    paths = session.audio_paths
    if not paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")

    resolved = matching.extract_mbid(album_id) or album_id
    match_obj = matching.find_candidate_by_id(paths, resolved, mbid=resolved)
    if match_obj is None:
        return _fragment(
            request,
            "_error.html",
            message="Der gewählte Kandidat konnte nicht erneut geladen werden. "
            "Bitte die Suche wiederholen.",
        )

    write_result = tagging.apply_album_match(
        match_obj, from_scratch=bool(from_scratch.strip())
    )

    # MusicBrainz-Kandidaten bekommen ihr Cover automatisch beim Import (die
    # 'coverart'-Quelle von fetchart, über die geschriebene Release-ID). Für
    # andere Quellen wie Discogs gibt es diesen Weg nicht -- siehe
    # cover.von_url_holen(). Ein vorhandenes, abfotografiertes Cover geht
    # immer vor.
    if not cover.vorhanden(session.directory):
        cover_url = match_obj.info.get("cover_art_url")
        if cover_url:
            cover.von_url_holen(session.directory, cover_url)

    return _fragment(
        request,
        "_applied.html",
        session_id=session_id,
        result=write_result,
        candidate=matching.serialize_candidate(match_obj, 0),
        health=beets_env.health(),
        cover_present=cover.vorhanden(session.directory),
        cover_version=_session_cover_version(session),
    )


@router.post("/ocr/{session_id}", response_class=HTMLResponse)
async def ocr_backcover(
    request: Request,
    session_id: str,
    bild: UploadFile | None = File(default=None),
    tracknummer: bool = Form(default=False),
    feld1: str = Form(default=""),
    feld2: str = Form(default=""),
    feld3: str = Form(default=""),
    zeilenweise: bool = Form(default=False),
    trenner: str = Form(default=" - "),
    dauer: bool = Form(default=False),
) -> HTMLResponse:
    """Liest Text aus einem Backcover-Bild und füllt die Trackliste vor."""
    session = _session_or_404(session_id)
    warnings: list[str] = []
    flags = trackparse.ParseFlags(
        tracknummer=tracknummer,
        dauer=dauer,
        felder=_felder(feld1, feld2, feld3),
        zeilenweise=zeilenweise,
        trenner=trenner or " - ",
    )

    if bild is None:
        warnings.append("Bitte zuerst ein Backcover-Foto auswählen.")
        return _fragment(
            request,
            "_manual_ocr.html",
            session_id=session_id,
            parse_flags=flags,
            ocr_text="",
            ocr_warnings=warnings,
            track_inputs=_track_inputs(session),
            ocr_overlay=None,
        )

    suffix = Path(bild.filename or "bild.jpg").suffix or ".jpg"
    payload = await bild.read()
    log.info(
        "Backcover-OCR angefordert | session=%s | datei=%s | groesse_kb=%d | parser=%s",
        session_id,
        bild.filename or "bild.jpg",
        len(payload) // 1024,
        flags,
    )
    try:
        result = ocr.recognize(payload, suffix=suffix)
    except ocr.OcrError as exc:
        warnings.append(str(exc))
        return _fragment(
            request,
            "_manual_ocr.html",
            session_id=session_id,
            parse_flags=flags,
            ocr_text="",
            ocr_warnings=warnings,
            track_inputs=_track_inputs(session),
            ocr_overlay=None,
        )

    parsed = trackparse.parse_text(result.text, flags)
    image_bytes = result.preview_bytes or payload
    image_type = result.preview_content_type or (
        bild.content_type
        if bild.content_type and bild.content_type.startswith("image/")
        else "image/jpeg"
    )
    image_url = f"data:{image_type};base64,{base64.b64encode(image_bytes).decode('ascii')}"
    overlay = _ocr_overlay(result, image_url)
    log.info(
        "Backcover-OCR fertig | session=%s | textzeilen=%d | parser_tracks=%d",
        session_id,
        len(result.lines),
        len(parsed),
    )
    warnings.extend(result.warnings)
    if len(parsed) != len(session.audio_paths):
        warnings.append(_track_zahl_warnung(len(parsed), len(session.audio_paths), flags))

    rows = _track_inputs(session, parsed)
    _entwurf_nach_ocr_lauf(session, result.text, flags, rows)
    return _fragment(
        request,
        "_manual_ocr.html",
        session_id=session_id,
        parse_flags=flags,
        ocr_text=result.text,
        ocr_warnings=warnings,
        track_inputs=rows,
        ocr_overlay=overlay,
    )


@router.post("/ocr/parse/{session_id}", response_class=HTMLResponse)
async def ocr_parse(
    request: Request,
    session_id: str,
    ocr_text: str = Form(default=""),
    tracknummer: bool = Form(default=False),
    feld1: str = Form(default=""),
    feld2: str = Form(default=""),
    feld3: str = Form(default=""),
    zeilenweise: bool = Form(default=False),
    trenner: str = Form(default=" - "),
    dauer: bool = Form(default=False),
) -> HTMLResponse:
    """Wendet die gewählten Parser-Schalter auf den OCR-Text an."""
    session = _session_or_404(session_id)
    flags = trackparse.ParseFlags(
        tracknummer=tracknummer,
        dauer=dauer,
        felder=_felder(feld1, feld2, feld3),
        zeilenweise=zeilenweise,
        trenner=trenner or " - ",
    )

    warnings: list[str] = []
    text = ocr_text.strip()
    parsed = trackparse.parse_text(text, flags) if text else []
    if not text:
        warnings.append("Noch kein OCR-Text vorhanden.")
    elif len(parsed) != len(session.audio_paths):
        warnings.append(_track_zahl_warnung(len(parsed), len(session.audio_paths), flags))

    rows = _track_inputs(session, parsed)
    if text:
        _entwurf_nach_ocr_lauf(session, text, flags, rows)
    return _fragment(
        request,
        "_manual_ocr.html",
        session_id=session_id,
        parse_flags=flags,
        ocr_text=text,
        ocr_warnings=warnings,
        track_inputs=rows,
        ocr_overlay=None,
    )


@router.post("/artist-match/{session_id}", response_class=HTMLResponse)
async def artist_match(
    request: Request,
    session_id: str,
    field: str = Form(...),
    name: str = Form(default=""),
) -> HTMLResponse:
    session = _session_or_404(session_id)
    if not session.audio_paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")

    # "interpret:<Dateiname>" ist derselbe Künstler-Lookup, nur je Track statt
    # einmal fürs ganze Album -- der Feldname trägt die Dateikennung schon in
    # sich, weil jede Tabellenzeile ihre eigene Trefferliste braucht. Die
    # Dateikennung muss dabei wirklich zu dieser Sitzung gehören, sonst könnte
    # ein beliebiger Feldname hier eine MusicBrainz-Anfrage auslösen.
    gueltiges_feld = field in {"albumartist", "artist"}
    if not gueltiges_feld and field.startswith("interpret:"):
        dateiname = field.partition(":")[2]
        gueltiges_feld = any(datei["key"] == dateiname for datei in _track_files(session))

    formular = await request.form()
    query = name.strip()
    if not query and gueltiges_feld:
        query = str(formular.get(field) or "").strip()

    # " / "-getrennte Namen (Kollaboration, Chor + Dirigent, ...) suchen wir
    # einzeln -- eine kombinierte MusicBrainz-Suche nach dem ganzen String
    # fände so gut wie nie einen Treffer.
    namen = tagging.kuenstlerliste(query) if query else []
    namen_treffer: list[dict[str, object]] = []
    for einzelname in namen:
        einzel_fehlgeschlagen = False
        einzel_matches: tuple[artist_ids.ArtistMatch, ...] = ()
        try:
            einzel_matches = artist_ids.search(einzelname)
        except artist_ids.LookupFehlgeschlagen:
            einzel_fehlgeschlagen = True
        namen_treffer.append(
            {
                "name": einzelname,
                "matches": einzel_matches,
                "exact_count": sum(1 for match in einzel_matches if match.exact),
                "lookup_failed": einzel_fehlgeschlagen,
            }
        )

    # Der einfache Fall (genau ein Name) behält seine bisherigen, flachen
    # Variablen -- unverändert gegenüber vorher, damit sich am gewohnten
    # Ein-Künstler-Weg nichts verschiebt.
    einzel = namen_treffer[0] if len(namen_treffer) == 1 else None
    return _fragment(
        request,
        "_artist_matches.html",
        field=field,
        field_label=_field_label(field),
        mbid_field=_mbid_field(field),
        query=query,
        matches=einzel["matches"] if einzel else (),
        exact_count=einzel["exact_count"] if einzel else 0,
        lookup_failed=einzel["lookup_failed"] if einzel else False,
        namen_treffer=namen_treffer,
        mehrfach=len(namen_treffer) > 1,
        invalid_field=not gueltiges_feld,
    )


@router.post("/entwurf/{session_id}", response_class=HTMLResponse)
async def entwurf_speichern(request: Request, session_id: str) -> HTMLResponse:
    """Sichert das Handtagging-Formular zwischendurch, im Hintergrund.

    Schreibt keinen Datei-Tag -- nur Klartext in einen Sitzungs-Ordner, damit
    eine unterbrochene Sitzung die halb ausgefüllten Felder nicht kostet.
    Antwortet absichtlich leer (``hx-swap="none"``); ein Fehler hier soll das
    Tippen nicht stören.
    """
    session = _session_or_404(session_id)
    formular = await request.form()
    # "not bild" in hx-params hält Dateifelder normalerweise schon draußen;
    # nur Text landet im Entwurf -- ein Upload wird nicht zwischengesichert.
    felder = {
        str(schluessel): wert.strip()
        for schluessel, wert in formular.items()
        if isinstance(wert, str) and wert.strip()
    }
    sessions.save_draft(session, felder)
    return HTMLResponse("")


@router.post("/manual-start/{session_id}", response_class=HTMLResponse)
def manual_start(request: Request, session_id: str) -> HTMLResponse:
    """Direkter Einstieg ins Handtagging, ohne MusicBrainz-Suche.

    Für den Fall, dass schon vorher klar ist, dass dort nichts zu finden sein
    wird -- sonst würde "Matches suchen" hier nur eine MusicBrainz-Anfrage
    verbraten, die absehbar mit "Kein Treffer" endet.
    """
    session = _session_or_404(session_id)
    if not session.audio_paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")
    draft = sessions.load_draft(session)
    return _fragment(
        request,
        "_manual_start.html",
        session_id=session_id,
        parse_flags=_parse_flags(draft),
        ocr_text=str(draft.get("ocr_text") or ""),
        ocr_warnings=[],
        track_inputs=_track_inputs(session, draft=draft),
        draft=draft,
        genre_vorschlaege=genres.katalog(),
        manual_basis_zusatz=_MANUAL_ALBUM_BASIS_ZUSATZ,
        manual_erweitert=_MANUAL_ALBUM_ERWEITERT,
        ocr_overlay=None,
    )


@router.post("/manual/{session_id}", response_class=HTMLResponse)
async def manual(
    request: Request,
    session_id: str,
    albumartist: str = Form(default=""),
    album: str = Form(default=""),
    artist: str = Form(default=""),
    composer: str = Form(default=""),
    year: str = Form(default=""),
    genre: str = Form(default=""),
    compilation: bool = Form(default=False),
) -> HTMLResponse:
    """Schreibt selbst eingetragene Tags, wenn kein Match passt.

    Neben den albumweiten Feldern kommen Titel, Interpret, Komponist und
    Tracknummer je Datei an -- als ``titel:<Dateiname>``,
    ``interpret:<Dateiname>``, ``komponist:<Dateiname>`` und
    ``nr:<Dateiname>``. Eine Sampler-CD braucht Titel/Interpret je Zeile:
    dort hat jeder Track einen anderen Interpreten, während der
    Albumkünstler „Various Artists" bleibt und das Compilation-Flag die
    Tracks zusammenhält. Der Komponist je Track deckt den Klassik-Fall ab, in
    dem eine Compilation mehrere Komponisten mischt -- das albumweite Feld
    gilt dort für keinen Track richtig; ohne eigenen Eintrag je Zeile bleibt
    es aber weiter die Vorbelegung. Die Tracknummer korrigiert eine falsch
    erkannte Reihenfolge, statt sich auf die Position in der Dateiliste zu
    verlassen. ``mbinterpret:<Dateiname>`` trägt die per Lupe-Suche bestätigte
    Artist-ID -- ohne sie greift beim Schreiben pro Track derselbe stille
    Exakt-Treffer-Abgleich wie beim albumweiten Künstler.
    """
    session = _session_or_404(session_id)
    paths = session.audio_paths
    if not paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")

    formular = await request.form()
    je_track: dict[str, dict[str, str]] = {}
    for schluessel, wert in formular.items():
        praefix, _, dateiname = str(schluessel).partition(":")
        feld = {
            "titel": "title",
            "interpret": "artists",
            "komponist": "composers",
            "mbinterpret": "mb_artistids",
            "nr": "track",
        }.get(praefix)
        if feld and dateiname and str(wert).strip():
            je_track.setdefault(dateiname, {})[feld] = str(wert)

    # Handgetaggt heißt: kein MusicBrainz-Treffer übernommen. Dann bestehen wir
    # auf Mindestangaben, statt eine Datei mit im Grunde leeren Metadaten
    # durchzulassen. Beim Sampler sagt "Various Artists" beim Albumkünstler
    # nichts über die tatsächlichen Interpreten aus -- dort zählt stattdessen
    # jede Zeile in "Titel je Track" für sich.
    fehlende: list[str] = []
    if not genre.strip():
        fehlende.append("Genre")
    if not year.strip():
        fehlende.append("Jahr")
    if compilation:
        ohne_interpret = [
            datei["key"]
            for datei in _track_files(session)
            if not str(je_track.get(datei["key"], {}).get("artists", "")).strip()
        ]
        if ohne_interpret:
            fehlende.append("Track-Künstler für: " + ", ".join(ohne_interpret))
    elif not albumartist.strip():
        fehlende.append("Albumkünstler")
    if fehlende:
        return _fragment(
            request,
            "_error.html",
            message="Ohne MusicBrainz-Treffer bitte noch ausfüllen: " + "; ".join(fehlende),
        )

    felder = {
        "albumartist": albumartist,
        "mb_albumartistids": str(formular.get("mb_albumartistids") or "").strip(),
        "album": album,
        "artists": artist,
        "mb_artistids": str(formular.get("mb_artistids") or "").strip(),
        "composers": composer,
        "year": year,
        # In beets 2.x heißt das Feld "genres"; als "genre" gesetzt landete
        # es früher nur als flexibles Attribut und nie in der Datei.
        "genres": genre,
        "comp": compilation,
    }
    # Restlicher Katalog (Label, Katalognummer, Album-Typ, ...) -- der
    # Formularname entspricht hier direkt dem Katalog-Schlüssel, eine
    # eigene Übersetzungstabelle wie oben braucht es nur für die historisch
    # anders benannten Basis-Felder.
    for katalog_feld in _MANUAL_ALBUM_BASIS_ZUSATZ + _MANUAL_ALBUM_ERWEITERT:
        felder[katalog_feld.key] = str(formular.get(katalog_feld.key) or "").strip()

    write_result = tagging.apply_manual_tags(
        paths,
        felder,
        je_track=je_track,
        relative_to=session.directory,
    )
    if not write_result.written and not write_result.failed:
        return _fragment(
            request,
            "_error.html",
            message="Es wurde kein Feld ausgefüllt -- nichts zu schreiben.",
        )
    # Die Felder stehen jetzt als echte Datei-Tags fest -- der Entwurf hat
    # seinen Zweck erfüllt und würde beim nächsten Öffnen nur noch verwirren.
    sessions.delete_draft(session)
    return _fragment(
        request,
        "_applied.html",
        session_id=session_id,
        result=write_result,
        candidate=None,
        health=beets_env.health(),
        cover_present=cover.vorhanden(session.directory),
        cover_version=_session_cover_version(session),
    )


@router.post("/import/{session_id}", response_class=HTMLResponse)
def run_import(
    request: Request,
    session_id: str,
    pretend: str = Form(default=""),
) -> HTMLResponse:
    """Übergibt den Staging-Ordner an das beets des Servers.

    Mit ``pretend`` wird nur gezeigt, was passieren würde -- nichts wird
    verschoben und nichts in die Library eingetragen.
    """
    session = _session_or_404(session_id)
    dry_run = bool(pretend.strip())

    health = beets_env.health()
    if not health["import_ready"] and not dry_run:
        return _fragment(
            request,
            "_error.html",
            message="Import ist gesperrt: " + " ".join(str(p) for p in health["problems"]),
        )

    # Vor dem Import lesen: hinterher liegt an diesem Ort nichts mehr
    # (verschoben oder kopiert), siehe _mb_albumid_der_session().
    mb_albumid = None if dry_run else _mb_albumid_der_session(session)

    result = importer.run_import(session.directory, pretend=dry_run)
    if result.ok and not dry_run:
        # Bestcase-Versuch: die Cover Art Archive antwortet gelegentlich mit
        # einem transienten Fehler, den fetchart nicht selbst wiederholt --
        # siehe albums.retry_missing_cover(). Verzögert die Rückmeldung nur,
        # wenn tatsächlich ein Cover fehlt; der Normalfall (Cover schon da)
        # kostet nichts außer einer einzelnen beet-list-Abfrage.
        #
        # Fehlt mb_albumid, läuft der Nachschlag gar nicht erst -- ohne
        # dieses Log wäre "kein Cover trotz MusicBrainz-Match" von "gar kein
        # MusicBrainz-Match für diese Session" im Log nicht zu unterscheiden.
        if mb_albumid:
            albums.retry_missing_cover(mb_albumid)
        else:
            log.info(
                "Kein Cover-Nachschlag für Session %s: keine MusicBrainz-Release-ID "
                "(as-is-Import ohne Match oder Discogs-Kandidat).",
                session_id,
            )

        # Nach einem Verschiebe-Import ist der Ordner leer; dann kann er weg.
        sessions.cleanup_if_empty(session)

        # Stammte diese Session von einem Rip, ist der Auftrag jetzt Geschichte
        # -- sonst zeigt der Audio-CD-Reiter beim nächsten Öffnen wieder den
        # Kopf der längst importierten Session an, bis jemand von Hand auf
        # "Verwerfen" drückt.
        job = rip.current()
        if job is not None and not job.laeuft and job.session_id == session_id:
            rip.reset()

    return _fragment(
        request,
        "_import.html",
        session_id=session_id,
        result=result,
    )


@router.delete("/session/{session_id}", response_class=HTMLResponse)
def discard(request: Request, session_id: str) -> HTMLResponse:
    """Verwirft einen Upload samt Dateien."""
    _session_or_404(session_id)
    sessions.delete_session(session_id)
    return HTMLResponse("")


@router.get("/health", response_class=HTMLResponse)
def health_fragment(request: Request) -> HTMLResponse:
    """Zustand der beets-Anbindung, für das Banner oben auf der Seite."""
    return _fragment(request, "_health.html", health=beets_env.health())


def _alben_oder_fehler(q: str = "") -> tuple[list[albums.Album], str]:
    try:
        return albums.list_albums(q), ""
    except albums.AlbumError as exc:
        return [], str(exc)


@router.get("/albums", response_class=HTMLResponse)
def album_list(request: Request, q: str = "") -> HTMLResponse:
    """Bereits importierte Alben durchsuchen.

    Die Liste selbst kommt erst per HTMX nach (``/albums/liste``) -- sie geht
    über den ``beet``-Subprozess und damit über die gefüllte Library, das
    dauert spürbar. Ungebremst hätte das jede Navigation auf diese Seite
    blockiert, auch wenn man nur vorbeischaut. So steht das Gerüst sofort,
    die Liste blendet mit Ladeanzeige nach.
    """
    liste_url = "/albums/liste"
    if q:
        liste_url += "?" + urlencode({"q": q})
    return _seite(request, "albums.html", "alben", q=q, liste_url=liste_url)


@router.get("/albums/liste", response_class=HTMLResponse)
def album_list_fragment(request: Request, q: str = "") -> HTMLResponse:
    """Nur die Albentabelle -- Ziel des lazy-load auf ``/albums``."""
    treffer, fehler = _alben_oder_fehler(q)
    return _fragment(request, "_albums_liste.html", alben=treffer, fehler=fehler)


def _album_mit_tracks(album_id: int) -> tuple[albums.Album, list[albums.Track], str]:
    """Ein Album samt Titeln -- 404, wenn es das Album gar nicht gibt.

    Fehlschläge beim Nachladen der Titel (Subprozess kaputt) sind dagegen kein
    404: das Album selbst ist ja da, nur seine Titelliste bleibt leer und die
    Fehlermeldung geht als ``fehler`` an die Seite statt als HTTP-Fehler.
    """
    album = albums.get_album(album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album nicht gefunden.")
    try:
        return album, albums.list_tracks(album_id), ""
    except albums.AlbumError as exc:
        return album, [], str(exc)


def _album_detail_fragment(
    request: Request, album_id: int, fehler: str = ""
) -> HTMLResponse:
    album, tracks, listen_fehler = _album_mit_tracks(album_id)
    return _fragment(
        request,
        "_album_detail.html",
        album=album,
        tracks=tracks,
        fehler=fehler or listen_fehler,
        genre_vorschlaege=genres.katalog(),
        album_gruppen=tag_catalog.ALBUM_GRUPPEN,
        track_gruppen=tag_catalog.TRACK_GRUPPEN,
        track_feld_nummer=tag_catalog.TRACK_FELDER_NACH_KEY["track"],
        track_feld_titel=tag_catalog.TRACK_FELDER_NACH_KEY["title"],
        track_feld_komponist=tag_catalog.TRACK_FELDER_NACH_KEY["composers"],
    )


@router.get("/albums/{album_id}", response_class=HTMLResponse)
def album_detail(request: Request, album_id: int) -> HTMLResponse:
    """Ein einzelnes Album: alle gesetzten Tags, alle Titel, Cover ändern."""
    album, tracks, fehler = _album_mit_tracks(album_id)
    return _seite(
        request,
        "album_detail.html",
        "alben",
        album=album,
        tracks=tracks,
        fehler=fehler,
        genre_vorschlaege=genres.katalog(),
        album_gruppen=tag_catalog.ALBUM_GRUPPEN,
        track_gruppen=tag_catalog.TRACK_GRUPPEN,
        track_feld_nummer=tag_catalog.TRACK_FELDER_NACH_KEY["track"],
        track_feld_titel=tag_catalog.TRACK_FELDER_NACH_KEY["title"],
        track_feld_komponist=tag_catalog.TRACK_FELDER_NACH_KEY["composers"],
    )


@router.get("/cover/album/{album_id}")
def album_cover(album_id: int, v: str = "") -> FileResponse:
    """Liefert das Coverbild eines Albums aus.

    ``v`` trägt die Änderungszeit in der Adresse, damit der Browser ein neues
    Cover sofort lädt und ein altes sonst aggressiv cachen darf.
    """
    album = albums.get_album(album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album nicht gefunden.")
    pfad = album.cover_path
    if pfad is None:
        raise HTTPException(
            status_code=404, detail="Für dieses Album gibt es kein Cover."
        )
    return FileResponse(
        pfad,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.post("/cover/album/{album_id}", response_class=HTMLResponse)
async def update_album_cover(
    request: Request, album_id: int, bild: UploadFile = File(...)
) -> HTMLResponse:
    """Nimmt ein neues Cover für ein bereits importiertes Album entgegen.

    Landet als ``cover.jpg`` im Albumordner und wird zusätzlich über
    ``beet embedart`` in die vorhandenen Dateien eingebettet -- die tragen ihr
    altes Cover sonst weiter in den eigenen Tags, ein Neuimport findet für ein
    schon importiertes Album ja nicht mehr statt.
    """
    album = albums.get_album(album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album nicht gefunden.")
    try:
        bild_pfad = cover.speichern(album.path, await bild.read())
        # Nur hier, nicht in cover.speichern() selbst: ein von fetchart
        # heruntergeladenes 'cover.png' kann ausschließlich im Album-Ordner
        # einer schon importierten Library liegen, siehe cover.py.
        cover.andere_erweiterungen_entfernen(album.path)
        albums.update_cover(album, bild_pfad)
    except (cover.CoverError, albums.AlbumError) as exc:
        return _album_detail_fragment(request, album_id, fehler=str(exc))
    return _album_detail_fragment(request, album_id)


def _mb_matches_fragment(
    request: Request, query: str, apply_url: str, target: str
) -> HTMLResponse:
    """Sucht Künstler auf MusicBrainz und rendert die Treffer als Formulare.

    Ein Fragment für Album- und Track-Interpret: beide sind „Namen eingeben,
    Treffer sehen, mit einem Klick eine MBID übernehmen" -- nur das Ziel des
    Übernehmen-Formulars unterscheidet sich.
    """
    treffer = artist_ids.search(query) if query else ()
    return _fragment(
        request,
        "_mb_matches.html",
        query=query,
        matches=treffer,
        apply_url=apply_url,
        target=target,
    )


@router.post("/albums/{album_id}/artist-lookup/{index}", response_class=HTMLResponse)
def album_artist_lookup(
    request: Request, album_id: int, index: int, name: str = Form(default="")
) -> HTMLResponse:
    """MusicBrainz-Kandidaten für einen Album-Interpreten.

    ``index`` ist die Position in der Interpretenliste (bei "A feat. B" mehr
    als eine) -- so bleibt bei mehreren Interpreten klar, welchen der Treffer
    am Ende verknüpft.
    """
    return _mb_matches_fragment(
        request, name.strip(), f"/albums/{album_id}/artist-apply/{index}", "#album-detail"
    )


@router.post("/albums/{album_id}/artist-apply/{index}", response_class=HTMLResponse)
def album_artist_apply(
    request: Request, album_id: int, index: int, mbid: str = Form(...)
) -> HTMLResponse:
    """Verknüpft einen Album-Interpreten (per Position) mit der gewählten MBID."""
    album = albums.get_album(album_id)
    if album is None:
        raise HTTPException(status_code=404, detail="Album nicht gefunden.")
    try:
        albums.set_album_artist_mbid(album, index, mbid)
    except albums.AlbumError as exc:
        return _album_detail_fragment(request, album_id, fehler=str(exc))
    return _album_detail_fragment(request, album_id)


@router.post("/albums/{album_id}/save", response_class=HTMLResponse)
async def album_save(request: Request, album_id: int) -> HTMLResponse:
    """Schreibt alle geänderten Katalogfelder auf einmal -- Album- und
    Titel-Ebene zusammen, ausgelöst durch den "Speichern"-Knopf am Ende der
    Detailseite. Anders als früher speichert kein Feld mehr für sich bei
    jeder Änderung; static/index.js filtert unveränderte Felder schon vor
    dem Absenden heraus (htmx:configRequest), hier kommen also nur
    tatsächlich geänderte Werte an.

    Formularschlüssel: ``album:<feld>`` für Album-Felder, ``track:<id>:<feld>``
    für Titel-Felder -- ein Präfix reicht, weil ein Speichern-Klick über
    mehrere Titel hinweg schreiben können muss. Unbekannte oder unlesbare
    Schlüssel werden übersprungen statt den ganzen Aufruf abzubrechen --
    kommen bei einem regulär bedienten Formular ohnehin nie vor.
    """
    alb = albums.get_album(album_id)
    if alb is None:
        raise HTTPException(status_code=404, detail="Album nicht gefunden.")

    formular = await request.form()
    fehler: list[str] = []
    tracks: dict[int, albums.Track | None] = {}

    for schluessel, roh_wert in formular.items():
        if not isinstance(roh_wert, str):
            continue
        wert = roh_wert

        if schluessel.startswith("album:"):
            katalog_feld = tag_catalog.ALBUM_FELDER_NACH_KEY.get(schluessel[len("album:") :])
            if katalog_feld is None:
                continue
            try:
                if katalog_feld.kuenstler_link:
                    albums.set_album_interpret(alb, wert)
                else:
                    albums.set_album_field(alb, katalog_feld, wert)
            except albums.AlbumError as exc:
                fehler.append(str(exc))
            continue

        if schluessel.startswith("track:"):
            teile = schluessel.split(":", 2)
            if len(teile) != 3:
                continue
            _, track_id_roh, feld_key = teile
            try:
                track_id = int(track_id_roh)
            except ValueError:
                continue
            katalog_feld = tag_catalog.TRACK_FELDER_NACH_KEY.get(feld_key)
            if katalog_feld is None:
                continue
            if track_id not in tracks:
                tracks[track_id] = albums.get_track(track_id)
            track = tracks[track_id]
            if track is None:
                continue
            try:
                if katalog_feld.kuenstler_link:
                    albums.set_track_interpret(track, wert)
                else:
                    albums.set_track_field(track, katalog_feld, wert)
            except albums.AlbumError as exc:
                fehler.append(str(exc))

    if not fehler:
        # Erfolgreich gespeichert: zurück zur Übersicht statt derselben
        # Detailseite -- HX-Redirect löst bei htmx eine echte Navigation aus,
        # kein bloßes Fragment-Swap.
        return HTMLResponse("", headers={"HX-Redirect": "/albums"})

    return _album_detail_fragment(request, album_id, fehler="; ".join(fehler))


@router.post(
    "/albums/{album_id}/tracks/{track_id}/artist-lookup/{index}", response_class=HTMLResponse
)
def track_artist_lookup(
    request: Request, album_id: int, track_id: int, index: int, name: str = Form(default="")
) -> HTMLResponse:
    """MusicBrainz-Kandidaten für einen Interpreten eines einzelnen Titels."""
    return _mb_matches_fragment(
        request,
        name.strip(),
        f"/albums/{album_id}/tracks/{track_id}/artist-apply/{index}",
        "#album-detail",
    )


@router.post(
    "/albums/{album_id}/tracks/{track_id}/artist-apply/{index}", response_class=HTMLResponse
)
def track_artist_apply(
    request: Request, album_id: int, track_id: int, index: int, mbid: str = Form(...)
) -> HTMLResponse:
    """Verknüpft einen Interpreten eines einzelnen Titels (per Position) mit der MBID."""
    track = albums.get_track(track_id)
    if track is None:
        raise HTTPException(status_code=404, detail="Titel nicht gefunden.")
    try:
        albums.set_track_artist_mbid(track, index, mbid)
    except albums.AlbumError as exc:
        return _album_detail_fragment(request, album_id, fehler=str(exc))
    return _album_detail_fragment(request, album_id)


