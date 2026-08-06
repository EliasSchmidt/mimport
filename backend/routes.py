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

import logging
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse

from backend import audio, beets_env, disc, importer, matching, sessions, tagging
from backend.config import AUDIO_EXTENSIONS, settings
from backend.templates import templates

log = logging.getLogger(__name__)

router = APIRouter()

#: Uploads in Häppchen auf die Platte schreiben, damit ein Album mit
#: verlustfreien Dateien nicht komplett im Speicher landet.
CHUNK_SIZE = 1024 * 1024


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
            f"{was} überschreitet das Limit von "
            f"{settings.max_upload_bytes / 1024**3:.1f} GB.",
        ),
        (
            frei,
            "Auf dem Server ist nicht genug Speicherplatz frei. Bitte zuerst "
            "laufende Importe abschließen.",
        ),
        (
            budget,
            "Der Staging-Bereich ist ausgelastet. Bitte zuerst laufende "
            "Importe abschließen oder nicht mehr benötigte Uploads verwerfen.",
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
    return _fragment(
        request,
        "_files.html",
        session_id=session.session_id,
        infos=infos,
        summary=audio.summarize(infos),
        health=beets_env.health(),
    )


@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    """Die einzige Seite der Anwendung."""
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "health": beets_env.health(),
            "settings": settings,
        },
    )


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
    return _fragment(
        request,
        "_candidates.html",
        session_id=session_id,
        result=result,
        file_count=len(paths),
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
    return _fragment(
        request,
        "_applied.html",
        session_id=session_id,
        result=write_result,
        candidate=matching.serialize_candidate(match_obj, 0),
        health=beets_env.health(),
    )


@router.post("/manual/{session_id}", response_class=HTMLResponse)
def manual(
    request: Request,
    session_id: str,
    albumartist: str = Form(default=""),
    album: str = Form(default=""),
    artist: str = Form(default=""),
    year: str = Form(default=""),
    genre: str = Form(default=""),
) -> HTMLResponse:
    """Schreibt selbst eingetragene Tags, wenn kein Match passt."""
    session = _session_or_404(session_id)
    paths = session.audio_paths
    if not paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")

    write_result = tagging.apply_manual_tags(
        paths,
        {
            "albumartist": albumartist,
            "album": album,
            "artist": artist,
            "year": year,
            "genre": genre,
        },
    )
    if not write_result.written and not write_result.failed:
        return _fragment(
            request,
            "_error.html",
            message="Es wurde kein Feld ausgefüllt -- nichts zu schreiben.",
        )
    return _fragment(
        request,
        "_applied.html",
        session_id=session_id,
        result=write_result,
        candidate=None,
        health=beets_env.health(),
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

    result = importer.run_import(session.directory, pretend=dry_run)
    if result.ok and not dry_run:
        # Nach einem Verschiebe-Import ist der Ordner leer; dann kann er weg.
        sessions.cleanup_if_empty(session)

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
