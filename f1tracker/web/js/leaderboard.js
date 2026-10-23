// Standings tower with animated re-ordering.

import { gap, lapTime, tyreLetter } from "./format.js";

export class Leaderboard {
  constructor(list, { onSelect }) {
    this.list = list;
    this.onSelect = onSelect;
    this.rows = new Map(); // driver -> <li>
    this.positions = new Map(); // driver -> last shown position
    this.drivers = new Map();
    this.isRace = true;
    list.addEventListener("click", (e) => {
      const row = e.target.closest(".lb-row");
      if (row) this.onSelect(Number(row.dataset.driver));
    });
  }

  setSession(drivers, isRace) {
    this.drivers = drivers;
    this.isRace = isRace;
    this.list.innerHTML = "";
    this.rows.clear();
    this.positions.clear();
  }

  /**
   * @param race      latest "state" message
   * @param gapMode   "interval" | "leader" (race) or "gap" | "time" (other sessions)
   * @param animate   false right after a seek, to avoid a burst of flashes
   */
  update(race, gapMode, selected, animate = true) {
    const before = new Map();
    for (const [n, li] of this.rows) before.set(n, li.getBoundingClientRect().top);

    const ordered = race.leaderboard.filter((r) => this.drivers.has(r.driver) || r.position);
    const fragment = document.createDocumentFragment();
    for (const row of ordered) {
      const li = this._row(row.driver);
      this._fill(li, row, gapMode, selected);
      const prev = this.positions.get(row.driver);
      if (animate && this.isRace && prev && row.position && prev !== row.position) {
        li.classList.remove("gained", "lost");
        void li.offsetWidth; // restart the flash animation
        li.classList.add(row.position < prev ? "gained" : "lost");
      }
      if (row.position) this.positions.set(row.driver, row.position);
      fragment.appendChild(li);
    }
    this.list.appendChild(fragment);

    // FLIP: slide rows from their old place to the new one.
    if (!animate) return;
    for (const [n, li] of this.rows) {
      const old = before.get(n);
      if (old == null) continue;
      const delta = old - li.getBoundingClientRect().top;
      if (Math.abs(delta) < 1) continue;
      li.style.transition = "none";
      li.style.transform = `translateY(${delta}px)`;
      requestAnimationFrame(() => {
        li.style.transition = "transform 0.55s cubic-bezier(.2,.8,.2,1)";
        li.style.transform = "";
      });
    }
  }

  _row(driver) {
    let li = this.rows.get(driver);
    if (!li) {
      li = document.createElement("li");
      li.className = "lb-row";
      li.dataset.driver = driver;
      li.innerHTML = `
        <span class="lb-pos"></span>
        <span class="lb-driver">
          <span class="lb-team-bar"></span>
          <span class="lb-code"></span>
          <span class="lb-badge" hidden></span>
          <span class="lb-team"></span>
        </span>
        <span class="lb-gap"></span>
        <span class="lb-tyre"><span class="tyre"></span><span class="tyre-age"></span></span>`;
      this.rows.set(driver, li);
    }
    return li;
  }

  /** Name, team and colour — refreshed because live driver info can arrive late. */
  _identity(li, number) {
    const d = this.drivers.get(number);
    const key = d ? `${d.code}|${d.team_name}|${d.team_colour}` : String(number);
    if (li.dataset.identity === key) return;
    li.dataset.identity = key;
    li.style.setProperty("--team", d?.team_colour ?? "#9aa0a6");
    li.querySelector(".lb-code").textContent = d?.code ?? number;
    li.querySelector(".lb-team").textContent = d?.team_name ?? "";
  }

  _fill(li, row, gapMode, selected) {
    this._identity(li, row.driver);
    li.dataset.pos = row.position ?? "";
    li.classList.toggle("selected", row.driver === selected);
    li.classList.toggle("retired", !!row.retired);
    li.querySelector(".lb-pos").textContent = row.position ?? "–";

    const badge = li.querySelector(".lb-badge");
    if (row.retired) setBadge(badge, "OUT", "out");
    else if (row.in_pit) setBadge(badge, "PIT", "pit");
    else badge.hidden = true;

    const gapEl = li.querySelector(".lb-gap");
    gapEl.className = "lb-gap";
    if (this.isRace) {
      if (row.position === 1) {
        gapEl.textContent = "LEADER";
        gapEl.classList.add("leader");
      } else {
        gapEl.textContent = gap(gapMode === "leader" ? row.gap_to_leader : row.interval ?? row.gap_to_leader);
      }
    } else if (gapMode === "time" || row.position === 1) {
      gapEl.textContent = lapTime(row.best_lap);
      if (row.best_gap === 0) gapEl.classList.add("fastest");
    } else {
      gapEl.textContent = row.best_gap == null ? "—" : `+${row.best_gap.toFixed(3)}`;
    }

    const tyre = li.querySelector(".tyre");
    const compound = row.compound ?? "?";
    tyre.dataset.c = compound;
    tyre.textContent = tyreLetter(compound);
    tyre.title = row.compound ? `${row.compound} — ${row.tyre_age ?? 0} laps old` : "Unknown compound";
    li.querySelector(".tyre-age").textContent = row.tyre_age ?? "";
  }
}

function setBadge(el, text, cls) {
  el.hidden = false;
  el.textContent = text;
  el.className = `lb-badge ${cls}`;
}
