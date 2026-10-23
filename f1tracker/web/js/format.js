// Formatting helpers shared by the UI components.

export const pad = (n, width = 2) => String(Math.trunc(n)).padStart(width, "0");

/** Time of day at the circuit, e.g. "14:03:27". */
export function trackClock(epochSeconds, gmtOffsetSeconds = 0) {
  if (epochSeconds == null) return "--:--:--";
  const d = new Date((epochSeconds + gmtOffsetSeconds) * 1000);
  return `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
}

/** Elapsed time, e.g. "1:23:45" or "23:45"; negative values get a leading minus. */
export function elapsed(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "--:--";
  const sign = seconds < 0 ? "-" : "";
  let s = Math.abs(Math.floor(seconds));
  const h = Math.floor(s / 3600);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  s -= m * 60;
  return h ? `${sign}${h}:${pad(m)}:${pad(s)}` : `${sign}${m}:${pad(s)}`;
}

/** Lap time, e.g. "1:37.604". */
export function lapTime(seconds) {
  if (seconds == null) return "—";
  const m = Math.floor(seconds / 60);
  const s = seconds - m * 60;
  return m ? `${m}:${s.toFixed(3).padStart(6, "0")}` : s.toFixed(3);
}

/** Gap to another car: numbers become "+1.234", labels like "+1 LAP" pass through. */
export function gap(value) {
  if (value == null) return "—";
  if (typeof value === "string") return value;
  return `+${value.toFixed(3)}`;
}

export function sector(seconds) {
  return seconds == null ? "—" : seconds.toFixed(3);
}

const SESSION_SHORT = {
  "Practice 1": "FP1",
  "Practice 2": "FP2",
  "Practice 3": "FP3",
  "Qualifying": "Q",
  "Sprint Qualifying": "SQ",
  "Sprint Shootout": "SQ",
  "Sprint": "S",
  "Race": "R",
  "Day 1": "D1",
  "Day 2": "D2",
  "Day 3": "D3",
};
export const shortSession = (name) => SESSION_SHORT[name] ?? name;

const TYRE_LETTER = { SOFT: "S", MEDIUM: "M", HARD: "H", INTERMEDIATE: "I", WET: "W" };
export const tyreLetter = (compound) => TYRE_LETTER[compound] ?? "?";

export const STATUS_LABEL = {
  NONE: "NO FLAGS",
  GREEN: "TRACK CLEAR",
  YELLOW: "YELLOW FLAG",
  SC: "SAFETY CAR",
  SC_ENDING: "SC ENDING",
  VSC: "VIRTUAL SC",
  VSC_ENDING: "VSC ENDING",
  RED: "RED FLAG",
  CHEQUERED: "CHEQUERED",
};

export const STATUS_COLOR = {
  SC: "#ffab00",
  VSC: "#ffab00",
  RED: "#ff3b30",
  YELLOW: "#ffd60a",
};

export function escapeHtml(text) {
  return String(text ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]
  );
}
