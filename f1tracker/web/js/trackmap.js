// Canvas renderer for the circuit and the cars on it.
//
// Coordinates: the feed's (x, y) are rotated by the circuit's canonical
// rotation ("map space"), then scaled/panned onto the screen by a camera.

import { STATUS_COLOR } from "./format.js";

const PAD = 48;
const TAIL_MS = 650; // length of the motion trail behind each car
const TAIL_SAMPLE_MS = 30;
const LABEL_MODES = ["all", "selected", "none"];

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

export class TrackMap {
  constructor(canvas, { onSelect } = {}) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.onSelect = onSelect;

    this.track = null; // { pts: [[mx,my]...], corners, sectorStarts }
    this.rotation = 0;
    this.drivers = new Map();
    this.race = null;
    this.selected = null;
    this.labelMode = "all";
    this.follow = false;

    // Camera: centre in map space, zoom factor on top of the fit scale.
    this.fitScale = 1;
    this.cam = { x: 0, y: 0, zoom: 1 };
    this.target = { x: 0, y: 0, zoom: 1 };
    this.bounds = null;

    this.tails = new Map(); // driver -> [{x, y, at}]
    this.screenCars = []; // for hit testing
    this.cloud = []; // fallback outline built from car positions when no layout exists
    this.lastCloudAt = 0;
    this.hovered = null;

    this._bindInput();
    new ResizeObserver(() => this._resize()).observe(canvas);
    this._resize();
  }

  // ------------------------------------------------------------------ data
  setTrack(track) {
    this.tails.clear();
    this.cloud = [];
    this.userAdjusted = false;
    if (!track) {
      this.track = null;
      this.rotation = 0;
      this.bounds = null;
      return;
    }
    this.rotation = (track.rotation * Math.PI) / 180;
    const pts = track.x.map((x, i) => this._rotate(x, track.y[i]));
    this.track = {
      pts,
      corners: track.corners.map((c) => ({ ...c, m: this._rotate(c.x, c.y) })),
      sectorStarts: track.marshal_sector_starts,
    };
    this._setBounds(pts);
    this.resetView(true);
  }

  setDrivers(drivers) {
    this.drivers = drivers;
  }

  setRace(race) {
    this.race = race;
    this.order = new Map((race?.leaderboard ?? []).map((row) => [row.driver, row]));
  }

  setSelected(driver) {
    this.selected = driver;
    if (driver == null) this.follow = false;
  }

  cycleLabels() {
    this.labelMode = LABEL_MODES[(LABEL_MODES.indexOf(this.labelMode) + 1) % LABEL_MODES.length];
    return this.labelMode;
  }

  setFollow(on) {
    this.follow = on && this.selected != null;
    if (this.follow) {
      this.userAdjusted = true;
      this.target.zoom = Math.max(this.target.zoom, 2.6);
    }
    return this.follow;
  }

  zoomBy(factor, sx = this.w / 2, sy = this.h / 2) {
    this.userAdjusted = true;
    const before = this._toMap(sx, sy, this.target);
    this.target.zoom = clamp(this.target.zoom * factor, 0.6, 14);
    if (!this.follow) {
      const scale = this.fitScale * this.target.zoom;
      this.target.x = before[0] - (sx - this.w / 2) / scale;
      this.target.y = before[1] + (sy - this.h / 2) / scale;
    }
  }

  /** Fit the whole circuit on screen (and resume auto-fitting while it's traced from cars). */
  resetView(instant = false) {
    this.follow = false;
    this.userAdjusted = false;
    const b = this.bounds;
    this.target = b ? { x: (b.minX + b.maxX) / 2, y: (b.minY + b.maxY) / 2, zoom: 1 } : { x: 0, y: 0, zoom: 1 };
    if (instant) this.cam = { ...this.target };
  }

  // ------------------------------------------------------------------ geometry
  _rotate(x, y) {
    const c = Math.cos(this.rotation);
    const s = Math.sin(this.rotation);
    return [x * c - y * s, x * s + y * c];
  }

  _setBounds(pts) {
    if (!pts.length) return; // keep the old bounds rather than ±Infinity
    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    for (const [x, y] of pts) {
      if (x < minX) minX = x;
      if (x > maxX) maxX = x;
      if (y < minY) minY = y;
      if (y > maxY) maxY = y;
    }
    this.bounds = { minX, minY, maxX, maxY };
    this._fit();
  }

  _fit() {
    const b = this.bounds;
    if (!b || !this.w) return;
    const w = Math.max(b.maxX - b.minX, 1);
    const h = Math.max(b.maxY - b.minY, 1);
    this.fitScale = Math.min((this.w - 2 * PAD) / w, (this.h - 2 * PAD) / h);
  }

  _toScreen(mx, my, cam = this.cam) {
    const s = this.fitScale * cam.zoom;
    return [this.w / 2 + (mx - cam.x) * s, this.h / 2 - (my - cam.y) * s];
  }

  _toMap(sx, sy, cam = this.cam) {
    const s = this.fitScale * cam.zoom;
    return [cam.x + (sx - this.w / 2) / s, cam.y - (sy - this.h / 2) / s];
  }

  _resize() {
    const dpr = window.devicePixelRatio || 1;
    const rect = this.canvas.getBoundingClientRect();
    this.w = rect.width;
    this.h = rect.height;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this._fit();
  }

  // ------------------------------------------------------------------ input
  _bindInput() {
    const c = this.canvas;
    let drag = null;

    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const rect = c.getBoundingClientRect();
      this.zoomBy(Math.exp(-e.deltaY * 0.0015), e.clientX - rect.left, e.clientY - rect.top);
    }, { passive: false });

    c.addEventListener("pointerdown", (e) => {
      drag = { x: e.clientX, y: e.clientY, moved: false };
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", (e) => {
      const rect = c.getBoundingClientRect();
      if (drag) {
        const dx = e.clientX - drag.x;
        const dy = e.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) {
          drag.moved = true;
          this.follow = false;
          c.classList.add("dragging");
        }
        if (drag.moved) {
          this.userAdjusted = true;
          const s = this.fitScale * this.cam.zoom;
          this.target.x -= dx / s;
          this.target.y += dy / s;
          this.cam.x = this.target.x;
          this.cam.y = this.target.y;
          drag.x = e.clientX;
          drag.y = e.clientY;
        }
      } else {
        this.hovered = this._hitTest(e.clientX - rect.left, e.clientY - rect.top);
        c.classList.toggle("hovering", this.hovered != null);
      }
    });
    c.addEventListener("pointerup", (e) => {
      c.classList.remove("dragging");
      if (drag && !drag.moved) {
        const rect = c.getBoundingClientRect();
        const hit = this._hitTest(e.clientX - rect.left, e.clientY - rect.top);
        this.onSelect?.(hit);
      }
      drag = null;
    });
    c.addEventListener("dblclick", () => this.resetView());
  }

  _hitTest(sx, sy) {
    let best = null;
    let bestD = 14 * 14;
    for (const car of this.screenCars) {
      const d = (car.x - sx) ** 2 + (car.y - sy) ** 2;
      if (d < bestD) {
        bestD = d;
        best = car.n;
      }
    }
    return best;
  }

  // ------------------------------------------------------------------ render
  render(positions, now = performance.now()) {
    const ctx = this.ctx;
    const mapPos = new Map();
    for (const [n, [x, y]] of positions) mapPos.set(n, this._rotate(x, y));

    if (!this.track) this._growCloud(mapPos, now);
    this._updateCamera(mapPos);
    this._updateTails(mapPos, now);

    ctx.clearRect(0, 0, this.w, this.h);
    if (this.track) this._drawTrack();
    else this._drawCloud();
    this._drawCars(mapPos, now);
  }

  _updateCamera(mapPos) {
    if (this.follow && this.selected != null && mapPos.has(this.selected)) {
      const [x, y] = mapPos.get(this.selected);
      this.target.x = x;
      this.target.y = y;
    }
    const k = this.follow ? 0.12 : 0.22;
    this.cam.x += (this.target.x - this.cam.x) * k;
    this.cam.y += (this.target.y - this.cam.y) * k;
    this.cam.zoom += (this.target.zoom - this.cam.zoom) * 0.2;
    if (![this.cam.x, this.cam.y, this.cam.zoom].every(Number.isFinite)) {
      this.cam = { ...this.target }; // never let a bad value stick
    }
  }

  _trackWidth() {
    return clamp(8 * Math.sqrt(this.cam.zoom), 6, 26);
  }

  _strokePath(points, width, style, close = true) {
    const ctx = this.ctx;
    ctx.beginPath();
    points.forEach(([x, y], i) => {
      const [sx, sy] = this._toScreen(x, y);
      i ? ctx.lineTo(sx, sy) : ctx.moveTo(sx, sy);
    });
    if (close) ctx.closePath();
    ctx.lineWidth = width;
    ctx.strokeStyle = style;
    ctx.stroke();
  }

  _drawTrack() {
    const ctx = this.ctx;
    const { pts, corners, sectorStarts } = this.track;
    const w = this._trackWidth();
    const status = this.race?.track_status;
    const glow = STATUS_COLOR[status === "SC_ENDING" ? "SC" : status === "VSC_ENDING" ? "VSC" : status];

    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    // Soft glow, tinted under SC / VSC / red flag.
    this._strokePath(pts, w + 26, glow ? hexA(glow, 0.1) : "rgba(120, 140, 200, 0.045)");
    this._strokePath(pts, w + 12, glow ? hexA(glow, 0.16) : "rgba(140, 160, 220, 0.06)");
    // Kerb edge, asphalt, centre line.
    this._strokePath(pts, w + 3, "#3b4252");
    this._strokePath(pts, w, "#171a22");
    ctx.setLineDash([2, 10]);
    this._strokePath(pts, 1, "rgba(255,255,255,0.08)");
    ctx.setLineDash([]);

    // Marshal sectors under yellow flags.
    for (const sector of this.race?.yellow_sectors ?? []) {
      const from = sectorStarts[sector - 1];
      if (from == null) continue;
      const to = sectorStarts[sector] ?? sectorStarts[0]; // the last sector wraps to the first
      const seg = to > from ? pts.slice(from, to + 1) : [...pts.slice(from), ...pts.slice(0, to + 1)];
      this._strokePath(seg, w + 10, "rgba(255, 214, 10, 0.25)", false);
      this._strokePath(seg, w * 0.55, "rgba(255, 214, 10, 0.95)", false);
    }

    this._drawStartLine(pts, w);

    // Corner numbers, placed beside the track in the direction the data suggests.
    ctx.font = `600 ${clamp(9 + this.cam.zoom * 0.6, 10, 13)}px "Titillium Web", sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillStyle = "rgba(170, 178, 194, 0.55)";
    const dist = w / 2 + 12;
    for (const c of corners) {
      const [sx, sy] = this._toScreen(...c.m);
      const a = (c.angle * Math.PI) / 180 + this.rotation;
      ctx.fillText(`${c.number}${c.letter}`, sx + Math.cos(a) * dist, sy - Math.sin(a) * dist);
    }
  }

  _drawStartLine(pts, w) {
    if (pts.length < 4) return;
    const ctx = this.ctx;
    const [x0, y0] = this._toScreen(...pts[0]);
    const [x1, y1] = this._toScreen(...pts[3]);
    const ang = Math.atan2(y1 - y0, x1 - x0);
    ctx.save();
    ctx.translate(x0, y0);
    ctx.rotate(ang);
    const sq = Math.max(2.5, w / 4);
    const rows = Math.ceil((w + 4) / sq);
    for (let r = 0; r < rows; r++) {
      for (let col = 0; col < 2; col++) {
        ctx.fillStyle = (r + col) % 2 ? "#0b0c10" : "#f4f4f4";
        ctx.fillRect(-sq + col * sq, -((rows * sq) / 2) + r * sq, sq, sq);
      }
    }
    // Direction chevron just past the line.
    ctx.translate(w * 1.6 + 8, 0);
    ctx.strokeStyle = "rgba(255,255,255,0.55)";
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(-4, -5);
    ctx.lineTo(2, 0);
    ctx.lineTo(-4, 5);
    ctx.stroke();
    ctx.restore();
  }

  _growCloud(mapPos, now) {
    if (!mapPos.size || now - this.lastCloudAt < 250 || this.cloud.length > 40000) return;
    this.lastCloudAt = now;
    const first = !this.bounds;
    for (const p of mapPos.values()) this.cloud.push(p);
    if (first || this.cloud.length % 400 < mapPos.size) {
      this._setBounds(this.cloud);
      if (!this.userAdjusted) this.resetView(first);
    }
  }

  _drawCloud() {
    const ctx = this.ctx;
    ctx.fillStyle = "rgba(160, 175, 210, 0.18)";
    for (const [x, y] of this.cloud) {
      const [sx, sy] = this._toScreen(x, y);
      ctx.fillRect(sx - 1.5, sy - 1.5, 3, 3);
    }
  }

  _updateTails(mapPos, now) {
    for (const [n, [x, y]] of mapPos) {
      let tail = this.tails.get(n);
      if (!tail) this.tails.set(n, (tail = []));
      const last = tail[tail.length - 1];
      if (last && Math.hypot(last.x - x, last.y - y) > 3000) tail.length = 0; // teleport (seek)
      if (!last || now - last.at >= TAIL_SAMPLE_MS) tail.push({ x, y, at: now });
      while (tail.length && now - tail[0].at > TAIL_MS) tail.shift();
    }
    for (const n of this.tails.keys()) if (!mapPos.has(n)) this.tails.delete(n);
  }

  _drawCars(mapPos, now) {
    const ctx = this.ctx;
    const order = this.order ?? new Map();
    const rank = (n) => order.get(n)?.position ?? 99;
    const cars = [...mapPos.keys()].sort((a, b) => rank(b) - rank(a));
    if (this.selected != null && mapPos.has(this.selected)) {
      cars.splice(cars.indexOf(this.selected), 1);
      cars.push(this.selected);
    }

    const r = clamp(5.2 * Math.pow(this.cam.zoom, 0.35), 5, 11);
    this.screenCars = [];

    // Motion trails.
    ctx.lineCap = "round";
    for (const n of cars) {
      const tail = this.tails.get(n);
      if (!tail || tail.length < 2) continue;
      const colour = this.drivers.get(n)?.team_colour ?? "#9aa0a6";
      for (let i = 1; i < tail.length; i++) {
        const a = this._toScreen(tail[i - 1].x, tail[i - 1].y);
        const b = this._toScreen(tail[i].x, tail[i].y);
        ctx.strokeStyle = hexA(colour, (i / tail.length) * 0.45);
        ctx.lineWidth = r * 1.1 * (i / tail.length);
        ctx.beginPath();
        ctx.moveTo(a[0], a[1]);
        ctx.lineTo(b[0], b[1]);
        ctx.stroke();
      }
    }

    // Car dots.
    for (const n of cars) {
      const [mx, my] = mapPos.get(n);
      const [sx, sy] = this._toScreen(mx, my);
      const driver = this.drivers.get(n);
      const row = order.get(n);
      const colour = driver?.team_colour ?? "#9aa0a6";
      const isSel = n === this.selected;
      const dimmed = row?.in_pit || row?.retired;
      this.screenCars.push({ n, x: sx, y: sy });

      ctx.globalAlpha = dimmed ? 0.5 : 1;
      if (isSel) {
        const pulse = (Math.sin(now / 260) + 1) / 2;
        ctx.fillStyle = hexA(colour, 0.18 + pulse * 0.12);
        ctx.beginPath();
        ctx.arc(sx, sy, r + 9 + pulse * 4, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.arc(sx, sy, r + 4, 0, Math.PI * 2);
        ctx.stroke();
      }
      ctx.fillStyle = colour;
      ctx.strokeStyle = "rgba(6, 7, 11, 0.9)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(sx, sy, isSel ? r + 1.5 : r, 0, Math.PI * 2);
      ctx.fill();
      ctx.stroke();
      // Specular highlight for a bit of depth.
      ctx.fillStyle = "rgba(255,255,255,0.35)";
      ctx.beginPath();
      ctx.arc(sx - r * 0.3, sy - r * 0.3, r * 0.35, 0, Math.PI * 2);
      ctx.fill();
      ctx.globalAlpha = 1;
    }

    this._drawLabels(cars, mapPos, order, r);
  }

  /**
   * Driver labels with simple collision avoidance: the selected / hovered car
   * and the leaders get placed first; each label tries a few spots around its
   * car and is skipped if none is free (so a bunched-up grid stays readable).
   */
  _drawLabels(cars, mapPos, order, r) {
    const ctx = this.ctx;
    const priority = (n) =>
      n === this.selected ? -2 : n === this.hovered ? -1 : order.get(n)?.position ?? 99;
    const queue = cars
      .filter((n) => this.labelMode === "all" || n === this.selected || n === this.hovered)
      .filter((n) => this.labelMode !== "none" || n === this.hovered)
      .sort((a, b) => priority(a) - priority(b));

    const placed = [];
    const overlaps = (box) =>
      placed.some((p) => box.x < p.x + p.w && box.x + box.w > p.x && box.y < p.y + p.h && box.y + box.h > p.y);

    ctx.textBaseline = "middle";
    ctx.textAlign = "left";
    for (const n of queue) {
      const isSel = n === this.selected;
      const forced = isSel || n === this.hovered;
      const [sx, sy] = this._toScreen(...mapPos.get(n));
      const driver = this.drivers.get(n);
      const row = order.get(n);
      const code = driver?.code ?? String(n);
      const text = isSel && row?.position ? `P${row.position}  ${code}` : code;
      ctx.font = `700 ${isSel ? 12.5 : 11}px "Titillium Web", sans-serif`;
      const w = ctx.measureText(text).width + 13;
      const h = isSel ? 20 : 17;
      const gap = r + 6;
      const candidates = [
        { x: sx + gap, y: sy - h / 2 },
        { x: sx - gap - w, y: sy - h / 2 },
        { x: sx + gap - 4, y: sy - h - gap + 4 },
        { x: sx + gap - 4, y: sy + gap - 4 },
        { x: sx - gap - w + 4, y: sy - h - gap + 4 },
        { x: sx - gap - w + 4, y: sy + gap - 4 },
      ].map((c) => ({ ...c, w, h }));
      const box = candidates.find((c) => !overlaps(c)) ?? (forced ? candidates[0] : null);
      if (!box) continue;
      placed.push(box);

      ctx.globalAlpha = row?.in_pit || row?.retired ? 0.55 : 1;
      ctx.fillStyle = isSel ? "rgba(255,255,255,0.95)" : "rgba(9, 11, 16, 0.84)";
      roundRect(ctx, box.x, box.y, w, h, 4);
      ctx.fill();
      ctx.fillStyle = driver?.team_colour ?? "#9aa0a6";
      ctx.fillRect(box.x, box.y + 3, 2.5, h - 6);
      ctx.fillStyle = isSel ? "#0a0b0f" : "#f2f3f7";
      ctx.fillText(text, box.x + 7, box.y + h / 2 + 0.5);
      ctx.globalAlpha = 1;
    }
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** "#RRGGBB" + alpha -> "rgba(...)". */
export function hexA(hex, alpha) {
  const h = hex.replace("#", "");
  const v = parseInt(h.length === 3 ? h.replace(/./g, "$&$&") : h, 16);
  return `rgba(${(v >> 16) & 255}, ${(v >> 8) & 255}, ${v & 255}, ${alpha})`;
}
