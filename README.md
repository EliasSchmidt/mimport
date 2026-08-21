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

`discogs` steht bewusst *nicht* in dieser Liste — siehe
[Discogs als zweite Metadatenquelle](#discogs-als-zweite-metadatenquelle-optional)
weiter unten.

## Cover abfotografieren

Auf dem Handy: Cover fotografieren, Ecken zurechtziehen, fertig. Der Ablauf ist
der eines Dokumentenscanners — Foto, geschätzter Rahmen, Korrektur,
perspektivische Entzerrung auf ein Quadrat.

**Alles davon läuft im Browser.** Das Handy hat das Foto ohnehin und rechnet für
sich, statt ein paar Megabyte hochzuladen und den Laptop zu beschäftigen. Zum
Server geht nur das fertige JPEG, 1000 × 1000 Pixel.

Wohin es kommt, war an beiden Wegen schon vorbereitet:

| Weg | Ziel | Wer nimmt es auf |
|---|---|---|
| Hörbuch | `cover.jpg` im Buchordner | wird beim Bündeln in die m4b eingebettet |
| Musik | `cover.jpg` in der Upload-Session | beets über `fetchart` (`cautious`, `cover_names: cover`) |

Deshalb heißt die Datei in beiden Fällen genau so.

### Nachträglich, für ein schon importiertes Album

Unter „Alben" (`/albums`) lässt sich die Library nach Interpret oder Titel
durchsuchen. „Bearbeiten" öffnet die Detailseite eines Albums (`/albums/<id>`)
mit Cover, allen gesetzten Album-Tags und der Titelliste — auch wenn der
Import längst gelaufen ist.

Zwei Dinge passieren dabei, nicht nur eines: die `cover.jpg` im Albumordner
wird überschrieben (wie oben), und zusätzlich läuft `beet embedart -f` über
die vorhandenen Dateien. Ohne Letzteres bliebe es bei einem neuen Bild im
Ordner — die schon importierten Tracks tragen ihr altes Cover weiterhin in
den eigenen Tags, ein `fetchart`-Automatismus greift nur beim Import selbst.
Wie beim Import selbst teilen sich beide Dienste dafür denselben
Library-Lock (siehe [Zwei Dienste](#zwei-dienste)).

### MusicBrainz-Künstlerlink nachtragen

Auf derselben Seite lässt sich pro Album und pro Titel eine fehlende
MusicBrainz-Artist-ID nachtragen -- etwa wenn der Import als-ist lief oder
MusicBrainz den Namen damals nicht fand. Die Suche ist dieselbe wie beim
manuellen Taggen vor dem Import (`backend.artist_ids`).

Zwei Fallstricke, auf die es dabei ankommt:

* `mb_albumartistid` (einzeln) und `mb_albumartistids` (Liste, beets 2.x)
  teilen sich beim Schreiben ins Datei-Tag denselben Speicherplatz. Wird nur
  das einzelne Feld gesetzt, meldet `beet modify` zwar „geändert" und die
  Datenbank stimmt -- die Datei bleibt aber unverändert, weil das
  gleichzeitig mitgeschriebene leere Listenfeld das einzelne beim Schreiben
  wieder leert. mimport setzt deshalb immer beide zusammen.
* Die Album-Zeile und ihre Titel sind in beets unabhängige Datenbankzeilen.
  Ein Fix für den Album-Interpreten braucht deshalb zwei `beet
  modify`-Aufrufe: einen über die Titel (`album_id:`, schreibt Datenbank
  *und* Datei), einen über die Album-Zeile selbst (`-a id:`, nur für die
  eigene Anzeige, mit `-W` ohne erneuten Dateizugriff). Ein Titel-Interpret
  dagegen ist eine einzelne Zeile -- da genügt ein Aufruf.

## Backcover-Text (OCR)

Für das **manuelle Taggen** kann mimport zusätzlich den Text eines
CD-Backcovers lesen. Dafür läuft `RapidOCR` **serverseitig im Container**;
der Browser lädt nur das Foto hoch. Das Ergebnis erscheint als Rohtext in der
Oberfläche und lässt sich dort mit einfachen Parsern (etwa `01 Titel 3:45`
oder `Artist - Titel`) in eine **weiter bearbeitbare** Trackliste vorfüllen.

Die OCR-Modelle werden beim Serverstart vorab geladen und im Daten-Volume unter
<span class="mono">/data/.rapidocr</span> abgelegt. Danach bleiben sie über
Neustarts hinweg erhalten. Zusätzliche Pakete auf dem Debian-Host braucht der
CPU-Betrieb nicht; alles Nötige steckt im Container-Image.

Für kleine Server begrenzt mimport das Backcover vor der Inferenz auf eine
maximale Bildkante und nutzt `RapidOCR` mit `onnxruntime` auf CPU. Das senkt
den RAM-Bedarf deutlich gegenüber dem bisherigen Paddle-Stack; der
Winkel-Klassifikator bleibt standardmäßig aus.

### In der Liste

Jedes Buch zeigt sein Cover als kleine Kachel, Bücher ohne bekommen einen
Platzhalter derselben Größe — sonst wären die Zeilen unterschiedlich hoch.

Bewusst **ohne Thumbnails**: Die Adresse trägt die Änderungszeit des Bilds
(`?v=…`) und darf deshalb als unveränderlich gelten. Der Browser holt jedes
Cover genau einmal und zeigt ein neu fotografiertes trotzdem sofort, weil sich
die Adresse mitändert. Die 198 KB eines Covers sind damit einmalige Kosten je
Buch — ein Thumbnail (3,5 KB, 32 ms) müsste dagegen irgendwo zwischengelagert
werden, und dafür gibt es keinen guten Ort: nicht im Buchordner (den scannt
Audiobookshelf) und nicht im Staging (das wird bei jedem Start geleert).

Die Liste steht ohnehin nur im DOM, wenn kein Auftrag läuft — der
Zweisekundentakt der Fortschrittsanzeige lädt also keine Bilder nach.

**Auch Cover, die nur in der m4b stecken.** Bücher, die nicht über mimport
kamen, haben oft kein Bild im Ordner, sondern nur eines in der Datei — und
`has_cover` sieht den Ordner. Beim Start holt mimport diese Cover deshalb
einmal heraus und legt sie als `cover.jpg` daneben. Danach ist die Frage „hat
dieses Buch ein Cover" wieder eine reine Dateisystemabfrage, und Audiobookshelf
findet das Bild ebenfalls.

Das läuft im Hintergrund und kostet pro Buch etwa 25 ms zum Nachsehen und
34 ms zum Herausholen (gemessen an 111 MB; ohne `faststart` genauso schnell,
ffprobe springt zum Index statt zu lesen). Drei Dinge sind dabei nicht
selbstverständlich:

- Geprüft wird die **disposition** `attached_pic`, nicht bloß „gibt es eine
  Videospur". Manche Hörbücher bringen ein echtes Video mit, und daraus ein
  Einzelbild in die Bibliothek zu schreiben wäre grob daneben.
- Geschrieben wird **immer JPEG**, auch wenn eingebettet ein PNG steckt. `-c
  copy` wäre schneller, legte dann aber ein PNG namens `cover.jpg` ab — Browser
  kommen damit klar, die eigene Formatprüfung nicht.
- Erst danebenschreiben, dann umbenennen, und nur bei nicht leerer Datei. Eine
  halbe `cover.jpg` machte `has_cover` wahr und ergäbe ein kaputtes Bild.

### Auch nachträglich, wenn die m4b schon steht

Solange Quelldateien im Buchordner liegen, genügt das Bild daneben — der Encode
nimmt es mit. Ist das Buch dagegen fertig gebündelt, sind die Quellen gelöscht
und die m4b ist alles, was es noch gibt. Das Cover wird dann **nachträglich in
die Datei kopiert**, ohne neu zu encodieren (`-c copy`): gemessen 230 MB/s, eine
m4b von 212 MB also in etwa einer Sekunde.

Der Knopf steht deshalb bei jedem Buch, egal in welchem Zustand.

Was dabei zu verlieren wäre, ist das ganze Hörbuch — es gibt keine zweite Kopie.
Also derselbe Weg wie beim Bündeln, nur kürzer:

1. Gearbeitet wird **im Staging**, nicht neben der m4b. Eine zweite Datei im
   Buchordner, und sei es für Sekunden, liest Audiobookshelf bei einem Scan als
   zweites Hörbuch ein.
2. Die neue Datei wird **geprüft, bevor sie die alte ersetzt**: gleiche
   Spieldauer, gleiche Kapitelzahl, genau ein eingebettetes Bild. Weicht etwas
   ab, bleibt das Original stehen.
3. Dieselbe Sperre wie beim Rippen und Bündeln — ein „Neu bauen" darf die Datei
   nicht unter den Händen wegziehen.
4. Zeitlimit auf dem ffmpeg-Aufruf, damit nicht ausgerechnet hier ein hängender
   Prozess an der einzigen Kopie sitzt.

Nachgemessen: Kapitel, Titel und Autor überstehen das Umkopieren unverändert,
und ein bereits vorhandenes Cover wird ersetzt statt gestapelt.

### Wie die Ecken gefunden werden

Der Trick eines Dokumentenscanners, ohne Bibliothek: Bei einem Viereck sind die
Ecken die **Extrema von x+y und x−y**. Es genügt also, mit einem Sobel-Filter
kontrastreiche Punkte zu sammeln und darunter die vier äußersten zu suchen —
keine Linienerkennung, kein OpenCV, kein Wachstum des Images um 200 MB.

Die Schwelle richtet sich nach dem Bild selbst; ein fester Wert fände bei
dunklen Fotos alles und bei hellen nichts. Punkte am äußersten Rand werden
verworfen, sonst zieht ein Finger oder eine Tischkante den Rahmen auseinander.
Findet sich zu wenig, kommt ein ehrlicher Standardrahmen statt geratener Ecken.

Gemessen an einem gemalten, schrägen Viereck: alle vier Ecken auf **1,4 Pixel**
genau. Die Homographie ist gegen eine unabhängige Rechnung in Python geprüft;
beide Prüfungen laufen als Test mit (`tests/test_cover_js.py`, braucht Node).

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

Der Fortschritt zählt den angefangenen Track anteilig mit — `cdparanoia -e`
meldet laufend, wo es steht. Das lohnt sich weniger wegen der Feinheit als bei
**zerkratzten CDs**: liest das Laufwerk dieselbe Stelle minutenlang neu, stünde
der Balken sonst still und man wüsste nicht, ob überhaupt noch etwas passiert.
Meldet cdparanoia dabei Mühe („Kratzer erkannt", „liest langsamer", „Lesefehler"),
steht das dabei.

> Wieder eine Einheit, die man nachrechnen muss: die Zahl in
> `##: 0 [read] @ 1009008` steht in **Samples**, 588 je Sektor — nicht in
> Sektoren und nicht in Bytes. Nur geteilt durch 588 ergeben die gemeldeten
> Werte glatte Sektornummern.

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

### Unfertiges liegt nicht im Buchordner

Discs und m4b entstehen in `/audiobooks/.mimport-unfertig/` und werden erst
fertig an ihren Platz geschoben. Der Grund ist Audiobookshelf: Es scannt die
Bibliothek, und eine halb gelesene Disc oder eine noch wachsende m4b würde es
als unvollständiges Buch einlesen — bei zwölf CDs ein Fenster von Stunden.
Während eines Rips existiert der Buchordner deshalb noch gar nicht.

Warum dieser Ordner *innerhalb* der Bibliothek liegt und nicht im
`/staging`-Volume der Uploads: Das ist ein Named Volume, die Bibliothek ein
Bind-Mount vom Host — **verschiedene Dateisysteme**. Ein Verschieben dorthin
wäre ein Kopiervorgang, bei zwölf CDs also mehrere Gigabyte zweimal geschrieben.
Von hier aus ist es ein `rename`: sofort und ohne zusätzlichen Platz. Der Punkt
am Anfang hält Audiobookshelf davon ab, den Ordner selbst als Buch zu lesen.

Ein Nebeneffekt, den man mitnimmt: Scheitert das Bündeln an der Laufzeitprüfung,
kommt die m4b gar nicht erst ins Buch. Der Zustand „m4b und Quellen liegen
nebeneinander" entsteht dadurch nicht mehr von selbst — nur noch über
*Von vorn einlesen*.

Reste eines Absturzes räumt der nächste Start weg; sonst läge dort dauerhaft
das halbe Hörbuch.

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
Hintergrundauftrag mit Fortschrittsanzeige.

### Bescheid, wenn es fertig ist

Ein Rip dauert eine halbe Stunde, ein Encode Stunden — niemand sitzt daneben.
Deshalb meldet sich die Seite, wenn ein Auftrag endet, in drei Stufen:

| Stufe | Bedingung |
|---|---|
| **Titel des Tabs** bekommt ein `✓` bzw. `✗` | immer, auch über HTTP |
| **Kurzer Zweiklang** | nach dem ersten Klick auf der Seite |
| **Echte Systembenachrichtigung** | **nur über HTTPS oder localhost** |

Die dritte Stufe ist der Haken: Die Notifications-API des Browsers verlangt
einen *secure context*. Über `http://server:8001` gibt es sie **nicht** — dort
bleiben Titel und Ton. Wer sie will, muss den Reverse-Proxy mit TLS betreiben;
das ist derselbe Proxy, der ohnehin für die Authentifizierung zuständig ist.

Damit man das überhaupt bemerkt, steht auf beiden Seiten ein Kasten, der den
eigenen Stand nennt: noch nicht gefragt (mit Knopf), erlaubt, abgelehnt, oder
gar nicht möglich mangels HTTPS. Ohne ihn war das Feature unauffindbar — es gab
nichts zu klicken und nichts zu lesen, und die Erlaubnis wurde nur beiläufig
beim ersten Klick auf einen Knopf angefragt.

Erkannt wird der Übergang an unsichtbaren Zustandsmarkern, die jedes Fragment
mitbringt (`data-auftrag`, `data-zustand`). Gemeldet wird nur der Wechsel von
„läuft" auf einen Endzustand — sonst käme im Zweisekundentakt eine neue Meldung.
Rip und m4b-Bau haben getrennte Marker, weil sie gleichzeitig laufen können.

### Rippen und Bündeln gleichzeitig

Das geht — und ist der Sinn der Sache: während ein Hörbuch encodiert wird, kann
schon die erste Disc des nächsten eingelesen werden. Der m4b-Bau braucht das
Laufwerk nicht, und beide Fortschrittsbalken stehen nebeneinander.

**Dasselbe Buch gleichzeitig geht nicht** und wird abgelehnt. Nachgestellt, weil
es harmloser klingt, als es ist: der m4b-Bau räumt am Ende leere Disc-Ordner
weg und erwischt dabei den Ordner, den der Rip gerade angelegt hat — der Rip
endete mit „No such file or directory".

Auf schwacher Hardware konkurrieren beide um die CPU. Wenn ein Rip auffällig
länger dauert als sonst oder cdparanoia oft „liest langsamer" meldet, lohnt es
sich, nacheinander zu arbeiten.

**Beide Vorgänge zeigen ihre Dauer an** — der Rip „läuft seit 4:32" und am Ende
„9 Tracks gelesen in 23:41", der m4b-Bau zusätzlich die geschätzte Restzeit und
seinen Faktor gegenüber Echtzeit („noch etwa 6:19 (46.2× Echtzeit)"). Der
Fortschritt kommt als Zeitstempel („4:32 von 11:04"), nicht als Kapitelzählung —
ffmpeg encodiert am Stück.

Der Faktor bezieht sich auf das **bisher Fertige**, nicht auf die Gesamtlänge
des Buchs. Mit der Gesamtlänge im Zähler und der wachsenden Laufzeit im Nenner
fällt der Wert wie 1/t: Selbst bei völlig gleichmäßiger Geschwindigkeit sähe das
nach stetiger Verlangsamung aus. Genau so stand es hier, und genau so wurde es
gemeldet.

**Gemessen** auf dem Zielrechner, an *Die Siedler von Catan* (7:21:00): rund
**46× Echtzeit**, macht knapp zehn Minuten für das ganze Buch. Ein sehr langes
Buch von 30 Stunden ist damit nach etwa 40 Minuten durch.

Beide Zeitschranken stehen seither auf begründeten Werten statt auf geratenen:

| Wert | vorher | jetzt | warum |
|---|---|---|---|
| `MIMPORT_M4B_TIMEOUT` | 6 h | **2 h** | 6 h entsprächen einem Hörbuch von 276 Stunden. 2 h lassen selbst bei halber Geschwindigkeit — etwa weil parallel gerippt wird — reichlich Luft. |
| `MIMPORT_M4B_STILLSTAND` | 15 min | **5 min** | 15 Minuten sind länger, als ein kompletter Bau dauert; die Überwachung war damit praktisch wirkungslos. |

Fünf Minuten sind auch nach unten sicher: Die einzige Phase, in der ffmpeg
legitim schweigt, ist das Umschreiben durch `-movflags +faststart` nach der
letzten Fortschrittsmeldung. Nachgemessen sind das bei 111 MB null Sekunden und
selbst auf einer alten Platte deutlich unter einer Minute.

### Wenn ffmpeg hängen bleibt

Drei Wege aus dem Stillstand, in dieser Reihenfolge:

1. **Die Stillstandsüberwachung.** Meldet ffmpeg `MIMPORT_M4B_STILLSTAND`
   Sekunden lang keinen Fortschritt (Vorgabe: 5 Minuten), wird er beendet und
   der Auftrag als fehlgeschlagen markiert. Das ist das schärfere Kriterium als
   eine Wanduhr: ein ehrlicher Encode läuft auf dem alten Laptop stundenlang,
   meldet dabei aber ständig Fortschritt.
2. **Das Zeitlimit** `MIMPORT_M4B_TIMEOUT` als zweite Bremse (Vorgabe: 2
   Stunden), falls ffmpeg zwar Fortschritt meldet, aber nie ankommt.
3. **Der Knopf „Bau abbrechen"** neben dem Fortschrittsbalken, für alles andere.
   Er verschwindet, sobald ffmpeg durch ist und nur noch die Laufzeit geprüft
   und die Datei verschoben wird — ab da verhindert ein Abbruch nichts mehr,
   und ein Knopf, der das nur noch absagt, wäre eine Falle.

In allen drei Fällen wird **nichts gelöscht**. Die Quelldateien fasst erst
`_quellen_loeschen` an, und dorthin führt der Weg nur über die bestandene
Laufzeitprüfung — ein abgebrochener Bau kommt dort nie an. Die halbfertige m4b
liegt im Arbeitsordner und wird mit ihm weggeräumt.

Drei Dinge standen dem vorher im Weg, alle drei behoben:

- Die Schleife über `prozess.stdout` blockierte unbegrenzt. Das Zeitlimit stand
  **dahinter** und konnte deshalb nie greifen — es war toter Code.
- `stderr` ging in eine Pipe, die niemand las. Die fasst 64 KiB, und ffmpeg
  schreibt dorthin rund 210 Byte je Sekunde Laufzeit — auch ohne
  `-loglevel debug`, nachgemessen. Nach gut fünf Minuten wäre sie voll gewesen,
  ffmpeg hätte beim Schreiben blockiert und mimport auf stdout gewartet: beide
  für immer. Nachgestellt und bestätigt. Jetzt geht `stderr` in eine Datei im
  Arbeitsordner, deren Ende bei einem Fehler in der Oberfläche erscheint.
- „Verwerfen" verweigerte die Arbeit, solange der Auftrag lief — und er lief
  für immer. Damit sperrte `_buch_belegt()` das Buch dauerhaft, und nur ein
  Neustart des Containers half.

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

### Mit erfundenen Alben ausprobieren, ohne die echte Library anzufassen

```bash
uv run python scripts/mock.py            # baut .mock/ (falls nötig) und startet den Server
uv run python scripts/mock.py --reset    # .mock/ verwerfen und neu aufbauen
```

Legt vier erfundene Alben (kein Treffer bei MusicBrainz möglich, ein Sampler,
ein Featuring-Track, eins mit schon gesetztem Label/Katalognummer) in einer
komplett isolierten, gitignorten beets-Bibliothek unter `.mock/` an und startet
den Server direkt darauf (`http://127.0.0.1:8000/albums`). Rührt nicht an
`~/.config/beets` oder eine echte Library.

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
| `MIMPORT_BEET_BIN` | `beet` | Pfad zum beets-Executable |
| `MIMPORT_MOVE` | `1` | Dateien verschieben (`0` = kopieren) |
| `MIMPORT_MAX_UPLOAD_BYTES` | 4 GB | Obergrenze pro Upload |
| `MIMPORT_MAX_FILES` | `500` | Obergrenze für die Dateianzahl |
| `MIMPORT_MIN_FREE_BYTES` | 2 GB | Sicherheitsabstand auf dem Dateisystem |
| `MIMPORT_MAX_STAGING_BYTES` | 20 GB | Obergrenze für alle Uploads zusammen |
| `MIMPORT_SESSION_TTL_HOURS` | `24` | Frist, nach der verwaiste Uploads verschwinden |
| `MIMPORT_IMPORT_TIMEOUT` | `1800` | Zeitlimit des Importlaufs in Sekunden |
| `MIMPORT_FINGERPRINT` | `0` | AcoustID-Fingerprinting (siehe unten) |
| `MIMPORT_DISCOGS_TOKEN` | leer | Discogs als zweite Metadatenquelle (siehe unten) |

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

## Auf dem Handy

Ein guter Teil der Bedienung passiert vom Telefon aus — Cover fotografieren,
nach einem Rip nachsehen. Geprüft wird gegen **360 Pixel**, das schmalste,
womit realistisch zu rechnen ist, und das Kriterium ist objektiv: Läuft der
Inhalt breiter als das Fenster, muss die Seite seitlich gescrollt werden.

Die Tabellen sind dort das Problem — fünf Spalten aus Namen, Zahlen und Knöpfen
passen nicht nebeneinander. Unterhalb von 40 rem werden sie deshalb zu Karten:
jede Zeile ein Block, die Spaltenüberschrift wandert vor den Wert (`Discs: 1`).
Ohne das quetschte sich ein Dateiname in 60 Pixel und brach mitten im Wort um.

`tests/test_mobil.py` misst das mit einem echten Browser und schlägt fehl,
sobald etwas übersteht. Ohne Playwright wird der Test übersprungen.

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

## Discogs als zweite Metadatenquelle (optional)

Hilft bei Releases, die MusicBrainz nicht kennt — etwa manche Vinyl-Pressungen,
Bootlegs oder Nischen-Compilations, die auf Discogs eher zu finden sind als in
MusicBrainz.

**Ohne eigenen Zugriffstoken bleibt Discogs komplett aus**, und das ist kein
Bug, sondern Absicht: Das `discogs`-Plugin authentifiziert sich beim Laden
synchron und würde ohne Token interaktiv nach einem OAuth-Code fragen
(`beets.ui.input_`) — mimport läuft aber ohne Terminal, das wäre also ein
Absturz oder ein für immer hängender Request, und zwar bei *jedem* Request,
nicht nur bei Discogs-Suchen, weil dieselbe Plugin-Ladung auch fürs
MusicBrainz-Matching läuft. Deshalb trägt `backend/beets_env.py` das Plugin
nur dann in die Konfiguration ein, wenn `MIMPORT_DISCOGS_TOKEN` gesetzt ist —
das Vorhandensein des Tokens *ist* der Schalter, kein separates Flag.

Einschalten:

1. Auf discogs.com unter *Einstellungen → Entwickler* einen persönlichen
   Zugriffstoken erzeugen.
2. `MIMPORT_DISCOGS_TOKEN` auf diesen Token setzen — im Container am besten
   über eine lokale, **nicht versionierte** `.env`-Datei neben
   `docker-compose.yml` (dort schon als `${MIMPORT_DISCOGS_TOKEN:-}`
   vorbereitet). **Nicht** in `beets/config.yaml` eintragen: die Datei liegt
   im Git-Repo und wird ins Image gebacken.
3. Neu starten.

Discogs ist bewusst als **sekundäre** Quelle eingestuft: In
`beets/config.yaml` steht dafür
`discogs.data_source_mismatch_penalty: 1.0` — das Maximum, das beets zulässt.
Ein Discogs-Kandidat schneidet damit gegenüber einem sonst gleichwertigen
MusicBrainz-Kandidaten immer schlechter ab und taucht in der Praxis nur dann
oben in der Liste auf, wenn MusicBrainz nichts Brauchbares liefert.

Ein Nebeneffekt, den man kennen sollte: Sobald zwei Metadatenquellen aktiv
sind, bezieht beets die Datenquelle grundsätzlich in die Distanzrechnung mit
ein — das verschiebt auch die angezeigte Sicherheit reiner
MusicBrainz-Treffer minimal (typischerweise leicht nach unten, da
MusicBrainz bewusst auf dem eingebauten Standardwert bleibt statt auf einen
eigens auf 0 gesetzten Wert, der Treffer hätte künstlich aufwerten können).
Das ist eine Eigenheit von beets selbst, keine mimport-Besonderheit.

### Warum Discogs sein Cover selbst mitbringt

Ein MusicBrainz-Kandidat bekommt sein Cover automatisch beim späteren
`beet import -A` — fetchart fragt dort mit der übernommenen Release-ID die
Cover Art Archive ab (siehe `fetch_for_asis` weiter oben). Für Discogs gibt es
diesen Weg nicht: Die Release-ID allein reicht dort nicht, das Bild kommt aus
dem Suchergebnis selbst (`AlbumInfo.cover_art_url`) — und dieses Feld ist kein
einbettbarer Tag. mimport trennt Tag-Schreiben und den `beet import -A`-Lauf
aber bewusst in zwei Prozesse (siehe "Wie der Import abläuft" unten); dazwischen
wäre das Feld verloren.

Deshalb lädt mimport das Bild bei einem Discogs-Match direkt beim Übernehmen
des Kandidaten selbst herunter und legt es als `cover.jpg` in die Session —
genau dort, wo auch ein abfotografiertes Cover landet. Die ganz normale
`filesystem`-Quelle von fetchart übernimmt es dann beim Import, ohne eigene
Anbindung. Ein bereits abfotografiertes Cover geht immer vor und wird nicht
überschrieben; schlägt der Download fehl (Netzausfall, kein Bild bei diesem
Release), bleibt es beim manuellen Fotografieren — siehe `backend/cover.py`,
`von_url_holen()`.

### Wenn bei MusicBrainz mal kein Cover ankommt

Das ist kein Rate-Limit und kein Hinweis auf zu viele Anfragen: Die Cover Art
Archive (`coverartarchive.org`, gehostet über archive.org) antwortet
gelegentlich mit einem transienten `500`, und beets' `fetchart`-Plugin hat für
genau diese Quelle **keine** eingebaute Wiederholung — anders als das
`musicbrainz`-Plugin, das für seine eigenen Anfragen automatisch erneut
versucht. Ein einzelner Fehlschlag bedeutet also dauerhaft kein Cover für
dieses Album, obwohl MusicBrainz eins hat. Nachtragen geht über die
Album-Seite (siehe „Cover abfotografieren" oben) — ein erneuter, kompletter
Import ist dafür nicht nötig.

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
