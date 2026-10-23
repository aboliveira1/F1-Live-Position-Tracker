"""Record a small slice of a real session from OpenF1 for offline tests.

    python tests/fixtures/make_fixture.py

Writes ``singapore_2026_race.json``: the 2026 Singapore GP around lights-out
(three cars, three minutes of positions) plus the timing rows and circuit
layout the loader needs. Re-run it to refresh the fixture.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

SESSION_KEY = 11388
CIRCUIT_KEY, YEAR = 61, 2026
DRIVERS = (1, 3, 16)
WINDOW = ("2026-10-11T12:02:30", "2026-10-11T12:05:30")
BASE = "https://api.openf1.org/v1"
OUT = Path(__file__).with_name("singapore_2026_race.json")


def get(client: httpx.Client, path: str) -> list | dict:
    time.sleep(0.4)  # stay well inside the free rate limit
    resp = client.get(path)
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    with httpx.Client(timeout=60) as client:
        q = f"session_key={SESSION_KEY}"
        sessions = get(client, f"{BASE}/sessions?{q}")
        rows = {
            "sessions": sessions,
            "meetings": get(client, f"{BASE}/meetings?meeting_key={sessions[0]['meeting_key']}"),
            "drivers": get(client, f"{BASE}/drivers?{q}"),
            "race_control": get(client, f"{BASE}/race_control?{q}"),
            "session_result": get(client, f"{BASE}/session_result?{q}"),
            "weather": get(client, f"{BASE}/weather?{q}")[:30],
            "position": get(client, f"{BASE}/position?{q}")[:120],
        }
        for endpoint in ("laps", "stints", "pit", "intervals"):
            data = get(client, f"{BASE}/{endpoint}?{q}")
            rows[endpoint] = [r for r in data if r["driver_number"] in DRIVERS and (r.get("lap_number") or 0) <= 6]
        rows["intervals"] = rows["intervals"][:150]

        location = []
        for driver in DRIVERS:
            data = get(
                client,
                f"{BASE}/location?{q}&driver_number={driver}&date>={WINDOW[0]}&date<{WINDOW[1]}",
            )
            location += [{k: r[k] for k in ("date", "driver_number", "session_key", "x", "y", "z")} for r in data]
        rows["location"] = location

        circuit = client.get(f"https://api.multiviewer.app/api/v1/circuits/{CIRCUIT_KEY}/{YEAR}").json()

    OUT.write_text(json.dumps({"openf1": rows, "circuit": circuit}, separators=(",", ":")))
    print(f"Wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB, {len(location)} position samples)")


if __name__ == "__main__":
    main()
