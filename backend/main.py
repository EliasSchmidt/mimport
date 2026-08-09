"""mimport -- Weboberfläche, um Musik über beets in die Library zu bringen.

Der Ablauf entspricht dem, was ``beet import`` im Terminal macht, nur im
Browser: hochladen, Match-Vorschläge ansehen, einen auswählen, importieren.

Aufgabenteilung:

* mimport zeigt die Kandidaten und schreibt die Tags des gewählten Kandidaten
  **selbst** in die Dateien.
* beets übernimmt danach nur noch Umbenennen und Einsortieren -- mit seiner
  Konfiguration, seinen Plugins und seinem Umbenennungsschema, aufgerufen mit
  ``-A`` und damit ohne erneutes Autotagging.

Warum nicht der naheliegende Weg ``beet import -q --search-id <MBID>``: im
Quiet-Modus wendet ``_summary_judgment`` einen Match nur bei
``Recommendation.strong`` an, alles darunter wird stillschweigend übersprungen
oder unverändert importiert. Ein bewusst bestätigter Match mit 64 % Sicherheit
-- der Normalfall bei unvollständigen Uploads -- wäre also wirkungslos
geblieben. Mit ``-A`` läuft beets über ``import_asis`` und erreicht diese
Abfrage gar nicht. Ausführlich in der README unter „Wie der Import abläuft".
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import audiobook, beets_env, sessions
from backend.config import settings
from backend.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("mimport")

BASE_DIR = Path(__file__).resolve().parent.parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Beim Start die beets-Anbindung prüfen und den Zustand protokollieren."""
    settings.staging_root.mkdir(parents=True, exist_ok=True)
    health = beets_env.health()
    log.info(
        "beets %s | beet-CLI %s | Metadatenquellen: %s | Fingerprinting: %s",
        health["beets_version"],
        health["beet_cli_version"] or "nicht gefunden",
        ", ".join(health["metadata_sources"]) or "keine",
        "an" if health["fingerprint"] else "aus",
    )
    for problem in health["problems"]:
        log.warning("%s", problem)

    # Ein Neustart ist der natürliche Zeitpunkt, um Liegengebliebenes
    # loszuwerden -- etwa Uploads, die ein Absturz mittendrin erwischt hat.
    entfernt = sessions.sweep_expired(settings.session_ttl_hours)
    if entfernt:
        log.info("%d verwaiste Session(s) beim Start entfernt.", entfernt)

    # Unfertige Hörbuch-Vorgänge liegen neben der Bibliothek und würden sonst
    # nie wieder angefasst -- ein Absturz mitten im Rip kostet Gigabyte.
    audiobook.staging_aufraeumen()

    log.info(
        "Staging-Ordner: %s | belegt %.1f GB von %.1f GB | frei auf dem "
        "Dateisystem: %.1f GB",
        settings.staging_root,
        sessions.usage_bytes() / 1024**3,
        settings.max_staging_bytes / 1024**3,
        settings.staging_free_bytes() / 1024**3,
    )
    yield


app = FastAPI(title="mimport", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(router)
