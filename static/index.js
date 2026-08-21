// Vorprüfung im Browser: noch vor dem Upload sehen, ob die Dateien verlustfrei
// sind. Das erspart es, ein halbes Gigabyte hochzuladen, nur um dann zu erfahren,
// dass es MP3s waren.
//
// Die Prüfung liest die Dateiendung und die ersten Bytes. Sie ist absichtlich
// nur ein Hinweis -- verbindlich entscheidet der Server mit mediafile. Vor allem
// .m4a lässt sich hier nicht auflösen: ALAC (verlustfrei) und AAC
// (verlustbehaftet) liegen im selben Container, und die Angabe steckt im
// moov-Atom, das oft am Dateiende sitzt.

const LOSSLESS_EXT = new Set(["flac", "wav", "aiff", "aif", "alac", "ape", "wv", "tta"]);
const LOSSY_EXT = new Set(["mp3", "aac", "ogg", "oga", "opus", "wma", "mpc", "m4b"]);
const AMBIGUOUS_EXT = new Set(["m4a", "mp4", "mka", "ogx"]);
const AUDIO_EXT = new Set([...LOSSLESS_EXT, ...LOSSY_EXT, ...AMBIGUOUS_EXT]);

/** Gemerkte Dateiauswahl je Upload-Formular. */
const selections = new WeakMap();

const extensionOf = (name) => (name.split(".").pop() || "").toLowerCase();

const ascii = (bytes, start, length) =>
  String.fromCharCode(...bytes.slice(start, start + length));

/**
 * Beurteilt eine Datei anhand ihrer ersten Bytes.
 * Gibt "lossless", "lossy" oder "unknown" zurück.
 */
function classifyMagic(bytes, extension) {
  if (bytes.length >= 4) {
    const head = ascii(bytes, 0, 4);

    if (head === "fLaC") return "lossless";
    if (head === "MAC ") return "lossless"; // Monkey's Audio
    if (head === "wvpk") return "lossless"; // WavPack
    if (head.startsWith("ID3")) return "lossy";
    if (head === "RIFF" && bytes.length >= 12 && ascii(bytes, 8, 4) === "WAVE") {
      return "lossless";
    }
    if (head === "FORM" && bytes.length >= 12) {
      const kind = ascii(bytes, 8, 4);
      // AIFC ist der komprimierte Verwandte und kann verlustbehaftet sein.
      if (kind === "AIFF") return "lossless";
      if (kind === "AIFC") return "unknown";
    }
    // ASF/WMA
    if (bytes[0] === 0x30 && bytes[1] === 0x26 && bytes[2] === 0xb2 && bytes[3] === 0x75) {
      return "lossy";
    }
    // MP3 ohne ID3: Frame-Sync, 11 gesetzte Bits.
    if (bytes[0] === 0xff && (bytes[1] & 0xe0) === 0xe0) return "lossy";
    // OggS ist ein Container -- Inhalt hier nicht bestimmbar.
    if (head === "OggS") return "unknown";
  }
  // MP4/M4A: ALAC oder AAC, hier nicht entscheidbar.
  if (bytes.length >= 8 && ascii(bytes, 4, 4) === "ftyp") return "unknown";

  // Kein bekanntes Muster: auf die Endung zurückfallen.
  if (LOSSLESS_EXT.has(extension)) return "lossless";
  if (LOSSY_EXT.has(extension)) return "lossy";
  return "unknown";
}

async function inspect(file) {
  const extension = extensionOf(file.name);
  let quality = "unknown";
  try {
    const buffer = await file.slice(0, 16).arrayBuffer();
    quality = classifyMagic(Array.from(new Uint8Array(buffer)), extension);
  } catch {
    // Nicht lesbar -- der Server sieht sich das ohnehin nochmal an.
    quality = "unknown";
  }
  return {
    file,
    name: file.webkitRelativePath || file.name,
    size: file.size,
    quality,
  };
}

const humanBytes = (value) => {
  const units = ["B", "KB", "MB", "GB"];
  let size = value;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? size : size.toFixed(1)} ${units[unit]}`;
};

const LABEL = {
  lossless: "verlustfrei",
  lossy: "verlustbehaftet",
  unknown: "unklar",
};

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

function render(results, target) {
  if (!target) return;
  if (!results.length) {
    target.innerHTML =
      '<div class="banner warn"><strong>Keine Audiodateien in der Auswahl</strong></div>';
    return;
  }

  const lossy = results.filter((r) => r.quality === "lossy");
  const unclear = results.filter((r) => r.quality === "unknown");
  const totalSize = results.reduce((sum, r) => sum + r.size, 0);

  let banner;
  if (lossy.length) {
    banner = `
      <div class="banner warn">
        <strong>${lossy.length} von ${results.length}
          ${lossy.length === 1 ? "Datei ist" : "Dateien sind"} verlustbehaftet</strong>
        <p>
          Verlustbehaftete Formate haben Information dauerhaft verworfen – die kommt
          auch beim Umwandeln nicht zurück. Wenn du die Wahl hast, lade die
          verlustfreie Fassung (FLAC, WAV, ALAC). Hochladen kannst du trotzdem.
        </p>
      </div>`;
  } else if (unclear.length === results.length) {
    banner = `
      <div class="banner info">
        <strong>Format wird nach dem Upload geprüft</strong>
        <p>
          Bei diesen Dateien lässt sich das im Browser nicht feststellen – etwa bei
          .m4a, wo verlustfreies ALAC und verlustbehaftetes AAC dieselbe Endung
          haben.
        </p>
      </div>`;
  } else {
    // Reine Bestätigung ohne Handlungsbedarf -- kein Kasten, sonst verliert
    // Farbe als Signal ihre Bedeutung, sobald auch der Normalfall sie trägt.
    banner = '<p class="hint">Alles verlustfrei.</p>';
  }

  const rows = results
    .map(
      (r) => `
      <tr>
        <td class="name" data-spalte="Datei">${escapeHtml(r.name)}</td>
        <td class="mono small" data-spalte="Größe">${humanBytes(r.size)}</td>
        <td><span class="badge ${r.quality}">${LABEL[r.quality]}</span></td>
      </tr>`
    )
    .join("");

  target.innerHTML = `
    ${banner}
    <table class="stapelbar files">
      <thead><tr><th>Datei</th><th>Größe</th><th>Vorprüfung</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint">${results.length} Dateien, ${humanBytes(totalSize)} insgesamt</p>`;
}

function optionalTarget(selector) {
  return selector ? document.querySelector(selector) : null;
}

function renderBusy(text) {
  return `
    <div class="banner info">
      <strong>${escapeHtml(text)}</strong>
      <p>Je nach Größe und Laufwerk kann das etwas dauern.</p>
      <div class="progress progress-indeterminate"><div class="progress-bar"></div></div>
    </div>`;
}

function bindUploadWidget(form) {
  if (!form || form.dataset.uploadBound === "ja") return;
  form.dataset.uploadBound = "ja";

  const folderInput = form.querySelector("[data-upload-folder]");
  const fileInput = form.querySelector("[data-upload-files]");
  const submitButton = form.querySelector("[data-upload-submit]");
  const preflightTarget = optionalTarget(form.dataset.preflightTarget);
  const resultTarget = optionalTarget(form.dataset.uploadTarget);
  const revealTarget = optionalTarget(form.dataset.uploadReveal);
  const endpoint = form.dataset.uploadEndpoint || "/upload";
  const autoSubmit = form.dataset.uploadAutosubmit === "true";

  selections.set(form, []);

  async function onPick(event) {
    const picked = Array.from(event.target.files || []).filter((file) =>
      AUDIO_EXT.has(extensionOf(file.name))
    );
    [folderInput, fileInput].forEach((input) => {
      if (input && input !== event.target) input.value = "";
    });

    const selection = await Promise.all(picked.map(inspect));
    selections.set(form, selection);
    render(selection, preflightTarget);
    if (submitButton) submitButton.disabled = selection.length === 0;
    if (autoSubmit && selection.length) {
      form.requestSubmit(submitButton || undefined);
    }
  }

  async function onSubmit(event) {
    event.preventDefault();
    if (!resultTarget) return;

    const submitter = event.submitter;
    const mode = submitter?.dataset.submitMode || "upload";
    const action = submitter?.dataset.submitEndpoint || endpoint;
    const progressText = submitter?.dataset.progressText || "Lade hoch …";
    const selection = selections.get(form) || [];
    if (mode === "upload" && !selection.length) return;

    const payload = new FormData();
    for (const [name, value] of new FormData(form).entries()) {
      if (!(value instanceof File)) payload.append(name, value);
    }
    if (mode === "upload") {
      selection.forEach((entry) => {
        payload.append("files", entry.file, entry.name);
      });
    }

    const buttons = Array.from(form.querySelectorAll("button"));
    const vorherDisabled = new Map(buttons.map((button) => [button, button.disabled]));
    buttons.forEach((button) => {
      button.disabled = true;
    });
    form.classList.add("busy");
    if (revealTarget) revealTarget.hidden = false;
    resultTarget.innerHTML = renderBusy(progressText);

    try {
      const response = await fetch(action, { method: "POST", body: payload });
      resultTarget.innerHTML = await response.text();
      if (window.htmx) window.htmx.process(resultTarget);
      bindUploadWidgets(resultTarget);
      bindGenreInputs(resultTarget);
    } catch (error) {
      resultTarget.innerHTML =
        '<div class="banner error"><strong>Upload fehlgeschlagen</strong><p>' +
        escapeHtml(String(error)) +
        "</p></div>";
    } finally {
      buttons.forEach((button) => {
        button.disabled = vorherDisabled.get(button) ?? false;
      });
      form.classList.remove("busy");
    }
  }

  folderInput?.addEventListener("change", onPick);
  fileInput?.addEventListener("change", onPick);
  form.addEventListener("submit", onSubmit);
}

function bindUploadWidgets(root = document) {
  root.querySelectorAll?.("[data-upload-widget]").forEach(bindUploadWidget);
}

bindUploadWidgets();
bindGenreInputs();

// Die Abschnitte 3 und 4 tauchen erst auf, wenn dort etwas landet.
document.body.addEventListener("htmx:afterSwap", (event) => {
  // Ein von der SSE-Extension ausgelöster Swap (Fortschritts-Ticks aus
  // /rip/events, /audiobook/events) liefert kein event.detail.target -- das
  // gibt es nur beim regulären htmx-Request/Response-Zyklus.
  if (!event.detail.target) return;

  bindUploadWidgets(event.detail.target);
  bindGenreInputs(event.detail.target);
  initSamplerZustand(event.detail.target);

  const reveal = { candidates: "match-step", result: "result-step" };
  const stepId = reveal[event.detail.target.id];
  if (!stepId) return;
  const step = document.getElementById(stepId);
  if (step) {
    step.hidden = false;
    step.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});

/* --------------------------------------------------- Sampler-Häkchen -----
 *
 * Ein Sampler hat keinen einheitlichen Interpreten -- jeder Track hat seinen
 * eigenen. Der Albumkünstler ist dafür der Platzhalter, über den Navidrome und
 * beets die Stücke zu einem Album zusammenfassen. Das Häkchen stellt beides
 * ein, statt es nur zu erklären: Albumkünstler vorbelegen, das Feld für einen
 * gemeinsamen Interpreten abschalten.
 *
 * Der Server macht dasselbe noch einmal -- wer das Häkchen ohne Albumkünstler
 * abschickt, bekommt ihn trotzdem. Hier geht es darum, dass man es sieht.
 */
const SAMPLER_NAME = "Various Artists";

function manualContainer(von) {
  return von.closest("[data-manual-form]");
}

function samplerUmschalten(haken) {
  const formular = manualContainer(haken);
  if (!formular) return;
  const albumartist = formular.querySelector("[data-albumartist]");
  const albumartistStern = formular.querySelector("[data-albumartist-pflicht]");
  const alleInterpreten = formular.querySelector("[data-alle-interpreten]");
  const hinweis = formular.querySelector("[data-sampler-hinweis]");
  const feld = alleInterpreten?.querySelector("input");
  // Ohne übernommenen MusicBrainz-Treffer ist "ein Interpret" Pflicht -- bei
  // einem Sampler sagt der Albumkünstler ("Various Artists") darüber aber
  // nichts aus, dafür zählt dann jede Zeile "Track-Künstler" einzeln.
  const trackInterpreten = formular.querySelectorAll('input[name^="interpret:"]');

  if (haken.checked) {
    if (albumartist && !albumartist.value.trim()) {
      albumartist.value = SAMPLER_NAME;
      albumartist.dataset.vonUns = "ja";
    }
    if (albumartist) albumartist.required = false;
    if (albumartistStern) albumartistStern.hidden = true;
    trackInterpreten.forEach((i) => { i.required = true; });
    if (feld) {
      feld.value = "";
      feld.disabled = true;
    }
    // feld.value wird hier programmgesteuert geleert -- das löst kein
    // "input"-Ereignis aus, also greift der Abgleich weiter unten nicht von
    // selbst. Ohne das hier bliebe eine zuvor per Lupe bestätigte Artist-ID
    // am jetzt leeren Namen kleben und würde beim Schreiben in jeden Track
    // ohne eigene Bestätigung übernommen.
    artistBestaetigungVerwerfen(formular, "artist");
    alleInterpreten?.classList.add("abgeschaltet");
    if (hinweis) hinweis.hidden = false;
  } else {
    // Nur zurücknehmen, was wir selbst gesetzt haben -- eine eigene Eingabe
    // bleibt stehen.
    if (albumartist?.dataset.vonUns === "ja") {
      albumartist.value = "";
      delete albumartist.dataset.vonUns;
      // "Various Artists" ist selbst ein echter MusicBrainz-Eintrag -- wurde
      // dafür zwischenzeitlich eine Artist-ID bestätigt, muss sie mit dem
      // jetzt wieder geleerten Namen verschwinden (siehe "artist" oben).
      artistBestaetigungVerwerfen(formular, "albumartist");
    }
    if (albumartist) albumartist.required = true;
    if (albumartistStern) albumartistStern.hidden = false;
    trackInterpreten.forEach((i) => { i.required = false; });
    if (feld) feld.disabled = false;
    alleInterpreten?.classList.remove("abgeschaltet");
    if (hinweis) hinweis.hidden = true;
  }
}

// Die Formulare kommen per htmx nach, deshalb am Dokument lauschen.
document.body.addEventListener("change", (event) => {
  if (event.target.matches("[data-sampler]")) samplerUmschalten(event.target);
});

/** Ein aus einem Entwurf wiederhergestelltes, schon angehaktes Häkchen
 * bekommt sonst nie das "change"-Ereignis, das Feld für den Track-Künstler
 * bliebe also fälschlich aktiv. */
function initSamplerZustand(root = document) {
  root.querySelectorAll?.("[data-sampler]:checked").forEach(samplerUmschalten);
}
initSamplerZustand();

/* ----------------------------------- MusicBrainz-Suche für Album/Titel -----
 *
 * "MB-Link fixen" (kuenstler_zeile in _album_detail.html) öffnete früher pro
 * Zeile ein eigenes <details> mit Eingabefeld+Suchen-Button+Ergebnisbereich --
 * bei vielen Titeln entsprechend viel Markup, und der Klick auf die Lupe
 * zeigte erst ein zweites Feld statt gleich ein Ergebnis. Ein einziger,
 * gemeinsam genutzter Dialog (#mb-such-dialog) ersetzt das: die Lupe öffnet
 * ihn, füllt das weiterhin editierbare Suchfeld mit dem aktuellen Namen und
 * sucht sofort -- Korrigieren (Tippfehler, "feat. X") bleibt einen Klick
 * entfernt, ohne dass erst ein zweites Feld auftauchen muss.
 */
function mbSucheAusfuehren(name, lookupPfad) {
  if (!window.htmx || !lookupPfad) return;
  window.htmx.ajax("POST", lookupPfad, {
    target: "#mb-such-ergebnisse",
    swap: "innerHTML",
    values: { name },
  });
}

document.body.addEventListener("click", (event) => {
  const knopf = event.target.closest("[data-mb-suchen]");
  if (!knopf) return;

  const dialog = document.getElementById("mb-such-dialog");
  const eingabe = document.getElementById("mb-such-eingabe");
  const ergebnisse = document.getElementById("mb-such-ergebnisse");
  if (!dialog || !eingabe || !ergebnisse) return;

  const name = knopf.dataset.name || "";
  const lookupPfad = knopf.dataset.lookupPfad || "";
  dialog.dataset.lookupPfad = lookupPfad;
  eingabe.value = name;
  ergebnisse.innerHTML = "";
  dialog.showModal();
  mbSucheAusfuehren(name, lookupPfad);
});

// Delegiert, weil #mb-such-form nach einem "Übernehmen" (voller Neu-Render
// von #album-detail) als neuer Knoten zurückkommt -- eine direkt gebundene
// Listener-Referenz auf den alten Knoten wäre dann verwaist.
document.body.addEventListener("submit", (event) => {
  const form = event.target.closest("#mb-such-form");
  if (!form) return;
  event.preventDefault();

  const dialog = document.getElementById("mb-such-dialog");
  const eingabe = document.getElementById("mb-such-eingabe");
  if (!dialog || !eingabe) return;
  mbSucheAusfuehren(eingabe.value, dialog.dataset.lookupPfad || "");
});

/* ------------------------------------------ MusicBrainz-Künstlerwahl -----
 *
 * Die Suche liefert nur Vorschläge. Erst mit „Übernehmen“ wird klar, welcher
 * Treffer gemeint ist, und genau dann merken wir uns die MBID im versteckten
 * Feld. Wird der Name danach wieder geändert, verwerfen wir die gemerkte ID.
 */
function artistStatusHtml(kind, title, text) {
  return `<div class="banner ${kind} artist-match-banner"><strong>${escapeHtml(title)}</strong><p>${escapeHtml(text)}</p></div>`;
}

function artistZielFeld(form, field) {
  return form?.querySelector(`[data-artist-field="${field}"]`);
}

function artistZielMbid(form, field) {
  return form?.querySelector(`[data-artist-mbid="${field}"]`);
}

function artistZielErgebnis(form, field) {
  return form?.querySelector(`[data-artist-results="${field}"]`);
}

/** Verwirft eine bestätigte MusicBrainz-Zuordnung, ohne dass der Nutzer den
 * Namen selbst geändert hätte -- z.B. weil "Sampler" das Feld gerade
 * programmgesteuert geleert hat. Anders als beim "input"-Ereignis unten gibt
 * es hier keine "Name geändert"-Warnung, sondern der neutrale Ausgangsstand:
 * die Änderung kam ja nicht von einer Eingabe, die neu geprüft werden muss. */
function artistBestaetigungVerwerfen(form, field) {
  const mbid = artistZielMbid(form, field);
  if (!mbid || !mbid.value) return;
  mbid.value = "";
  delete mbid.dataset.selectedName;
  const ergebnis = artistZielErgebnis(form, field);
  if (ergebnis) {
    ergebnis.innerHTML = '<div class="hint">Noch kein MusicBrainz-Match ausgewählt.</div>';
  }
}

document.body.addEventListener("click", (event) => {
  const button = event.target.closest("[data-artist-choose]");
  if (!button) return;

  const name = String(button.dataset.name || "").trim();
  const value = String(button.dataset.mbid || "").trim();
  if (!name || !value) return;

  // Mehrere " / "-getrennte Namen (Kollaboration, Chor + Dirigent, ...):
  // jeder Name hat seinen eigenen Abschnitt mit eigener Auswahl, statt dass
  // "Übernehmen" wie sonst das ganze Feld überschreibt.
  const slot = button.closest("[data-name-slot]");
  if (slot) {
    mehrfachUebernehmen(slot, name, value);
    return;
  }

  const form = manualContainer(button);
  if (!form) return;

  const field = button.dataset.field || "";
  const input = artistZielFeld(form, field);
  const mbid = artistZielMbid(form, field);
  const result = artistZielErgebnis(form, field);
  const label = String(button.dataset.fieldLabel || field || "Künstler");
  if (!input || !mbid || !result) return;

  input.value = name;
  mbid.value = value;
  mbid.dataset.selectedName = name;
  result.innerHTML = artistStatusHtml(
    "ok",
    `${label}: MusicBrainz-Match gewählt`,
    `${name} wird mit Artist-ID geschrieben.`,
  );
});

/** Eine Auswahl innerhalb eines Mehrfach-Namen-Abschnitts einordnen und das
 * Feld aus allen bisherigen Auswahlen (oder, wo noch nichts gewählt wurde,
 * dem ursprünglich getippten Namen) neu zusammensetzen. */
function mehrfachUebernehmen(slot, name, value) {
  slot.dataset.chosenName = name;
  slot.dataset.chosenMbid = value;
  const status = slot.querySelector("[data-artist-multi-status]");
  if (status) {
    status.innerHTML = artistStatusHtml("ok", "Übernommen", `${name} wird verwendet.`);
  }
  mehrfachZusammensetzen(slot.closest("[data-artist-multi]"));
}

function mehrfachZusammensetzen(gruppe) {
  if (!gruppe) return;
  const form = manualContainer(gruppe);
  const field = gruppe.dataset.field || "";
  const input = artistZielFeld(form, field);
  const mbid = artistZielMbid(form, field);
  if (!input || !mbid) return;

  const slots = [...gruppe.querySelectorAll("[data-name-slot]")];
  const namen = slots.map((s) => s.dataset.chosenName || s.dataset.originalName || "");
  const ids = slots.map((s) => s.dataset.chosenMbid || "");
  // Nur wenn wirklich jeder Name eine Artist-ID hat, schreiben wir sie --
  // eine halbe Liste wäre mehrdeutig, welcher Name zu welcher ID gehört.
  const vollstaendig = ids.length > 0 && ids.every((id) => id);

  input.value = namen.join(" / ");
  mbid.dataset.selectedName = input.value;
  mbid.value = vollstaendig ? ids.join("; ") : "";
}

document.body.addEventListener("input", (event) => {
  const input = event.target.closest("[data-artist-field]");
  if (!input) return;

  const form = manualContainer(input);
  const field = input.dataset.artistField || "";
  const mbid = artistZielMbid(form, field);
  const result = artistZielErgebnis(form, field);
  if (!mbid || !result) return;

  const selected = String(mbid.dataset.selectedName || "").trim();
  if (!selected) return;
  if (input.value.trim() === selected) return;

  mbid.value = "";
  delete mbid.dataset.selectedName;
  result.innerHTML = artistStatusHtml(
    "warn",
    "Name geändert – Match bitte neu prüfen",
    "Der zuletzt gewählte MusicBrainz-Treffer passt jetzt möglicherweise nicht mehr.",
  );
});

/* ------------------------------------------------ Genre-Vorschläge -------
 *
 * Das Feld erlaubt mehrere Genres per Semikolon. Ein normales <datalist>
 * würde aber immer nur den gesamten Feldwert vorschlagen; nach dem ersten
 * Eintrag wäre das unhandlich. Deshalb bauen wir die Vorschläge beim Tippen
 * aus dem letzten Teilstück neu zusammen.
 */
const GENRE_LIMIT = 12;

function genreVorschlaegeAktualisieren(input, datalist) {
  const katalog = datalist._genreKatalog || [];
  const teile = String(input.value || "").split(";");
  const letzterRohwert = teile.pop() || "";
  const prefix = teile.map((teil) => teil.trim()).filter(Boolean).join("; ");
  const basis = prefix ? `${prefix}; ` : "";
  const suchwort = letzterRohwert.trim().toLocaleLowerCase();

  let treffer = katalog;
  if (suchwort) {
    const beginntMit = katalog.filter((genre) => genre.toLocaleLowerCase().startsWith(suchwort));
    const enthaelt = katalog.filter(
      (genre) => !genre.toLocaleLowerCase().startsWith(suchwort)
        && genre.toLocaleLowerCase().includes(suchwort),
    );
    treffer = [...beginntMit, ...enthaelt];
  }

  datalist.replaceChildren(
    ...treffer.slice(0, GENRE_LIMIT).map((genre) => {
      const option = document.createElement("option");
      option.value = `${basis}${genre}`;
      return option;
    }),
  );
}

function bindGenreInput(input) {
  if (input.dataset.genreBound === "ja") return;
  const listId = input.getAttribute("list");
  if (!listId) return;
  const datalist = document.getElementById(listId);
  if (!datalist) return;

  datalist._genreKatalog = Array.from(datalist.options)
    .map((option) => option.value.trim())
    .filter(Boolean);

  const aktualisieren = () => genreVorschlaegeAktualisieren(input, datalist);
  input.addEventListener("focus", aktualisieren);
  input.addEventListener("input", aktualisieren);
  input.dataset.genreBound = "ja";
  aktualisieren();
}

function bindGenreInputs(root = document) {
  root.querySelectorAll?.("[data-genre-input]").forEach(bindGenreInput);
}

/* -------------------------------------------- Editierbare Tag-Felder -----
 *
 * Album-Tags und Titelliste (siehe _album_detail.html) sammeln Änderungen
 * nur im Browser -- geschrieben wird erst beim Klick auf "Speichern" ganz
 * unten im Formular, nicht mehr bei jeder einzelnen Feldänderung.
 * "Abbrechen" lädt die Seite neu: es wurde ja nie etwas geschrieben, ein
 * Reload zeigt einfach wieder den unveränderten Stand.
 *
 * Jedes Eingabefeld trägt seinen Ausgangswert in data-original. Beim
 * Speichern-Klick filtert htmx:configRequest alle unveränderten Felder aus
 * dem Request heraus -- sonst würde ein Speichern-Klick für jedes Feld
 * jedes Titels einen eigenen beet-modify-Aufruf verbraten, egal ob sich
 * dort überhaupt etwas geändert hat.
 */
function istFeldGeaendert(eingabe) {
  const original = eingabe.dataset.original ?? "";
  const aktuell = eingabe.type === "checkbox" ? (eingabe.checked ? "True" : "") : eingabe.value;
  return aktuell !== original;
}

document.body.addEventListener("htmx:configRequest", (event) => {
  if (!event.detail.elt.hasAttribute("data-album-speichern")) return;
  const formular = document.getElementById("album-form");
  if (!formular) return;

  formular.querySelectorAll("[data-feld-eingabe]").forEach((eingabe) => {
    // Das Sampler-Häkchen (einziges Bool-Feld) schickt immer seinen
    // aktuellen Zustand mit -- ein abgewähltes Häkchen taucht in den
    // Formulardaten ohnehin nie auf, das herauszufiltern lohnt sich nicht.
    if (eingabe.type === "checkbox" || !eingabe.name) return;
    if (!istFeldGeaendert(eingabe)) delete event.detail.parameters[eingabe.name];
  });
});

/* Ein "Übernehmen" bei der MusicBrainz-Suche rendert #album-detail komplett
 * neu (siehe target in _mb_matches.html) -- alle noch nicht gespeicherten
 * Feldänderungen wären damit weg, ohne dass der Nutzer das erwartet. Warnen,
 * statt es einfach zu verwerfen. */
document.body.addEventListener("htmx:confirm", (event) => {
  if (!event.detail.elt.closest("[data-mb-uebernehmen]")) return;
  const formular = document.getElementById("album-form");
  if (!formular) return;

  const geaendert = [...formular.querySelectorAll("[data-feld-eingabe]")].some(istFeldGeaendert);
  if (!geaendert) return;

  event.preventDefault();
  if (window.confirm(
    "Es gibt noch nicht gespeicherte Änderungen an den Feldern -- die gehen beim " +
    "Verknüpfen mit MusicBrainz verloren. Trotzdem fortfahren?"
  )) {
    event.detail.issueRequest(true);
  }
});

/* ------------------------------------------------------------- Tabs ------
 *
 * Ersetzt "alle Quellen gleichzeitig sichtbar": nur der geöffnete Reiter
 * zeigt seinen Inhalt. Ein Reiter mit [data-tab-lazy] lädt seinen Inhalt erst
 * beim ersten Öffnen nach -- auf einer knappen Maschine soll niemand für
 * eine eingelegte CD oder ein Laufwerk bezahlen, die er gar nicht ansieht.
 * Delegiert am Dokument, weil die Tab-Leiste beim Seitenaufruf schon steht.
 */
document.body.addEventListener("click", (event) => {
  const tab = event.target.closest("[data-tab]");
  if (!tab) return;
  const container = tab.closest("[data-tabs]");
  if (!container) return;

  container.querySelectorAll(":scope > .tab-bar > .tab").forEach((t) => {
    t.classList.toggle("active", t === tab);
  });
  const panel = container.querySelector(
    `[data-tab-panel="${tab.dataset.tab}"]`
  );
  container.querySelectorAll(":scope > .tab-panel").forEach((p) => {
    p.classList.toggle("active", p === panel);
  });

  const url = tab.dataset.tabLazy;
  if (url && panel && panel.dataset.tabLoaded !== "ja") {
    panel.dataset.tabLoaded = "ja";
    if (window.htmx) window.htmx.ajax("GET", url, { target: panel, swap: "innerHTML" });
  }
});
