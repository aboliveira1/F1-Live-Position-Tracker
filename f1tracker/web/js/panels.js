// Sidebar panels: focused driver, track conditions, race-control feed.

import { escapeHtml, gap, lapTime, sector, trackClock, tyreLetter } from "./format.js";

export class DriverCard {
  constructor(els) {
    this.els = els; // { card, empty, body }
    this.shown = null;
  }

  update(driver, row, isRace) {
    const { empty, body, card } = this.els;
    if (!driver) {
      empty.hidden = false;
      body.hidden = true;
      this.shown = null;
      return;
    }
    empty.hidden = true;
    body.hidden = false;
    card.style.setProperty("--team", driver.team_colour);

    if (this.shown !== driver.number) {
      this.shown = driver.number;
      const initials = (driver.code || "").slice(0, 3);
      body.innerHTML = `
        <div class="dc-top">
          ${driver.headshot_url
            ? `<img class="dc-photo" src="${escapeHtml(driver.headshot_url)}" alt="" onerror="this.outerHTML='<div class=&quot;dc-photo&quot;>${escapeHtml(initials)}</div>'">`
            : `<div class="dc-photo">${escapeHtml(initials)}</div>`}
          <div>
            <div class="dc-number">#${driver.number}</div>
            <div class="dc-name">${escapeHtml(driver.first_name)} <b>${escapeHtml(driver.last_name)}</b></div>
            <div class="dc-team">${escapeHtml(driver.team_name)}</div>
          </div>
          <div class="dc-pos" data-k="pos"></div>
        </div>
        <div class="dc-grid">
          <div class="dc-stat"><div class="k">${isRace ? "Interval" : "Gap"}</div><div class="v" data-k="gap"></div></div>
          <div class="dc-stat"><div class="k">Last lap</div><div class="v" data-k="last"></div></div>
          <div class="dc-stat"><div class="k">Best lap</div><div class="v" data-k="best"></div></div>
          <div class="dc-stat"><div class="k">Lap</div><div class="v" data-k="lap"></div></div>
          <div class="dc-stat"><div class="k">Tyre</div><div class="v" data-k="tyre"></div></div>
          <div class="dc-stat"><div class="k">Stops</div><div class="v" data-k="stops"></div></div>
        </div>
        <div class="dc-sectors">
          <div class="dc-sector"><span>S1</span><b data-k="s1"></b></div>
          <div class="dc-sector"><span>S2</span><b data-k="s2"></b></div>
          <div class="dc-sector"><span>S3</span><b data-k="s3"></b></div>
        </div>`;
    }

    const set = (k, v) => (body.querySelector(`[data-k="${k}"]`).innerHTML = v);
    set("pos", row?.position ? `<small>P</small>${row.position}` : "–");
    if (isRace) set("gap", row?.position === 1 ? "Leader" : gap(row?.interval ?? row?.gap_to_leader));
    else set("gap", row?.best_gap == null ? "—" : `+${row.best_gap.toFixed(3)}`);
    set("last", lapTime(row?.last_lap));
    set("best", lapTime(row?.best_lap));
    set("lap", row?.lap || "–");
    set("tyre", row?.compound ? `${tyreLetter(row.compound)} · ${row.tyre_age ?? 0}L` : "—");
    set("stops", row?.in_pit ? `${row.pit_stops} · PIT` : row?.pit_stops ?? 0);
    const [s1, s2, s3] = row?.sectors ?? [];
    set("s1", sector(s1));
    set("s2", sector(s2));
    set("s3", sector(s3));
  }
}

export class WeatherPanel {
  constructor(els) {
    this.els = els; // { air, track, humidity, wind, rain }
  }

  update(weather) {
    const { air, track, humidity, wind, rain } = this.els;
    if (!weather) {
      for (const el of [air, track, humidity, wind]) el.textContent = "–";
      rain.hidden = true;
      return;
    }
    air.textContent = weather.air_temp != null ? `${weather.air_temp.toFixed(1)}°` : "–";
    track.textContent = weather.track_temp != null ? `${weather.track_temp.toFixed(1)}°` : "–";
    humidity.textContent = weather.humidity != null ? `${Math.round(weather.humidity)}%` : "–";
    const deg = weather.wind_direction ?? 0;
    wind.innerHTML = weather.wind_speed != null
      ? `<svg class="wind-arrow" viewBox="0 0 12 12" style="transform: rotate(${deg + 180}deg)"><path d="M6 10V2M3 5l3-3 3 3"/></svg>${weather.wind_speed.toFixed(1)}`
      : "–";
    rain.hidden = !weather.rainfall;
  }
}

export class RaceControlFeed {
  constructor(list, count) {
    this.list = list;
    this.count = count;
    this.signature = "";
  }

  update(messages, gmtOffset) {
    const signature = messages.length ? `${messages.length}:${messages[0].t}:${messages[0].message}` : "";
    if (signature === this.signature) return;
    this.signature = signature;
    this.count.textContent = messages.length ? `${messages.length} latest` : "";
    if (!messages.length) {
      this.list.innerHTML = `<li class="feed-empty">No messages yet</li>`;
      return;
    }
    this.list.innerHTML = messages
      .map((m) => {
        const chip = chipFor(m);
        return `<li>
          <span class="when"><b>${trackClock(m.t, gmtOffset).slice(0, 5)}</b>${m.lap ? `L${m.lap}` : ""}</span>
          <span class="msg">${chip}${escapeHtml(m.message)}</span>
        </li>`;
      })
      .join("");
  }
}

function chipFor(m) {
  if (m.category === "Flag" && m.flag) {
    const key = m.flag.split(" ")[0];
    return `<span class="chip flag-${escapeHtml(key)}">${escapeHtml(m.flag)}</span>`;
  }
  if (m.category === "SafetyCar") return `<span class="chip cat-SafetyCar">SC</span>`;
  if (m.category === "SessionStatus") return `<span class="chip cat-SessionStatus">SESSION</span>`;
  return "";
}
