"""Async client for the OpenF1 REST API (https://openf1.org).

* Historical data is free and needs no account (rate limited to 3 req/s, 30 req/min).
* Real-time data needs an OpenF1 subscription; with ``OPENF1_USERNAME`` /
  ``OPENF1_PASSWORD`` set, requests carry an OAuth2 bearer token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from urllib.parse import quote, urlencode

import httpx
import orjson

from .. import __version__
from ..config import Settings
from .cache import JsonCache

log = logging.getLogger(__name__)

MAX_RETRIES = 4
# After a failed sign-in, wait this long before trying again.
SIGN_IN_RETRY_S = 60.0
# Cached empty answers are asked for again after this long.
EMPTY_RECHECK_S = 6 * 3600
# After a live-session lockout answer, report "locked" for this long (unless a request succeeds).
LOCKOUT_MEMORY_S = 5 * 60
# Cache folder holding per-session data (one sub-folder per session_key).
SESSION_NAMESPACE = "sessions"


class OpenF1Error(RuntimeError):
    """OpenF1 could not be reached or returned an unusable response."""


class OpenF1AuthError(OpenF1Error):
    """Credentials were rejected, or the request needs a paid subscription."""


LOCKOUT_MESSAGE = (
    "A live F1 session is in progress, and OpenF1 limits all data (including past "
    "sessions) to subscribers until it ends."
)
LOCKOUT_SAVED_HINT = " Sessions saved on this computer still play."


def _is_live_lockout(resp: httpx.Response) -> bool:
    """OpenF1 blocks all free access while a session is live, and says so in the detail."""
    try:
        detail = str(resp.json().get("detail") or "")
    except (ValueError, AttributeError):
        return False
    return "session in progress" in detail.lower()


def _refusal_message(resp: httpx.Response, endpoint: str, keeps_data: bool) -> str:
    """Explain a 401/403, using OpenF1's own reason when it gives one."""
    if _is_live_lockout(resp):
        return LOCKOUT_MESSAGE + (LOCKOUT_SAVED_HINT if keeps_data else "")
    return (
        f"OpenF1 refused /{endpoint} (HTTP {resp.status_code}). Live data during a session "
        "needs an OpenF1 subscription; see README."
    )


def time_filter(field: str, op: str, t: float) -> str:
    """Build an OpenF1 comparison filter such as ``date>=2024-05-26T13:00:00``."""
    stamp = datetime.fromtimestamp(t, tz=UTC).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{field}{op}{stamp}"


class RateLimiter:
    """Sliding-window limiter for "N per second and M per minute" quotas."""

    def __init__(self, per_second: int, per_minute: int) -> None:
        self.per_second = per_second
        self.per_minute = per_minute
        self._stamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                while self._stamps and now - self._stamps[0] >= 60.0:
                    self._stamps.popleft()
                last_second = sum(1 for s in self._stamps if now - s < 1.0)
                if len(self._stamps) < self.per_minute and last_second < self.per_second:
                    self._stamps.append(now)
                    return
                if len(self._stamps) >= self.per_minute:
                    wait = self._stamps[0] + 60.0 - now
                    log.info("OpenF1 rate limit reached; waiting %.1fs", wait)
                else:
                    wait = self._stamps[-self.per_second] + 1.0 - now
                await asyncio.sleep(max(wait, 0.02))


class OpenF1Client:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        limiter: RateLimiter | None = None,
    ):
        self._settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.openf1_base_url,
            timeout=httpx.Timeout(90.0, connect=15.0),
            headers={"User-Agent": f"f1tracker/{__version__}", "Accept": "application/json"},
            transport=transport,
            follow_redirects=True,
        )
        if limiter is not None:
            self._limiter = limiter
        elif settings.has_openf1_credentials:
            self._limiter = RateLimiter(per_second=6, per_minute=60)
        else:
            self._limiter = RateLimiter(per_second=3, per_minute=30)
        self._cache = JsonCache(settings.cache_dir / "openf1")
        self._token: str | None = None
        self._token_expires = 0.0
        self._token_lock = asyncio.Lock()
        self._sign_in_error: OpenF1Error | None = None
        self._sign_in_failed_at = 0.0
        self._warned_sign_in = False
        self._locked_at = 0.0  # when OpenF1 last refused us because a session is live

    @property
    def authenticated(self) -> bool:
        return self._settings.has_openf1_credentials

    @property
    def locked_out(self) -> bool:
        """True while OpenF1 is refusing our requests because a session is live."""
        return time.time() - self._locked_at < LOCKOUT_MEMORY_S

    def is_cached(self, endpoint: str, params: Mapping[str, object]) -> bool:
        """Whether this request has an answer on disk (so it works even when OpenF1 doesn't)."""
        return self._cache.has(self._namespace(params), self._url(endpoint, params))

    def clear_session_data(self) -> int:
        """Delete every session's saved data (calendars stay); returns the bytes freed."""
        return self._cache.clear(SESSION_NAMESPACE)

    @staticmethod
    def _namespace(params: Mapping[str, object] | None) -> str:
        """Session data gets a folder per session, so it can be deleted on its own."""
        key = (params or {}).get("session_key")
        return f"{SESSION_NAMESPACE}/{key}" if key is not None else "calendar"

    @staticmethod
    def _url(endpoint: str, params: Mapping[str, object] | None, filters: Sequence[str] = ()) -> str:
        parts = [urlencode(params or {})] + [quote(f, safe="=:") for f in filters]
        query = "&".join(p for p in parts if p)
        return f"/{endpoint}?{query}" if query else f"/{endpoint}"

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ #
    async def access_token(self) -> str | None:
        """OAuth2 token for the configured account (refreshed before it expires)."""
        if not self.authenticated:
            return None
        async with self._token_lock:
            if self._token and time.time() < self._token_expires - 300:
                return self._token
            if self._sign_in_error and time.time() - self._sign_in_failed_at < SIGN_IN_RETRY_S:
                raise self._sign_in_error
            try:
                self._token, self._token_expires = await self._sign_in()
            except OpenF1Error as exc:
                self._sign_in_error, self._sign_in_failed_at = exc, time.time()
                raise
            self._sign_in_error = None
            return self._token

    async def _sign_in(self) -> tuple[str, float]:
        try:
            resp = await self._http.post(
                self._settings.openf1_token_url,
                data={
                    "username": self._settings.openf1_username,
                    "password": self._settings.openf1_password,
                },
            )
        except httpx.TransportError as exc:
            raise OpenF1Error(f"Could not reach OpenF1 to sign in ({exc.__class__.__name__})") from exc
        if resp.status_code in (400, 401, 403):
            raise OpenF1AuthError("OpenF1 rejected the username/password configured in .env")
        if resp.status_code >= 400:
            raise OpenF1Error(f"OpenF1 sign-in failed with HTTP {resp.status_code}")
        body = resp.json()
        log.info("Signed in to OpenF1 (token valid for %ss)", body.get("expires_in"))
        return body["access_token"], time.time() + float(body.get("expires_in", 3600))

    # ------------------------------------------------------------------ #
    async def get(
        self,
        endpoint: str,
        params: Mapping[str, object] | None = None,
        *,
        filters: Sequence[str] = (),
        cache: bool = False,
        cache_max_age: float | None = None,
    ) -> list[dict]:
        """Fetch rows from ``/v1/<endpoint>``.

        ``params`` are equality filters; ``filters`` are raw comparison filters
        built with :func:`time_filter`. With ``cache=True`` the response is
        stored on disk and reused (only do that for data that won't change).
        """
        url = self._url(endpoint, params, filters)
        ns = self._namespace(params)
        if cache:
            cached = await asyncio.to_thread(self._cache.get, ns, url, max_age=cache_max_age)
            # An empty answer may only mean the data wasn't ingested yet, so re-check those now and then.
            if cached or (cached == [] and (self._cache.age(ns, url) or 0) < EMPTY_RECHECK_S):
                return cached
        try:
            rows = await self._request(endpoint, url)
        except OpenF1Error:
            # OpenF1 locks out free users entirely while a session is live; an
            # older cached copy beats no data at all.
            if cache and (stale := await asyncio.to_thread(self._cache.get, ns, url)) is not None:
                log.info("OpenF1 unavailable; using cached /%s", endpoint)
                return stale
            raise
        if cache:
            await asyncio.to_thread(self._cache.put, ns, url, rows)
        return rows

    async def _bearer_headers(self) -> dict[str, str]:
        try:
            token = await self.access_token()
        except OpenF1Error as exc:
            # Historical data is free, so a sign-in problem shouldn't stop replays.
            if not self._warned_sign_in:
                log.warning("%s — continuing without an OpenF1 token", exc)
                self._warned_sign_in = True
            return {}
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _request(self, endpoint: str, url: str) -> list[dict]:
        attempt = 0
        while True:
            await self._limiter.acquire()
            headers = await self._bearer_headers()
            try:
                resp = await self._http.get(url, headers=headers)
            except httpx.TransportError as exc:
                if attempt >= MAX_RETRIES:
                    raise OpenF1Error(f"Could not reach OpenF1 ({exc.__class__.__name__})") from exc
                await asyncio.sleep(2**attempt)
                attempt += 1
                continue

            status = resp.status_code
            if status in (200, 404):
                self._locked_at = 0.0  # OpenF1 is answering us normally again
            if status == 404:  # OpenF1 answers 404 when nothing matches the filters
                return []
            if status == 429 or status >= 500:
                if attempt >= MAX_RETRIES:
                    raise OpenF1Error(f"OpenF1 /{endpoint} kept failing (HTTP {status})")
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * 2**attempt
                log.warning("OpenF1 /%s returned HTTP %s; retrying in %.0fs", endpoint, status, wait)
                await asyncio.sleep(wait)
                attempt += 1
                continue
            if status in (401, 403):
                if _is_live_lockout(resp):
                    self._locked_at = time.time()
                raise OpenF1AuthError(_refusal_message(resp, endpoint, self._settings.keep_data))
            if status >= 400:
                raise OpenF1Error(f"OpenF1 /{endpoint} returned HTTP {status}: {resp.text[:200]}")

            # Large responses (car positions) are decoded off the event loop.
            if len(resp.content) > 256_000:
                data = await asyncio.to_thread(orjson.loads, resp.content)
            else:
                data = orjson.loads(resp.content)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "detail" in data:  # e.g. {"detail": "No results found."}
                return []
            return [data] if isinstance(data, dict) else []
