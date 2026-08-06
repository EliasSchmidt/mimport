# mimport

Weboberfläche, um Musik in eine beets-Library zu bringen — mit dem Tagging-Dialog
im Browser statt im Terminal.

Der Ablauf ist derselbe wie bei `beet import`, nur sichtbar: hochladen,
Match-Vorschläge mit ihrer Sicherheit ansehen, einen auswählen, importieren.

## Was die Oberfläche zeigt

Die drei Angaben, um die es beim Tagging wirklich geht, kommen unverändert aus
beets und werden nicht nachgebaut:

| Anzeige | Herkunft |
|---|---|
| **Sicherheit in Prozent** | `1 - AlbumMatch.distance` (0.0 = perfekt) |
| **Warum unsicher** | `Distance.items()` — die einzelnen Abzüge, ins Deutsche übersetzt |
| **Was fehlt** | `extra_tracks` (Tracks ohne Datei) und `extra_items` (Dateien ohne Track) |

Beispiel: Lädt man 6 der 17 Tracks von *Abbey Road* hoch, zeigt mimport 64,5 %
Sicherheit, nennt als Grund `Tracks des Releases fehlen im Upload` und listet die
11 fehlenden Titel namentlich auf. Genau das, was man vor einer Entscheidung
sehen will.

Dazu kommt eine Gegenüberstellung pro Track: bisheriger Titel und Tracknummer
gegen den neuen Wert, samt Längenabweichung in Sekunden.

Wenn nichts passt, lassen sich Künstler, Album, Jahr und Genre auch von Hand
setzen — oder eine MusicBrainz-Release-ID direkt angeben, als nackte ID oder als
kopierte Adresse.

## Verlustfrei oder nicht

Die Prüfung passiert zweimal, mit Absicht:

1. **Im Browser, vor dem Upload.** Endung plus die ersten 16 Bytes (`fLaC`,
   `RIFF`/`WAVE`, `FORM`/`AIFF`, ID3 bzw. MP3-Frame-Sync). Wer MP3s ausgewählt
   hat, erfährt das, bevor ein halbes Gigabyte über die Leitung geht.
2. **Auf dem Server, verbindlich.** Über `mediafile` (kommt mit beets, es braucht
   also weder ffmpeg noch ffprobe).

`.m4a` lässt sich im Browser grundsätzlich nicht auflösen: verlustfreies ALAC und
verlustbehaftetes AAC liegen im selben Container, und die Angabe steckt im
`moov`-Atom, das oft am Dateiende sitzt. Solche Dateien werden als „unklar"
markiert und erst serverseitig entschieden.

Verlustbehaftete Dateien werden **bemängelt, nicht abgelehnt** — der Hinweis
erklärt, dass sich das Fehlende später nicht zurückholen lässt, der Import geht
aber trotzdem.

## Betrieb im Container (empfohlen)

Alles steckt im Container: Weboberfläche *und* beets. Nach außen führt nur ein
Volume mit dem fertigen Ergebnis.

```bash
# Zielverzeichnis in docker-compose.yml anpassen (Standard: /srv/musik)
docker compose up -d --build
# Oberfläche: http://127.0.0.1:8000
```

Das hat zwei handfeste Vorteile:

- **Nur eine beets-Version.** Zwei beets-Installationen unterschiedlicher Version
  migrieren die `library.db` gegeneinander, und die ältere kann sie danach nicht
  mehr lesen. Im Container kann das nicht passieren.
- **Fremde Dateien werden abgeschottet geparst.** Audio-Metadaten-Parser arbeiten
  auf nicht vertrauenswürdigen Bytes — im Container als unprivilegierter Nutzer,
  ohne Capabilities, mit `no-new-privileges`.

Die beets-Konfiguration liegt in [`beets/config.yaml`](beets/config.yaml) und
bestimmt Zielverzeichnis, Benennungsschema und Plugins. Zum Anpassen ohne Neubau
in `docker-compose.yml` als Volume einhängen.

Volumes:

| Pfad | Zweck |
|---|---|
| `/music` | **Ergebnis.** Getaggt, umbenannt, einsortiert. Nach außen gemountet. |
| `/data` | beets-Datenbank. Gehört zum Werkzeug, nicht zur Sammlung. |
| `/staging` | Laufende Uploads, bis sie importiert sind. |
| `/config` | beets-Konfiguration (`BEETSDIR`). |

## Sicherheit

**Die Anwendung hat keine Authentifizierung.** Wer sie erreicht, kann Dateien
hochladen und Schreibvorgänge auslösen. `docker-compose.yml` bindet deshalb
absichtlich nur an `127.0.0.1`. Soll die Oberfläche von außen erreichbar sein,
gehört ein Reverse-Proxy mit Authentifizierung davor — das ist keine Kür.

Was abgesichert und durch Tests abgedeckt ist:

- **Pfad-Traversal.** Dateinamen und Ordnerpfade kommen vom Browser und werden
  als feindlich behandelt: `../`, absolute Pfade, Laufwerksbuchstaben, Nullbytes
  und Steuerzeichen werden entfernt; zusätzlich muss jeder aufgelöste Zielpfad
  innerhalb des Session-Ordners liegen.
- **Session-IDs** erzeugt der Server (`secrets`), niemals die Anfrage.
- **Kein `shell=True`.** Der `beet`-Aufruf geht als Argumentliste raus,
  Sonderzeichen in Dateinamen können nichts auslösen.
- **Grenzen** für Uploadgröße und Dateianzahl.
- **Doppelte Alben** werden von beets übersprungen (`duplicate_action: skip`),
  ein vorhandenes Album kann also nicht überschrieben werden.

Was offen bleibt: Ein Nutzer kann das Staging-Volume vollschreiben. Die
Speicherbegrenzung in `docker-compose.yml` begrenzt den Schaden, beseitigt ihn
aber nicht.

## Lokale Entwicklung

```bash
uv sync
uv run fastapi dev backend/main.py
```

Ohne Container nutzt mimport die beets-Konfiguration des Systems
(`BEETSDIR` bzw. `~/.config/beets/config.yaml`) und ruft `beet` aus dem `PATH`
auf. Weicht dessen Version von der hier installierten ab, warnt die Oberfläche
und **sperrt den Import** — genau wegen der Datenbank-Migration oben. Der
Probelauf funktioniert weiterhin.

### Einstellungen

| Variable | Standard | Bedeutung |
|---|---|---|
| `MIMPORT_STAGING` | `./staging` | Ordner für laufende Uploads |
| `MIMPORT_BEET_BIN` | `beet` | Pfad zum beets-Executable |
| `MIMPORT_MOVE` | `1` | Dateien verschieben (`0` = kopieren) |
| `MIMPORT_MAX_UPLOAD_BYTES` | 4 GB | Obergrenze pro Upload |
| `MIMPORT_MAX_FILES` | `500` | Obergrenze für die Dateianzahl |
| `MIMPORT_IMPORT_TIMEOUT` | `1800` | Zeitlimit des Importlaufs in Sekunden |
| `MIMPORT_FINGERPRINT` | `0` | AcoustID-Fingerprinting (siehe unten) |

## Tests

```bash
uv run pytest -m "not network"   # ohne Netz, ca. 5 Sekunden
uv run pytest                    # inklusive echter MusicBrainz-Abfrage
```

Die Tests laufen mit eigenem `BEETSDIR` und eigenem Staging-Ordner und fassen
weder die echte Konfiguration noch eine bestehende Library an.

## AcoustID-Fingerprinting (optional, standardmäßig aus)

Hilft bei Dateien, die gar keine brauchbaren Tags haben: dort erkennt beets das
Album sonst nicht. Einschalten lohnt vor allem für solche Fälle.

Was es kostet, für einen älteren Rechner eingeordnet:

- **Platz:** `fpcalc` selbst ist unter 1 MB, zieht aber die libav\*-Dekoder nach
  — insgesamt etwa 50–100 MB, falls ffmpeg nicht schon vorhanden ist.
- **Rechenzeit:** Dekodieren plus FFT, **single-threaded, keine GPU**. Grob
  1–3 Sekunden pro Track auf einer älteren Intel-Mobil-CPU, unter 50 MB RAM.
  Ein Album also 20–40 Sekunden.
- **Der eigentliche Flaschenhals ist das Netz,** nicht die CPU: AcoustID wird pro
  Track abgefragt und ist ratenbegrenzt.

Einschalten:

1. Im `Dockerfile` `libchromaprint-tools` installieren.
2. In `beets/config.yaml` das Plugin `chroma` aktivieren und `pyacoustid` als
   Abhängigkeit ergänzen.
3. `MIMPORT_FINGERPRINT=1` setzen.

mimport prüft zur Laufzeit, ob `fpcalc` wirklich vorhanden ist — ein gesetzter
Schalter ohne Binary bleibt wirkungslos statt zu scheitern.

## Wie der Import abläuft

Der Punkt, an dem es leicht schiefgeht, und warum es hier so gelöst ist:

mimport schreibt die Tags des gewählten Kandidaten **selbst** in die Dateien
(`AlbumMatch.apply_metadata()` plus `Item.try_write()`) und ruft beets dann mit
`-A` auf — also **ohne** erneutes Autotagging. beets übernimmt die Tags wie sie
sind und kümmert sich nur noch um Umbenennen und Einsortieren.

Der naheliegende Weg, beets stattdessen die gewählte Release-ID mitzugeben
(`beet import -q --search-id <MBID>`), funktioniert **nicht**. In
`beets/ui/commands/import_/session.py` entscheidet `_summary_judgment`:

```python
if config["import"]["quiet"]:
    if rec == Recommendation.strong:
        return importer.Action.APPLY
    action = config["import"]["quiet_fallback"].as_choice({"skip": ..., "asis": ...})
```

Im Quiet-Modus wendet beets einen Match **nur bei `Recommendation.strong`** an;
alles darunter wird übersprungen oder unverändert importiert. `--search-id`
schränkt lediglich die Suche ein, es erzwingt keine Anwendung. Ein bewusst
bestätigter Match mit 64 % Sicherheit — der Normalfall bei unvollständigen
Uploads — wäre also stillschweigend ignoriert worden. Mit `-A` läuft beets über
`import_asis` und erreicht diese Abfrage gar nicht.

## Aufbau

| Datei | Aufgabe |
|---|---|
| `backend/beets_env.py` | beets-Konfiguration und Plugins laden, Zustand prüfen |
| `backend/matching.py` | Kandidaten holen und für die Anzeige aufbereiten |
| `backend/audio.py` | Formaterkennung verlustfrei/verlustbehaftet |
| `backend/tagging.py` | gewählte Metadaten in die Dateien schreiben |
| `backend/importer.py` | `beet import` aufrufen |
| `backend/sessions.py` | Staging-Ordner, Pfad-Absicherung |
| `backend/routes.py` | Endpunkte (HTML-Fragmente für HTMX) |
| `templates/` | Seite und Fragmente |
| `static/index.js` | Vorprüfung im Browser, Upload |

Das Matching öffnet **nie** die beets-Datenbank — `tag_album` braucht nur
geladene Plugins. Nur der Import fasst die Library an.
