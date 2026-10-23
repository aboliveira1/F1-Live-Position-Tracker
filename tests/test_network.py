"""Smoke tests against the real OpenF1 API. Opt in with F1TRACKER_NETWORK_TESTS=1."""

import os

import pytest

from f1tracker.data.circuits import CircuitRepository
from f1tracker.data.openf1_client import OpenF1Client
from f1tracker.data.schedule import ScheduleService
from f1tracker.data.session_loader import SessionLoader

pytestmark = [
    pytest.mark.network,
    pytest.mark.skipif(os.environ.get("F1TRACKER_NETWORK_TESTS") != "1", reason="network tests are opt-in"),
]


async def test_calendar_and_session_metadata(settings):
    client = OpenF1Client(settings)
    circuits = CircuitRepository(settings)
    try:
        season = await ScheduleService(client).season(2025)
        assert len(season) >= 20
        race = next(s for m in season for s in m["sessions"] if s["type"] == "Race" and s["status"] == "completed")
        session, _ = await SessionLoader(client, circuits).load(race["session_key"])
        assert len(session.drivers) >= 20
        assert session.track is not None
        assert session.lap_markers
    finally:
        await client.aclose()
        await circuits.aclose()
