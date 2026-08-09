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

const form = document.getElementById("upload-form");
const folderInput = document.getElementById("upload");
const fileInput = document.getElementById("upload-files");
const submitButton = document.getElementById("upload-submit");
const preflight = document.getElementById("preflight");
const filesTarget = document.getElementById("files");

/** Aktuell gewählte Dateien (aus einem der beiden Inputs). */
let selection = [];

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

function render(results) {
  if (!results.length) {
    preflight.innerHTML =
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
    banner = '<div class="banner ok"><strong>Alles verlustfrei</strong></div>';
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

  preflight.innerHTML = `
    ${banner}
    <table class="stapelbar files">
      <thead><tr><th>Datei</th><th>Größe</th><th>Vorprüfung</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
    <p class="hint">${results.length} Dateien, ${humanBytes(totalSize)} insgesamt</p>`;
}

async function onPick(event) {
  const picked = Array.from(event.target.files || []).filter((file) =>
    AUDIO_EXT.has(extensionOf(file.name))
  );
  // Den jeweils anderen Input leeren, damit nicht versehentlich beide
  // Auswahlen zusammen hochgehen.
  [folderInput, fileInput].forEach((input) => {
    if (input && input !== event.target) input.value = "";
  });

  selection = await Promise.all(picked.map(inspect));
  render(selection);
  submitButton.disabled = selection.length === 0;
}

/**
 * Upload von Hand statt über hx-post: nur so kommt die Ordnerstruktur mit.
 * Der Browser setzt in `filename` sonst ausschließlich den Basisnamen, der
 * Unterordner steckt in `webkitRelativePath` -- und den hängen wir hier als
 * dritten fetch-Parameter an, wo er beim Server als Dateiname ankommt.
 */
async function onSubmit(event) {
  event.preventDefault();
  if (!selection.length) return;

  const payload = new FormData();
  selection.forEach((entry) => {
    payload.append("files", entry.file, entry.name);
  });

  submitButton.disabled = true;
  form.classList.add("busy");
  document.getElementById("files-step").hidden = false;
  filesTarget.innerHTML = '<p class="hint">Lade hoch …</p>';

  try {
    const response = await fetch("/upload", { method: "POST", body: payload });
    filesTarget.innerHTML = await response.text();
    // Neu eingefügtes HTML enthält hx-Attribute, die htmx erst kennen muss.
    if (window.htmx) window.htmx.process(filesTarget);
  } catch (error) {
    filesTarget.innerHTML =
      '<div class="banner error"><strong>Upload fehlgeschlagen</strong><p>' +
      escapeHtml(String(error)) +
      "</p></div>";
  } finally {
    submitButton.disabled = false;
    form.classList.remove("busy");
  }
}

folderInput?.addEventListener("change", onPick);
fileInput?.addEventListener("change", onPick);
form?.addEventListener("submit", onSubmit);

// Die Abschnitte 3 und 4 tauchen erst auf, wenn dort etwas landet.
document.body.addEventListener("htmx:afterSwap", (event) => {
  const reveal = { candidates: "match-step", result: "result-step" };
  const stepId = reveal[event.detail.target.id];
  if (!stepId) return;
  const step = document.getElementById(stepId);
  if (step) {
    step.hidden = false;
    step.scrollIntoView({ behavior: "smooth", block: "start" });
  }
});
