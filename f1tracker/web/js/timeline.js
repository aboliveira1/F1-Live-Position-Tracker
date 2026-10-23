// Playback controls and the session scrubber.

import { elapsed, trackClock, STATUS_COLOR } from "./format.js";
import { hexA } from "./trackmap.js";

const SPEED_CHOICES = [0.5, 1, 2, 4, 8, 16, 32, 64];
const BAR_Y = 26;
const BAR_H = 6;

export class Timeline {
  constructor(els, handlers) {
    this.els = els;
    this.h = handlers; // { seek, toggle, speed, skip, goLive }
    this.ctx = els.canvas.getContext("2d");
    this.timeline = null;
    this.session = null;
    this.buffer = [];
    this.clock = null;
    this.t = null;
    this.hoverX = null;
    this.dragging = false;

    els.play.addEventListener("click", () => this.h.toggle());
    els.back.addEventListener("click", (e) => this.h.skip(e.shiftKey ? -60 : -10));
    els.fwd.addEventListener("click", (e) => this.h.skip(e.shiftKey ? 60 : 10));
    els.liveEdge.addEventListener("click", () => this.h.goLive());

    els.speed.innerHTML = SPEED_CHOICES.map((s) => `<button data-speed="${s}">${s}×</button>`).join("");
    els.speed.addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (b) this.h.speed(Number(b.dataset.speed));
    });

    this._bindScrubber();
    new ResizeObserver(() => this._resize()).observe(els.canvas);
    this._resize();
  }

  setSession(session, timeline, live) {
    this.session = session;
    this.timeline = timeline;
    this.live = live;
    this.els.liveEdge.hidden = !live;
  }

  setBuffer(ranges) {
    this.buffer = ranges;
  }

  /** Called every animation frame with the latest clock and the rendered time. */
  update(clock, t) {
    this.clock = clock;
    this.t = t;
    const playing = clock?.playing;
    this.els.play.classList.toggle("playing", !!playing);
    for (const b of this.els.speed.children) {
      b.classList.toggle("active", clock && Number(b.dataset.speed) === clock.speed);
    }
    this.els.liveEdge.dataset.atEdge = clock?.following_live ? "true" : "false";

    if (this.session && t != null) {
      const tl = this.timeline;
      const ref = tl?.race_start && t >= tl.race_start - 600 ? tl.race_start : this.session.date_start;
      this.els.time.textContent = elapsed(t - ref);
      if (this.live && clock?.following_live) {
        this.els.timeSub.textContent = `live · ${clock.delay?.toFixed(1) ?? "–"} s delay`;
      } else if (this.live && clock) {
        this.els.timeSub.textContent = `${elapsed(clock.end - t)} behind live`;
      } else {
        this.els.timeSub.textContent = ref === tl?.race_start ? "since lights out" : "session time";
      }
    }
    this._draw();
  }

  // ------------------------------------------------------------------ scrubber
  _range() {
    if (!this.timeline || !this.clock) return null;
    const start = this.timeline.start;
    const end = this.live ? Math.max(this.clock.end, start + 60) : this.timeline.end;
    return { start, end };
  }

  _xFor(t, r) {
    return 8 + ((t - r.start) / (r.end - r.start)) * (this.w - 16);
  }

  _tFor(x, r) {
    return r.start + ((x - 8) / (this.w - 16)) * (r.end - r.start);
  }

  /** Lap ticks only mean something in races; other sessions have per-driver laps. */
  _lapMarkers() {
    return this.session?.is_race ? this.timeline?.lap_markers ?? [] : [];
  }

  _lapAt(t) {
    const markers = this._lapMarkers();
    let lap = null;
    for (const [n, start] of markers) {
      if (start <= t) lap = n;
      else break;
    }
    return lap;
  }

  _bindScrubber() {
    const c = this.els.canvas;
    const seekTo = (e, final) => {
      const r = this._range();
      if (!r) return;
      const rect = c.getBoundingClientRect();
      const x = Math.min(Math.max(e.clientX - rect.left, 8), this.w - 8);
      const now = performance.now();
      if (final || !this._lastSeek || now - this._lastSeek > 90) {
        this._lastSeek = now;
        this.h.seek(this._tFor(x, r));
      }
    };
    c.addEventListener("pointerdown", (e) => {
      this.dragging = true;
      c.setPointerCapture(e.pointerId);
      seekTo(e, false);
    });
    c.addEventListener("pointermove", (e) => {
      const rect = c.getBoundingClientRect();
      this.hoverX = e.clientX - rect.left;
      if (this.dragging) seekTo(e, false);
      this._tooltip();
    });
    c.addEventListener("pointerup", (e) => {
      if (this.dragging) seekTo(e, true);
      this.dragging = false;
    });
    c.addEventListener("pointerleave", () => {
      this.hoverX = null;
      this.els.tooltip.hidden = true;
    });
  }

  _tooltip() {
    const r = this._range();
    if (!r || this.hoverX == null) return;
    const t = this._tFor(this.hoverX, r);
    const lap = this._lapAt(t);
    const clock = trackClock(t, this.session?.gmt_offset_s ?? 0);
    this.els.tooltip.hidden = false;
    this.els.tooltip.style.left = `${Math.min(Math.max(this.hoverX, 50), this.w - 50)}px`;
    this.els.tooltip.textContent = lap ? `Lap ${lap} · ${clock}` : clock;
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.els.canvas.getBoundingClientRect();
    this.w = rect.width;
    this.hgt = rect.height;
    this.els.canvas.width = Math.round(rect.width * dpr);
    this.els.canvas.height = Math.round(rect.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  _draw() {
    const ctx = this.ctx;
    ctx.clearRect(0, 0, this.w, this.hgt);
    const r = this._range();
    if (!r) return;
    const x0 = 8;
    const x1 = this.w - 8;

    // Base bar and downloaded ranges.
    ctx.fillStyle = "rgba(255,255,255,0.07)";
    pill(ctx, x0, BAR_Y, x1 - x0, BAR_H);
    ctx.fillStyle = "rgba(255,255,255,0.16)";
    for (const [a, b] of this.buffer) {
      const xa = Math.max(this._xFor(a, r), x0);
      const xb = Math.min(this._xFor(b, r), x1);
      if (xb > xa) pill(ctx, xa, BAR_Y, xb - xa, BAR_H);
    }

    // Played portion.
    if (this.t != null) {
      const xp = Math.min(Math.max(this._xFor(this.t, r), x0), x1);
      const grad = ctx.createLinearGradient(x0, 0, xp, 0);
      grad.addColorStop(0, "rgba(225, 6, 0, 0.55)");
      grad.addColorStop(1, "#ff2a1f");
      ctx.fillStyle = grad;
      pill(ctx, x0, BAR_Y, Math.max(xp - x0, BAR_H), BAR_H);
    }

    // Safety car / VSC / red flag periods.
    for (const [a, b, kind] of this.timeline.status_periods ?? []) {
      const xa = Math.max(this._xFor(a, r), x0);
      const xb = Math.min(this._xFor(b, r), x1);
      if (xb <= xa) continue;
      ctx.fillStyle = hexA(STATUS_COLOR[kind] ?? "#ffd60a", 0.95);
      pill(ctx, xa, BAR_Y - 9, Math.max(xb - xa, 3), 4);
      ctx.fillStyle = hexA(STATUS_COLOR[kind] ?? "#ffd60a", 0.12);
      ctx.fillRect(xa, BAR_Y - 5, Math.max(xb - xa, 2), BAR_H + 10);
    }

    // Lap ticks with adaptive labelling.
    const markers = this._lapMarkers();
    if (markers.length > 1) {
      const span = this._xFor(markers[markers.length - 1][1], r) - this._xFor(markers[0][1], r);
      const perLap = span / markers.length;
      const every = [1, 2, 5, 10, 20].find((n) => perLap * n >= 30) ?? 25;
      ctx.font = `600 10px "JetBrains Mono", monospace`;
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      for (const [lap, t] of markers) {
        const x = this._xFor(t, r);
        if (x < x0 || x > x1) continue;
        const major = lap % every === 0 || lap === 1;
        ctx.fillStyle = major ? "rgba(255,255,255,0.4)" : "rgba(255,255,255,0.13)";
        ctx.fillRect(x, BAR_Y + BAR_H + 3, 1, major ? 6 : 3);
        if (major) {
          ctx.fillStyle = "rgba(180, 186, 198, 0.6)";
          ctx.fillText(lap === 1 ? "L1" : String(lap), x, BAR_Y + BAR_H + 11);
        }
      }
    }

    // Lights-out marker.
    if (this.timeline.race_start) {
      const x = this._xFor(this.timeline.race_start, r);
      ctx.fillStyle = "#1ed760";
      ctx.beginPath();
      ctx.moveTo(x, BAR_Y - 2);
      ctx.lineTo(x - 4, BAR_Y - 9);
      ctx.lineTo(x + 4, BAR_Y - 9);
      ctx.closePath();
      ctx.fill();
    }

    // Hover line.
    if (this.hoverX != null) {
      ctx.fillStyle = "rgba(255,255,255,0.35)";
      ctx.fillRect(Math.round(this.hoverX), BAR_Y - 10, 1, BAR_H + 20);
    }

    // Playhead.
    if (this.t != null) {
      const xp = Math.min(Math.max(this._xFor(this.t, r), x0), x1);
      ctx.shadowColor = "rgba(225, 6, 0, 0.8)";
      ctx.shadowBlur = 12;
      ctx.fillStyle = "#fff";
      ctx.beginPath();
      ctx.arc(xp, BAR_Y + BAR_H / 2, 7, 0, Math.PI * 2);
      ctx.fill();
      ctx.shadowBlur = 0;
      ctx.fillStyle = "#e10600";
      ctx.beginPath();
      ctx.arc(xp, BAR_Y + BAR_H / 2, 3, 0, Math.PI * 2);
      ctx.fill();
    }
  }
}

function pill(ctx, x, y, w, h) {
  const r = Math.min(h / 2, w / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
  ctx.fill();
}
