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
import shutil
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from backend import (
    audio,
    audiobook,
    beets_env,
    cover,
    disc,
    importer,
    matching,
    rip,
    sessions,
    tagging,
)
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


@router.get("/sessions", response_class=HTMLResponse)
def open_sessions(request: Request) -> HTMLResponse:
    """Was im Staging liegt und noch nicht importiert ist.

    Die Session-ID steht sonst nur im ausgelieferten HTML -- ein geschlossener
    Tab oder ein leerer Akku kostete sonst den ganzen Upload, obwohl die
    Dateien noch da sind.
    """
    return _fragment(
        request,
        "_sessions.html",
        offen=sessions.list_open(),
        ttl=settings.session_ttl_hours,
    )


@router.delete("/sessions/{session_id}", response_class=HTMLResponse)
def discard_from_list(request: Request, session_id: str) -> HTMLResponse:
    """Verwirft eine Sitzung aus der Übersicht heraus.

    Eigene Route neben ``DELETE /session/{id}``: dort wird der laufende Upload
    verworfen und ein leerer Bereich zurückgegeben, hier bleibt die Liste
    stehen und zeigt danach den neuen Stand.
    """
    sessions.delete_session(session_id)
    return _fragment(
        request,
        "_sessions.html",
        offen=sessions.list_open(),
        ttl=settings.session_ttl_hours,
    )


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
    """Fortschritt eines laufenden Rips -- Ziel der Abfrage im Sekundentakt."""
    return _rip_fragment(request)


@router.post("/rip", response_class=HTMLResponse)
def rip_start(request: Request) -> HTMLResponse:
    """Startet den Rip der eingelegten Audio-CD.

    Kehrt sofort zurück; gelesen wird im Hintergrund. Ein Rip dauert 10 bis 40
    Minuten, so lange darf keine Anfrage offen stehen.
    """
    sessions.sweep_expired(settings.session_ttl_hours)
    erlaubt, grenzmeldung = _storage_allowance("Diese CD")
    if erlaubt <= 0:
        return _fragment(request, "_error.html", message=grenzmeldung)

    try:
        rip.start(allowance=erlaubt)
    except rip.RipError as exc:
        log.warning("Rip nicht gestartet: %s", exc)
        # Der Fehler steht im Auftrag und wird mit angezeigt.
        return _rip_fragment(request)
    return _rip_fragment(request)


@router.delete("/rip", response_class=HTMLResponse)
def rip_reset(request: Request) -> HTMLResponse:
    """Verwirft einen abgeschlossenen Auftrag, damit die nächste CD kann."""
    job = rip.current()
    try:
        rip.reset()
    except rip.RipError as exc:
        return _fragment(request, "_error.html", message=str(exc))
    if job is not None and job.session_id:
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


def _audiobook_fragment(
    request: Request, *, meldung: str = "", fehler: str = ""
) -> HTMLResponse:
    """Der Hörbuch-Bereich: Formular, laufende Aufträge, angefangene Bücher."""
    job = rip.current()
    return _fragment(
        request,
        "_audiobook.html",
        job=job if job is not None and job.modus == "hoerbuch" else None,
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
    """Stand des Hörbuch-Bereichs -- auch Ziel der Abfrage im Sekundentakt."""
    return _audiobook_fragment(request)


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


@router.post("/cover/session/{session_id}", response_class=HTMLResponse)
async def cover_session(
    request: Request, session_id: str, bild: UploadFile = File(...)
) -> HTMLResponse:
    """Nimmt ein Cover für einen laufenden Musik-Upload entgegen.

    Es landet als ``cover.jpg`` neben den Audiodateien; beim Import zieht beets
    es über ``fetchart`` heran, statt selbst eines zu suchen.
    """
    session = _session_or_404(session_id)
    try:
        cover.speichern(session.directory, await bild.read())
    except cover.CoverError as exc:
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
async def manual(
    request: Request,
    session_id: str,
    albumartist: str = Form(default=""),
    album: str = Form(default=""),
    artist: str = Form(default=""),
    year: str = Form(default=""),
    genre: str = Form(default=""),
    compilation: bool = Form(default=False),
) -> HTMLResponse:
    """Schreibt selbst eingetragene Tags, wenn kein Match passt.

    Neben den albumweiten Feldern kommen Titel und Interpret je Datei an --
    als ``titel:<Dateiname>`` und ``artist:<Dateiname>``. Eine Sampler-CD
    braucht das: dort hat jeder Track einen anderen Interpreten, während der
    Albumkünstler „Various Artists" bleibt und das Compilation-Flag die Tracks
    zusammenhält.
    """
    session = _session_or_404(session_id)
    paths = session.audio_paths
    if not paths:
        return _fragment(request, "_error.html", message="In dieser Sitzung liegen keine Dateien.")

    formular = await request.form()
    je_track: dict[str, dict[str, str]] = {}
    for schluessel, wert in formular.items():
        praefix, _, dateiname = str(schluessel).partition(":")
        feld = {"titel": "title", "interpret": "artists"}.get(praefix)
        if feld and dateiname and str(wert).strip():
            je_track.setdefault(dateiname, {})[feld] = str(wert)

    write_result = tagging.apply_manual_tags(
        paths,
        {
            "albumartist": albumartist,
            "album": album,
            "artists": artist,
            "year": year,
            # In beets 2.x heißt das Feld "genres"; als "genre" gesetzt landete
            # es früher nur als flexibles Attribut und nie in der Datei.
            "genres": genre,
            "comp": compilation,
        },
        je_track=je_track,
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
