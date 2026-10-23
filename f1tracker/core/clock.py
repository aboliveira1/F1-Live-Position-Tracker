"""Playback clock: maps wall-clock time to session time."""

from __future__ import annotations

import time
from collections.abc import Callable

SPEEDS = (0.25, 0.5, 1, 2, 4, 8, 16, 32, 64)
# How quickly the live delay may change: seconds of delay per second of wall time.
DELAY_SLEW = 0.2


class PlaybackClock:
    """Session-time clock for both replays and live sessions.

    * Replay: plays, pauses, seeks and changes speed within ``[start, end]``.
    * Live: follows the *live edge* (``now - live_delay``). Pausing or seeking
      back turns it into a DVR bounded by the live edge; catching up to the
      edge, or calling :meth:`go_live`, resumes following it.
    """

    def __init__(
        self,
        start: float,
        end: float,
        *,
        live: bool = False,
        live_delay: float = 4.0,
        wallclock: Callable[[], float] = time.time,
    ) -> None:
        self.live = live
        self._delay = self._target_delay = live_delay
        self._delay_at = wallclock()
        self._wall = wallclock
        self._start = start
        self._end = end
        self._speed = 1.0
        self._playing = False
        self._following_live = live
        self._anchor_t = start
        self._anchor_wall = wallclock()
        # Bumped on every jump (seek / back to live), so viewers know not to
        # interpolate between positions from before and after it.
        self.epoch = 0

    # ------------------------------------------------------------------ #
    @property
    def start(self) -> float:
        return self._start

    @property
    def end(self) -> float:
        if not self.live:
            return self._end
        wall = self._wall()
        if self._delay != self._target_delay:
            # Ease towards the new delay so the picture slows down instead of jumping.
            step = DELAY_SLEW * max(wall - self._delay_at, 0.0)
            if self._delay < self._target_delay:
                self._delay = min(self._target_delay, self._delay + step)
            else:
                self._delay = max(self._target_delay, self._delay - step)
        self._delay_at = wall
        return wall - self._delay

    @property
    def live_delay(self) -> float:
        return self._delay

    def set_live_delay(self, seconds: float) -> None:
        """Change how far behind real time the live edge sits (applied gradually)."""
        self._target_delay = seconds

    @property
    def playing(self) -> bool:
        return self._following_live or self._playing

    @property
    def speed(self) -> float:
        return 1.0 if self._following_live else self._speed

    @property
    def following_live(self) -> bool:
        return self._following_live

    def set_bounds(self, start: float, end: float) -> None:
        t = self.now()
        self._start, self._end = start, end
        self._reanchor(min(max(t, start), self.end))

    # ------------------------------------------------------------------ #
    def now(self) -> float:
        if self._following_live:
            return self.end
        t = self._anchor_t
        if self._playing:
            t += (self._wall() - self._anchor_wall) * self._speed
        end = self.end
        if t >= end:
            if self._playing and self.live:
                self._following_live = True  # caught up with the live edge
                self._speed = 1.0
            elif self._playing:
                self._playing = False  # replay reached the end
                self._reanchor(end)
            return end
        return max(t, self._start)

    def _reanchor(self, t: float) -> None:
        self._anchor_t = t
        self._anchor_wall = self._wall()

    def play(self) -> None:
        if not self.playing:
            self._reanchor(self.now())
            self._playing = True

    def pause(self) -> None:
        t = self.now()
        self._following_live = False
        self._playing = False
        self._reanchor(t)

    def toggle(self) -> None:
        self.pause() if self.playing else self.play()

    def seek(self, t: float) -> None:
        was_playing = self.playing
        t = min(max(t, self._start), self.end)
        self._following_live = False
        self._playing = was_playing
        self._reanchor(t)
        self.epoch += 1
        if self.live and t >= self.end - 0.5:
            self.go_live()

    def set_speed(self, speed: float) -> None:
        speed = float(min(max(speed, SPEEDS[0]), SPEEDS[-1]))
        if self._following_live and speed >= 1.0:
            return  # can't run ahead of real time at the live edge
        t = self.now()
        if self._following_live:  # slow motion drops behind the edge
            self._following_live = False
            self._playing = True
        self._speed = speed
        self._reanchor(t)

    def go_live(self) -> None:
        if self.live:
            if not self._following_live:
                self.epoch += 1
            self._following_live = True
            self._speed = 1.0

    def to_dict(self) -> dict:
        return {
            "t": self.now(),
            "start": self._start,
            "end": self.end,
            "playing": self.playing,
            "speed": self.speed,
            "live": self.live,
            "following_live": self._following_live,
            "delay": round(self._delay, 2) if self.live else None,
            "epoch": self.epoch,
        }
