"""Race state at the current playback time: leaderboard, flags, messages, weather.

Every field is updated with *last-writer-wins on the event timestamp*, so the
result doesn't depend on the order events arrive in. That matters for live
feeds, where messages from different topics routinely arrive out of order.
"""

from __future__ import annotations

import math
from bisect import insort
from dataclasses import dataclass, field

from .models import (
    DriverRetired,
    Gap,
    IntervalUpdate,
    LapCompleted,
    LapStarted,
    PitStop,
    PositionUpdate,
    RaceControlMessage,
    StintStarted,
    TimingEvent,
    WeatherUpdate,
)

# Track-wide statuses, in the vocabulary the UI understands.
GREEN, YELLOW, SC, SC_ENDING, VSC, VSC_ENDING, RED, CHEQUERED = (
    "GREEN", "YELLOW", "SC", "SC_ENDING", "VSC", "VSC_ENDING", "RED", "CHEQUERED",
)
UNKNOWN = "NONE"

_TRACK_FLAGS = {
    "GREEN": GREEN,
    "CLEAR": GREEN,
    "YELLOW": YELLOW,
    "DOUBLE YELLOW": YELLOW,
    "RED": RED,
    "CHEQUERED": CHEQUERED,
}

# Pit-lane time to assume when the feed hasn't reported one yet.
_DEFAULT_PIT_LANE_S = 25.0
# Messages shown in the race-control feed.
_FEED_LENGTH = 40


def classify_track_status(msg: RaceControlMessage) -> str | None:
    """Track-wide status implied by a race-control message, or None."""
    text = msg.message.upper()
    if msg.category == "SafetyCar":
        virtual = "VIRTUAL" in text or "VSC" in text
        if "ENDING" in text or "IN THIS LAP" in text:
            return VSC_ENDING if virtual else SC_ENDING
        if "DEPLOYED" in text:
            return VSC if virtual else SC
        return None
    if msg.category == "Flag" and msg.scope == "Track":
        return _TRACK_FLAGS.get((msg.flag or "").upper())
    return None


def is_feed_worthy(msg: RaceControlMessage) -> bool:
    """Blue flags are constant noise during a race; leave them out of the feed."""
    return not (msg.category == "Flag" and (msg.flag or "").upper() == "BLUE")


def status_periods(
    messages: list[RaceControlMessage], end: float
) -> list[tuple[float, float, str]]:
    """Spans of SC / VSC / red flag / track yellow, for colouring the timeline."""
    periods: list[tuple[float, float, str]] = []
    current: str | None = None
    since = 0.0
    for msg in sorted(messages, key=lambda m: m.t):
        status = classify_track_status(msg)
        if status is None:
            continue
        kind = {SC_ENDING: SC, VSC_ENDING: VSC}.get(status, status)
        if kind == current:
            continue
        if current in (SC, VSC, RED, YELLOW):
            periods.append((since, msg.t, current))
        current, since = kind, msg.t
    if current in (SC, VSC, RED, YELLOW) and end > since:
        periods.append((since, end, current))
    return periods


@dataclass
class DriverTiming:
    number: int
    position: int | None = None
    gap_to_leader: Gap = None
    interval: Gap = None
    lap: int = 0
    last_lap: float | None = None
    best_lap: float | None = None
    last_sectors: tuple = (None, None, None)
    stint: int = 0
    compound: str | None = None
    stint_lap_start: int = 1
    tyre_age_at_start: int = 0
    pits: dict = field(default_factory=dict)  # lap (or time) -> (entry_t, lane_duration)
    retired: bool = False

    def tyre_age(self) -> int | None:
        if self.stint == 0:
            return None
        return self.tyre_age_at_start + max(0, self.lap - self.stint_lap_start)

    def in_pit(self, t: float) -> bool:
        return any(
            entry <= t < entry + (lane or _DEFAULT_PIT_LANE_S)
            for entry, lane in self.pits.values()
        )


class RaceState:
    def __init__(self, drivers, *, is_race: bool, total_laps: int | None = None) -> None:
        self._initial_drivers = list(drivers)
        self.is_race = is_race
        self.total_laps = total_laps
        self.reset()

    # ------------------------------------------------------------------ #
    def reset(self) -> None:
        self.drivers: dict[int, DriverTiming] = {n: DriverTiming(n) for n in self._initial_drivers}
        self._stamps: dict[tuple[int, str], float] = {}
        self.track_status = UNKNOWN
        self._track_status_t = -math.inf
        self._sector_flags: dict[int, tuple[float, bool]] = {}  # sector -> (t, yellow?)
        self._track_clear_t = -math.inf
        self.messages: list[RaceControlMessage] = []
        self._message_keys: set[tuple[float, str]] = set()
        self.weather: WeatherUpdate | None = None

    def _driver(self, number: int) -> DriverTiming:
        timing = self.drivers.get(number)
        if timing is None:
            timing = self.drivers[number] = DriverTiming(number)
        return timing

    def _newer(self, driver: int, field_name: str, t: float) -> bool:
        """Last-writer-wins check; records ``t`` when it wins."""
        key = (driver, field_name)
        if t < self._stamps.get(key, -math.inf):
            return False
        self._stamps[key] = t
        return True

    # ------------------------------------------------------------------ #
    def apply(self, event: TimingEvent) -> None:
        match event:
            case PositionUpdate(t=t, driver=n, position=pos):
                if self._newer(n, "position", t):
                    self._driver(n).position = pos
            case IntervalUpdate(t=t, driver=n):
                if self._newer(n, "interval", t):
                    d = self._driver(n)
                    d.gap_to_leader, d.interval = event.gap_to_leader, event.interval
            case LapStarted(driver=n, lap=lap):
                d = self._driver(n)
                d.lap = max(d.lap, lap)
            case LapCompleted(t=t, driver=n):
                d = self._driver(n)
                if self._newer(n, "last_lap", t):
                    d.last_lap, d.last_sectors = event.duration, event.sectors
                if d.best_lap is None or event.duration < d.best_lap:
                    d.best_lap = event.duration
            case StintStarted(driver=n):
                d = self._driver(n)
                if event.stint >= d.stint:
                    d.stint = event.stint
                    d.compound = event.compound or d.compound
                    d.stint_lap_start = event.lap_start
                    d.tyre_age_at_start = event.tyre_age_at_start
            case PitStop(t=t, driver=n):
                key = event.lap if event.lap is not None else round(t)
                self._driver(n).pits[key] = (t, event.lane_duration)
            case RaceControlMessage():
                self._apply_race_control(event)
            case WeatherUpdate(t=t):
                if self.weather is None or t >= self.weather.t:
                    self.weather = event
            case DriverRetired(driver=n):
                self._driver(n).retired = True

    def _apply_race_control(self, msg: RaceControlMessage) -> None:
        key = (msg.t, msg.message)
        if key in self._message_keys:
            return
        self._message_keys.add(key)
        insort(self.messages, msg, key=lambda m: m.t)

        status = classify_track_status(msg)
        if status is not None and msg.t >= self._track_status_t:
            self.track_status, self._track_status_t = status, msg.t
            if status == GREEN:
                self._track_clear_t = max(self._track_clear_t, msg.t)

        if msg.category == "Flag" and msg.scope == "Sector" and msg.sector is not None:
            flag = (msg.flag or "").upper()
            previous = self._sector_flags.get(msg.sector)
            if previous is None or msg.t >= previous[0]:
                self._sector_flags[msg.sector] = (msg.t, flag in ("YELLOW", "DOUBLE YELLOW"))

    # ------------------------------------------------------------------ #
    def current_lap(self) -> int:
        lap = max((d.lap for d in self.drivers.values()), default=0)
        return min(lap, self.total_laps) if self.total_laps else lap

    def yellow_sectors(self) -> list[int]:
        return sorted(
            sector
            for sector, (t, yellow) in self._sector_flags.items()
            if yellow and t > self._track_clear_t
        )

    def leaderboard(self, t: float) -> list[dict]:
        rows = sorted(
            self.drivers.values(),
            key=lambda d: (d.position is None, d.position or 0, d.number),
        )
        fastest = min((d.best_lap for d in rows if d.best_lap), default=None)
        return [
            {
                "driver": d.number,
                "position": d.position,
                "gap_to_leader": d.gap_to_leader,
                "interval": d.interval,
                "lap": d.lap,
                "last_lap": d.last_lap,
                "best_lap": d.best_lap,
                "best_gap": (d.best_lap - fastest) if d.best_lap and fastest else None,
                "sectors": d.last_sectors,
                "compound": d.compound,
                "tyre_age": d.tyre_age(),
                "pit_stops": len(d.pits),
                "in_pit": d.in_pit(t),
                "retired": d.retired,
            }
            for d in rows
        ]

    def snapshot(self, t: float) -> dict:
        feed = [m for m in self.messages if is_feed_worthy(m)][-_FEED_LENGTH:]
        weather = self.weather
        return {
            "type": "state",
            "t": t,
            "lap": self.current_lap(),
            "total_laps": self.total_laps,
            "track_status": self.track_status,
            "yellow_sectors": self.yellow_sectors(),
            "leaderboard": self.leaderboard(t),
            "race_control": [
                {
                    "t": m.t,
                    "category": m.category,
                    "flag": m.flag,
                    "message": m.message,
                    "lap": m.lap,
                    "driver": m.driver,
                }
                for m in reversed(feed)
            ],
            "weather": None
            if weather is None
            else {
                "air_temp": weather.air_temp,
                "track_temp": weather.track_temp,
                "humidity": weather.humidity,
                "rainfall": weather.rainfall,
                "wind_speed": weather.wind_speed,
                "wind_direction": weather.wind_direction,
            },
        }
