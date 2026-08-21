#!/usr/bin/env python3
"""Startet mimport gegen eine erfundene, lokale beets-Bibliothek zum Anschauen.

Nichts davon rührt an die echte Konfiguration (``~/.config/beets`` oder eine
im Container gemountete Library) -- alles liegt isoliert unter ``.mock/`` im
Projektwurzelverzeichnis (gitignored) und wird über ``BEETSDIR`` eingehängt.

Verwendung:

    uv run python scripts/mock.py            # einrichten (falls nötig) + Server starten
    uv run python scripts/mock.py --reset     # .mock/ verwerfen und neu aufbauen

Die vier erfundenen Alben sind bewusst keine echten Bands -- ein Treffer bei
der MusicBrainz-Suche wäre hier nur verwirrend. Eins davon bleibt absichtlich
ohne MusicBrainz-Verknüpfung (zum Ausprobieren von "MB-Link fixen"), eins hat
schon Label/Katalognummer gesetzt (zum Ausprobieren von "Weitere Felder"),
eins hat einen Featuring-Track (Mehrfach-Interpret), eins ist ein Sampler.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests.flacfixture import write_flac  # noqa: E402

MOCK_DIR = REPO_ROOT / ".mock"
BEETSDIR = MOCK_DIR / "beetsdir"
MUSIK_DIR = MOCK_DIR / "musik"
STAGING_DIR = MOCK_DIR / "staging"
QUELLE_DIR = MOCK_DIR / "quelle"

CONFIG_YAML = f"""\
# Nur für scripts/mock.py -- keine echte mimport-Konfiguration.
directory: {MUSIK_DIR}
library: {BEETSDIR / "library.db"}

plugins:
  - musicbrainz

paths:
  default: $albumartist/$album%aunique{{}}/$track $title
  singleton: Singletons/$artist - $title
  comp: Compilations/$album%aunique{{}}/$track $title

import:
  move: yes
  copy: no
  write: yes
  quiet: yes
  incremental: no
  duplicate_action: skip
  resume: no
  timid: no

ui:
  color: no

original_date: no
per_disc_numbering: no
"""


def _tagge(path: Path, **felder: object) -> None:
    import mediafile

    media = mediafile.MediaFile(path)
    for schluessel, wert in felder.items():
        setattr(media, schluessel, wert)
    media.save()


def _album_anlegen(
    ordner: Path,
    *,
    albumartist: str,
    album: str,
    year: int,
    genre: str,
    tracks: list[dict[str, object]],
    **album_felder: object,
) -> None:
    for i, track in enumerate(tracks, start=1):
        titel = track.pop("title")
        artist = track.pop("artist", albumartist)
        pfad = ordner / f"{i:02d} {titel}.flac".replace("/", "_")
        write_flac(pfad, seconds=5)
        _tagge(
            pfad,
            artist=artist,
            albumartist=albumartist,
            album=album,
            title=titel,
            track=i,
            year=year,
            genre=genre,
            **album_felder,
            **track,
        )


def _mock_bibliothek_aufbauen() -> None:
    print("Baue erfundene Alben ...")
    QUELLE_DIR.mkdir(parents=True, exist_ok=True)

    _album_anlegen(
        QUELLE_DIR / "Mondlicht Quartett - Nachtfahrten",
        albumartist="Mondlicht Quartett",
        album="Nachtfahrten",
        year=2018,
        genre="Electronic",
        tracks=[
            {"title": "Ankunft"},
            {"title": "Regenlicht"},
            {"title": "Letzter Zug"},
        ],
    )

    _album_anlegen(
        QUELLE_DIR / "Kaltes Feuer - Aschewege",
        albumartist="Kaltes Feuer",
        album="Aschewege",
        year=2015,
        genre="Rock",
        label="Testlabel Records",
        catalognum="TL-042",
        country="DE",
        tracks=[
            {"title": "Erste Asche"},
            {"title": "Wegzehrung"},
            {"title": "Kalte Glut"},
            {"title": "Aschewege (Reprise)"},
        ],
    )

    _album_anlegen(
        QUELLE_DIR / "Nordlicht Ensemble - Polarnaechte",
        albumartist="Nordlicht Ensemble",
        album="Polarnächte",
        year=2021,
        genre="Ambient",
        tracks=[
            {"title": "Weites Eis"},
            # Mehrfach-Interpret -- genau der Fall, für den die
            # Einzelnamen-MB-Verknüpfung pro Künstler gedacht ist.
            {"title": "Gemeinsames Licht", "artist": "Nordlicht Ensemble feat. Jonas Weber"},
            {"title": "Rückweg"},
        ],
    )

    _album_anlegen(
        QUELLE_DIR / "Various Artists - Sommerhits 2024",
        albumartist="Various Artists",
        album="Sommerhits 2024",
        year=2024,
        genre="Pop",
        comp=True,
        tracks=[
            {"title": "Sonnenweg", "artist": "Freibad Kollektiv"},
            {"title": "Blaue Stunde", "artist": "Lichtjahre"},
            {"title": "Ferienlaerm", "artist": "Kies & Salz"},
        ],
    )

    print(f"Importiere as-is nach {MUSIK_DIR} ...")
    umgebung = dict(os.environ, BEETSDIR=str(BEETSDIR))
    proc = subprocess.run(
        ["beet", "import", "-A", "-q", str(QUELLE_DIR)],
        env=umgebung,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        print(proc.stdout)
        print(proc.stderr, file=sys.stderr)
        raise SystemExit("beet import ist fehlgeschlagen -- siehe Ausgabe oben.")

    shutil.rmtree(QUELLE_DIR, ignore_errors=True)
    print("Fertig.")


def main() -> None:
    if "--reset" in sys.argv and MOCK_DIR.exists():
        print(f"Verwerfe {MOCK_DIR} ...")
        shutil.rmtree(MOCK_DIR)

    if not BEETSDIR.exists():
        BEETSDIR.mkdir(parents=True)
        MUSIK_DIR.mkdir(parents=True, exist_ok=True)
        (BEETSDIR / "config.yaml").write_text(CONFIG_YAML, encoding="utf-8")
        _mock_bibliothek_aufbauen()
    else:
        print(f"Nutze vorhandene Mock-Bibliothek unter {MOCK_DIR} (--reset zum Neuaufbau).")

    STAGING_DIR.mkdir(parents=True, exist_ok=True)

    umgebung = dict(
        os.environ,
        BEETSDIR=str(BEETSDIR),
        MIMPORT_STAGING=str(STAGING_DIR),
    )
    print()
    print(f"BEETSDIR={BEETSDIR}")
    print(f"MIMPORT_STAGING={STAGING_DIR}")
    print("Starte Server -- http://127.0.0.1:8000/albums zeigt die Mock-Alben direkt.")
    print()
    subprocess.run(
        ["uv", "run", "fastapi", "dev", "backend/main.py"],
        env=umgebung,
        cwd=REPO_ROOT,
    )


if __name__ == "__main__":
    main()
