// Ein Cover abfotografieren, geradeziehen, hochladen.
//
// Alles hier passiert im Browser: Das Handy hat das Foto ohnehin und rechnet
// für sich, statt ein paar Megabyte hochzuladen und den Server damit zu
// beschäftigen. Zum Server geht nur das fertige, quadratische JPEG.
//
// Der Ablauf ist der eines Dokumentenscanners:
//   1. Foto aufnehmen (capture="environment" öffnet direkt die Kamera)
//   2. Ecken schätzen -- ein Vorschlag, kein Anspruch
//   3. Ecken ziehen, bis der Rahmen sitzt
//   4. Perspektivisch entzerren und als Quadrat ausgeben

const ZIEL_KANTE = 1000; // Pixel des fertigen Covers
const ANALYSE_KANTE = 320; // Auf so viel wird für die Eckensuche verkleinert

/* ------------------------------------------------------------------ Mathe */

/**
 * Homographie aus vier Punktpaaren: acht Unbekannte, acht Gleichungen,
 * gelöst per Gauß mit Spaltenpivotisierung.
 */
function homographie(quelle, ziel) {
  const A = [];
  const b = [];
  for (let i = 0; i < 4; i++) {
    const [x, y] = quelle[i];
    const [u, v] = ziel[i];
    A.push([x, y, 1, 0, 0, 0, -u * x, -u * y]);
    b.push(u);
    A.push([0, 0, 0, x, y, 1, -v * x, -v * y]);
    b.push(v);
  }
  const n = 8;
  for (let i = 0; i < n; i++) {
    let p = i;
    for (let r = i + 1; r < n; r++) if (Math.abs(A[r][i]) > Math.abs(A[p][i])) p = r;
    [A[i], A[p]] = [A[p], A[i]];
    [b[i], b[p]] = [b[p], b[i]];
    for (let r = i + 1; r < n; r++) {
      const f = A[r][i] / A[i][i];
      for (let c = i; c < n; c++) A[r][c] -= f * A[i][c];
      b[r] -= f * b[i];
    }
  }
  const h = new Array(n).fill(0);
  for (let i = n - 1; i >= 0; i--) {
    let summe = b[i];
    for (let c = i + 1; c < n; c++) summe -= A[i][c] * h[c];
    h[i] = summe / A[i][i];
  }
  h.push(1);
  return h;
}

const anwenden = (h, x, y) => {
  const d = h[6] * x + h[7] * y + h[8];
  return [(h[0] * x + h[1] * y + h[2]) / d, (h[3] * x + h[4] * y + h[5]) / d];
};

/* -------------------------------------------------------- Eckenerkennung */

/**
 * Schätzt die vier Ecken des Covers.
 *
 * Der Trick eines Dokumentenscanners: Bei einem Viereck sind die Ecken die
 * Extrema von x+y und x−y. Es genügt also, kontrastreiche Punkte zu sammeln
 * und darunter die vier äußersten zu suchen -- keine Linienerkennung nötig.
 *
 * Ein CD-Cover ist quadratisch und hebt sich meist klar ab; für einen
 * Vorschlag reicht das. Danebengegriffen wird trotzdem manchmal, deshalb ist
 * das Nachziehen kein Zusatz, sondern Teil des Ablaufs.
 */
function eckenSchaetzen(bilddaten, breite, hoehe) {
  const grau = new Float32Array(breite * hoehe);
  for (let i = 0; i < breite * hoehe; i++) {
    const p = i * 4;
    grau[i] =
      0.299 * bilddaten[p] + 0.587 * bilddaten[p + 1] + 0.114 * bilddaten[p + 2];
  }

  // Sobel: wie stark ändert sich die Helligkeit an dieser Stelle?
  const staerken = [];
  let summe = 0;
  for (let y = 1; y < hoehe - 1; y++) {
    for (let x = 1; x < breite - 1; x++) {
      const i = y * breite + x;
      const gx =
        -grau[i - breite - 1] + grau[i - breite + 1] +
        -2 * grau[i - 1] + 2 * grau[i + 1] +
        -grau[i + breite - 1] + grau[i + breite + 1];
      const gy =
        -grau[i - breite - 1] - 2 * grau[i - breite] - grau[i - breite + 1] +
        grau[i + breite - 1] + 2 * grau[i + breite] + grau[i + breite + 1];
      const g = Math.hypot(gx, gy);
      staerken.push(g);
      summe += g;
    }
  }

  // Schwelle relativ zum Bild: eine feste Zahl würde bei dunklen Fotos alles
  // und bei hellen nichts finden.
  const mittel = summe / staerken.length;
  const schwelle = Math.max(mittel * 2.5, 30);

  const punkte = [];
  let k = 0;
  for (let y = 1; y < hoehe - 1; y++) {
    for (let x = 1; x < breite - 1; x++) {
      if (staerken[k++] > schwelle) punkte.push([x, y]);
    }
  }

  // Zu wenig erkannt: lieber ein ehrlicher Standardrahmen als geratene Ecken.
  if (punkte.length < 50) return standardRahmen(breite, hoehe);

  // Ausreißer am Bildrand (Finger, Tischkante) verwerfen: nur das mittlere
  // Feld der gefundenen Punkte zählt.
  const xs = punkte.map((p) => p[0]).sort((a, b) => a - b);
  const ys = punkte.map((p) => p[1]).sort((a, b) => a - b);
  const q = (arr, t) => arr[Math.floor(arr.length * t)];
  const [x0, x1] = [q(xs, 0.02), q(xs, 0.98)];
  const [y0, y1] = [q(ys, 0.02), q(ys, 0.98)];
  const innen = punkte.filter(
    ([x, y]) => x >= x0 && x <= x1 && y >= y0 && y <= y1
  );
  if (innen.length < 50) return standardRahmen(breite, hoehe);

  const beste = (bewerten) =>
    innen.reduce((a, p) => (bewerten(p) < bewerten(a) ? p : a));

  return [
    beste(([x, y]) => x + y), // oben links
    beste(([x, y]) => -(x - y)), // oben rechts
    beste(([x, y]) => -(x + y)), // unten rechts
    beste(([x, y]) => x - y), // unten links
  ];
}

const standardRahmen = (breite, hoehe) => {
  const rx = breite * 0.1;
  const ry = hoehe * 0.1;
  return [
    [rx, ry],
    [breite - rx, ry],
    [breite - rx, hoehe - ry],
    [rx, hoehe - ry],
  ];
};

/* ----------------------------------------------------------- Entzerren */

/** Zieht das Viereck auf ein Quadrat gerade. */
function entzerren(quellCanvas, ecken, kante = ZIEL_KANTE) {
  const quelle = quellCanvas
    .getContext("2d")
    .getImageData(0, 0, quellCanvas.width, quellCanvas.height);

  const ziel = document.createElement("canvas");
  ziel.width = kante;
  ziel.height = kante;
  const ausgabe = ziel.getContext("2d").createImageData(kante, kante);

  // Rückwärts rechnen: für jeden Zielpixel den Quellpixel suchen. Vorwärts
  // blieben Löcher.
  const h = homographie(
    [[0, 0], [kante, 0], [kante, kante], [0, kante]],
    ecken
  );

  for (let y = 0; y < kante; y++) {
    for (let x = 0; x < kante; x++) {
      const [sx, sy] = anwenden(h, x, y);
      const qx = Math.round(sx);
      const qy = Math.round(sy);
      const zi = (y * kante + x) * 4;
      if (qx < 0 || qy < 0 || qx >= quelle.width || qy >= quelle.height) {
        ausgabe.data[zi + 3] = 255;
        continue;
      }
      const qi = (qy * quelle.width + qx) * 4;
      ausgabe.data[zi] = quelle.data[qi];
      ausgabe.data[zi + 1] = quelle.data[qi + 1];
      ausgabe.data[zi + 2] = quelle.data[qi + 2];
      ausgabe.data[zi + 3] = 255;
    }
  }
  ziel.getContext("2d").putImageData(ausgabe, 0, 0);
  return ziel;
}


/* ------------------------------------------------------------ Oberfläche */

const dialog = () => document.getElementById("cover-dialog");

let zustand = null;

/** Rechnet Bildkoordinaten in Anzeigekoordinaten um und zurück. */
const skala = () => {
  const canvas = zustand.canvas;
  return canvas.width / canvas.getBoundingClientRect().width;
};

function zeichnen() {
  const { canvas, foto, ecken } = zustand;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(foto, 0, 0, canvas.width, canvas.height);

  ctx.lineWidth = Math.max(2, canvas.width / 250);
  ctx.strokeStyle = "#7fb08d";
  ctx.fillStyle = "rgba(127, 176, 141, 0.15)";
  ctx.beginPath();
  ecken.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
  ctx.closePath();
  ctx.fill();
  ctx.stroke();

  const r = Math.max(10, canvas.width / 45);
  ecken.forEach(([x, y]) => {
    ctx.beginPath();
    ctx.arc(x, y, r, 0, Math.PI * 2);
    ctx.fillStyle = "#7fb08d";
    ctx.fill();
    ctx.strokeStyle = "#10130f";
    ctx.stroke();
  });
}

function naechsteEcke(x, y) {
  let beste = 0;
  let abstand = Infinity;
  zustand.ecken.forEach(([ex, ey], i) => {
    const d = Math.hypot(ex - x, ey - y);
    if (d < abstand) {
      abstand = d;
      beste = i;
    }
  });
  // Nur greifen, wenn man halbwegs in der Nähe ist.
  return abstand < zustand.canvas.width / 6 ? beste : null;
}

function punktAus(event) {
  const rect = zustand.canvas.getBoundingClientRect();
  const quelle = event.touches?.[0] ?? event;
  const f = skala();
  return [(quelle.clientX - rect.left) * f, (quelle.clientY - rect.top) * f];
}

function ziehenStart(event) {
  const [x, y] = punktAus(event);
  zustand.greift = naechsteEcke(x, y);
  if (zustand.greift !== null) event.preventDefault();
}

function ziehen(event) {
  if (zustand?.greift === null || zustand?.greift === undefined) return;
  event.preventDefault();
  const [x, y] = punktAus(event);
  const { canvas } = zustand;
  zustand.ecken[zustand.greift] = [
    Math.min(Math.max(x, 0), canvas.width),
    Math.min(Math.max(y, 0), canvas.height),
  ];
  zeichnen();
}

const ziehenEnde = () => {
  if (zustand) zustand.greift = null;
};

/** Lädt das Foto, verkleinert es und schlägt Ecken vor. */
async function fotoLaden(datei) {
  const bild = new Image();
  const url = URL.createObjectURL(datei);
  await new Promise((fertig, fehler) => {
    bild.onload = fertig;
    bild.onerror = () => fehler(new Error("Bild nicht lesbar"));
    bild.src = url;
  });

  // Auf eine handliche Größe bringen: das Original kann 12 Megapixel haben,
  // und darauf pixelweise zu rechnen dauert auf einem Handy spürbar.
  const faktor = Math.min(1, 1600 / Math.max(bild.width, bild.height));
  const canvas = document.getElementById("cover-canvas");
  canvas.width = Math.round(bild.width * faktor);
  canvas.height = Math.round(bild.height * faktor);
  canvas.getContext("2d").drawImage(bild, 0, 0, canvas.width, canvas.height);
  URL.revokeObjectURL(url);

  // Die Eckensuche läuft auf einer noch kleineren Fassung -- sie braucht
  // Struktur, keine Auflösung.
  const klein = document.createElement("canvas");
  const kf = Math.min(1, ANALYSE_KANTE / Math.max(canvas.width, canvas.height));
  klein.width = Math.round(canvas.width * kf);
  klein.height = Math.round(canvas.height * kf);
  const kctx = klein.getContext("2d");
  kctx.drawImage(canvas, 0, 0, klein.width, klein.height);
  const daten = kctx.getImageData(0, 0, klein.width, klein.height).data;

  const ecken = eckenSchaetzen(daten, klein.width, klein.height).map(([x, y]) => [
    x / kf,
    y / kf,
  ]);

  zustand = { canvas, foto: canvas.cloneNode(false), ecken, greift: null };
  zustand.foto.width = canvas.width;
  zustand.foto.height = canvas.height;
  zustand.foto.getContext("2d").drawImage(canvas, 0, 0);
  zeichnen();

  dialog().querySelector(".cover-schritt-2").hidden = false;
}

async function uebernehmen() {
  const ziel = entzerren(zustand.foto, zustand.ecken);
  const blob = await new Promise((f) => ziel.toBlob(f, "image/jpeg", 0.9));

  const form = dialog().querySelector("form");
  const daten = new FormData(form);
  daten.set("bild", blob, "cover.jpg");

  const knopf = dialog().querySelector(".cover-uebernehmen");
  knopf.disabled = true;
  knopf.textContent = "lädt …";
  try {
    const antwort = await fetch(form.action, { method: "POST", body: daten });
    const ziel_id = form.dataset.ziel;
    document.getElementById(ziel_id).innerHTML = await antwort.text();
    if (window.htmx) window.htmx.process(document.getElementById(ziel_id));
    schliessen();
  } catch (fehler) {
    knopf.textContent = "Fehlgeschlagen – nochmal";
    knopf.disabled = false;
  }
}

function oeffnen(action, zielId, titel) {
  const d = dialog();
  const form = d.querySelector("form");
  form.action = action;
  form.dataset.ziel = zielId;
  d.querySelector(".cover-titel").textContent = titel;
  d.querySelector(".cover-schritt-2").hidden = true;
  d.querySelector(".cover-uebernehmen").disabled = false;
  d.querySelector(".cover-uebernehmen").textContent = "Als Cover übernehmen";
  d.querySelector("input[type=file]").value = "";
  zustand = null;
  d.showModal();
}

const schliessen = () => dialog()?.close();

document.addEventListener("DOMContentLoaded", () => {
  const d = dialog();
  if (!d) return;

  d.querySelector("input[type=file]").addEventListener("change", (event) => {
    const datei = event.target.files?.[0];
    if (datei) fotoLaden(datei).catch(() => {
      d.querySelector(".cover-titel").textContent = "Bild nicht lesbar";
    });
  });

  const canvas = document.getElementById("cover-canvas");
  ["mousedown", "touchstart"].forEach((e) =>
    canvas.addEventListener(e, ziehenStart, { passive: false })
  );
  ["mousemove", "touchmove"].forEach((e) =>
    canvas.addEventListener(e, ziehen, { passive: false })
  );
  ["mouseup", "mouseleave", "touchend", "touchcancel"].forEach((e) =>
    canvas.addEventListener(e, ziehenEnde)
  );

  d.querySelector(".cover-uebernehmen").addEventListener("click", uebernehmen);
  d.querySelector(".cover-abbrechen").addEventListener("click", schliessen);
  d.querySelector(".cover-neu").addEventListener("click", () =>
    d.querySelector("input[type=file]").click()
  );
});

// Von den Knöpfen in den Fragmenten aus aufgerufen.
window.coverAufnehmen = oeffnen;
