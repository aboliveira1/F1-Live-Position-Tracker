"""Live mode: MQTT-shaped messages flow into the same store and event log as replays."""

import time
from datetime import UTC, datetime

import pytest

from f1tracker.core.clock import PlaybackClock
from f1tracker.core.engine import PlaybackEngine
from f1tracker.data.circuits import CircuitRepository
from f1tracker.data.openf1_client import OpenF1Client
from f1tracker.data.openf1_live import STATUS_TOPIC
from f1tracker.data.session_loader import SessionLoader
from f1tracker.server.controller import FeedLatency, TrackerController
from f1tracker.server.hub import ClientHub
from tests.helpers import UNLIMITED, fake_openf1


class FakeFeed:
    """Stands in for OpenF1LiveFeed: yields a fixed list of (topic, row) messages."""

    def __init__(self, messages):
        self._messages = messages

    async def messages(self):
        for message in self._messages:
            yield message


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=UTC).isoformat()


async def test_live_messages_update_positions_timing_and_drivers(settings, recorded):
    transport = fake_openf1(recorded)
    client = OpenF1Client(settings, transport=transport, limiter=UNLIMITED)
    circuits = CircuitRepository(settings, transport=transport)
    session, parser = await SessionLoader(client, circuits).load(11388, live=True)
    key = session.info.session_key
    now = time.time()

    controller = TrackerController(settings, ClientHub())
    feed = FakeFeed(
        [
            (STATUS_TOPIC, {"state": "connected", "message": "Connected"}),
            ("v1/location", {"session_key": key, "driver_number": 81, "date": iso(now - 2), "x": 100, "y": 200, "z": 0}),
            ("v1/location", {"session_key": key, "driver_number": 81, "date": iso(now - 1), "x": 300, "y": 400, "z": 0}),
            ("v1/location", {"session_key": 999, "driver_number": 5, "date": iso(now - 1), "x": 1, "y": 1, "z": 0}),
            ("v1/position", {"session_key": key, "driver_number": 81, "date": iso(now - 1.5), "position": 1}),
            ("v1/race_control", {"session_key": key, "date": iso(now - 1.2), "category": "SafetyCar", "message": "SAFETY CAR DEPLOYED"}),
            ("v1/drivers", {"session_key": key, "driver_number": 81, "name_acronym": "PIA", "team_colour": "F47600",
                            "first_name": "Oscar", "last_name": "PIASTRI", "team_name": "McLaren"}),
        ]
    )
    await controller._consume_feed(feed, session, parser)

    assert controller._feed_status["state"] == "connected"
    assert 5 not in session.store  # other sessions' messages are ignored
    assert session.drivers[81].code == "PIA"

    clock = PlaybackClock(session.timeline_start, now + 3600, live=True, live_delay=0.5)
    published = []

    async def publish(message):
        published.append(message)

    engine = PlaybackEngine(session, clock, publish)
    frame, state = engine.snapshot_messages()
    cars = {car[0]: car[1:] for car in frame["cars"]}
    assert 81 in cars  # interpolated / held from the live samples
    assert state["track_status"] == "SC"
    assert next(r for r in state["leaderboard"] if r["driver"] == 81)["position"] == 1

    await client.aclose()
    await circuits.aclose()
    await controller.shutdown()


def test_feed_latency_suggests_a_delay_above_the_slowest_samples():
    latency = FeedLatency()
    now = time.time()
    for i in range(100):
        latency.add(now - (1.0 if i < 85 else 6.0))  # 15% of samples arrive 6 s late
    assert latency.suggested_delay(minimum=4.0) == pytest.approx(6.8, abs=0.1)
    assert latency.suggested_delay(minimum=4.0) is None  # rate limited


def test_feed_latency_respects_the_configured_minimum():
    latency = FeedLatency()
    for _ in range(50):
        latency.add(time.time() - 0.5)
    assert latency.suggested_delay(minimum=4.0) == 4.0
