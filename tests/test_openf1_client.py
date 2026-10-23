import asyncio
import time

import httpx
import pytest

from f1tracker.config import Settings
from f1tracker.data import openf1_client
from f1tracker.data.openf1_client import (
    OpenF1AuthError,
    OpenF1Client,
    RateLimiter,
    time_filter,
)
from tests.helpers import UNLIMITED


def client_with(handler, settings: Settings) -> OpenF1Client:
    return OpenF1Client(settings, transport=httpx.MockTransport(handler), limiter=UNLIMITED)


def test_time_filter_format():
    assert time_filter("date", ">=", 1791720000) == "date>=2026-10-11T12:00:00"


async def test_builds_openf1_comparison_filters(settings):
    seen = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=[{"ok": 1}])

    client = client_with(handler, settings)
    rows = await client.get(
        "location",
        {"session_key": 9158},
        filters=[time_filter("date", ">=", 1791720000), time_filter("date", "<", 1791720900)],
    )
    await client.aclose()
    assert rows == [{"ok": 1}]
    raw = str(seen[0])
    assert "session_key=9158" in raw
    assert "date%3E=2026-10-11T12:00:00" in raw
    assert "date%3C2026-10-11T12:15:00" in raw


async def test_no_results_become_empty_lists(settings):
    def handler(request):
        if "drivers" in request.url.path:
            return httpx.Response(404, json={"detail": "No results found."})
        return httpx.Response(200, json={"detail": "No results found."})

    client = client_with(handler, settings)
    assert await client.get("drivers", {"session_key": 1}) == []
    assert await client.get("starting_grid", {"session_key": 1}) == []
    await client.aclose()


async def test_retries_rate_limited_requests(settings, monkeypatch):
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=[1, 2])

    client = client_with(handler, settings)
    assert await client.get("laps") == [1, 2]
    assert len(attempts) == 3
    await client.aclose()


async def test_bad_credentials_dont_block_free_data(tmp_path):
    settings = Settings(cache_dir=tmp_path, openf1_username="me", openf1_password="wrong")
    sign_ins = []

    def handler(request):
        if request.url.path.endswith("/token"):
            sign_ins.append(1)
            return httpx.Response(401, json={"detail": "bad credentials"})
        assert "authorization" not in request.headers
        return httpx.Response(200, json=[{"session_key": 1}])

    client = client_with(handler, settings)
    assert await client.get("sessions") == [{"session_key": 1}]
    assert await client.get("laps") == [{"session_key": 1}]
    with pytest.raises(OpenF1AuthError):  # live mode still gets a clear error
        await client.access_token()
    assert len(sign_ins) == 1  # failures are remembered instead of retried per request
    await client.aclose()


async def test_live_only_endpoints_report_the_subscription_requirement(settings):
    def handler(request):
        return httpx.Response(401, json={"detail": "Unauthorized"})

    client = client_with(handler, settings)
    with pytest.raises(OpenF1AuthError, match="subscription"):
        await client.get("location", {"session_key": "latest"})
    await client.aclose()


LIVE_LOCKOUT = {
    "detail": "Live F1 session in progress. Global API access (including past sessions) is "
    "restricted to authenticated users until the session ends. Get an API key here: https://example"
}


async def test_live_session_lockout_falls_back_to_stale_cache(settings, monkeypatch):
    locked = False

    def handler(request):
        if locked:
            return httpx.Response(401, json=LIVE_LOCKOUT)
        return httpx.Response(200, json=[{"meeting_key": 1}])

    client = client_with(handler, settings)
    assert await client.get("meetings", {"year": 2026}, cache=True, cache_max_age=600) == [{"meeting_key": 1}]

    locked = True
    # Pretend the cached copy has expired: max_age=0 forces a refetch, which is refused.
    assert await client.get("meetings", {"year": 2026}, cache=True, cache_max_age=0) == [{"meeting_key": 1}]

    with pytest.raises(OpenF1AuthError, match="live F1 session is in progress"):
        await client.get("laps", {"session_key": 99})  # nothing cached to fall back on
    await client.aclose()


async def test_empty_answers_are_cached_but_rechecked_later(settings, monkeypatch):
    calls = []
    locked = False

    def handler(request):
        calls.append(1)
        if locked:
            return httpx.Response(401, json=LIVE_LOCKOUT)
        return httpx.Response(404, json={"detail": "No results found."})

    client = client_with(handler, settings)
    assert await client.get("pit", {"session_key": 1}, cache=True) == []
    assert await client.get("pit", {"session_key": 1}, cache=True) == []
    assert len(calls) == 1  # fresh empty answer reused

    monkeypatch.setattr(openf1_client, "EMPTY_RECHECK_S", 0)
    assert await client.get("pit", {"session_key": 1}, cache=True) == []
    assert len(calls) == 2  # old empty answer asked for again

    locked = True
    assert await client.get("pit", {"session_key": 1}, cache=True) == []  # lockout: cached empty is fine
    await client.aclose()


async def test_bearer_token_is_sent_when_credentials_are_configured(tmp_path):
    settings = Settings(cache_dir=tmp_path, openf1_username="me", openf1_password="pw")
    headers = []

    def handler(request):
        if request.url.path.endswith("/token"):
            assert b"username=me" in request.content
            return httpx.Response(200, json={"access_token": "abc", "expires_in": "3600", "token_type": "bearer"})
        headers.append(request.headers.get("authorization"))
        return httpx.Response(200, json=[])

    client = client_with(handler, settings)
    await client.get("sessions")
    await client.get("sessions", {"year": 2026})
    await client.aclose()
    assert headers == ["Bearer abc", "Bearer abc"]


async def test_cache_round_trip(settings):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, json=[{"lap": 1}])

    client = client_with(handler, settings)
    assert await client.get("laps", {"session_key": 1}, cache=True) == [{"lap": 1}]
    assert await client.get("laps", {"session_key": 1}, cache=True) == [{"lap": 1}]
    await client.aclose()
    assert len(calls) == 1


async def test_rate_limiter_spaces_out_bursts(monkeypatch):
    limiter = RateLimiter(per_second=2, per_minute=100)
    start = time.monotonic()
    await asyncio.gather(*(limiter.acquire() for _ in range(5)))
    # 5 requests at 2/s need at least two full one-second windows.
    assert time.monotonic() - start >= 1.9


def test_retry_budget_is_bounded():
    assert 1 <= openf1_client.MAX_RETRIES <= 6


def test_session_data_lives_in_its_own_folder(settings):
    client = client_with(lambda request: httpx.Response(200, json=[{"x": 1}]), settings)
    assert client._namespace({"session_key": 11388}) == "sessions/11388"
    assert client._namespace({"year": 2026}) == "calendar"
    assert client._namespace(None) == "calendar"


async def test_clearing_session_data_keeps_the_calendar(settings):
    client = client_with(lambda request: httpx.Response(200, json=[{"x": 1}]), settings)
    await client.get("laps", {"session_key": 7}, cache=True)
    await client.get("sessions", {"year": 2026}, cache=True)
    assert client.clear_session_data() > 0
    assert not client.is_cached("laps", {"session_key": 7})
    assert client.is_cached("sessions", {"year": 2026})
    await client.aclose()
