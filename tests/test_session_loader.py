"""End-to-end replay pipeline against a recorded slice of the 2026 Singapore GP."""

import httpx

from f1tracker.core.clock import PlaybackClock
from f1tracker.core.engine import PlaybackEngine
from f1tracker.data.circuits import CircuitRepository
from f1tracker.data.openf1_client import OpenF1Client
from f1tracker.data.session_loader import RACE_LEAD_IN_S, SessionLoader
from tests.helpers import UNLIMITED, fake_openf1


async def load(settings, transport, *, persist=False):
    client = OpenF1Client(settings, transport=transport, limiter=UNLIMITED)
    circuits = CircuitRepository(settings, transport=transport)
    loader = SessionLoader(client, circuits, persist=persist)
    stages = []

    async def progress(stage, fraction):
        stages.append((stage, fraction))

    session, _ = await loader.load(11388, progress=progress)
    await loader.load_positions(session)
    await client.aclose()
    await circuits.aclose()
    return session, stages


async def test_loads_metadata_timing_track_and_positions(settings, openf1_transport):
    session, stages = await load(settings, openf1_transport)

    assert session.info.meeting_name == "Singapore Grand Prix"
    assert len(session.drivers) == 22
    assert session.track is not None and session.track.source == "multiviewer"
    assert len(session.track.marshal_sector_starts) > 10
    assert session.total_laps == 62
    assert session.race_start is not None
    assert session.default_start == session.race_start - RACE_LEAD_IN_S
    assert session.timeline_start < session.race_start < session.timeline_end
    assert set(session.store.drivers) == {1, 3, 16}
    # Every position chunk was downloaded, so the whole timeline is buffered.
    assert len(session.buffered) == 1
    assert session.buffered[0][0] <= session.timeline_start
    assert session.buffered[0][1] >= session.timeline_end
    assert [f for _, f in stages] == sorted(f for _, f in stages)  # progress only moves forward


async def test_engine_produces_frames_and_state_from_the_recording(settings, openf1_transport):
    session, _ = await load(settings, openf1_transport)
    clock = PlaybackClock(session.timeline_start, session.timeline_end)
    clock.seek(session.race_start + 30)

    published = []

    async def publish(message):
        published.append(message)

    engine = PlaybackEngine(session, clock, publish)
    frame, state = engine.snapshot_messages()

    assert {car[0] for car in frame["cars"]} == {1, 3, 16}
    assert state["lap"] == 1
    assert state["leaderboard"][0]["position"] == 1
    assert any(m["message"] == "RACE START" for m in state["race_control"])

    # Seeking backwards rebuilds the race state from scratch.
    clock.seek(session.timeline_start)
    _, before_start = engine.snapshot_messages()
    assert before_start["lap"] == 0


async def test_by_default_nothing_about_the_session_is_written_to_disk(settings, recorded):
    await load(settings, fake_openf1(recorded))
    session_folder = settings.cache_dir / "openf1" / "sessions"
    assert not session_folder.exists() or not any(session_folder.rglob("*.gz"))


async def test_finished_sessions_are_cached_on_disk_when_keeping_data(settings, recorded):
    first: list[tuple[str, int]] = []
    await load(settings, fake_openf1(recorded, first), persist=True)
    second: list[tuple[str, int]] = []
    await load(settings, fake_openf1(recorded, second), persist=True)

    had_data = {url for url, status in first if status == 200 and "/sessions?" not in url}
    refetched = {url for url, _ in second}
    assert had_data  # sanity: the first load did get data
    # Anything that returned data comes from disk the second time; only the
    # session lookup and answers that were empty (maybe not ingested yet) are asked again.
    assert not had_data & refetched


async def test_cached_race_replays_while_openf1_is_locked(settings, recorded):
    """OpenF1 refuses free users during live sessions; a race opened before must still play,
    even if only the season calendar (not the direct session lookup) was cached."""
    # 1. Open the race once (with data kept) and browse the calendar, as a user would.
    await load(settings, fake_openf1(recorded), persist=True)
    client = OpenF1Client(settings, transport=fake_openf1(recorded), limiter=UNLIMITED)
    await client.get("sessions", {"year": 2026}, cache=True, cache_max_age=600)
    # Simulate a cache written by an older version that didn't keep the direct lookup.
    client._cache._path("sessions/11388", "/sessions?session_key=11388").unlink()
    await client.aclose()

    # 2. A live session starts: OpenF1 now refuses every request.
    lockout = httpx.MockTransport(
        lambda request: httpx.Response(401, json={"detail": "Live F1 session in progress. Global API access ..."})
    )
    session, _ = await load(settings, lockout, persist=True)
    assert session.info.meeting_name == "Singapore Grand Prix"
    assert set(session.store.drivers) == {1, 3, 16}
    assert session.track is not None
