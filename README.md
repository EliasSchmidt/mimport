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

Auf einer gemeinsamen Seite war jeweils die Hälfte der Bedienelemente Ballast —
und ein laufender Hörbuch-Rip tauchte im Musikbereich als eigener Auftrag auf,
obwohl er dort nichts zu suchen hat. Es gibt nur ein Laufwerk und damit einen
Auftrag; die jeweils andere Seite meldet jetzt bloß, dass es belegt ist.

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
# Zielverzeichnisse in docker-compose.yml anpassen (/srv/music, /srv/audiobooks)
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
| `/audiobooks` | Hörbücher. Getrennt von der Musik, ohne beets. |

## Eine bestehende beets-Library übernehmen

### Die Datenbank überlebt den Neubau

`mimport-data` ist ein Named Volume. Es übersteht `docker compose up -d --build`
und auch `docker compose down`. Weg ist es nur nach `docker compose down -v`
oder einem ausdrücklichen `docker volume rm`.

### Wofür die Datenbank überhaupt da ist

Sie ist der **Katalog, nicht die Musik**. Die Tags stehen in den Dateien selbst;
die `library.db` führt Buch darüber, was in der Sammlung ist, wo es liegt und
mit welchen MusicBrainz-IDs.

Für mimport gilt: **das Matching öffnet sie nie**, nur der Import schreibt
hinein. Geht sie verloren, kostet das die Duplikaterkennung (`duplicate_action:
skip` hat dann nichts zum Vergleichen) sowie `beet list`, `stats`, `move` und
nachträgliche Plugin-Läufe. Wiederherstellbar ist sie, indem man die Sammlung
erneut importiert — ärgerlich, nicht katastrophal.

### Eine vorhandene `library.db` hereinholen

Vorher **von Hand kopieren**. beets legt zwar selbst eine Sicherung an, bevor es
ein Schema migriert, aber danach kann eine ältere beets-Installation dieselbe
Datei nicht mehr lesen — genau die Falle, die weiter oben beschrieben ist.

Und die ist hier kein Gedankenspiel: Der Zielrechner hat **beets 2.1.0**
installiert, der Container bringt **2.13.1** mit. Ausweichen geht nicht, denn
mimport braucht `beets.metadata_plugins`, und das Modul gibt es in 2.1.0 noch
nicht — es ist dieselbe Umstellung, mit der MusicBrainz zum Plugin wurde.

Sobald der Container die Datenbank einmal geöffnet hat, ist sie für das
System-beets verloren. Zur Wahl stehen:

- **Das beets auf dem Host mitziehen** auf dieselbe Version. Dann bleiben beide
  Wege offen. Maßgeblich ist, was `uv.lock` festschreibt — derzeit **2.13.1**
  (nachsehen mit `grep -A1 '^name = "beets"' uv.lock`). Zwei Dinge dabei nicht
  vergessen: das Extra für `lastgenre` mitinstallieren (`beets[lastgenre]`,
  sonst fehlt `pylast`), und **`musicbrainz` in die Konfiguration des Hosts
  eintragen** — ab dieser Version ist es ein Plugin, ohne den Eintrag hat auch
  das Host-beets keine Metadatenquelle mehr.
- **Auf dem Host nicht mehr direkt importieren** und alles über die Oberfläche
  laufen lassen. Für `beet list` und Ähnliches dann
  `docker compose exec mimport beet …` benutzen.

Was nicht funktioniert, ist beides gleichzeitig ohne Versionsabgleich.

Dann entscheidet ein Blick, wie viel Arbeit der Umzug macht:

```bash
sqlite3 /pfad/zur/library.db "SELECT path FROM items LIMIT 5;"
```

beets speichert Pfade **relativ zu `directory`**, solange die Dateien darunter
liegen — nachgemessen mit 2.13: `Album/song.flac`. Nur was außerhalb liegt,
steht mit absolutem Pfad drin.

- **Relative Pfade:** Datei nach `/data/library.db` kopieren, Musikverzeichnis
  auf `/music` mounten, fertig. Wo es auf dem Host liegt, ist gleichgültig.
- **Absolute Pfade:** Einfacher, als die Datenbank umzuschreiben, ist es, den
  Pfad im Container gleich zu lassen — also etwa `/srv/music:/srv/music`
  einhängen und `directory: /srv/music` setzen.

### Plugins

Voreingestellt sind `musicbrainz`, `lastgenre`, `fetchart` und `embedart`.

**`musicbrainz` darf nicht fehlen.** In beets 2.x ist es ein Plugin, und ohne
das gibt es keine einzige Metadatenquelle — die Oberfläche bliebe leer.

`lastgenre` braucht `pylast`; das kommt über das Extra `beets[lastgenre]` in
`pyproject.toml`. `fetchart` und `embedart` brauchen nichts weiter — solange bei
`embedart` kein `maxwidth` gesetzt ist, skaliert es nicht und kommt ohne
ImageMagick aus.

Eine Eigenheit, die man kennen sollte: Das `musicbrainz`-Plugin liefert von sich
aus **keine** Genres (`genres: False`). `lastgenre` tritt also nicht gegen
MusicBrainz an. Mit `force: yes` und `keep_existing: no` überschreibt es aber ein
Genre, das bereits **in der hochgeladenen Datei** stand.

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

## Eine Audio-CD rippen

Die andere Sorte CD: **kein Dateisystem**, die Tracks lassen sich nicht
kopieren, sie müssen ausgelesen werden. Deshalb geht hier auch kein Host-Mount
— das Gerät selbst muss in den Container.

```yaml
# in docker-compose.yml, Dienst mimport-cd
devices:
  - /dev/sr0:/dev/sr0
group_add:
  - "24"        # ANPASSEN, siehe unten
```

Die Gruppen-ID ist die einzige Angabe, die vom Rechner abhängt:

```bash
ls -l /dev/sr0 && getent group cdrom
```

`/dev/sr0` gehört üblicherweise `root:cdrom` mit Modus 660 — der Container-Nutzer
muss also in dieser Gruppe sein, sonst gibt es „Permission denied". Auf Debian
ist die gid meist 24; nachsehen ist billiger als raten.

### Wie es abläuft

1. **Inhaltsverzeichnis lesen** (`cdparanoia -Q`). Daraus wird die
   **MusicBrainz-DiscID** berechnet.
2. **Track für Track lesen**, nicht die CD am Stück. Das kostet nichts und
   bringt zweierlei: der Fortschritt ist exakt bekannt, und ein unlesbarer Track
   reißt nicht den ganzen Lauf mit. Jeder Track wird direkt nach FLAC gepackt,
   das WAV dazwischen sofort gelöscht — es belegt das Vierfache.
3. **DiscID bei MusicBrainz nachschlagen.** Das ist der eigentliche Trick: eine
   frisch gerippte CD hat **keine Tags**, und `tag_album` leitet seine
   Suchbegriffe sonst aus genau denen ab. Über die DiscID trifft der Match
   trotzdem exakt — sie identifiziert die Pressung, nicht nur das Album.
4. Ab da wie immer: Dateiliste, Kandidaten, Import.

Kennt MusicBrainz die CD nicht, bleibt die Eingabe von Hand — Künstler und Album
stehen ja bereits als Felder bereit.

**Beim Packen wird die Tracknummer gesetzt**, und das ist nicht kosmetisch. Ohne
sie ordnet beets die Dateien allein nach Spieldauer den Tracks des Releases zu —
bei ähnlich langen Stücken kommt dabei eine vertauschte Reihenfolge heraus.
Nachgemessen: ohne Tracknummer landet Datei 1 auf Track 6, mit ihr stimmt jede
Zuordnung auf die Sekunde. Beides steht als Test in `tests/test_rip.py`.

**Die Sicherheit fällt nach einem Rip niedrig aus — etwa ein Drittel — und das
ist in Ordnung.** beets kann keine Titel vergleichen, weil auf einer Audio-CD
keine stehen; dieser Abzug bleibt zwangsläufig. Aussagekräftig sind hier die
Spieldauern und die Tracknummern, nicht die Prozentzahl.

Der Rip läuft im Hintergrund, die Oberfläche fragt den Stand alle zwei Sekunden
ab. **Ein Laufwerk heißt ein Auftrag**: mehr Verwaltung als „läuft gerade einer?"
braucht es nicht.

### Warum cdparanoia und nicht abcde

`abcde` rippt, encodiert **und taggt** aus CDDB. Das Taggen kollidiert direkt
mit „mimport schreibt die Tags selbst" — es müsste also abgeschaltet werden,
und übrig bliebe ein Shell-Wrapper um genau das, was `cdparanoia` und `flac` in
zwei Schritten selbst tun.

Beide Pakete sind zusammen unter 2 MB. Das ist der Unterschied zum
Fingerprinting weiter unten, das die libav\*-Dekoder nachzieht.

## Hörbücher

Ein eigener Weg neben dem Musikweg, und zwar bewusst: **für Hörbücher gibt es
keinen Match und keinen beets-Import.** MusicBrainz kennt sie praktisch nicht,
der Kandidaten-Dialog hätte nichts zu zeigen. Die Metadaten holt sich
Audiobookshelf später selbst über den Audible-Provider. mimport endet hier beim
fertigen Buchordner.

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

**Nächste CD** übergibt den Buchpfad, damit Autor und Titel nicht erneut
eingetippt und nicht ein zweites Mal entschärft werden — aus einem schon
bereinigten Namen könnte sonst ein abweichender Ordner entstehen.

**Von vorn einlesen** ist für den Fall, dass sich der Rip als fehlerhaft
erweist. Die fertige m4b wird dabei **umbenannt, nicht gelöscht**: sie bekommt
`.ersetzt` angehängt und gilt damit weder für mimport noch für Audiobookshelf
als Hörbuch. Wäre sie gelöscht, stünde man nach einem gescheiterten zweiten
Versuch mit nichts da; bliebe sie liegen, zeigte Audiobookshelf das Buch
stundenlang doppelt an, solange der neue Rip läuft. Umbenannt wird erst, wenn
das Einlesen wirklich angelaufen ist — ein Fehlstart lässt das Buch unverändert.

Der Buchordner **ist** der Zustand — welche Discs schon eingelesen sind, steht
im Dateisystem und in keiner Datenbank. Ein Neustart mitten in einem
zwölfteiligen Hörbuch verliert deshalb nichts. Für die nächste Disc einfach
dieselben Angaben erneut eintragen.

Audio-CDs werden gerippt, Daten-CDs kopiert; was von beidem vorliegt,
entscheidet mimport selbst. Eine **erste** Daten-CD landet flach im Buchordner
statt in einem `CD 1`, das für immer allein bliebe — eine MP3-CD trägt meistens
das ganze Buch.

### Bündeln zur m4b

**Erst alle Discs einlesen, dann einmal bündeln.** Das ist keine Stilfrage: beim
Bündeln werden die Quelldateien gelöscht. Wer nach Disc 1 bündelt und danach
Disc 2 einliest, hat für einen zweiten Bau nur noch Disc 2 — der Inhalt von
Disc 1 liegt dann nirgends mehr vor. mimport lehnt einen zweiten Bau deshalb ab
und nennt beide Laufzeiten; erzwingen lässt es sich, aber nur ausdrücklich.

**Eine Datei je Buch**, alle Discs zusammengeführt: aus zwölf CDs FLAC (gut
4 GB) wird eine `<Titel>.m4b` von etwa 300 MB, 64 kbit/s in Mono. Eine Lesung
braucht keine Musikqualität, und genau darum geht es hier. Ein `cover.jpg` im
Buchordner wird eingebettet.

### Kapitelnamen

Kapitelgrenzen sind die Trackgrenzen. Für den Namen gilt der Reihe nach:

1. **Eigene Angabe.** Beim Bündeln lässt sich eine Liste eintragen — eine Zeile
   je Kapitel, in der Reihenfolge der Tracks.
2. **Das Titel-Tag**, sofern jeder Track eines hat und keines doppelt vorkommt.
   MP3-Hörbuch-CDs bringen meist brauchbare mit.
3. **Durchgezählt** — `Kapitel 1` … `Kapitel N`.

Der dritte Fall ist beim Rip der Normalfall, und er ist wichtiger, als er
aussieht: Eine gerippte Audio-CD hat gar keine Tags, und der Dateiname taugt
nicht als Ersatz, weil der Rip auf jeder Disc wieder bei `01` anfängt. Bei zwölf
CDs stünde `01 Track 1` sonst zwölfmal in der Kapitelliste. Sind Titel zwar
vorhanden, aber mehrfach vergeben, wird ihnen die laufende Nummer vorangestellt
(`1. Intro`, `2. Intro`) — der Name bleibt erhalten, eindeutig wird er
trotzdem.

Zwei Bremsen, beide aus schmerzhafter Erfahrung:

- **Verlustbehaftete Quellen werden nicht umgewandelt.** MP3 nach AAC ist lossy
  auf lossy und bringt nichts außer Verlust. Audiobookshelf spielt einen Ordner
  mit MP3s ohnehin klaglos. Wer trotzdem eine Einzeldatei will, kann es
  erzwingen.
- **Gelöscht wird erst, wenn die Laufzeit stimmt.** Die m4b muss auf
  **1500 Millisekunden** genau so lang sein wie die Summe der Quellen. Das ist
  bewusst ein fester Wert und kein Prozentsatz: das Padding des Encoders liegt
  unabhängig von der Gesamtlänge im Millisekundenbereich, ein fehlender Track
  dagegen sind immer Minuten. Passt es nicht, bleibt alles liegen und man hört
  selbst hinein.

Warum überhaupt gelöscht wird: Audiobookshelf liest *alle* Audiodateien eines
Buchordners als Tracks desselben Buchs. Bleiben die FLACs neben der m4b liegen,
steht das Buch doppelt in der Bibliothek.

Ein Encode läuft auf schwacher Hardware Stunden, deshalb wieder ein
Hintergrundauftrag mit Fortschrittsanzeige. Der Fortschritt kommt als
Zeitstempel („4:32 von 11:04"), nicht als Kapitelzählung — ffmpeg encodiert am
Stück.

> Eine Falle am Rande, nachgemessen statt geglaubt: `ffmpeg -progress` liefert
> einen Schlüssel `out_time_ms`, dessen Wert in **Mikrosekunden** steht. mimport
> liest deshalb `out_time_us`, das wenigstens ehrlich benannt ist.

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
| `MIMPORT_CDROM` | `/dev/sr0` | Laufwerk für Audio-CDs |
| `MIMPORT_CDPARANOIA` | `cdparanoia` | Pfad zum Ripper |
| `MIMPORT_FLAC` | `flac` | Pfad zum FLAC-Encoder |
| `MIMPORT_RIP_TOC_TIMEOUT` | `60` | Zeitlimit fürs Inhaltsverzeichnis (s) |
| `MIMPORT_RIP_TRACK_TIMEOUT` | `1200` | Zeitlimit je Track (s) |
| `MIMPORT_AUDIOBOOKS` | `/audiobooks` | Wurzel der Hörbuch-Bibliothek |
| `MIMPORT_M4B_BITRATE` | `64k` | Zielbitrate der m4b |
| `MIMPORT_M4B_MONO` | `1` | Auf einen Kanal mischen |
| `MIMPORT_M4B_MIN_KBPS` | `96` | Darunter gilt Umwandeln als nicht lohnend |
| `MIMPORT_M4B_TIMEOUT` | `21600` | Zeitlimit für den m4b-Bau (s) |
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

### Unterbrochene Sitzungen

Die Session-ID entsteht beim Upload und steht ausschließlich im ausgelieferten
HTML — es gibt kein Cookie und keinen gespeicherten Zustand im Browser. Ein
geschlossener Tab, ein leerer Akku oder ein Neuladen hätte damit den Upload
gekostet, obwohl die Dateien noch im Staging liegen.

Deshalb listet die Startseite auf, was dort liegt: Auswahl, Dateizahl, Größe,
Alter, dazu **Fortsetzen** und **Verwerfen**. Weil die Liste serverseitig
entsteht, funktioniert sie auch von einem anderen Gerät — am Rechner hochladen,
am Telefon weitermachen.

Das ist bewusst *keine* Zuordnung zu einem Benutzer: jeder sieht jede offene
Sitzung. Bei einem Dienst hinter einem Reverse-Proxy mit Authentifizierung und
einer überschaubaren Zahl vertrauter Nutzer ist das die einfachere und
robustere Lösung — es gibt nichts, was kaputtgehen kann, wenn ein Header mal
fehlt. Soll es später doch pro Benutzer getrennt sein, wird die Liste gefiltert;
wegwerfen muss man sie dafür nicht.

Der Rip macht das schon länger richtig, allerdings aus einem anderen Grund: es
gibt genau ein Laufwerk und damit genau einen Auftrag, den jeder sieht.

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
