// The session picker: go live, or choose any session of any season to replay.

import { escapeHtml, shortSession } from "./format.js";

const LOCKED_TITLE = "Not available while a session is live (OpenF1 is subscriber-only until it ends)";

export class SessionBrowser {
  constructor(els, { onPick, onLive }) {
    this.els = els; // { modal, grid, tabs, liveCard, error, lockout, legend }
    this.onPick = onPick;
    this.onLive = onLive;
    this.year = null;
    this.currentKey = null;
    this.lastData = new Map(); // year -> last schedule response (avoids flashing "Loading")

    els.modal.addEventListener("click", (e) => {
      if (e.target.closest("[data-close]")) {
        this.close();
        return;
      }
      if (e.target.closest("[data-action='live']")) {
        this.close();
        this.onLive();
        return;
      }
      const chip = e.target.closest(".session-chip");
      if (chip && !chip.disabled) {
        this.close();
        if (chip.dataset.status === "live") this.onLive();
        else this.onPick(Number(chip.dataset.key));
      }
    });
    els.tabs.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) this.showYear(Number(b.dataset.year));
    });
  }

  get isOpen() {
    return !this.els.modal.hidden;
  }

  /** Open the picker; ``error`` explains why the last choice didn't load. */
  async open(currentKey, currentYear, { error } = {}) {
    this.currentKey = currentKey;
    this.els.error.hidden = !error;
    this.els.error.textContent = error ?? "";
    this.els.modal.hidden = false;
    if (!this.els.tabs.children.length) {
      try {
        const { seasons } = await (await fetch("/api/seasons")).json();
        this.els.tabs.innerHTML = seasons.map((y) => `<button data-year="${y}">${y}</button>`).join("");
      } catch {
        /* the grid below reports connection problems */
      }
    }
    await this.showYear(currentYear ?? this.year ?? new Date().getUTCFullYear());
  }

  close() {
    this.els.modal.hidden = true;
  }

  async showYear(year) {
    this.year = year;
    for (const b of this.els.tabs.children) b.classList.toggle("active", Number(b.dataset.year) === year);
    const previous = this.lastData.get(year);
    if (previous) this._render(previous);
    else this.els.grid.innerHTML = `<div class="grid-message">Loading ${year} calendar…</div>`;
    try {
      const resp = await fetch(`/api/schedule?year=${year}`);
      const data = await resp.json();
      this.lastData.set(year, data);
      if (this.year === year) this._render(data);
    } catch {
      this.els.grid.innerHTML = `<div class="grid-message">Can't reach the tracker server. Is run_f1_tracker.py still running?</div>`;
    }
  }

  // ------------------------------------------------------------------ rendering
  _render(data) {
    this._renderLiveCard(data.next_session, data.live_available);
    this.els.lockout.hidden = !data.locked;
    this.els.lockout.innerHTML = data.keeps_data
      ? `OpenF1 only serves subscribers while a session is live. Sessions marked <span class="saved-dot"></span>
         are saved on this computer and play now; the rest unlock when the session ends.`
      : "OpenF1 only serves subscribers while a session is live. Replays unlock again when the session ends.";
    this.els.legend.style.visibility = data.keeps_data ? "visible" : "hidden";
    if (data.error && !data.meetings.length) {
      this.els.grid.innerHTML = `<div class="grid-message">Couldn't load the ${data.year} calendar.<br><span class="muted">${escapeHtml(data.error)}</span></div>`;
      return;
    }
    if (!data.meetings.length) {
      this.els.grid.innerHTML = `<div class="grid-message">No sessions found for ${data.year}.</div>`;
      return;
    }
    this._renderGrid(data.meetings, data.locked);
  }

  _renderLiveCard(next, liveAvailable) {
    const card = this.els.liveCard;
    if (!next) {
      card.className = "live-card";
      card.innerHTML = `<div class="live-card-badge idle">NO UPCOMING SESSIONS</div>
        <div class="live-card-text"><b>Season complete</b><span>Pick any session below to replay it.</span></div>`;
      return;
    }
    const isLive = next.status === "live";
    card.className = `live-card ${isLive ? "is-live" : ""}`;
    const when = new Date(next.date_start * 1000).toLocaleString(undefined, {
      weekday: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
    card.innerHTML = isLive
      ? `<div class="live-card-badge"><span class="live-dot"></span> LIVE NOW</div>
         <div class="live-card-text"><b>${escapeHtml(next.meeting_name)}</b><span>${escapeHtml(next.name)}</span></div>
         <div class="live-card-action">
           <button class="btn btn-watch-live" data-action="live"><span class="live-dot"></span> Watch live</button>
           ${liveAvailable ? "" : `<span class="live-card-hint">Needs an OpenF1 subscription</span>`}
         </div>`
      : `<div class="live-card-badge idle">NEXT LIVE SESSION</div>
         <div class="live-card-text"><b>${escapeHtml(next.meeting_name)}</b><span>${escapeHtml(next.name)} · ${escapeHtml(when)} (${relative(next.date_start)})</span></div>
         <div class="live-card-action"><button class="btn" disabled>Not live yet</button></div>`;
  }

  _renderGrid(meetings, locked) {
    const fmt = new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short" });
    const time = new Intl.DateTimeFormat(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
    let round = 0;
    this.els.grid.innerHTML = meetings
      .map((m) => {
        const testing = m.name?.toLowerCase().includes("testing");
        if (!testing) round += 1;
        const current = m.sessions.some((s) => s.session_key === this.currentKey);
        const chips = m.sessions
          .map((s) => {
            const lockedOut = locked && s.status === "completed" && !s.saved;
            const disabled = s.status === "upcoming" || s.status === "cancelled" || lockedOut;
            const cls = [
              "session-chip",
              s.status,
              s.saved ? "saved" : "",
              s.session_key === this.currentKey ? "active" : "",
            ].join(" ");
            const title = lockedOut
              ? LOCKED_TITLE
              : `${s.name} — ${time.format(new Date(s.date_start * 1000))}${s.status === "live" ? " (LIVE)" : ""}${s.saved ? " · saved" : ""}`;
            return `<button class="${cls}" data-key="${s.session_key}" data-status="${s.status}" title="${escapeHtml(title)}" ${disabled ? "disabled" : ""}>${escapeHtml(shortSession(s.name))}</button>`;
          })
          .join("");
        const first = m.sessions[0].date_start;
        const last = m.sessions[m.sessions.length - 1].date_start;
        return `<article class="meeting ${current ? "current" : ""} ${m.cancelled ? "cancelled" : ""}">
          <div class="meeting-head">
            ${m.flag_url ? `<img src="${escapeHtml(m.flag_url)}" alt="">` : ""}
            <div>
              <div class="meeting-round">${testing ? "TESTING" : `ROUND ${round}`}${m.cancelled ? " · CANCELLED" : ""}${current ? " · WATCHING" : ""}</div>
              <div class="meeting-name">${escapeHtml(m.name)}</div>
            </div>
          </div>
          <div class="meeting-meta">${escapeHtml(m.circuit ?? m.location ?? "")} · ${fmt.format(new Date(first * 1000))} – ${fmt.format(new Date(last * 1000))}</div>
          <div class="session-chips">${chips}</div>
        </article>`;
      })
      .join("");
    this.els.grid.querySelector(".meeting.current")?.scrollIntoView({ block: "center" });
  }
}

function relative(epochSeconds) {
  const diff = epochSeconds - Date.now() / 1000;
  if (diff <= 0) return "starting now";
  const d = Math.floor(diff / 86400);
  const h = Math.floor((diff % 86400) / 3600);
  const m = Math.floor((diff % 3600) / 60);
  return `in ${d ? `${d}d ` : ""}${h ? `${h}h ` : ""}${m}m`;
}
