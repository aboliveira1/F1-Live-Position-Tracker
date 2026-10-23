// Entry point: wires the server stream to the UI components.

import { Connection } from "./connection.js";
import { FrameBuffer } from "./frames.js";
import { TrackMap } from "./trackmap.js";
import { Leaderboard } from "./leaderboard.js";
import { Timeline } from "./timeline.js";
import { DriverCard, RaceControlFeed, WeatherPanel } from "./panels.js";
import { SessionBrowser } from "./sessionBrowser.js";
import { STATUS_LABEL, shortSession, trackClock } from "./format.js";

const $ = (id) => document.getElementById(id);

const app = {
  status: { mode: "idle" },
  session: null, // latest "session" message
  drivers: new Map(),
  race: null, // latest "state" message
  clock: null, // latest clock from a frame
  selected: null,
  gapMode: "interval",
  lastStateT: null,
};

const frames = new FrameBuffer();
const conn = new Connection(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);

// ------------------------------------------------------------------ components
const map = new TrackMap($("track-canvas"), { onSelect: (n) => select(n) });
const board = new Leaderboard($("leaderboard"), {
  onSelect: (n) => select(n === app.selected ? null : n),
});
const timeline = new Timeline(
  {
    canvas: $("scrubber-canvas"),
    tooltip: $("scrub-tooltip"),
    play: $("btn-play"),
    back: $("btn-back"),
    fwd: $("btn-fwd"),
    speed: $("speed-control"),
    time: $("time-main"),
    timeSub: $("time-sub"),
    liveEdge: $("btn-live-edge"),
  },
  {
    seek: (t) => conn.send("seek", { t }),
    toggle: () => conn.send("toggle"),
    speed: (value) => conn.send("speed", { value }),
    skip: (seconds) => skip(seconds),
    goLive: () => conn.send("go_live"),
  }
);
const driverCard = new DriverCard({ card: $("driver-card"), empty: $("driver-card-empty"), body: $("driver-card-body") });
const weather = new WeatherPanel({
  air: $("wx-air"),
  track: $("wx-track"),
  humidity: $("wx-humidity"),
  wind: $("wx-wind"),
  rain: $("rain-chip"),
});
const feed = new RaceControlFeed($("feed"), $("feed-count"));
const browser = new SessionBrowser(
  {
    modal: $("session-modal"),
    grid: $("meeting-grid"),
    tabs: $("season-tabs"),
    liveCard: $("live-card"),
    error: $("picker-error"),
    lockout: $("picker-lockout"),
    legend: $("picker-legend"),
  },
  { onPick: (key) => pick(key), onLive: () => goLive() }
);
// The choice made in the picker that hasn't loaded yet. If it fails, the
// picker comes back with the reason, so you can pick something else.
let pendingChoice = null;
let pickerShownForIdle = false;

// ------------------------------------------------------------------ actions
function select(driver) {
  app.selected = driver ?? null;
  map.setSelected(app.selected);
  refreshRaceViews(false);
}

function skip(seconds) {
  const t = frames.time;
  if (t != null) conn.send("seek", { t: t + seconds });
}

function pick(sessionKey) {
  pendingChoice = { key: sessionKey, at: Date.now() };
  conn.send("load", { session_key: sessionKey });
}

function goLive() {
  if (!app.status.live_available) {
    $("live-modal").hidden = false;
    return;
  }
  pendingChoice = { key: "live", at: Date.now() };
  conn.send("live");
}

function openBrowser(options = {}) {
  browser.open(app.session?.session.session_key, app.session?.session.year, options);
}

function toast(message, kind = "info", ms = 7000) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("toasts").appendChild(el);
  setTimeout(() => {
    el.classList.add("leaving");
    setTimeout(() => el.remove(), 300);
  }, ms);
}

// ------------------------------------------------------------------ server messages
conn.on("status", (msg) => {
  const feedChanged = msg.feed?.state !== app.status.feed?.state;
  app.status = msg;
  const mode = msg.mode;
  $("mode-badge").dataset.mode = mode;
  $("mode-badge").textContent = { live: "LIVE", replay: "REPLAY", loading: "LOADING", idle: "IDLE" }[mode] ?? mode;
  $("btn-live").dataset.active = String(mode === "live");
  $("loading").hidden = mode !== "loading";
  if (mode === "idle" && msg.session_key == null && app.session) clearSession();
  const empty = mode === "idle" && !app.session;
  $("empty-state").hidden = !empty;
  if (empty && msg.notice) $("empty-message").textContent = msg.notice;
  if (msg.feed && feedChanged) toast(msg.feed.message, msg.feed.state === "error" ? "error" : "info", 4000);
  // Nothing to show (e.g. the first start failed): go straight to the picker, once.
  if (empty && !pickerShownForIdle && !browser.isOpen) {
    pickerShownForIdle = true;
    openBrowser({ error: msg.notice });
  }
});

conn.on("loading", (msg) => {
  $("loading").hidden = false;
  $("empty-state").hidden = true;
  $("loading-stage").textContent = msg.stage;
  $("loading-bar").style.width = `${Math.round(msg.progress * 100)}%`;
});

conn.on("session", (msg) => {
  if (pendingChoice && (pendingChoice.key === msg.session.session_key || (pendingChoice.key === "live" && msg.live))) {
    pendingChoice = null;
  }
  const isNew = app.session?.session.session_key !== msg.session.session_key || app.session?.live !== msg.live;
  app.session = msg;
  app.drivers = new Map(msg.drivers.map((d) => [d.number, d]));
  map.setDrivers(app.drivers);
  timeline.setSession(msg.session, msg.timeline, msg.live);

  if (isNew) {
    frames.reset();
    app.race = null;
    app.lastStateT = null;
    app.selected = null;
    map.setSelected(null);
    map.setTrack(msg.track);
    board.setSession(app.drivers, msg.session.is_race);
    timeline.setBuffer([]);
    setGapModes(msg.session.is_race);
    renderHeader(msg);
    $("empty-state").hidden = true;
  } else {
    if (msg.track && !map.track) map.setTrack(msg.track);
    board.drivers = app.drivers;
  }
});

conn.on("buffer", (msg) => timeline.setBuffer(msg.ranges));

conn.on("frame", (msg) => {
  frames.push(msg);
  app.clock = msg.clock;
});

conn.on("state", (msg) => {
  const continuous = app.lastStateT != null && msg.t >= app.lastStateT && msg.t - app.lastStateT < 30;
  app.lastStateT = msg.t;
  app.race = msg;
  map.setRace(msg);
  refreshRaceViews(continuous);
});

conn.on("error", (msg) => {
  if (pendingChoice && Date.now() - pendingChoice.at < 120_000) {
    pendingChoice = null;
    openBrowser({ error: msg.message }); // let the user choose again, with the reason on top
    return;
  }
  toast(msg.message, "error", 9000);
});
// If the server goes away (stopped with Ctrl+C, or it quit after the tab was
// closed elsewhere), say so instead of leaving a frozen map on screen.
let stoppedTimer = null;
conn.on("close", () => {
  $("mode-badge").textContent = "OFFLINE";
  stoppedTimer ??= setTimeout(() => ($("app-stopped").hidden = false), 3000);
});
conn.on("open", () => {
  clearTimeout(stoppedTimer);
  stoppedTimer = null;
  $("app-stopped").hidden = true;
});

// ------------------------------------------------------------------ views
/** The server has nothing loaded any more: go back to the empty state. */
function clearSession() {
  app.session = null;
  app.race = null;
  app.lastStateT = null;
  frames.reset();
  map.setTrack(null);
  map.setRace(null);
  select(null);
  driverCard.update(null);
  board.setSession(new Map(), true);
  timeline.setSession(null, null, false);
  $("session-name").textContent = "No session loaded";
  $("session-sub").textContent = "Pick a session to begin";
  $("session-flag").hidden = true;
}

function driverInfo(number) {
  return (
    app.drivers.get(number) ?? {
      number,
      code: String(number),
      first_name: "",
      last_name: `Car ${number}`,
      team_name: "",
      team_colour: "#9AA0A6",
      headshot_url: null,
    }
  );
}

function setGapModes(isRace) {
  const modes = isRace ? [["interval", "Interval"], ["leader", "Leader"]] : [["gap", "Gap"], ["time", "Best lap"]];
  app.gapMode = modes[0][0];
  $("gap-mode").innerHTML = modes
    .map(([key, label], i) => `<button data-mode="${key}" class="${i ? "" : "active"}">${label}</button>`)
    .join("");
  $("gap-col-label").textContent = isRace ? "Gap" : "Best";
}

function renderHeader(msg) {
  const s = msg.session;
  $("session-name").textContent = s.meeting_name;
  $("session-sub").textContent = `${s.session_name} · ${s.circuit_name} · ${s.year}`;
  const flag = $("session-flag");
  flag.hidden = !s.country_flag_url;
  if (s.country_flag_url) flag.src = s.country_flag_url;
  $("lap-counter").hidden = !s.is_race;
  document.title = `${s.meeting_name} · ${s.session_name} — F1 Live Tracker`;
}

function refreshRaceViews(animate) {
  const race = app.race;
  if (!race || !app.session) return;
  const isRace = app.session.session.is_race;

  board.update(race, app.gapMode, app.selected, animate);

  const status = race.track_status ?? "NONE";
  $("track-status").dataset.status = status;
  $("track-status").querySelector(".label").textContent = STATUS_LABEL[status] ?? status;
  $("lap-value").innerHTML = race.lap
    ? `${race.lap}${race.total_laps ? `<span class="total">/${race.total_laps}</span>` : ""}`
    : "–";

  const legend = $("legend");
  legend.hidden = !race.yellow_sectors.length;
  legend.innerHTML = `<span><i style="background: var(--yellow)"></i>Yellow flag · sector ${race.yellow_sectors.join(", ")}</span>`;

  const row = race.leaderboard.find((r) => r.driver === app.selected);
  driverCard.update(app.selected == null ? null : driverInfo(app.selected), row, isRace);
  weather.update(race.weather);
  feed.update(race.race_control, app.session.session.gmt_offset_s);
}

let lastClockText = "";
function renderClockCard(t) {
  const s = app.session?.session;
  const text = s && t != null ? trackClock(t, s.gmt_offset_s) : "--:--:--";
  if (text !== lastClockText) {
    lastClockText = text;
    $("clock-time").textContent = text;
    $("clock-sub").textContent = s ? `local time · ${s.location || s.circuit_name}` : "local track time";
  }
  const live = app.status.mode === "live";
  $("mode-badge").dataset.behind = String(live && app.clock && !app.clock.following_live);
}

// ------------------------------------------------------------------ session awareness
// Keep the "Go live" button honest: say when a session is on, or when the next one starts.
const announced = new Set();
async function refreshNextSession() {
  try {
    const resp = await fetch("/api/schedule");
    if (!resp.ok) return;
    const { next_session: next } = await resp.json();
    const button = $("btn-live");
    const isLive = next?.status === "live";
    button.classList.toggle("on-now", isLive);
    button.innerHTML = `<span class="live-dot"></span> ${isLive ? `Live now · ${shortSession(next.name)}` : "Go live"}`;
    button.title = next
      ? `${next.meeting_name} — ${next.name}: ${isLive ? "on now" : new Date(next.date_start * 1000).toLocaleString()}`
      : "No upcoming sessions";
    if (isLive && app.status.mode !== "live" && !announced.has(next.session_key)) {
      announced.add(next.session_key);
      toast(`${next.meeting_name} ${next.name} is live now — press “Live now” to watch.`, "info", 10000);
    }
  } catch {
    /* offline; try again later */
  }
}
refreshNextSession();
setInterval(refreshNextSession, 60_000);

// ------------------------------------------------------------------ render loop
const followButton = $("btn-follow");
function frame(now) {
  const t = frames.renderTime(now);
  map.render(t == null ? new Map() : frames.positionsAt(t), now);
  timeline.update(app.clock, t);
  renderClockCard(t);
  followButton.classList.toggle("active", map.follow); // dragging or resetting ends follow mode
  requestAnimationFrame(frame);
}
requestAnimationFrame(frame);

// ------------------------------------------------------------------ controls
for (const id of ["btn-sessions", "btn-empty-sessions", "brand-home", "session-title"]) {
  $(id).addEventListener("click", () => openBrowser());
}
$("btn-live").addEventListener("click", goLive);
$("btn-zoom-in").addEventListener("click", () => map.zoomBy(1.4));
$("btn-zoom-out").addEventListener("click", () => map.zoomBy(1 / 1.4));
$("btn-reset-view").addEventListener("click", () => map.resetView());
$("btn-follow").addEventListener("click", toggleFollow);
$("btn-labels").addEventListener("click", cycleLabels);
$("gap-mode").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  app.gapMode = b.dataset.mode;
  for (const btn of $("gap-mode").children) btn.classList.toggle("active", btn === b);
  refreshRaceViews(false);
});
$("live-modal").addEventListener("click", (e) => {
  if (e.target.closest("[data-close]")) $("live-modal").hidden = true;
});

function toggleFollow() {
  if (app.selected == null) {
    toast("Select a driver first, then press F to follow them.", "info", 3500);
    return;
  }
  map.setFollow(!map.follow);
}

function cycleLabels() {
  const mode = map.cycleLabels();
  toast(`Driver labels: ${mode}`, "info", 1500);
}

document.addEventListener("keydown", (e) => {
  if (e.target.closest("input, textarea")) return;
  if (e.key === "Escape") {
    if (browser.isOpen) browser.close();
    else if (!$("live-modal").hidden) $("live-modal").hidden = true;
    else select(null);
    return;
  }
  if (browser.isOpen) return;
  const speeds = [0.5, 1, 2, 4, 8, 16, 32, 64];
  const speedIndex = speeds.indexOf(app.clock?.speed ?? 1);
  switch (e.key) {
    case " ":
      e.preventDefault();
      conn.send("toggle");
      break;
    case "ArrowLeft":
      skip(e.shiftKey ? -60 : -10);
      break;
    case "ArrowRight":
      skip(e.shiftKey ? 60 : 10);
      break;
    case "ArrowUp":
      e.preventDefault();
      conn.send("speed", { value: speeds[Math.min(speedIndex + 1, speeds.length - 1)] });
      break;
    case "ArrowDown":
      e.preventDefault();
      conn.send("speed", { value: speeds[Math.max(speedIndex - 1, 0)] });
      break;
    case "l":
    case "L":
      conn.send("go_live");
      break;
    case "f":
    case "F":
      toggleFollow();
      break;
    case "n":
    case "N":
      cycleLabels();
      break;
    case "s":
    case "S":
      openBrowser();
      break;
    case "+":
    case "=":
      map.zoomBy(1.4);
      break;
    case "-":
      map.zoomBy(1 / 1.4);
      break;
    case "0":
      map.resetView();
      break;
  }
});

conn.connect();
