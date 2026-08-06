"""mimport -- Weboberfläche, um Musik über beets in die Library zu bringen.

Der Ablauf entspricht dem, was ``beet import`` im Terminal macht, nur im
Browser: hochladen, Match-Vorschläge ansehen, einen auswählen, importieren.

Aufgabenteilung:

* mimport zeigt die Kandidaten und schreibt die Tags des gewählten Kandidaten.
* Das beets des Servers übernimmt danach den Import selbst -- mit seiner
  Konfiguration, seinen Plugins und seinem Umbenennungsschema.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import beets_env
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
    log.info("Staging-Ordner: %s", settings.staging_root)
    yield


app = FastAPI(title="mimport", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.include_router(router)
