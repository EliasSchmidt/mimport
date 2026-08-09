// Bescheid geben, wenn ein Rip oder ein m4b-Bau fertig ist.
//
// Ein Rip dauert eine halbe Stunde, ein Encode Stunden -- niemand sitzt daneben.
// Deshalb drei Stufen, absteigend in der Verlässlichkeit:
//
//   1. Titel des Tabs. Funktioniert immer und überall, auch über HTTP.
//   2. Echte Benachrichtigung über die Notifications-API. Die verlangt einen
//      "secure context", also HTTPS oder localhost -- über http://server:8001
//      gibt es sie nicht. Deshalb ist sie das Extra und nicht die Grundlage.
//   3. Ein kurzer Ton, erzeugt über die WebAudio-API. Braucht keine Datei, aber
//      eine vorherige Nutzerinteraktion -- die gab es, wer den Rip gestartet hat.
//
// Die Aufträge melden ihren Zustand über unsichtbare Marker im HTML, die htmx
// bei jedem Nachladen mitbringt. Dieses Skript vergleicht nur, was sich
// gegenüber dem letzten Stand geändert hat.

const bekannt = new Map();
let tonErlaubt = false;
let titelOriginal = document.title;
let offeneMeldungen = 0;

/** Fragt die Erlaubnis für Benachrichtigungen -- nur aus einem Klick heraus. */
function erlaubnisAnfragen() {
  if (!("Notification" in window)) return;
  if (Notification.permission !== "default") return;
  Notification.requestPermission().catch(() => {});
}

/**
 * Ein kurzer Zweiklang. Bewusst selbst erzeugt statt als Datei: das spart eine
 * Ressource, und über die WebAudio-API klingt es überall gleich.
 */
function tonSpielen() {
  if (!tonErlaubt) return;
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    [880, 1174].forEach((hz, i) => {
      const osc = ctx.createOscillator();
      const gain = ctx.createGain();
      osc.frequency.value = hz;
      osc.type = "sine";
      gain.gain.setValueAtTime(0.0001, ctx.currentTime);
      gain.gain.exponentialRampToValueAtTime(0.15, ctx.currentTime + 0.02 + i * 0.18);
      gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.3 + i * 0.18);
      osc.connect(gain).connect(ctx.destination);
      osc.start(ctx.currentTime + i * 0.18);
      osc.stop(ctx.currentTime + 0.4 + i * 0.18);
    });
    setTimeout(() => ctx.close().catch(() => {}), 1200);
  } catch {
    // Kein Audio verfügbar -- die anderen zwei Stufen genügen.
  }
}

/** Titel zurücksetzen, sobald der Tab wieder angesehen wird. */
function titelZuruecksetzen() {
  offeneMeldungen = 0;
  document.title = titelOriginal;
}

function titelMarkieren(erfolg) {
  if (!document.hidden) return;
  offeneMeldungen += 1;
  const zeichen = erfolg ? "✓" : "✗";
  document.title = `${zeichen} ${titelOriginal}`;
}

function melden(text, erfolg) {
  titelMarkieren(erfolg);
  tonSpielen();

  if (!("Notification" in window) || Notification.permission !== "granted") return;
  try {
    new Notification(erfolg ? "mimport: fertig" : "mimport: fehlgeschlagen", {
      body: text,
      // Gleiches tag heißt: eine neue Meldung ersetzt die alte, statt sich zu
      // stapeln, wenn mehrere Aufträge nacheinander enden.
      tag: "mimport-auftrag",
    });
  } catch {
    // Manche Browser werfen hier trotz Erlaubnis -- dann bleibt es beim Titel.
  }
}

/**
 * Vergleicht die Marker im HTML mit dem letzten Stand. Gemeldet wird nur der
 * Übergang von "läuft" auf einen Endzustand -- sonst käme bei jedem Nachladen
 * im Zweisekundentakt eine neue Meldung.
 */
function auftraegePruefen(wurzel) {
  (wurzel || document).querySelectorAll?.("[data-auftrag]").forEach((el) => {
    const id = el.dataset.auftrag;
    const zustand = el.dataset.zustand;
    const vorher = bekannt.get(id);
    bekannt.set(id, zustand);

    if (vorher === "laeuft" && zustand !== "laeuft") {
      melden(el.dataset.text || "Fertig.", zustand === "fertig");
    }
  });
}

document.addEventListener("visibilitychange", () => {
  if (!document.hidden && offeneMeldungen) titelZuruecksetzen();
});

// Jeder Klick auf einen Start-Knopf ist der Moment, in dem beides zulässig ist:
// die Erlaubnisanfrage und später der Ton.
document.addEventListener("click", (event) => {
  if (!event.target.closest("button, .button")) return;
  tonErlaubt = true;
  erlaubnisAnfragen();
});

// htmx tauscht die Fragmente aus; danach stehen die neuen Marker im Dokument.
document.body.addEventListener("htmx:afterSwap", (event) => {
  auftraegePruefen(event.detail.target);
});

// Beim Seitenaufruf den Ausgangsstand aufnehmen, ohne zu melden: ein bereits
// fertiger Auftrag von vorhin ist keine Neuigkeit.
document.addEventListener("DOMContentLoaded", () => {
  titelOriginal = document.title;
  auftraegePruefen(document);
});

/* -------------------------------------------------- Sichtbarer Zustand ---
 *
 * Ein Feature, das man nicht findet, gibt es nicht. Deshalb zeigt die Seite,
 * woran man ist: ob eine Systembenachrichtigung möglich ist, ob sie erlaubt
 * wurde, und was stattdessen passiert. Titel und Ton laufen ohnehin -- die
 * brauchen keine Erlaubnis.
 */
function zustandZeigen() {
  const kasten = document.getElementById("benachrichtigung");
  if (!kasten) return;

  const sicher = window.isSecureContext;
  const moeglich = "Notification" in window && sicher;
  const stand = moeglich ? Notification.permission : null;

  let text;
  let knopf = "";

  if (stand === "granted") {
    text = "<strong>Benachrichtigung an.</strong> Wenn ein Rip oder ein " +
           "m4b-Bau endet, meldet sich das System – dazu ein Ton und ein " +
           "Haken im Tab-Titel.";
  } else if (stand === "default") {
    text = "Bei fertigem Rip oder m4b piept es und der Tab-Titel bekommt " +
           "einen Haken. Für eine Systembenachrichtigung braucht es einmal " +
           "deine Erlaubnis.";
    knopf = '<button type="button" class="button">Benachrichtigungen erlauben</button>';
  } else if (stand === "denied") {
    text = "Systembenachrichtigungen sind für diese Seite abgelehnt – das " +
           "lässt sich nur in den Browsereinstellungen zurücknehmen. Ton und " +
           "Haken im Tab-Titel kommen trotzdem.";
  } else if (!sicher) {
    text = "Bei fertigem Rip oder m4b piept es und der Tab-Titel bekommt " +
           "einen Haken. <strong>Systembenachrichtigungen gibt es nur über " +
           "HTTPS</strong> – Browser lassen sie über eine unverschlüsselte " +
           "Verbindung nicht zu.";
  } else {
    text = "Bei fertigem Rip oder m4b piept es und der Tab-Titel bekommt " +
           "einen Haken.";
  }

  kasten.innerHTML = `<span>${text}</span>${knopf}`;
  kasten.hidden = false;

  kasten.querySelector("button")?.addEventListener("click", () => {
    Notification.requestPermission().then(zustandZeigen).catch(() => {});
  });
}

document.addEventListener("DOMContentLoaded", zustandZeigen);
