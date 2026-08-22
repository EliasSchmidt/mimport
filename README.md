# mimport

Weboberfläche, um Musik in eine beets-Library zu bringen — mit dem Tagging-Dialog
im Browser statt im Terminal.

Der Ablauf ist derselbe wie bei `beet import`, nur sichtbar: hochladen,
Match-Vorschläge mit ihrer Sicherheit ansehen, einen auswählen, importieren.

## Die zwei Wege

Die Startseite stellt nur eine Frage: **Musik oder Hörbuch?** Dahinter liegen
zwei getrennte Seiten, weil die Wege fast nichts gemeinsam haben.

| Seite | Weg |
|---|---|
| `/musik` | Upload, Daten-CD, Audio-CD rippen → Match gegen MusicBrainz → beets |
| `/hoerbuch` | Disc für Disc einlesen → m4b mit Kapiteln. Kein beets, kein Match. |

Es gibt nur ein Laufwerk und damit einen Auftrag; läuft gerade ein
Hörbuch-Rip, meldet die Musikseite bloß, dass das Laufwerk belegt ist, und
umgekehrt.

## Was die Oberfläche zeigt

Die drei Angaben, um die es beim Tagging wirklich geht, kommen unverändert aus
beets:

| Anzeige | Herkunft |
|---|---|
| **Sicherheit in Prozent** | `1 - AlbumMatch.distance` (0.0 = perfekt) |
| **Warum unsicher** | die einzelnen Abzüge, ins Deutsche übersetzt |
| **Was fehlt** | Tracks ohne Datei, Dateien ohne Track |

Beispiel: Lädt man 6 der 17 Tracks von *Abbey Road* hoch, zeigt mimport 64,5 %
Sicherheit, nennt als Grund „Tracks des Releases fehlen im Upload" und listet
die 11 fehlenden Titel namentlich auf.

Dazu kommt eine Gegenüberstellung pro Track: bisheriger Titel und Tracknummer
gegen den neuen Wert, samt Längenabweichung in Sekunden.

Wenn nichts passt, lassen sich Künstler, Album, Jahr und Genre auch von Hand
setzen — oder eine MusicBrainz-Release-ID direkt angeben, als nackte ID oder als
kopierte Adresse.

## Verlustfrei oder nicht

Die Prüfung passiert zweimal: im Browser vor dem Upload (an Endung und den
ersten Bytes der Datei), und verbindlich noch einmal auf dem Server. Wer MP3s
ausgewählt hat, erfährt das also, bevor ein halbes Gigabyte über die Leitung
geht. `.m4a` lässt sich im Browser grundsätzlich nicht auflösen (ALAC und AAC
liegen im selben Container) und wird als „unklar" markiert, bis der Server
entscheidet.

Verlustbehaftete Dateien werden **bemängelt, nicht abgelehnt** — der Hinweis
erklärt, dass sich das Fehlende später nicht zurückholen lässt, der Import geht
aber trotzdem.

## Betrieb im Container (empfohlen)

Alles steckt im Container: Weboberfläche *und* beets.

```bash
# Zielverzeichnisse in docker-compose.yml anpassen (/srv/music, /srv/audiobooks)
docker compose up -d --build
# Oberfläche: http://127.0.0.1:8000
```

Das hat zwei handfeste Vorteile: **nur eine beets-Version** im Container (zwei
beets-Installationen unterschiedlicher Version migrieren die `library.db`
sonst gegeneinander, und die ältere kann sie danach nicht mehr lesen), und
**fremde Dateien werden abgeschottet geparst** — als unprivilegierter Nutzer,
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
| `/audiobooks` | Hörbücher. Getrennt von der Musik, ohne beets. |

## Eine bestehende beets-Library übernehmen

`mimport-data` ist ein Named Volume und übersteht `docker compose up -d
--build` sowie `docker compose down`. Weg ist es nur nach `docker compose
down -v` oder einem ausdrücklichen `docker volume rm`.

**Wichtig, bevor eine vorhandene `library.db` hereinkommt: von Hand
kopieren.** beets migriert das Schema beim ersten Öffnen einer älteren
Datenbank, und danach kann eine ältere beets-Installation dieselbe Datei
nicht mehr lesen — eine Einbahnstraße. Läuft auf dem Zielrechner noch ein
älteres System-beets (das war hier der Fall: 2.1.0 auf dem Host gegen 2.13.1
im Container), entweder das Host-beets auf dieselbe Version ziehen (dabei
nicht vergessen: `beets[lastgenre]` für `pylast`, und `musicbrainz` explizit
in die Host-Konfiguration eintragen — ab dieser Version ist es ein Plugin),
oder auf dem Host gar nicht mehr direkt importieren und für `beet list` &
Co. `docker compose exec mimport beet …` benutzen. Beides gleichzeitig ohne
Versionsabgleich funktioniert nicht.

Ein Blick zeigt, wie viel Arbeit der Umzug macht:

```bash
sqlite3 /pfad/zur/library.db "SELECT path FROM items LIMIT 5;"
```

beets speichert Pfade relativ zu `directory`, solange die Dateien darunter
liegen. Stehen dort **relative Pfade**, reicht es, die Datei nach
`/data/library.db` zu kopieren und das Musikverzeichnis auf `/music` zu
mounten — wo es auf dem Host liegt, ist egal. Stehen dort **absolute Pfade**,
ist es einfacher, den Pfad im Container gleich zu lassen (z. B.
`/srv/music:/srv/music` einhängen, `directory: /srv/music` setzen), als die
Datenbank umzuschreiben.

Voreingestellte Plugins: `musicbrainz`, `lastgenre`, `fetchart`, `embedart`.
**`musicbrainz` darf nicht fehlen** — ohne das Plugin gibt es in beets 2.x
keine einzige Metadatenquelle. `discogs` steht bewusst *nicht* in dieser
Liste, siehe [unten](#discogs-als-zweite-metadatenquelle-optional).

## Cover abfotografieren

Auf dem Handy: Cover fotografieren, Ecken zurechtziehen, fertig — wie bei
einem Dokumentenscanner. Die Bildverarbeitung läuft im Browser, zum Server
geht nur das fertige JPEG (1000 × 1000 Pixel).

| Weg | Ziel | Wer nimmt es auf |
|---|---|---|
| Hörbuch | `cover.jpg` im Buchordner | wird beim Bündeln in die m4b eingebettet |
| Musik | `cover.jpg` in der Upload-Session | beets über `fetchart` |

Unter „Alben" (`/albums`) lässt sich die Library nach Interpret oder Titel
durchsuchen; „Bearbeiten" öffnet ein schon importiertes Album mit Cover,
Tags und Titelliste. Ein neues Foto ersetzt dort sowohl die `cover.jpg` im
Ordner als auch das eingebettete Cover der schon vorhandenen Dateien. Auf
derselben Seite lässt sich außerdem pro Album und Titel eine fehlende
MusicBrainz-Artist-ID nachtragen, etwa wenn der Import als-ist lief.

## Backcover-Text (OCR)

Für das **manuelle Taggen** kann mimport den Text eines CD-Backcovers lesen
(serverseitig, der Browser lädt nur das Foto hoch). Das Ergebnis erscheint
als Rohtext und lässt sich mit einfachen Parsern (etwa „01 Titel 3:45" oder
„Artist - Titel") in eine weiter bearbeitbare Trackliste vorfüllen — die
Felder und ihre Reihenfolge sind frei wählbar, ebenso ob Tracknummer/Dauer
erkannt werden sollen.

Die OCR-Modelle werden beim Serverstart vorab geladen und bleiben über
Neustarts hinweg im Daten-Volume erhalten. Für kleine Server ist das Backcover
vor der Inferenz auf eine maximale Bildkante begrenzt (CPU-only, kein
zusätzliches Systempaket nötig).

## Von einer Daten-CD importieren

Gemeint ist eine CD mit einem Dateisystem darauf (typischerweise eine
MP3-Sammlung) — die muss man nicht rippen, nur kopieren. Eine **Audio-CD**
(CDDA) hat kein Dateisystem und geht auf diesem Weg *nicht*.

Gemountet wird auf dem Host, nicht im Container:

```bash
# auf dem Host, z. B. per Automount oder von Hand:
mount -o ro /dev/sr0 /media/cdrom
```

Es gibt keinen Schalter für das Feature — ob unter `/disc` etwas liegt, *ist*
der Schalter.

Die Oberfläche listet die Ordner der CD einzeln auf, mit Trackzahl und Größe;
ein Ordner wird als *ein* Album übernommen, weil eine MP3-CD oft ein Dutzend
Alben in Unterordnern trägt. Ab dem Übernehmen ist der Weg identisch mit
einem Upload. Bricht das Lesen mittendrin ab (zerkratzte CD), wird die
halbfertige Session verworfen und die betroffene Datei genannt.

## Eine Audio-CD rippen

Die andere Sorte CD: kein Dateisystem, die Tracks müssen ausgelesen werden.
Das Gerät selbst muss dafür in den Container:

```yaml
# in docker-compose.yml, Dienst mimport-cd
devices:
  - /dev/sr0:/dev/sr0
group_add:
  - "24"        # ANPASSEN, siehe unten
```

Die Gruppen-ID hängt vom Rechner ab:

```bash
ls -l /dev/sr0 && getent group cdrom
```

`/dev/sr0` gehört üblicherweise `root:cdrom` mit Modus 660 — der
Container-Nutzer muss also in dieser Gruppe sein, sonst gibt es „Permission
denied". Auf Debian ist die gid meist 24; nachsehen ist billiger als raten.

Ablauf: Inhaltsverzeichnis lesen (`cdparanoia -Q`) → daraus die
MusicBrainz-DiscID berechnen → Track für Track als FLAC packen → DiscID bei
MusicBrainz nachschlagen (das trifft den Match exakt, auch ohne dass die
frisch gerippte CD irgendwelche Tags hätte) → Dateiliste, Kandidaten, Import
wie gewohnt. Kennt MusicBrainz die CD nicht, bleibt die Eingabe von Hand.

**Die Sicherheit fällt nach einem Rip niedrig aus (etwa ein Drittel), und das
ist normal** — beets kann keine Titel vergleichen, weil auf einer Audio-CD
keine stehen. Aussagekräftig sind hier die Spieldauern und Tracknummern,
nicht die Prozentzahl.

Der Rip läuft im Hintergrund, die Oberfläche zeigt den Fortschritt live und
bei zerkratzten CDs auch, wenn cdparanoia Mühe meldet („Kratzer erkannt",
„liest langsamer", „Lesefehler"). Friert cdparanoia an einer beschädigten
Stelle fest, statt mit einem Fehler abzubrechen, beendet der Knopf „Rip
abbrechen" den Lesevorgang von Hand — betroffen ist nur der angefangene
Track bzw. die angefangene Disc, zuvor erfolgreich gelesene Discs eines
Mehrfach-CD-Albums bleiben erhalten.

## Hörbücher

Ein eigener Weg neben dem Musikweg: **für Hörbücher gibt es keinen Match und
keinen beets-Import.** MusicBrainz kennt sie praktisch nicht; die Metadaten
holt sich Audiobookshelf später selbst über den Audible-Provider. mimport
endet beim fertigen Buchordner:

```
/audiobooks/<Autor>/<Titel>/CD 1/01 Track 1.flac
                           /CD 2/…
                           /<Titel>.m4b      (nach dem Bündeln)
```

In der Liste hat jedes Buch die Knöpfe, die zu seinem Zustand passen:

| Zustand | Knöpfe |
|---|---|
| angefangen | **Nächste CD** · **m4b bauen** |
| gebündelt | **Von vorn einlesen** |
| m4b *und* Quellen | **Neu bauen** (mit Warnung) |

**Von vorn einlesen** ist für einen fehlerhaften Rip: Die fertige m4b wird
dabei umbenannt (`.ersetzt`), nicht gelöscht, und zählt damit weder für
mimport noch für Audiobookshelf als Hörbuch.

Ein Rip mitten in einem zwölfteiligen Hörbuch übersteht einen Neustart ohne
Datenverlust — der Buchordner selbst ist der Zustand. Für die nächste Disc
einfach dieselben Angaben erneut eintragen.

Laufende Rips und ein laufender m4b-Bau liegen dabei nicht im Buchordner
selbst, sondern in `/audiobooks/.mimport-unfertig/` und werden erst fertig
an ihren Platz geschoben — Audiobookshelf soll ein unvollständiges Buch nie
zu Gesicht bekommen. Reste eines Absturzes räumt der nächste Start weg;
dieser Ordner ist also kein Hörbuch, das man von Hand aufräumen müsste.

### Bündeln zur m4b

**Erst alle Discs einlesen, dann einmal bündeln** — beim Bündeln werden die
Quelldateien gelöscht. mimport lehnt einen zweiten Bau ab und nennt beide
Laufzeiten; erzwingen lässt es sich, aber nur ausdrücklich.

Aus zwölf CDs FLAC (gut 4 GB) wird eine `<Titel>.m4b` von etwa 300 MB,
64 kbit/s in Mono — für eine Lesung reicht das. Ein `cover.jpg` im
Buchordner wird eingebettet.

Kapitelgrenzen sind die Trackgrenzen, der Name kommt der Reihe nach aus:
einer eigenen Angabe beim Bündeln, sonst dem Titel-Tag (falls vorhanden und
eindeutig), sonst durchgezählt (`Kapitel 1` … `Kapitel N`).

Zwei Bremsen: **verlustbehaftete Quellen werden nicht umgewandelt** (MP3
nach AAC bringt nur Verlust, Audiobookshelf spielt MP3-Ordner ohnehin
klaglos ab; wer trotzdem eine Einzeldatei will, kann es erzwingen), und
**gelöscht wird erst, wenn die Laufzeit der m4b zur Summe der Quellen
passt**.

Ein Encode läuft auf schwacher Hardware Stunden — Rippen und Bündeln können
parallel laufen (nur dasselbe Buch nicht gleichzeitig), beide Balken zeigen
ihre Dauer und geschätzte Restzeit an.

Bleibt ein Bau hängen, gibt es drei Bremsen in dieser Reihenfolge: eine
Stillstandsüberwachung (`MIMPORT_M4B_STILLSTAND`), ein Zeitlimit
(`MIMPORT_M4B_TIMEOUT`), und den Knopf „Bau abbrechen" für alles andere. In
allen drei Fällen wird nichts gelöscht — die Quelldateien fasst erst der
Schritt nach der bestandenen Laufzeitprüfung an.

### Bescheid, wenn es fertig ist

Ein Rip dauert eine halbe Stunde, ein Encode Stunden — niemand sitzt daneben.
Die Seite meldet sich in drei Stufen:

| Stufe | Bedingung |
|---|---|
| **Titel des Tabs** bekommt ein `✓` bzw. `✗` | immer, auch über HTTP |
| **Kurzer Zweiklang** | nach dem ersten Klick auf der Seite |
| **Echte Systembenachrichtigung** | nur über HTTPS oder localhost |

Die dritte Stufe braucht einen *secure context* — über `http://server:8001`
gibt es sie nicht, dort bleiben Titel und Ton. Ein Kasten auf beiden Seiten
zeigt den eigenen Stand (noch nicht gefragt, erlaubt, abgelehnt, oder mangels
HTTPS gar nicht möglich).

## Zwei Dienste

`docker-compose.yml` startet dasselbe Image zweimal:

| Dienst | Port | Erreichbar | Zweck |
|---|---|---|---|
| `mimport` | 8000 | nur `127.0.0.1` | Uploads |
| `mimport-cd` | 8001 | im Heimnetz | Daten-CDs |

Beide teilen sich die `library.db`; ein Dateilock neben der Datenbank
verhindert, dass zwei gleichzeitige Importe sich in die Quere kommen.

**`mimport-cd` ist im ganzen Heimnetz erreichbar und hat keine
Authentifizierung.** Wer im WLAN ist, kann importieren. Dieser Port darf von
außen nicht erreichbar sein.

## Sicherheit

**Die Anwendung hat keine Authentifizierung.** Wer sie erreicht, kann Dateien
hochladen und Schreibvorgänge auslösen. `docker-compose.yml` bindet deshalb
absichtlich nur an `127.0.0.1`. Soll die Oberfläche von außen erreichbar sein,
gehört ein Reverse-Proxy mit Authentifizierung davor — das ist keine Kür.

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
und **sperrt den Import**. Der Probelauf funktioniert weiterhin.

### Mit erfundenen Alben ausprobieren, ohne die echte Library anzufassen

```bash
uv run python scripts/mock.py            # baut .mock/ (falls nötig) und startet den Server
uv run python scripts/mock.py --reset    # .mock/ verwerfen und neu aufbauen
```

Legt vier erfundene Alben in einer komplett isolierten, gitignorten
beets-Bibliothek unter `.mock/` an und startet den Server direkt darauf
(`http://127.0.0.1:8000/albums`). Rührt nicht an `~/.config/beets` oder eine
echte Library.

### Einstellungen

| Variable | Standard | Bedeutung |
|---|---|---|
| `MIMPORT_STAGING` | `./staging` | Ordner für laufende Uploads |
| `MIMPORT_DISC_PATH` | `/disc` | Wo die eingehängte Daten-CD liegt |
| `MIMPORT_CDROM` | `/dev/sr0` | Laufwerk für Audio-CDs |
| `MIMPORT_CDPARANOIA` | `cdparanoia` | Pfad zum Ripper |
| `MIMPORT_FLAC` | `flac` | Pfad zum FLAC-Encoder |
| `MIMPORT_RIP_TOC_TIMEOUT` | `60` | Zeitlimit fürs Inhaltsverzeichnis (s) |
| `MIMPORT_RIP_TRACK_TIMEOUT` | `1200` | Zeitlimit je Track (s) |
| `MIMPORT_AUDIOBOOKS` | `/audiobooks` | Wurzel der Hörbuch-Bibliothek |
| `MIMPORT_M4B_BITRATE` | `64k` | Zielbitrate der m4b |
| `MIMPORT_M4B_MONO` | `1` | Auf einen Kanal mischen |
| `MIMPORT_M4B_MIN_KBPS` | `96` | Darunter gilt Umwandeln als nicht lohnend |
| `MIMPORT_M4B_TIMEOUT` | `7200` | Zeitlimit für den m4b-Bau (s) |
| `MIMPORT_M4B_STILLSTAND` | `300` | So lange darf ffmpeg schweigen, dann gilt er als hängend (s) |
| `MIMPORT_FFMPEG` | `ffmpeg` | Pfad zum ffmpeg-Executable |
| `MIMPORT_FFPROBE` | `ffprobe` | Pfad zum ffprobe-Executable |
| `MIMPORT_BEET_BIN` | `beet` | Pfad zum beets-Executable |
| `MIMPORT_MOVE` | `1` | Dateien verschieben (`0` = kopieren) |
| `MIMPORT_MAX_UPLOAD_BYTES` | 4 GB | Obergrenze pro Upload |
| `MIMPORT_MAX_FILES` | `500` | Obergrenze für die Dateianzahl |
| `MIMPORT_MIN_FREE_BYTES` | 2 GB | Sicherheitsabstand auf dem Dateisystem |
| `MIMPORT_MAX_STAGING_BYTES` | 20 GB | Obergrenze für alle Uploads zusammen |
| `MIMPORT_SESSION_TTL_HOURS` | `24` | Frist, nach der verwaiste Uploads verschwinden |
| `MIMPORT_IMPORT_TIMEOUT` | `1800` | Zeitlimit des Importlaufs in Sekunden |
| `MIMPORT_FINGERPRINT` | `0` | AcoustID-Fingerprinting, siehe CLAUDE.md |
| `MIMPORT_DISCOGS_TOKEN` | leer | Discogs als zweite Metadatenquelle (siehe unten) |

### Platz auf dem Server

Beim Upload greifen drei Grenzen, die kleinste gewinnt: `MIMPORT_MAX_UPLOAD_BYTES`
für einen einzelnen Upload, freier Platz minus `MIMPORT_MIN_FREE_BYTES` gegen
ein volllaufendes Dateisystem, und `MIMPORT_MAX_STAGING_BYTES` minus Belegtem
gegen viele Uploads, die sich summieren. Der **freie Platz ist die Grenze, die
wirklich schützt** — eine Obergrenze von 20 GB nützt nichts auf einer Platte
mit nur noch 5 GB frei, und Staging und beets-Datenbank liegen auf demselben
Docker-Dateisystem.

### Unterbrochene Sitzungen

Ein geschlossener Tab, ein leerer Akku oder ein Neuladen kostet den Upload
nicht: Die Startseite listet auf, was im Staging liegt (Auswahl, Dateizahl,
Größe, Alter), dazu **Fortsetzen** und **Verwerfen**. Weil die Liste
serverseitig entsteht, funktioniert sie auch von einem anderen Gerät aus — am
Rechner hochladen, am Telefon weitermachen. Sessions, die
`MIMPORT_SESSION_TTL_HOURS` lang nicht angefasst wurden, räumt mimport von
selbst weg.

## Auf dem Handy

Ein guter Teil der Bedienung passiert vom Telefon aus — Cover fotografieren,
nach einem Rip nachsehen. Die Tabellen werden unterhalb von 40 rem zu Karten,
damit sich kein Dateiname in 60 Pixel quetscht.

## Tests

```bash
uv run pytest -m "not network"   # ohne Netz, ca. 5 Sekunden
uv run pytest                    # inklusive echter MusicBrainz-Abfrage
```

Die Tests laufen mit eigenem `BEETSDIR` und eigenem Staging-Ordner und fassen
weder die echte Konfiguration noch eine bestehende Library an.

## Discogs als zweite Metadatenquelle (optional)

Hilft bei Releases, die MusicBrainz nicht kennt — etwa manche
Vinyl-Pressungen, Bootlegs oder Nischen-Compilations. **Ohne eigenen
Zugriffstoken bleibt Discogs komplett aus**, das Vorhandensein des Tokens
*ist* der Schalter.

Einschalten:

1. Auf discogs.com unter *Einstellungen → Entwickler* einen persönlichen
   Zugriffstoken erzeugen.
2. `MIMPORT_DISCOGS_TOKEN` auf diesen Token setzen — am besten über eine
   lokale, **nicht versionierte** `.env`-Datei neben `docker-compose.yml`
   (dort schon als `${MIMPORT_DISCOGS_TOKEN:-}` vorbereitet). **Nicht** in
   `beets/config.yaml` eintragen, die Datei landet im Git-Repo.
3. Neu starten.

Discogs ist bewusst als **sekundäre** Quelle eingestuft und taucht in der
Praxis nur oben in der Liste auf, wenn MusicBrainz nichts Brauchbares
liefert.

### Wenn ein Cover ausbleibt

Ein einzelner Fehlschlag bei der Cover Art Archive kann dauerhaft dazu
führen, dass ein sonst erfolgreiches Album ohne Cover bleibt — mimport
versucht es deshalb nach einem Import mit MusicBrainz-Treffer automatisch
bis zu dreimal erneut. Reicht das nicht, bleibt am Ende: von Hand
nachtragen über die Album-Seite (siehe „Cover abfotografieren" oben), ein
erneuter, kompletter Import ist dafür nicht nötig.

Ein heruntergeladenes Cover kann dabei auch als `cover.png` statt
`cover.jpg` im Album-Ordner landen (die Cover Art Archive liefert neben
JPEG regelmäßig auch PNG) — mimport erkennt beide Fälle gleichermaßen als
„Cover vorhanden". Bleibt eins trotzdem aus, steht die vollständige
`beet import`/`fetchart`-Ausgabe jetzt immer im Log (nicht nur bei einem
Fehler), das grenzt „kein Treffer bei fetchart" von anderen Ursachen ein.
