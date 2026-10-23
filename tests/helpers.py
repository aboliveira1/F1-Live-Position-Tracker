"""Test helpers: a fake OpenF1 server backed by a recorded session."""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qsl, unquote

import httpx

from f1tracker.data.openf1_client import RateLimiter
from f1tracker.data.openf1_parse import parse_time

FIXTURE = Path(__file__).parent / "fixtures" / "singapore_2026_race.json"

# Tests talk to fake servers, so they don't need the real API's request quota.
UNLIMITED = RateLimiter(per_second=10_000, per_minute=1_000_000)


def load_recording() -> dict:
    return json.loads(FIXTURE.read_text())


def _matches(row: dict, key: str, value: str) -> bool:
    """Apply one OpenF1-style filter (``date>=...``, ``session_key=...``) to a row."""
    if key.endswith((">", "<")):  # "date>=X" parses as key "date>", value "X"
        field, op = key[:-1], key[-1] + "="
    elif ">" in key or "<" in key:  # "date<X" parses as key "date<X", empty value
        op = ">" if ">" in key else "<"
        field, value = key.split(op, 1)
    else:
        return str(row.get(key)) == value

    left, right = parse_time(row[field]), parse_time(value + "+00:00")
    return {">=": left >= right, "<=": left <= right, ">": left > right, "<": left < right}[op]


def fake_openf1(recorded: dict, calls: list | None = None) -> httpx.MockTransport:
    """An httpx transport answering OpenF1 and MultiViewer requests from the recording.

    If ``calls`` is given, each request is recorded there as ``(url, status_code)``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        response = respond(request)
        if calls is not None:
            calls.append((str(request.url), response.status_code))
        return response

    def respond(request: httpx.Request) -> httpx.Response:
        url = request.url
        if url.host == "api.multiviewer.app":
            return httpx.Response(200, json=recorded["circuit"])
        endpoint = url.path.rsplit("/", 1)[-1]
        rows = recorded["openf1"].get(endpoint)
        if rows is None:
            return httpx.Response(404, json={"detail": "No results found."})
        query = unquote(url.query.decode())
        for key, value in parse_qsl(query, keep_blank_values=True):
            rows = [r for r in rows if _matches(r, key, value)]
        if not rows:
            return httpx.Response(404, json={"detail": "No results found."})
        return httpx.Response(200, json=rows)

    return httpx.MockTransport(handler)
