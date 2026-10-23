"""Time-ordered log of timing events with a playback cursor."""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Iterable

from .models import TimingEvent


class EventLog:
    """Holds every timing event of a session, sorted by time.

    The engine repeatedly calls :meth:`drain_until` with the playback time and
    applies whatever it gets back. Events that arrive late (with a timestamp
    the cursor has already passed — common with live feeds) are handed out on
    the next drain instead of being lost.
    """

    def __init__(self) -> None:
        self._times: list[float] = []
        self._events: list[TimingEvent] = []
        self._cursor = 0
        self._late: list[TimingEvent] = []

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(self._events)

    def add(self, event: TimingEvent) -> None:
        idx = bisect_right(self._times, event.t)
        self._times.insert(idx, event.t)
        self._events.insert(idx, event)
        if idx < self._cursor:
            self._cursor += 1
            self._late.append(event)

    def extend(self, events: Iterable[TimingEvent]) -> None:
        events = list(events)
        if not self._events and self._cursor == 0:
            events.sort(key=lambda e: e.t)  # stable: keeps source order for ties
            self._events = events
            self._times = [e.t for e in events]
        else:
            for event in events:
                self.add(event)

    def drain_until(self, t: float) -> list[TimingEvent]:
        """Return events with timestamp <= ``t`` that haven't been handed out yet."""
        end = bisect_right(self._times, t)
        out = self._late
        self._late = []
        if end > self._cursor:
            out.extend(self._events[self._cursor : end])
            self._cursor = end
        return out

    def rewind(self) -> None:
        """Start handing out events from the beginning again (used when seeking back)."""
        self._cursor = 0
        self._late.clear()
