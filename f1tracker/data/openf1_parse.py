"""Convert OpenF1 records into domain objects.

REST rows and live MQTT messages share the same schema, so replay and live
go through exactly the same parsing code.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime

import numpy as np

from ..core.models import (
    Driver,
    DriverRetired,
    Gap,
    IntervalUpdate,
    LapCompleted,
    LapStarted,
    PitStop,
    PositionUpdate,
    RaceControlMessage,
    SessionInfo,
    StintStarted,
    TimingEvent,
    WeatherUpdate,
)

log = logging.getLogger(__name__)

TIMING_ENDPOINTS = ("laps", "stints", "position", "intervals", "pit", "race_control", "weather")


# --------------------------------------------------------------------------- #
# Primitive helpers
# --------------------------------------------------------------------------- #
def parse_time(value: str | None) -> float | None:
    """ISO-8601 timestamp -> UTC epoch seconds."""
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()


def parse_gmt_offset(value: str | None) -> int:
    """'08:00:00' / '-05:00:00' -> seconds."""
    if not value:
        return 0
    sign = -1 if value.startswith("-") else 1
    hours, minutes, *rest = value.lstrip("+-").split(":")
    seconds = int(rest[0]) if rest else 0
    return sign * (int(hours) * 3600 + int(minutes) * 60 + seconds)


def _gap(value) -> Gap:
    if value is None or isinstance(value, (int, float)):
        return None if value is None else float(value)
    try:
        return float(value)
    except ValueError:
        return str(value)  # e.g. "+1 LAP"


def _colour(value: str | None) -> str:
    if not value:
        return "#9AA0A6"
    return value if value.startswith("#") else f"#{value}"


def _int_or_none(value) -> int | None:
    return None if value is None else int(value)


def _float_or_none(value) -> float | None:
    return None if value is None else float(value)


# --------------------------------------------------------------------------- #
# Session metadata
# --------------------------------------------------------------------------- #
def parse_session(row: dict, meeting: dict | None = None) -> SessionInfo:
    meeting = meeting or {}
    return SessionInfo(
        session_key=int(row["session_key"]),
        meeting_key=int(row["meeting_key"]),
        year=int(row["year"]),
        session_name=row.get("session_name") or "Session",
        session_type=row.get("session_type") or "",
        meeting_name=meeting.get("meeting_name") or f"{row.get('country_name', '')} Grand Prix",
        circuit_key=int(row.get("circuit_key") or 0),
        circuit_name=row.get("circuit_short_name") or "",
        country_name=row.get("country_name") or "",
        country_code=row.get("country_code") or "",
        country_flag_url=meeting.get("country_flag"),
        location=row.get("location") or "",
        date_start=parse_time(row["date_start"]),
        date_end=parse_time(row["date_end"]),
        gmt_offset_s=parse_gmt_offset(row.get("gmt_offset")),
    )


def parse_driver(row: dict) -> Driver:
    number = int(row["driver_number"])
    last = (row.get("last_name") or "").title()
    first = row.get("first_name") or ""
    return Driver(
        number=number,
        code=row.get("name_acronym") or str(number),
        first_name=first,
        last_name=last,
        full_name=f"{first} {last}".strip() or row.get("full_name") or str(number),
        team_name=row.get("team_name") or "",
        team_colour=_colour(row.get("team_colour")),
        headshot_url=row.get("headshot_url"),
    )


def parse_drivers(rows: Iterable[dict]) -> dict[int, Driver]:
    drivers: dict[int, Driver] = {}
    for row in rows:
        if row.get("driver_number") is not None:
            driver = parse_driver(row)
            drivers[driver.number] = driver
    return drivers


# --------------------------------------------------------------------------- #
# Car positions
# --------------------------------------------------------------------------- #
def parse_location(row: dict) -> tuple[int, float, float, float] | None:
    """One location row -> (driver, t, x, y); x/y are NaN while the car is off track."""
    t = parse_time(row.get("date"))
    if t is None or row.get("driver_number") is None:
        return None
    x, y = float(row.get("x") or 0), float(row.get("y") or 0)
    if x == 0 and y == 0:  # the feed reports the origin when it has no position
        x = y = float("nan")
    return int(row["driver_number"]), t, x, y


def parse_locations(rows: Iterable[dict]) -> dict[int, tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """Bulk version of :func:`parse_location`, grouped per driver."""
    grouped: dict[int, list[tuple[float, float, float]]] = defaultdict(list)
    for row in rows:
        parsed = parse_location(row)
        if parsed is not None:
            driver, t, x, y = parsed
            grouped[driver].append((t, x, y))
    out = {}
    for driver, samples in grouped.items():
        arr = np.asarray(samples, dtype=np.float64)
        out[driver] = (arr[:, 0], arr[:, 1], arr[:, 2])
    return out


# --------------------------------------------------------------------------- #
# Timing events
# --------------------------------------------------------------------------- #
class TimingParser:
    """Turns timing rows from any endpoint into :mod:`~f1tracker.core.models` events.

    It remembers when each lap started, because stints are only reported by
    lap number. Feed ``laps`` rows before ``stints`` rows when bulk loading.

    ``fallback_time`` supplies a timestamp for rows that can't be placed in
    time from their own content (used by the live feed: "now").
    """

    def __init__(self, fallback_time: Callable[[], float] | None = None) -> None:
        self._lap_starts: dict[tuple[int, int], float] = {}
        self._fallback_time = fallback_time

    def parse(self, endpoint: str, row: dict) -> list[TimingEvent]:
        handler = getattr(self, f"_parse_{endpoint}", None)
        if handler is None:
            return []
        try:
            return handler(row)
        except (KeyError, TypeError, ValueError) as exc:
            log.debug("Skipping malformed %s row %r (%s)", endpoint, row, exc)
            return []

    def parse_many(self, endpoint: str, rows: Iterable[dict]) -> list[TimingEvent]:
        return [event for row in rows for event in self.parse(endpoint, row)]

    def lap_start(self, driver: int, lap: int) -> float | None:
        return self._lap_starts.get((driver, lap))

    # ------------------------------------------------------------------ #
    def _parse_position(self, row: dict) -> list[TimingEvent]:
        return [PositionUpdate(parse_time(row["date"]), int(row["driver_number"]), int(row["position"]))]

    def _parse_intervals(self, row: dict) -> list[TimingEvent]:
        return [
            IntervalUpdate(
                parse_time(row["date"]),
                int(row["driver_number"]),
                _gap(row.get("gap_to_leader")),
                _gap(row.get("interval")),
            )
        ]

    def _parse_laps(self, row: dict) -> list[TimingEvent]:
        driver, lap = int(row["driver_number"]), int(row["lap_number"])
        start = parse_time(row.get("date_start"))
        if start is None and self._fallback_time is not None:
            start = self._fallback_time()
        if start is None:
            return []
        self._lap_starts[(driver, lap)] = start
        events: list[TimingEvent] = [LapStarted(start, driver, lap)]
        if (duration := row.get("lap_duration")) is not None:
            events.append(
                LapCompleted(
                    start + float(duration),
                    driver,
                    lap,
                    float(duration),
                    (
                        _float_or_none(row.get("duration_sector_1")),
                        _float_or_none(row.get("duration_sector_2")),
                        _float_or_none(row.get("duration_sector_3")),
                    ),
                    bool(row.get("is_pit_out_lap")),
                )
            )
        return events

    def _parse_stints(self, row: dict) -> list[TimingEvent]:
        driver = int(row["driver_number"])
        lap_start = int(row.get("lap_start") or 1)
        t = self._stint_time(driver, lap_start)
        if t is None:
            return []
        compound = (row.get("compound") or "").upper() or None
        return [
            StintStarted(
                t,
                driver,
                int(row["stint_number"]),
                None if compound == "UNKNOWN" else compound,
                lap_start,
                int(row.get("tyre_age_at_start") or 0),
            )
        ]

    def _stint_time(self, driver: int, lap_start: int) -> float | None:
        if lap_start <= 1:
            return 0.0  # first stint: in effect from the very beginning
        if (t := self._lap_starts.get((driver, lap_start))) is not None:
            return t
        if (t := self._lap_starts.get((driver, lap_start + 1))) is not None:
            return t - 1.0
        return self._fallback_time() if self._fallback_time else None

    def _parse_pit(self, row: dict) -> list[TimingEvent]:
        lane = row.get("lane_duration", row.get("pit_duration"))
        return [
            PitStop(
                parse_time(row["date"]),
                int(row["driver_number"]),
                _int_or_none(row.get("lap_number")),
                _float_or_none(lane),
                _float_or_none(row.get("stop_duration")),
            )
        ]

    def _parse_race_control(self, row: dict) -> list[TimingEvent]:
        return [
            RaceControlMessage(
                t=parse_time(row["date"]),
                category=row.get("category") or "Other",
                flag=row.get("flag"),
                scope=row.get("scope"),
                sector=_int_or_none(row.get("sector")),
                driver=_int_or_none(row.get("driver_number")),
                message=row.get("message") or "",
                lap=_int_or_none(row.get("lap_number")),
            )
        ]

    def _parse_weather(self, row: dict) -> list[TimingEvent]:
        return [
            WeatherUpdate(
                t=parse_time(row["date"]),
                air_temp=_float_or_none(row.get("air_temperature")),
                track_temp=_float_or_none(row.get("track_temperature")),
                humidity=_float_or_none(row.get("humidity")),
                pressure=_float_or_none(row.get("pressure")),
                rainfall=bool(row.get("rainfall")),
                wind_speed=_float_or_none(row.get("wind_speed")),
                wind_direction=_float_or_none(row.get("wind_direction")),
            )
        ]


# --------------------------------------------------------------------------- #
# Derived session facts
# --------------------------------------------------------------------------- #
def total_laps(results: Iterable[dict]) -> int | None:
    laps = [int(r["number_of_laps"]) for r in results if r.get("number_of_laps")]
    return max(laps) if laps else None


def retirements(results: Iterable[dict], events: Iterable[TimingEvent]) -> list[DriverRetired]:
    """A DNF event for each non-finisher, placed shortly after their last lap ended."""
    out_drivers = {int(r["driver_number"]) for r in results if r.get("dnf")}
    if not out_drivers:
        return []
    last_seen: dict[int, float] = {}
    for e in events:
        if isinstance(e, (LapStarted, LapCompleted)) and e.driver in out_drivers:
            last_seen[e.driver] = max(last_seen.get(e.driver, e.t), e.t)
    return [DriverRetired(t + 120.0, n) for n, t in last_seen.items()]
