// Nur die reinen Funktionen aus cover.js prüfen -- sie brauchen kein DOM.
import { readFileSync } from "node:fs";
const quelle = readFileSync("static/cover.js", "utf8");
// Alles ab der Oberfläche abschneiden: dort wird document angefasst.
const mathe = quelle.split("/* ------------------------------------------------------------ Oberfläche */")[0];
const modul = await import("data:text/javascript," + encodeURIComponent(
  mathe + "\nexport { homographie, anwenden, eckenSchaetzen, standardRahmen };"
));

let fehler = 0;
const pruefe = (name, bedingung, zusatz = "") => {
  console.log(`  ${bedingung ? "ok  " : "FEHL"}  ${name}${zusatz}`);
  if (!bedingung) fehler++;
};

console.log("Homographie -- dieselben Werte wie in der Python-Gegenrechnung:");
const ecken = [[20, 30], [180, 10], [200, 160], [10, 140]];
const ziel = [[0, 0], [100, 0], [100, 100], [0, 100]];
const h = modul.homographie(ecken, ziel);
ecken.forEach((e, i) => {
  const got = modul.anwenden(h, e[0], e[1]);
  const passt = Math.abs(got[0] - ziel[i][0]) < 1e-6 && Math.abs(got[1] - ziel[i][1]) < 1e-6;
  pruefe(`(${e}) -> (${got[0].toFixed(2)}, ${got[1].toFixed(2)})`, passt, `  erwartet (${ziel[i]})`);
});

console.log("\nRückrichtung (beim Entzerren gebraucht):");
const rueck = modul.homographie(ziel, ecken);
ziel.forEach((z, i) => {
  const got = modul.anwenden(rueck, z[0], z[1]);
  const passt = Math.abs(got[0] - ecken[i][0]) < 1e-6 && Math.abs(got[1] - ecken[i][1]) < 1e-6;
  pruefe(`(${z}) -> (${got[0].toFixed(2)}, ${got[1].toFixed(2)})`, passt);
});

console.log("\nEckenerkennung an einem gemalten Cover:");
// Ein helles Viereck auf dunklem Grund, leicht schräg.
const W = 200, H = 200;
const daten = new Uint8ClampedArray(W * H * 4);
const innen = (x, y) => {
  const p = [[40, 25], [165, 45], [150, 170], [30, 150]];
  let d = false;
  for (let i = 0, j = 3; i < 4; j = i++) {
    const [xi, yi] = p[i], [xj, yj] = p[j];
    if ((yi > y) !== (yj > y) && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) d = !d;
  }
  return d;
};
for (let y = 0; y < H; y++) for (let x = 0; x < W; x++) {
  const i = (y * W + x) * 4;
  const v = innen(x, y) ? 230 : 30;
  daten[i] = daten[i + 1] = daten[i + 2] = v;
  daten[i + 3] = 255;
}
const gefunden = modul.eckenSchaetzen(daten, W, H);
const soll = [[40, 25], [165, 45], [150, 170], [30, 150]];
gefunden.forEach((g, i) => {
  const d = Math.hypot(g[0] - soll[i][0], g[1] - soll[i][1]);
  pruefe(`Ecke ${i}: (${g[0]},${g[1]})`, d < 12, `  gemalt (${soll[i]}), Abstand ${d.toFixed(1)} px`);
});

console.log("\nOhne erkennbare Kanten kommt ein Standardrahmen:");
const leer = new Uint8ClampedArray(W * H * 4).fill(128);
const r = modul.eckenSchaetzen(leer, W, H);
pruefe("vier Punkte, im Bild", r.length === 4 && r.every(([x, y]) => x >= 0 && y >= 0 && x <= W && y <= H));

process.exit(fehler ? 1 : 0);
