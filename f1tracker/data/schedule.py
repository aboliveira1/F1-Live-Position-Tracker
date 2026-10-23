"""Season calendar from OpenF1, and detection of the session that is live right now."""

from __future__ import annotations

import time
from collections import defaultdict

from .openf1_client import OpenF1Client
from .openf1_parse import parse_time

# A session counts as "live" from shortly before it starts until a while after
# its scheduled end (sessions often overrun after red flags).
LIVE_BEFORE_S = 30 * 60
LIVE_AFTER_S = 45 * 60


def session_status(row: dict, now: float) -> str:
    if row.get("is_cancelled"):
        return "cancelled"
    start, end = parse_time(row["date_start"]), parse_time(row["date_end"])
    if now < start - LIVE_BEFORE_S:
        return "upcoming"
    if now <= end + LIVE_AFTER_S:
        return "live"
    return "completed"


class ScheduleService:
    def __init__(self, client: OpenF1Client) -> None:
        self._client = client

    async def season(self, year: int) -> list[dict]:
        """Meetings of a season, each with its sessions and their status."""
        sessions = await self._client.get("sessions", {"year": year}, cache=True, cache_max_age=600)
        meetings = await self._client.get("meetings", {"year": year}, cache=True, cache_max_age=3600)
        now = time.time()

        by_meeting: dict[int, list[dict]] = defaultdict(list)
        for row in sessions:
            by_meeting[row["meeting_key"]].append(
                {
                    "session_key": row["session_key"],
                    "name": row.get("session_name"),
                    "type": row.get("session_type"),
                    "date_start": parse_time(row["date_start"]),
                    "date_end": parse_time(row["date_end"]),
                    "status": session_status(row, now),
                    # Saved on this computer: plays even while OpenF1 is unavailable.
                    "saved": self._client.is_cached("drivers", {"session_key": row["session_key"]}),
                }
            )

        out = []
        for m in meetings:
            items = sorted(by_meeting.get(m["meeting_key"], []), key=lambda s: s["date_start"])
            if not items:
                continue
            out.append(
                {
                    "meeting_key": m["meeting_key"],
                    "name": m.get("meeting_name"),
                    "official_name": m.get("meeting_official_name"),
                    "country": m.get("country_name"),
                    "country_code": m.get("country_code"),
                    "flag_url": m.get("country_flag"),
                    "location": m.get("location"),
                    "circuit": m.get("circuit_short_name"),
                    "date_start": parse_time(m["date_start"]),
                    "cancelled": bool(m.get("is_cancelled")),
                    "sessions": items,
                }
            )
        return sorted(out, key=lambda m: m["date_start"])

    async def live_session(self) -> dict | None:
        """The session in its live window right now, if any."""
        for meeting in await self.season(time.gmtime().tm_year):
            for s in meeting["sessions"]:
                if s["status"] == "live":
                    return {**s, "meeting_name": meeting["name"]}
        return None

    async def next_session(self) -> dict | None:
        now = time.time()
        upcoming = [
            {**s, "meeting_name": m["name"]}
            for m in await self.season(time.gmtime().tm_year)
            for s in m["sessions"]
            if s["status"] in ("upcoming", "live") and s["date_end"] + LIVE_AFTER_S > now
        ]
        return min(upcoming, key=lambda s: s["date_start"], default=None)

    async def latest_completed(self, *, prefer_race: bool = True) -> dict | None:
        year = time.gmtime().tm_year
        for y in (year, year - 1):
            done = [
                {**s, "meeting_name": m["name"]}
                for m in await self.season(y)
                for s in m["sessions"]
                if s["status"] == "completed"
            ]
            if prefer_race:
                races = [s for s in done if s["type"] == "Race"]
                done = races or done
            if done:
                return max(done, key=lambda s: s["date_start"])
        return None
