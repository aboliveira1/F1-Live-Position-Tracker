"""Playback engine: advances the clock and publishes car positions and race state."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable

from .clock import PlaybackClock
from .race_state import RaceState
from .session import SessionData

Publish = Callable[[dict], Awaitable[None]]


class PlaybackEngine:
    """Drives one session.

    Every tick it reads the clock, applies the timing events that have become
    due, and publishes a ``frame`` (interpolated car positions). A ``state``
    message (leaderboard, flags, ...) goes out a few times per second.
    """

    STATE_INTERVAL_S = 0.25

    def __init__(
        self,
        session: SessionData,
        clock: PlaybackClock,
        publish: Publish,
        *,
        tick_hz: float = 20.0,
    ) -> None:
        self.session = session
        self.clock = clock
        self._publish = publish
        self._period = 1.0 / tick_hz
        self.state = RaceState(
            session.drivers, is_race=session.info.is_race, total_laps=session.total_laps
        )
        self._applied_t = -math.inf
        self._last_state_at = -math.inf

    def advance(self) -> float:
        """Bring the race state up to the clock's current time; returns that time."""
        t = self.clock.now()
        if t < self._applied_t:  # jumped backwards: rebuild state from the start
            self.state.reset()
            self.session.events.rewind()
        for event in self.session.events.drain_until(t):
            self.state.apply(event)
        self._applied_t = t
        return t

    def frame(self, t: float) -> dict:
        cars = [[n, round(x), round(y)] for n, x, y in self.session.store.positions_at(t)]
        return {"type": "frame", "t": round(t, 3), "cars": cars, "clock": self.clock.to_dict()}

    def snapshot_messages(self) -> list[dict]:
        """Current frame and state, e.g. for a client that just connected."""
        t = self.advance()
        return [self.frame(t), self.state.snapshot(t)]

    def request_state_push(self) -> None:
        """Send race state on the next tick (after a seek, so the UI updates at once)."""
        self._last_state_at = -math.inf

    def refresh_session_meta(self) -> None:
        self.state.total_laps = self.session.total_laps

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        next_tick = loop.time()
        while True:
            t = self.advance()
            await self._publish(self.frame(t))
            now = loop.time()
            if now - self._last_state_at >= self.STATE_INTERVAL_S:
                self._last_state_at = now
                await self._publish(self.state.snapshot(t))

            next_tick += self._period
            delay = next_tick - loop.time()
            if delay < 0:  # fell behind (e.g. a slow client); don't try to catch up
                next_tick = loop.time()
                delay = 0
            await asyncio.sleep(delay)
