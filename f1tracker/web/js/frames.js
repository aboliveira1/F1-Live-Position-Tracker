// Buffers position frames from the server and interpolates between them.
//
// The server sends ~20 frames/s, each stamped with session time. We keep a
// smoothed estimate of the server clock and render slightly behind it, so
// there are always two frames to interpolate between.
//
// The render clock itself advances at playback speed every animation frame
// and is *steered* towards that target rather than set to it. Speed changes,
// network jitter and catch-ups therefore show as cars briefly moving faster
// or slower, never as jumps. Only real jumps (seeks) snap it.

const RENDER_DELAY_S = 0.12; // wall-clock seconds behind the newest frame
const CONVERGE_S = 0.35; // how quickly the render clock closes the gap to its target
const STALE_MS = 1000; // stop advancing if frames stop arriving
const MAX_FRAMES = 60;

export class FrameBuffer {
  constructor() {
    this.reset();
  }

  reset() {
    this.frames = [];
    this.base = null; // { t, wall, speed } — anchor of the smoothed server clock
    this.epoch = null;
    this.rt = null; // session time currently rendered
    this.snap = true;
    this.lastArrival = 0;
    this.lastRender = 0;
  }

  /** Session time of the last rendered frame (null before any data). */
  get time() {
    return this.rt;
  }

  push(frame) {
    const now = performance.now();
    const { clock } = frame;
    const speed = clock.playing ? clock.speed : 0;
    const last = this.frames[this.frames.length - 1];
    // The server bumps the epoch on seeks; never interpolate across one.
    const jumped =
      last != null &&
      (clock.epoch !== this.epoch || frame.t < last.t - 1e-3 || frame.t - last.t > Math.max(30, speed * 5));
    this.epoch = clock.epoch;
    if (jumped) {
      this.frames = [];
      this.snap = true;
    }

    const cars = new Map();
    for (const [n, x, y] of frame.cars) cars.set(n, [x, y]);
    this.frames.push({ t: frame.t, cars });
    if (this.frames.length > MAX_FRAMES) this.frames.shift();
    this.lastArrival = now;

    if (!this.base || jumped || this.base.speed !== speed || speed === 0) {
      this.base = { t: frame.t, wall: now, speed };
    } else {
      const predicted = this.base.t + ((now - this.base.wall) / 1000) * speed;
      const error = frame.t - predicted;
      if (Math.abs(error) > Math.max(0.5, speed * 0.5)) this.base = { t: frame.t, wall: now, speed };
      else this.base.t += error * 0.1; // gently correct drift
    }
  }

  /** Session time to draw at this instant (call once per animation frame). */
  renderTime(now = performance.now()) {
    if (!this.base) return null;
    const latest = this.frames.length ? this.frames[this.frames.length - 1].t : this.base.t;
    const { t, wall, speed } = this.base;
    const target = speed === 0 ? latest : t + ((now - wall) / 1000) * speed - RENDER_DELAY_S * speed;
    const dt = this.lastRender ? Math.min(Math.max((now - this.lastRender) / 1000, 0), 0.25) : 0;
    this.lastRender = now;

    if (this.rt == null || this.snap) {
      this.rt = target;
      this.snap = false;
    } else {
      const nominal = this.rt + dt * speed;
      const error = target - nominal;
      const next =
        Math.abs(error) > Math.max(10, speed * 2)
          ? target // far off (e.g. after the tab was hidden): just jump
          : nominal + error * Math.min(1, dt / CONVERGE_S);
      this.rt = Math.max(next, this.rt); // never run backwards between seeks
    }
    if (now - this.lastArrival > STALE_MS) this.rt = Math.min(this.rt, latest);
    return this.rt;
  }

  /** Map of driver -> [x, y] at session time ``t``. */
  positionsAt(t) {
    const frames = this.frames;
    if (!frames.length) return new Map();
    if (t <= frames[0].t) return frames[0].cars;
    const last = frames[frames.length - 1];
    if (t >= last.t) return last.cars;

    let i = frames.length - 2;
    while (i > 0 && frames[i].t > t) i--;
    const a = frames[i];
    const b = frames[i + 1];
    const f = (t - a.t) / (b.t - a.t || 1);
    const out = new Map();
    for (const [n, [bx, by]] of b.cars) {
      const pa = a.cars.get(n);
      out.set(n, pa ? [pa[0] + (bx - pa[0]) * f, pa[1] + (by - pa[1]) * f] : [bx, by]);
    }
    return out;
  }
}
