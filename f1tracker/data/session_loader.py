"""Builds a :class:`~f1tracker.core.session.SessionData` from OpenF1."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable

from ..config import FIRST_SEASON
from ..core.models import Driver, LapCompleted, RaceControlMessage, SessionInfo
from ..core.session import SessionData
from .circuits import CircuitRepository, outline_from_samples
from .openf1_client import OpenF1Client, OpenF1Error, time_filter
from .openf1_parse import (
    TIMING_ENDPOINTS,
    TimingParser,
    parse_drivers,
    parse_locations,
    parse_session,
    retirements,
    total_laps,
)

log = logging.getLogger(__name__)

Progress = Callable[[str, float], Awaitable[None]]

# Car positions are downloaded in windows of this length (≈10 MB each for a full grid).
LOCATION_CHUNK_S = 15 * 60
LOCATION_CONCURRENCY = 3
# Data is treated as final (and cached on disk) this long after a session ends.
FINAL_AFTER_S = 2 * 3600
# Seconds before lights-out at which race replays begin.
RACE_LEAD_IN_S = 60

_ENDPOINT_LABELS = {
    "laps": "lap times",
    "stints": "tyre stints",
    "position": "positions",
    "intervals": "intervals",
    "pit": "pit stops",
    "race_control": "race control",
    "weather": "weather",
}


async def _no_progress(stage: str, fraction: float) -> None:
    pass


class SessionLoader:
    """Loads sessions from OpenF1.

    With ``persist`` off (the default) session data is held only in memory;
    with it on, finished sessions are also saved under the cache folder.
    """

    def __init__(self, client: OpenF1Client, circuits: CircuitRepository, *, persist: bool = False) -> None:
        self._client = client
        self._circuits = circuits
        self._persist = persist

    def _saveable(self, session: SessionInfo, live: bool) -> bool:
        """Only finished sessions are saved: their data won't change any more."""
        return self._persist and not live and time.time() > session.date_end + FINAL_AFTER_S

    async def load(
        self, session_key: int, *, live: bool = False, progress: Progress = _no_progress
    ) -> tuple[SessionData, TimingParser]:
        """Load metadata and timing for a session (car positions come separately)."""
        await progress("Fetching session details", 0.02)
        row = await self._session_row(session_key)
        meetings = await self._client.get(
            "meetings", {"meeting_key": row["meeting_key"]}, cache=True, cache_max_age=86400
        )
        info = parse_session(row, meetings[0] if meetings else None)
        final = self._saveable(info, live)
        params = {"session_key": info.session_key}

        await progress("Fetching drivers", 0.08)
        drivers = parse_drivers(await self._client.get("drivers", params, cache=final))

        parser = TimingParser(fallback_time=time.time if live else None)
        events = []
        endpoints = [e for e in TIMING_ENDPOINTS if info.is_race or e != "intervals"]
        for i, endpoint in enumerate(endpoints):
            await progress(f"Fetching {_ENDPOINT_LABELS[endpoint]}", 0.12 + 0.68 * i / len(endpoints))
            events.extend(parser.parse_many(endpoint, await self._client.get(endpoint, params, cache=final)))

        results = []
        if not live:
            results = await self._client.get("session_result", params, cache=final)
            events.extend(retirements(results, events))

        await progress("Fetching circuit layout", 0.85)
        track = await self._circuits.layout(info.circuit_key, info.year)

        # Drivers seen in timing data but missing from the driver list still get shown.
        for number in {e.driver for e in events if hasattr(e, "driver")} - drivers.keys():
            if number is not None:
                drivers[number] = _placeholder_driver(number)

        start, end = _timeline_bounds(info, events, live)
        session = SessionData(
            info=info,
            drivers=drivers,
            track=track,
            timeline_start=start,
            timeline_end=end,
            live=live,
            total_laps=total_laps(results) if info.is_race else None,
        )
        session.events.extend(events)
        session.refresh_derived()
        if live:
            default_start = time.time()
        elif session.race_start:
            default_start = session.race_start - RACE_LEAD_IN_S
        else:
            default_start = info.date_start
        session.default_start = min(max(default_start, start), end)
        await progress("Loading car positions", 0.95)
        return session, parser

    async def _session_row(self, session_key: int) -> dict:
        """The OpenF1 ``sessions`` row for a session.

        If the direct lookup fails (OpenF1 locks out non-subscribers during live
        sessions), the cached season calendar, which lists every session with
        the same fields, is searched instead.
        """
        try:
            rows = await self._client.get(
                "sessions", {"session_key": session_key}, cache=self._persist, cache_max_age=600
            )
        except OpenF1Error as exc:
            if (row := await self._session_row_from_calendar(session_key)) is not None:
                return row
            raise exc
        if not rows:
            raise OpenF1Error(f"Session {session_key} was not found on OpenF1")
        return rows[0]

    async def _session_row_from_calendar(self, session_key: int) -> dict | None:
        for year in range(time.gmtime().tm_year, FIRST_SEASON - 1, -1):
            try:
                season = await self._client.get("sessions", {"year": year}, cache=True, cache_max_age=600)
            except OpenF1Error:
                continue
            for row in season:
                if row.get("session_key") == session_key:
                    return row
        return None

    async def load_positions(
        self,
        session: SessionData,
        *,
        until: float | None = None,
        on_chunk: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """Download car positions for the session timeline in chunks.

        The chunk containing the replay's starting point is fetched first so
        playback can begin while the rest streams in.
        """
        lo = math.floor(session.timeline_start)
        hi = math.ceil(until if until is not None else session.timeline_end)
        windows = [(t, min(t + LOCATION_CHUNK_S, hi)) for t in range(lo, hi, LOCATION_CHUNK_S)]
        windows.sort(key=lambda w: (not (w[0] <= session.default_start < w[1]), w[0]))

        final = self._saveable(session.info, session.live)
        semaphore = asyncio.Semaphore(LOCATION_CONCURRENCY)

        async def fetch(window: tuple[int, int]) -> None:
            async with semaphore:
                rows = await self._client.get(
                    "location",
                    {"session_key": session.info.session_key},
                    filters=[time_filter("date", ">=", window[0]), time_filter("date", "<", window[1])],
                    cache=final,
                )
            per_driver = await asyncio.to_thread(parse_locations, rows)
            for driver, (t, x, y) in per_driver.items():
                session.store.add_samples(driver, t, x, y)
                if driver not in session.drivers:
                    session.drivers[driver] = _placeholder_driver(driver)
            session.mark_buffered(*window)
            if on_chunk:
                await on_chunk()

        try:
            async with asyncio.TaskGroup() as group:
                for window in windows:
                    group.create_task(fetch(window))
        except ExceptionGroup as eg:  # surface the first failure, not the group wrapper
            raise eg.exceptions[0] from None

        if session.track is None:
            session.track = self._trace_outline(session)

    @staticmethod
    def _trace_outline(session: SessionData):
        """Fallback outline: the fastest lap of any car, traced from its positions."""
        best = None
        for event in session.events:
            if isinstance(event, LapCompleted) and not event.pit_out_lap:
                if best is None or event.duration < best.duration:
                    best = event
        if best is None or (track := session.store.track(best.driver)) is None:
            return None
        return outline_from_samples(
            track.times, track.xs, track.ys, best.t - best.duration, best.t
        )


def _placeholder_driver(number: int) -> Driver:
    return Driver(
        number=number,
        code=str(number),
        first_name="",
        last_name=f"Car {number}",
        full_name=f"Car {number}",
        team_name="",
        team_colour="#9AA0A6",
        headshot_url=None,
    )


def _timeline_bounds(info, events, live: bool) -> tuple[float, float]:
    lead_in = 10 * 60 if info.is_race else 5 * 60
    start = info.date_start - lead_in
    if live:
        return start, info.date_end + 30 * 60
    finishes = [
        e.t
        for e in events
        if isinstance(e, RaceControlMessage)
        and ((e.flag or "").upper() == "CHEQUERED" or "SESSION FINISHED" in e.message.upper())
    ]
    if finishes:
        return start, max(finishes) + (5 * 60 if info.is_race else 3 * 60)
    last = max((e.t for e in events if e.t > start), default=None)
    return start, (last + 120) if last else info.date_end + 10 * 60
