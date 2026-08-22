# mimport — Kontext für die Arbeit am Code

Diese Datei ist für KI-Assistenten und Entwickler, die an mimport arbeiten:
Architektur, Begründungen, Messwerte, bekannte Fallstricke. Was die
Anwendung tut und wie man sie betreibt, steht in [`README.md`](README.md) --
das ist für Menschen, die mimport benutzen, nicht für Menschen (oder KIs),
die daran arbeiten. Bitte nichts von hier zurück in die README schreiben,
ohne den Unterschied im Kopf zu behalten.

## Zielumgebung

Der Produktivserver ist ein alter Windows-8-Laptop mit Debian, knappe
Ressourcen. Das ist kein Nebendetail, sondern das Auslegungskriterium für so
gut wie jede Performance- und Speicherentscheidung im Code: OCR läuft
absichtlich CPU-only mit begrenzter Bildkante, der m4b-Bau mischt auf Mono
herunter, AcoustID ist standardmäßig aus. Wenn eine Änderung mehr RAM, mehr
CPU oder ein zusätzliches Systempaket braucht, gegen diesen Rechner denken,
nicht gegen eine Entwicklungsmaschine.

## Architektur

| Datei | Aufgabe |
|---|---|
| `backend/beets_env.py` | beets-Konfiguration und Plugins laden, Zustand prüfen |
| `backend/matching.py` | Kandidaten holen und für die Anzeige aufbereiten |
| `backend/audio.py` | Formaterkennung verlustfrei/verlustbehaftet |
| `backend/tagging.py` | gewählte Metadaten in die Dateien schreiben |
| `backend/importer.py` | `beet import` aufrufen, Hintergrundauftrag mit Fortschritt |
| `backend/sessions.py` | Staging-Ordner, Pfad-Absicherung |
| `backend/routes.py` | Endpunkte (HTML-Fragmente für HTMX) |
| `templates/` | Seite und Fragmente |
| `static/index.js` | Vorprüfung im Browser, Upload |

**Das Matching öffnet nie die beets-Datenbank** -- `tag_album` braucht nur
geladene Plugins. Nur der Import (und `beet embedart`/`fetchart` in
`backend/albums.py`) fasst die Library an, und beide teilen sich deshalb
denselben Datei-Lock (`importer.library_lock()`).

Endpunkte sind bewusst als `def`, nicht `async def`, wo sie `beet` oder
MusicBrainz aufrufen -- das sind Sekunden-lange synchrone Aufrufe, die in
einer echten `async`-Funktion den gesamten Server anhielten. FastAPI führt
`def`-Endpunkte in einem Threadpool aus.

## Wie der Import abläuft, und warum nicht `--search-id`

mimport schreibt die Tags des gewählten Kandidaten **selbst** in die
Dateien (`AlbumMatch.apply_metadata()` plus `Item.try_write()`, siehe
`backend/tagging.py`) und ruft beets dann mit `-A` auf -- also **ohne**
erneutes Autotagging. beets übernimmt die Tags wie sie sind und kümmert
sich nur noch um Umbenennen und Einsortieren.

Der naheliegende Weg, beets stattdessen die gewählte Release-ID mitzugeben
(`beet import -q --search-id <MBID>`), funktioniert **nicht**. In
`beets/ui/commands/import_/session.py` entscheidet `_summary_judgment`:

```python
if config["import"]["quiet"]:
    if rec == Recommendation.strong:
        return importer.Action.APPLY
    action = config["import"]["quiet_fallback"].as_choice({"skip": ..., "asis": ...})
```

Im Quiet-Modus wendet beets einen Match **nur bei `Recommendation.strong`**
an; alles darunter wird übersprungen oder unverändert importiert.
`--search-id` schränkt lediglich die Suche ein, es erzwingt keine
Anwendung. Ein bewusst bestätigter Match mit 64 % Sicherheit -- der
Normalfall bei unvollständigen Uploads -- wäre also stillschweigend
ignoriert worden. Mit `-A` läuft beets über `import_asis` und erreicht diese
Abfrage gar nicht.

## Import-Hintergrundauftrag (`backend/importer.py`)

Ein echter (nicht simulierter) Import läuft im Hintergrundthread
(`importer.start_job`, `ImportJob`) statt in der Anfrage selbst: er kann an
der Library-Sperre warten, ein großes Album verschieben und ein Cover
nachladen, das kann insgesamt spürbar dauern. Die Oberfläche fragt den
Stand per SSE ab (`GET /import/{id}/events`) -- dasselbe Muster wie beim
Rip (`backend/rip.py`) und beim m4b-Bau. Der Probelauf (`--pretend`) bleibt
synchron: er schreibt nichts und lädt kein Cover nach, das passt locker in
eine Anfrage.

Die Phasenauflösung ist grob, nicht zeilenweise: `run_import()` liefert
seine Ausgabe erst am Ende des Subprozesses, es gibt also nur "läuft" und
"fertig, räumt auf" als Zwischenstände. Für echte Line-by-Line-Progress
müsste der Subprozess über `Popen` gestreamt werden -- dabei unbedingt
`stderr=subprocess.STDOUT` setzen, siehe den m4b-Postmortem weiter unten
zum unread-stderr-Deadlock, der sonst hier genauso zuschlagen würde.

**Bekannte Grenze:** Ein Neuladen der Seite mitten im Import hängt sich
nicht wieder an den laufenden Auftrag -- anders als `/rip`, das beim Laden
aus `rip.current()` rendert. `importer.current(session_id)` gäbe es dafür
her, nur ist die Seite bisher nicht so verdrahtet.

## SSE-Fortschritt: das `hx-target`-Footgun

Rip, m4b-Bau und Import pushen ihren Fortschritt per Server-Sent Events
statt zu pollen (`_rip_events_strom`, `_audiobook_events_strom`,
`_import_events_strom` in `backend/routes.py`). Alle folgen demselben
Markup-Muster, und das aus gutem Grund:

```html
<div hx-ext="sse" sse-connect="/…/events" sse-close="fertig"
     hx-trigger="sse:fertig" hx-get="/…" hx-target="#aussen" hx-swap="innerHTML">
    <div sse-swap="fortschritt" hx-target="this" hx-swap="innerHTML">
        {% include "…_fortschritt.html" %}
    </div>
</div>
```

Das innere `sse-swap`-Div **braucht sein eigenes `hx-target="this"`**. Ohne
das erbt es das `hx-target` des äußeren Divs -- dann swapt der erste
"fortschritt"-Tick das äußere Element komplett und reißt das innere Div
samt seinem eigenen SSE-Listener aus dem DOM. Der nächste Tick findet ihn
nicht mehr, meldet sich still ab: genau ein Update, dann für immer Stille,
ohne Fehlermeldung. Bei jedem neuen SSE-Block dieses Musters zuerst
prüfen, ob dieses `hx-target="this"` da ist.

## beets-Datenbank: die Versions-Falle

Der Zielrechner hatte **beets 2.1.0** installiert, der Container bringt
**2.13.1** mit. beets legt vor einer Schema-Migration selbst eine Sicherung
an, aber danach kann die ältere Installation dieselbe `library.db` nicht
mehr lesen -- eine Einbahnstraße. Ausweichen (beide Versionen parallel
nutzen) geht nicht: mimport braucht `beets.metadata_plugins`, das gibt es
in 2.1.0 noch nicht -- dieselbe Umstellung, mit der MusicBrainz zum Plugin
wurde. **`musicbrainz` darf deshalb in keiner beets-Konfiguration fehlen**,
die mimport zugrunde liegt -- ohne das Plugin gibt es keine einzige
Metadatenquelle, die Oberfläche bliebe leer.

`lastgenre` liefert selbst dann kein Genre, wenn `musicbrainz` es nicht
tut (`genres: False` bei `musicbrainz`) -- die beiden Plugins konkurrieren
also nicht.

**`lastgenre` läuft nicht mehr automatisch beim Import (`auto: no`),
sondern explizit danach** (`albums.refresh_genre_after_import()`, aufgerufen
aus `routes.run_import()`). Grund: Die automatische Import-Stufe ruft laut
eigenem Quellcode immer `try_sync(write=False)` auf, und beets' Pipeline
schreibt unter `-A` (as-is, das mimport immer nutzt) ohnehin nichts zurück
(`ImportTask.manipulate_files` nur bei `Action.APPLY`/`RETAG`, nie bei
`ASIS`). Ein von Last.fm gefundenes Genre landete also zuverlässig nur in
der Datenbank, nie in der Datei -- nachgewiesen an Album 36 (Cecilia
Bartoli): `beet ls` zeigte für alle 21 Titel ein Genre, keine einzige Datei
hatte eins. Dass es bei anderen Alben trotzdem "funktionierte", war ein
Nebeneffekt von `retry_missing_cover()`: ein erfolgreich eingebettetes
Cover schreibt über `embedart` beiläufig alle Feldwerte inklusive Genre mit
in die Datei. Der explizite `beet lastgenre --no-force`-Aufruf benutzt
dagegen `ui.should_write()` und schreibt tatsächlich. Anker ist die
MusicBrainz-Release-ID, wenn vorhanden, sonst Albumkünstler+Album aus den
Dateien (Handtag-Import über `/musik` setzt nie eine Release-ID, siehe
`routes._album_kernfelder_der_session`). `force: no` und `keep_existing:
no` sorgen dafür, dass ein manuell gesetztes Genre unangetastet bleibt --
Last.fm füllt nur eine bislang leere Lücke.

## `mb_albumartistid` vs. `mb_albumartistids`

`mb_albumartistid` (einzeln) und `mb_albumartistids` (Liste, beets 2.x)
teilen sich beim Schreiben ins Datei-Tag denselben Speicherplatz. Wird nur
das einzelne Feld gesetzt, meldet `beet modify` zwar „geändert" und die
Datenbank stimmt -- die Datei bleibt aber unverändert, weil das
gleichzeitig mitgeschriebene leere Listenfeld das einzelne beim Schreiben
wieder leert. **Immer beide zusammen setzen.** Dasselbe Paar gibt es für
Track-Interpreten (`mb_artistid`/`mb_artistids`); `backend/tag_catalog.py`
hält das an allen Stellen konsistent zusammen.

Album-Zeile und ihre Titel sind in beets unabhängige Datenbankzeilen. Ein
Fix für den Album-Interpreten braucht deshalb zwei `beet modify`-Aufrufe:
einen über die Titel (`album_id:`, schreibt Datenbank *und* Datei), einen
über die Album-Zeile selbst (`-a id:`, nur für die eigene Anzeige, mit
`-W` ohne erneuten Dateizugriff). Ein Titel-Interpret ist dagegen eine
einzelne Zeile -- da genügt ein Aufruf.

## Discogs als sekundäre Quelle

`backend/beets_env.py` trägt das `discogs`-Plugin nur ein, wenn
`MIMPORT_DISCOGS_TOKEN` gesetzt ist -- kein separates Flag, das
Vorhandensein des Tokens *ist* der Schalter. Grund: Das Plugin
authentifiziert sich beim Laden synchron und würde ohne Token interaktiv
nach einem OAuth-Code fragen (`beets.ui.input_`); mimport läuft ohne
Terminal, das wäre ein Absturz oder ein für immer hängender Request, und
zwar bei *jedem* Request, weil dieselbe Plugin-Ladung auch fürs
MusicBrainz-Matching läuft.

`discogs.data_source_mismatch_penalty: 1.0` in `beets/config.yaml` ist das
Maximum, das beets zulässt -- ein Discogs-Kandidat schneidet gegenüber
einem gleichwertigen MusicBrainz-Kandidaten immer schlechter ab. Sobald
zwei Metadatenquellen aktiv sind, bezieht beets die Quelle grundsätzlich in
die Distanzrechnung ein -- das senkt auch die angezeigte Sicherheit reiner
MusicBrainz-Treffer leicht, weil MusicBrainz auf dem beets-Standardwert
bleibt statt auf einem künstlich auf 0 gesetzten. Eigenheit von beets
selbst, keine mimport-Besonderheit.

**Warum ein Discogs-Treffer keine `mb_*`-Tags bekommt:** beets benennt
Release-/Release-Group-/Künstler-IDs quellenunabhängig unter
MusicBrainz-Namen (`AlbumInfo.MEDIA_FIELD_MAP`). Ein Discogs-Treffer trüge
dort sonst seine eigene numerische Release-ID statt einer
MusicBrainz-UUID -- jeder Player oder Scanner, der `MUSICBRAINZ_ALBUMID`
liest, hielte den Wert für eine echte MusicBrainz-ID.
`tagging.apply_album_match()` leert diese Felder deshalb für jeden Treffer,
dessen `data_source` nicht `"MusicBrainz"` ist, bevor die Tags geschrieben
werden.

**Warum Discogs sein Cover selbst mitbringt:** Ein MusicBrainz-Kandidat
bekommt sein Cover automatisch bei `beet import -A` (`fetch_for_asis`,
fragt die Cover Art Archive über die Release-ID ab). Für Discogs gibt es
diesen Weg nicht -- die Release-ID reicht dort nicht, das Bild kommt aus
dem Suchergebnis selbst (`AlbumInfo.cover_art_url`), kein einbettbarer Tag.
mimport lädt es deshalb beim Übernehmen des Kandidaten direkt herunter und
legt es als `cover.jpg` in die Session (`backend/cover.py`,
`von_url_holen()`) -- genau dort, wo auch ein abfotografiertes Cover
landet, das immer Vorrang hat und nicht überschrieben wird.

**Cover-Art-Archive-Fehlschläge:** `coverartarchive.org` antwortet
gelegentlich mit einem transienten `5xx`; `fetchart` hat dafür -- anders
als das `musicbrainz`-Plugin für seine eigenen Anfragen -- **keine**
eingebaute Wiederholung. `/import/{session_id}` ruft deshalb nach einem
erfolgreichen Import mit MusicBrainz-Release-ID `albums.retry_missing_cover()`
auf: fehlt das Cover, bis zu drei weitere Versuche mit ein paar Sekunden
Abstand, sonst bleibt manuelles Nachtragen über die Album-Seite.

**„Fehlt das Cover" hieß lange nicht zwingend: keine Bilddatei im Ordner.**
beets benennt ein heruntergeladenes Cover nach dem Content-Type der Quelle
(`Album.art_destination` in beets selbst) -- die Cover Art Archive liefert
neben JPEG regelmäßig auch PNG. Ein erfolgreich geladenes Cover konnte also
als `cover.png` im Ordner liegen, nicht als `cover.jpg`. `beet fetchart`
selbst kommt damit klar (es fragt die Datenbank, nicht den Dateinamen) --
eine Prüfung, die stur nach `cover.jpg` sucht, hielt ein vorhandenes Cover
dagegen für fehlend. Das betraf sowohl `Album.has_cover` als auch den Retry
oben, unabhängig davon, ob es im Einzelfall auch die Ursache für ein konkret
fehlendes Cover war. `cover.gefunden()` sucht deshalb jetzt unter mehreren
Erweiterungen, und `album_cover()`/`update_album_cover()` in `routes.py`
lesen über `album.cover_path` statt über ein hartkodiertes `cover.jpg`. Ein
Ersetzen des Covers über die Album-Seite räumt danach eine ältere,
andersnamige Datei aus demselben Ordner weg (`cover.andere_erweiterungen_entfernen()`,
gezielt in der Route, nicht in `cover.speichern()` selbst -- das schreibt
auch in die Upload-Session, wo niemals ein fetchart-Cover liegt), damit nicht
`cover.jpg` und ein verwaistes `cover.png` nebeneinander liegen bleiben.

Zum Unterscheiden „PNG statt JPEG gefunden" von „fetchart fand wirklich
nichts" auf dem Server:

```
docker compose exec mimport-cd sqlite3 /data/library.db \
  "SELECT id, album, artpath FROM albums
   WHERE artpath IS NULL OR artpath = '' OR artpath NOT LIKE '%cover.jpg';"
```

Ein leerer `artpath` heißt: fetchart fand nichts oder lief gar nicht erst.
Dafür steht die vollständige `beet import`/`fetchart`-Ausgabe jetzt immer im
Log, nicht nur im Fehlerfall (`build_command()` setzt `-v` -- **vor** dem
Subcommand, weil es bei beets eine globale Option ist; danach nimmt beets
sie klaglos als wirkungslose `import`-Option und die Ausgabe bleibt genauso
knapp wie ohne). `routes.run_import()` loggt zusätzlich, wenn eine Session
ganz ohne MusicBrainz-Release-ID importiert wird, sonst ist „kein Cover
trotz Match" von „gar kein Match" im Log nicht zu unterscheiden.

## Cover: Ecken finden ohne Bibliothek

Der Trick eines Dokumentenscanners, ohne OpenCV: Bei einem Viereck sind die
Ecken die **Extrema von x+y und x−y**. Ein Sobel-Filter sammelt
kontrastreiche Punkte, darunter werden die vier äußersten gesucht -- keine
Linienerkennung, kein Wachstum des Images um 200 MB. Die Schwelle richtet
sich nach dem Bild selbst (ein fester Wert fände bei dunklen Fotos alles,
bei hellen nichts); Punkte am äußersten Rand werden verworfen, sonst zieht
ein Finger oder eine Tischkante den Rahmen auseinander. Findet sich zu
wenig, kommt ein ehrlicher Standardrahmen statt geratener Ecken.

Nachgemessen an einem gemalten, schrägen Viereck: alle vier Ecken auf
**1,4 Pixel** genau. Die Homographie ist gegen eine unabhängige Rechnung in
Python geprüft; beide Prüfungen laufen als Test mit
(`tests/test_cover_js.py`, braucht Node).

## Hörbuch-Cover ohne Thumbnails

Die Adresse eines Buch-Covers trägt die Änderungszeit des Bilds (`?v=…`)
und gilt deshalb als unveränderlich; der Browser holt jedes Cover genau
einmal. Bewusst **ohne Thumbnails**: 198 KB je Cover sind einmalige Kosten,
ein Thumbnail (3,5 KB, 32 ms) müsste dagegen irgendwo zwischengelagert
werden, und dafür gibt es keinen guten Ort (nicht im Buchordner, den
Audiobookshelf scannt; nicht im Staging, das bei jedem Start geleert
wird). Die Liste steht ohnehin nur im DOM, wenn kein Auftrag läuft -- der
Zweisekundentakt der Fortschrittsanzeige lädt also keine Bilder nach.

**Auch Cover, die nur in der m4b stecken:** Bücher, die nicht über mimport
kamen, haben oft kein Bild im Ordner, nur eins in der Datei --
`has_cover` sieht aber nur den Ordner. Beim Start holt mimport solche
Cover einmal heraus (~25 ms Prüfung, ~34 ms Extraktion je Buch, gemessen an
111 MB). Drei Dinge dabei nicht selbstverständlich:

- Geprüft wird die **disposition** `attached_pic`, nicht bloß "gibt es
  eine Videospur" -- manche Hörbücher haben ein echtes Video, daraus ein
  Einzelbild zu schreiben wäre falsch.
- Geschrieben wird **immer JPEG**, auch bei eingebettetem PNG. `-c copy`
  wäre schneller, legte aber ein PNG namens `cover.jpg` ab.
- Erst danebenschreiben, dann umbenennen, nur bei nicht leerer Datei --
  eine halbe `cover.jpg` machte `has_cover` wahr und ergäbe ein kaputtes
  Bild.

**Cover nachträglich in eine fertige m4b:** ohne Neu-Encode (`-c copy`,
gemessen 230 MB/s). Läuft im Staging, nicht neben der m4b (eine zweite
Datei im Buchordner läse Audiobookshelf als zweites Hörbuch ein), prüft
Spieldauer/Kapitelzahl/Bildanzahl der neuen Datei gegen die alte, bevor sie
ersetzt wird, teilt sich die Rip/Bau-Sperre und hat ein eigenes
ffmpeg-Zeitlimit.

## Audio-CD-Rip

**Tracknummer beim Packen setzen ist nicht kosmetisch.** Ohne sie ordnet
beets Dateien allein nach Spieldauer den Tracks des Releases zu -- bei
ähnlich langen Stücken kommt eine vertauschte Reihenfolge heraus.
Nachgemessen: ohne Tracknummer landet Datei 1 auf Track 6, mit ihr stimmt
jede Zuordnung auf die Sekunde (`tests/test_rip.py`).

**Warum cdparanoia und nicht abcde:** `abcde` rippt, encodiert *und taggt*
aus CDDB -- kollidiert mit "mimport schreibt die Tags selbst", müsste
abgeschaltet werden, übrig bliebe ein Shell-Wrapper um das, was
`cdparanoia` und `flac` in zwei Schritten selbst tun. Beide Pakete
zusammen unter 2 MB.

> Fortschritts-Falle: `cdparanoia -e` meldet Positionen in **Samples**, 588
> je Sektor -- nicht in Sektoren, nicht in Bytes. Nur geteilt durch 588
> ergeben die gemeldeten Werte glatte Sektornummern.

**Warum eine Daten-CD sicher genug behandelt wird wie ein Upload:**
Ordnerangaben aus dem Formular werden gegen den tatsächlich aufgelösten
Pfad geprüft, und Symlinks auf der CD (über Rock Ridge möglich) werden
beim Kopieren übergangen -- sonst landete ein Link auf `/etc/passwd` als
`track03.mp3` im Staging.

## Hörbuch-m4b-Bau

**Warum `.mimport-unfertig/` innerhalb der Bibliothek liegt, nicht im
Staging-Volume:** Staging ist ein Named Volume, die Bibliothek ein
Bind-Mount vom Host -- verschiedene Dateisysteme. Ein Verschieben von dort
wäre ein Kopiervorgang (bei zwölf CDs mehrere GB zweimal geschrieben); von
hier aus ist es ein `rename`, sofort und ohne zusätzlichen Platz. Der Punkt
am Anfang hält Audiobookshelf davon ab, den Ordner selbst als Buch zu
lesen. Nebeneffekt: Scheitert die Laufzeitprüfung, kommt die m4b gar nicht
erst ins Buch -- der Zustand "m4b und Quellen liegen nebeneinander"
entsteht nur noch über "Von vorn einlesen".

**Löschen erst nach Laufzeitprüfung auf 1500 ms genau**, bewusst ein
fester Wert statt Prozentsatz: das Encoder-Padding liegt unabhängig von
der Gesamtlänge im Millisekundenbereich, ein fehlender Track sind immer
Minuten.

**Kapitelnamen, dritter Fall (`Kapitel 1` … `Kapitel N`) ist beim Rip der
Normalfall, nicht die Ausnahme:** eine gerippte Audio-CD hat keine Tags,
und der Dateiname taugt nicht als Ersatz, weil der Rip auf jeder Disc
wieder bei `01` anfängt -- bei zwölf CDs stünde `01 Track 1` sonst zwölfmal
in der Liste.

**`MIMPORT_M4B_TIMEOUT`/`MIMPORT_M4B_STILLSTAND`-Herleitung:** Gemessen auf
dem Zielrechner an *Die Siedler von Catan* (7:21:00): rund **46× Echtzeit**,
knapp zehn Minuten fürs ganze Buch. Ein 30-Stunden-Buch ist damit nach
~40 Minuten durch, 2 h Timeout lassen selbst bei halber Geschwindigkeit
reichlich Luft (die vorherigen 6 h entsprachen einem 276-Stunden-Hörbuch).
5 Minuten Stillstands-Grenze: länger als ein kompletter Bau früher dauerte
(15 min vorher, praktisch wirkungslos); die einzige legitime Stille ist das
`-movflags +faststart`-Umschreiben am Ende, nachgemessen bei 111 MB null
Sekunden.

Beim gleichzeitigen Rippen und Encodieren bezieht sich der gemeldete
Geschwindigkeitsfaktor auf das **bisher Fertige**, nicht die Gesamtlänge --
mit der Gesamtlänge im Zähler und wachsender Laufzeit im Nenner fällt der
Wert wie 1/t. Bei völlig gleichmäßiger Geschwindigkeit sähe das nach
stetiger Verlangsamung aus; das ist beobachtet und bewusst so gelassen,
nicht korrigiert.

**ffmpeg-Hang-Postmortem -- drei behobene Fallen, nicht wieder einbauen:**

1. Die Schleife über `prozess.stdout` blockierte unbegrenzt. Das
   Zeitlimit stand **dahinter** und konnte nie greifen -- toter Code.
2. `stderr` ging in eine Pipe, die niemand las (64 KiB Kapazität, ffmpeg
   schreibt ~210 Byte/s Laufzeit dorthin, auch ohne `-loglevel debug`).
   Nach gut fünf Minuten wäre sie voll gewesen, ffmpeg hätte beim
   Schreiben blockiert, mimport auf stdout gewartet: beide für immer.
   Jetzt geht `stderr` in eine Datei im Arbeitsordner. **Dieselbe Falle
   droht bei jedem neuen `subprocess`-Aufruf mit langlebigem Kindprozess
   und capture-loser Pipe** -- stderr immer entweder mitlesen oder in eine
   Datei/`STDOUT` umleiten, nie unbeobachtet lassen.
3. "Verwerfen" verweigerte die Arbeit, solange der Auftrag lief -- und er
   lief für immer, sperrte `_buch_belegt()` dauerhaft, nur ein
   Container-Neustart half.

## Benachrichtigungen: Zustandsmarker

Erkannt wird der Übergang "läuft" → Endzustand an unsichtbaren
Markern, die jedes Fortschritts-Fragment mitbringt (`data-auftrag`,
`data-zustand`, siehe `static/notify.js`). Gemeldet wird nur der
**Wechsel**, sonst käme im Zweisekundentakt eine neue Meldung. Rip und
m4b-Bau haben getrennte Marker, weil sie gleichzeitig laufen können.

## AcoustID-Fingerprinting

Steht im Code, ist standardmäßig aus (`MIMPORT_FINGERPRINT=0`) und braucht
zum Aktivieren `libchromaprint-tools` im Image, das `chroma`-Plugin in
`beets/config.yaml` und `pyacoustid` als Abhängigkeit. mimport prüft zur
Laufzeit, ob `fpcalc` wirklich vorhanden ist -- ein gesetzter Schalter ohne
Binary bleibt wirkungslos statt zu scheitern.

Kosten, für den Zielrechner eingeordnet: `fpcalc` selbst ist unter 1 MB,
zieht aber die libav\*-Dekoder nach (50–100 MB, falls ffmpeg fehlt); Decode
plus FFT ist single-threaded, keine GPU, grob 1–3 s/Track auf einer
älteren Intel-Mobil-CPU, unter 50 MB RAM. Der eigentliche Flaschenhals ist
das Netz (AcoustID ist ratenbegrenzt), nicht die CPU.

## OCR-Tracklisten-Parser (`backend/trackparse.py`)

`ParseFlags.felder` erlaubt eine frei wählbare Reihenfolge (Titel,
Interpret, Komponist); `zeilenweise` schaltet von "Trenner in einer Zeile"
auf "ein Feld pro Zeile" um. Tracknummer und Dauer werden je Zeilengruppe
in **jeder** Zeile gesucht (`_tracknummer_und_dauer()`), nicht nur in der
ersten -- vorher war das an die erste Zeile der Gruppe hartkodiert und
brach bei vertauschter Feldreihenfolge (siehe Commit-Historie).

**Bekannte, akzeptierte Grenze:** Ein Medley ohne Zwischenzeile (mehrere
Titel ohne zugehörige Interpretenzeile) wird trotzdem strikt abwechselnd
gepaart und gerät durcheinander -- die Oberfläche weist im Hinweistext
darauf hin, das ist von Hand zu korrigieren, nicht automatisch lösbar ohne
echtes Sprachverständnis des Layouts.

## Sicherheit -- Implementierungsdetails

- **Pfad-Traversal:** Dateinamen/Ordnerpfade vom Browser gelten als
  feindlich: `../`, absolute Pfade, Laufwerksbuchstaben, Nullbytes,
  Steuerzeichen werden entfernt; jeder aufgelöste Zielpfad muss innerhalb
  des Session-Ordners liegen (`backend/sessions.py`).
- **Session-IDs** kommen aus `secrets`, nie aus der Anfrage.
- **Kein `shell=True`** bei `beet`- oder `ffmpeg`-Aufrufen; Argumentlisten,
  keine Shell-Interpolation.
- Doppelte Alben überspringt beets selbst (`duplicate_action: skip`).

Die Grenze, die offen bleibt (Staging-Volume vollschreiben), steht in der
README unter „Sicherheit".

## Test-Konventionen

Tests, die Netzzugriff brauchen (echte MusicBrainz-Abfragen), sind mit
`@pytest.mark.network` markiert; der Standardlauf ist in der README
beschrieben (`uv run pytest -m "not network"`).

Ein echter (nicht simulierter) Import läuft seit dem Hintergrundauftrag als
Thread -- Tests, die den Endzustand prüfen wollen, müssen erst
`importer.current(session_id).thread.join()` abwarten, siehe
`tests/test_routes.py::_warte_auf_import`.
