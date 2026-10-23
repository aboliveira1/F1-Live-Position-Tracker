"""Everything known about one session, independent of where it came from."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from .event_log import EventLog
from .models import Driver, LapStarted, RaceControlMessage, SessionInfo, TrackLayout
from .race_state import status_periods
from .trajectories import TrajectoryStore


@dataclass
class SessionData:
    info: SessionInfo
    drivers: dict[int, Driver]
    track: TrackLayout | None
    timeline_start: float
    timeline_end: float
    default_start: float = 0.0
    live: bool = False
    total_laps: int | None = None
    store: TrajectoryStore = field(default_factory=TrajectoryStore)
    events: EventLog = field(default_factory=EventLog)
    # Time ranges whose car positions have been downloaded.
    buffered: list[tuple[float, float]] = field(default_factory=list)

    # Derived from the event log by refresh_derived():
    race_start: float | None = None
    # (lap number, time the leader started it) — drawn as ticks on the timeline.
    lap_markers: list[tuple[int, float]] = field(default_factory=list)
    # (start, end, "SC" | "VSC" | "RED" | "YELLOW") — coloured bands on the timeline.
    status_periods: list[tuple[float, float, str]] = field(default_factory=list)

    def refresh_derived(self) -> None:
        """Recompute timeline annotations from the event log (live sessions keep growing)."""
        first_start: dict[int, float] = {}
        messages: list[RaceControlMessage] = []
        for event in self.events:
            if isinstance(event, LapStarted):
                if event.lap not in first_start or event.t < first_start[event.lap]:
                    first_start[event.lap] = event.t
            elif isinstance(event, RaceControlMessage):
                messages.append(event)
        self.lap_markers = sorted(first_start.items())
        if self.info.is_race and self.lap_markers:
            self.race_start = self.lap_markers[0][1]
        end = time.time() if self.live else self.timeline_end
        self.status_periods = status_periods(messages, end)

    def mark_buffered(self, start: float, end: float) -> None:
        """Record that positions for [start, end] are loaded (merges overlapping ranges)."""
        ranges = sorted([*self.buffered, (start, end)])
        merged: list[tuple[float, float]] = []
        for lo, hi in ranges:
            if merged and lo <= merged[-1][1] + 1.0:
                merged[-1] = (merged[-1][0], max(merged[-1][1], hi))
            else:
                merged.append((lo, hi))
        self.buffered = merged

    def to_payload(self) -> dict:
        return {
            "type": "session",
            "live": self.live,
            "session": self.info.to_dict(),
            "drivers": [d.to_dict() for d in sorted(self.drivers.values(), key=lambda d: d.number)],
            "track": self.track.to_dict() if self.track else None,
            "timeline": {
                "start": self.timeline_start,
                "end": self.timeline_end,
                "race_start": self.race_start,
                "total_laps": self.total_laps,
                "lap_markers": self.lap_markers,
                "status_periods": self.status_periods,
            },
        }

    def buffer_payload(self, complete: bool) -> dict:
        return {"type": "buffer", "ranges": self.buffered, "complete": complete}
