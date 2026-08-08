# mimport im Container: Weboberfläche und beets in einer Einheit.
#
# Der Container enthält alles, was zum Taggen und Importieren nötig ist. Nach
# außen führt nur ein Volume mit dem fertigen Ergebnis. Das hat zwei Vorteile:
# es gibt nur eine beets-Version (keine Datenbank-Migration durch eine zweite
# Installation), und der Umgang mit fremden Audiodateien passiert abgeschottet.

FROM python:3.12-slim AS base

# uv für reproduzierbare Installation aus uv.lock.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Zum Rippen von Audio-CDs: cdparanoia liest die Sektoren, flac packt sie.
# Zusammen unter 2 MB -- anders als die libav*-Dekoder, die fpcalc für das
# Fingerprinting nachziehen würde. Ohne eingelegte Audio-CD tun sie nichts.
#
# ffmpeg kommt für die Hörbücher dazu: es bündelt die Discs zu einer m4b mit
# Kapiteln. Es ist das mit Abstand größte Paket hier (mit den libav*-Dekodern
# grob 200 MB) -- ohne m4b-Bau kann man die beiden Zeilen streichen.
RUN apt-get update \
 && apt-get install -y --no-install-recommends cdparanoia flac ffmpeg \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    # beets findet seine Konfiguration hierüber.
    BEETSDIR=/config \
    # Innerhalb des Containers liegt das venv an festem Ort.
    VIRTUAL_ENV=/app/.venv \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# Abhängigkeiten zuerst, damit Änderungen am Code den Layer-Cache nicht
# entwerten.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Jetzt der Code.
COPY backend/ ./backend/
COPY templates/ ./templates/
COPY static/ ./static/
COPY beets/config.yaml /config/config.yaml

# Ein unprivilegierter Nutzer: hochgeladene Dateien werden von Parsern
# gelesen, die auf fremden Bytes arbeiten -- das soll nicht als root laufen.
RUN groupadd --gid 1000 mimport \
 && useradd --uid 1000 --gid 1000 --no-create-home mimport \
 && mkdir -p /music /data /staging /config /disc /audiobooks \
 && chown -R mimport:mimport /app /music /data /staging /config /disc /audiobooks

USER mimport

ENV MIMPORT_STAGING=/staging \
    # Hier taucht eine eingelegte Daten-CD auf. Gemountet wird auf dem Host,
    # hereingereicht wird nur der fertige Mount -- der Container braucht
    # dadurch weder /dev/sr0 noch CAP_SYS_ADMIN. Kein Mount, kein CD-Bereich.
    MIMPORT_DISC_PATH=/disc

EXPOSE 8000

# Ein einzelner Worker genügt: die Arbeit ist I/O-gebunden (MusicBrainz), und
# FastAPI schiebt die synchronen Endpunkte in seinen Threadpool.
CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
