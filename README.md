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
| `/disc` | Eingelegte Daten-CD, read-only vom Host. |

## Von einer Daten-CD importieren

Gemeint ist die CD mit einem Dateisystem darauf — typischerweise eine
MP3-Sammlung. Die muss man nicht rippen, nur kopieren. Eine **Audio-CD** (CDDA)
hat kein Dateisystem und geht auf diesem Weg *nicht*.

**Gemountet wird auf dem Host, nicht im Container.** Das ist keine Bequemlichkeit,
sondern der Grund, warum das Feature ohne zusätzliche Rechte auskommt: `mount()`
verlangt `CAP_SYS_ADMIN`, und das compose wirft alle Capabilities weg. Der Host
hängt die CD ein, der Container liest read-only mit.

```bash
# auf dem Host, z. B. per Automount oder von Hand:
mount -o ro /dev/sr0 /media/cdrom
```

Es gibt **keinen Schalter** für das Feature. Ob unter `/disc` etwas liegt, *ist*
der Schalter — ein Dienst ohne eingehängte CD zeigt schlicht „keine CD".

### Ein Ordner ist ein Album

Die Oberfläche listet die Ordner der CD einzeln auf, mit Trackzahl und Größe,
und man übernimmt einen davon. Das ist Absicht: beets bewertet eine Auswahl als
*ein* Album, und eine MP3-CD trägt oft ein Dutzend Alben in Unterordnern. Alle
zusammen zu matchen ergäbe Unsinn. Für mehrere Alben nacheinander vorgehen — die
Liste bleibt nach jedem Import abrufbar.

Ab dem Übernehmen ist der Weg identisch mit einem Upload: dieselbe Dateiliste,
dieselben Match-Kandidaten, derselbe Import. Nichts in `matching`, `tagging` oder
`importer` weiß, woher die Dateien kamen.

Zwei Dinge, die der Code abfängt, weil eine fremde CD nicht vertrauenswürdiger
ist als ein Upload: Ordnerangaben aus dem Formular werden gegen den
tatsächlich aufgelösten Pfad geprüft, und Symlinks auf der CD (über Rock Ridge
möglich) werden beim Kopieren übergangen — sonst landete ein Link auf
`/etc/passwd` als `track03.mp3` im Staging.

Bricht das Lesen mittendrin ab, weil die CD zerkratzt ist, wird die halbfertige
Session verworfen und die betroffene Datei genannt. Ein unvollständiges Album
gegen MusicBrainz zu matchen wäre schlimmer als ein klarer Fehler.

### Zwei Dienste

`docker-compose.yml` startet dasselbe Image zweimal:

| Dienst | Port | Erreichbar | Zweck |
|---|---|---|---|
| `mimport` | 8000 | nur `127.0.0.1` | Uploads |
| `mimport-cd` | 8001 | im Heimnetz | Daten-CDs |

Der Unterschied ist allein die Bindung und das eingehängte CD-Verzeichnis.

**Beide teilen sich die `library.db`** — und damit rufen zwei Prozesse `beet
import` auf derselben SQLite-Datei auf. Ein Import ist eine lange Transaktion;
zwei gleichzeitig geraten sich in die Quere. mimport serialisiert sie deshalb
über ein Dateilock neben der Datenbank. Der Pfad wird aus der beets-Konfiguration
abgeleitet und nicht separat eingestellt: eine eigene Einstellung könnte man je
Dienst unterschiedlich setzen und hätte den Schutz still ausgehebelt.

Und noch einmal deutlich, weil es die dokumentierte Haltung ändert: **`mimport-cd`
ist im ganzen Heimnetz erreichbar und hat keine Authentifizierung.** Wer im WLAN
ist, kann importieren. Dieser Port darf von außen nicht erreichbar sein.

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
| `MIMPORT_DISC_PATH` | `/disc` | Wo die eingehängte Daten-CD liegt |
| `MIMPORT_BEET_BIN` | `beet` | Pfad zum beets-Executable |
| `MIMPORT_MOVE` | `1` | Dateien verschieben (`0` = kopieren) |
| `MIMPORT_MAX_UPLOAD_BYTES` | 4 GB | Obergrenze pro Upload |
| `MIMPORT_MAX_FILES` | `500` | Obergrenze für die Dateianzahl |
| `MIMPORT_MIN_FREE_BYTES` | 2 GB | Sicherheitsabstand auf dem Dateisystem |
| `MIMPORT_MAX_STAGING_BYTES` | 20 GB | Obergrenze für alle Uploads zusammen |
| `MIMPORT_SESSION_TTL_HOURS` | `24` | Frist, nach der verwaiste Uploads verschwinden |
| `MIMPORT_IMPORT_TIMEOUT` | `1800` | Zeitlimit des Importlaufs in Sekunden |
| `MIMPORT_FINGERPRINT` | `0` | AcoustID-Fingerprinting (siehe unten) |

### Platz auf dem Server

Beim Upload greifen drei Grenzen, die kleinste gewinnt:

| Grenze | Wogegen sie hilft |
|---|---|
| `MIMPORT_MAX_UPLOAD_BYTES` | ein einzelner übergroßer Upload |
| freier Platz minus `MIMPORT_MIN_FREE_BYTES` | ein volllaufendes Dateisystem |
| `MIMPORT_MAX_STAGING_BYTES` minus Belegtem | viele Uploads, die sich summieren |

Der **freie Platz ist die einzige Grenze, die wirklich schützt**: eine
Obergrenze von 20 GB nützt nichts auf einer Platte, auf der nur noch 5 GB frei
sind. Die beiden konfigurierten Grenzen sind Politik obendrauf.

Und es geht dabei um mehr als den Upload-Bereich: `mimport-staging` und
`mimport-data` sind beide Named Volumes, liegen also auf demselben
Docker-Dateisystem. Ein vollgeschriebenes Staging nimmt die `library.db` mit —
deshalb der Sicherheitsabstand, und deshalb ist er großzügig voreingestellt.

Zwei *gleichzeitige* Uploads sehen denselben Stand und dürfen beide los, zusammen
also etwas mehr als das Budget. Das ist hingenommen, nicht übersehen: mimport
bedient einen Nutzer auf `127.0.0.1`, und der Sicherheitsabstand fängt den
Überhang auf. Eine Reservierung wäre Aufwand ohne Gegenwert.

Dazu kommt das Aufräumen. Sessions, die `MIMPORT_SESSION_TTL_HOURS` lang nicht
angefasst wurden, verschwinden — geprüft beim Start und vor jedem neuen Upload,
ohne Hintergrunddienst. Die Frist ist bewusst lang, weil zwischen Upload und
Entscheidung eine ausgedehnte Pause liegen darf. Bricht der Browser mitten im
Upload ab, wird die angefangene Session sofort verworfen; sonst würde ein
geschlossener Tab genügen, um Reste anzuhäufen.

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
