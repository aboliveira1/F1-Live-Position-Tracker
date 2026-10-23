"""Owns the active session and turns browser commands into actions.

Session lifecycle
-----------------
* The **active** session is the one on screen. It's served by an engine task
  plus helper tasks, and driven by one task that keeps feeding it data
  (downloading positions for a replay, consuming the feed when live).
* Picking another session starts a **pending** load in the background. The
  active session keeps playing until the new one is ready, and stays on
  screen if the load fails. A newer request cancels an older pending one.
* Switches are serialised by a lock, so there is only ever one engine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Coroutine
from datetime import datetime

from .. import __version__
from ..config import Settings
from ..core.clock import SPEEDS, PlaybackClock
from ..core.engine import PlaybackEngine
from ..core.session import SessionData
from ..data.circuits import CircuitRepository
from ..data.openf1_client import OpenF1Client, OpenF1Error
from ..data.openf1_live import STATUS_TOPIC, OpenF1LiveFeed
from ..data.openf1_parse import TimingParser, parse_driver, parse_location
from ..data.schedule import ScheduleService
from ..data.session_loader import SessionLoader
from .hub import ClientHub

log = logging.getLogger(__name__)

LIVE_NEEDS_ACCOUNT = (
    "Live tracking uses OpenF1's real-time feed, which needs an OpenF1 subscription. "
    "Add OPENF1_USERNAME and OPENF1_PASSWORD to the .env file and restart. "
    "Replays of finished sessions are free."
)
# How often live sessions refresh lap markers / flag periods on the timeline.
LIVE_TIMELINE_REFRESH_S = 15.0


class UserFacingError(Exception):
    """An expected problem with a message that can be shown as-is."""


class FeedLatency:
    """Measures how late live position samples arrive, to size the live delay.

    Rendering must stay behind the newest samples or cars would freeze at their
    last known spot, so the delay tracks the 90th-percentile latency plus a margin.
    """

    MARGIN_S = 0.8
    MAX_DELAY_S = 20.0
    UPDATE_EVERY_S = 5.0

    def __init__(self, window: int = 400) -> None:
        self._samples: deque[float] = deque(maxlen=window)
        self._last_update = 0.0

    def add(self, sample_time: float) -> None:
        self._samples.append(time.time() - sample_time)

    def suggested_delay(self, minimum: float) -> float | None:
        """A new delay every few seconds once enough samples exist, else None."""
        now = time.time()
        if len(self._samples) < 40 or now - self._last_update < self.UPDATE_EVERY_S:
            return None
        self._last_update = now
        p90 = sorted(self._samples)[int(len(self._samples) * 0.9)]
        return min(max(minimum, p90 + self.MARGIN_S), self.MAX_DELAY_S)


async def _cancel(task: asyncio.Task | None) -> None:
    if task is not None and not task.done() and task is not asyncio.current_task():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class TrackerController:
    def __init__(self, settings: Settings, hub: ClientHub) -> None:
        self.settings = settings
        self.hub = hub
        self.client = OpenF1Client(settings)
        self.circuits = CircuitRepository(settings)
        self.loader = SessionLoader(self.client, self.circuits, persist=settings.keep_data)
        self.schedule = ScheduleService(self.client)
        if not settings.keep_data:
            self._delete_saved_sessions()

        self.mode = "idle"  # idle | loading | replay | live
        self.session: SessionData | None = None
        self.engine: PlaybackEngine | None = None

        self._switch_lock = asyncio.Lock()
        self._startup: asyncio.Task | None = None
        self._pending: asyncio.Task | None = None  # loading the next session
        self._active: asyncio.Task | None = None  # feeding the session on screen
        self._helpers: set[asyncio.Task] = set()  # engine + other tasks of the active session
        self._feed: OpenF1LiveFeed | None = None  # live feed of the active session

        self._feed_status: dict | None = None
        self._loading: dict | None = None
        self._notice: str | None = None
        self._autoplay_pending = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def begin_startup(self, *, session_key: int | None = None, live: bool = False) -> None:
        self._startup = asyncio.create_task(self._startup_flow(session_key, live))

    async def _startup_flow(self, session_key: int | None, live: bool) -> None:
        """Pick what to show first: a requested session, a live one, or the latest race."""
        try:
            if session_key:
                await self.load_replay(session_key)
                return
            wants_live = live or (
                self.settings.has_openf1_credentials and await self.schedule.live_session()
            )
            if wants_live and await self.go_live():
                return
            if latest := await self.schedule.latest_completed():
                await self.load_replay(latest["session_key"])
        except asyncio.CancelledError:
            raise
        except OpenF1Error as exc:  # expected now and then (offline, live-session lockout)
            log.warning("Couldn't load a session at startup: %s", exc)
            await self._fail(str(exc))
        except Exception as exc:
            log.exception("Startup failed")
            await self._fail(f"Couldn't reach OpenF1 ({exc}). Pick a session to retry.")

    async def shutdown(self) -> None:
        for task in (self._startup, self._pending, self._active):
            await _cancel(task)
        await self._stop_active()
        await self.client.aclose()
        await self.circuits.aclose()
        if not self.settings.keep_data:
            self._delete_saved_sessions()

    def _delete_saved_sessions(self) -> None:
        """Session data is kept in memory only; remove anything left on disk."""
        freed = self.client.clear_session_data()
        if freed:
            log.info("Deleted %.1f MB of saved session data", freed / 1e6)

    def _spawn_helper(self, coro: Coroutine) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._helpers.add(task)
        task.add_done_callback(self._helpers.discard)
        return task

    async def _stop_active(self) -> None:
        helpers = list(self._helpers)
        for task in helpers:
            task.cancel()
        await asyncio.gather(*helpers, return_exceptions=True)
        await self._close_feed()
        self.session = self.engine = None

    async def _close_feed(self) -> None:
        feed, self._feed, self._feed_status = self._feed, None, None
        if feed is not None:
            await feed.stop()

    async def _begin_switch(self, coro: Coroutine) -> None:
        """Start loading another session in the background."""
        async with self._switch_lock:
            await _cancel(self._pending)
            self._notice = None
            self._pending = asyncio.create_task(coro)

    async def _activate(self, session: SessionData, clock: PlaybackClock, mode: str) -> None:
        """Put a freshly loaded session on screen (called from its pending task)."""
        me = asyncio.current_task()
        async with self._switch_lock:
            await _cancel(self._active)
            await self._stop_active()
            self._active, self._pending = me, None
            self.session = session
            self.engine = PlaybackEngine(session, clock, self.hub.broadcast, tick_hz=self.settings.tick_hz)
            self._loading = None
        await self.hub.broadcast(session.to_payload())
        await self._set_mode(mode)
        self._spawn_helper(self.engine.run())

    # ------------------------------------------------------------------ #
    # Messages for browsers
    # ------------------------------------------------------------------ #
    def status_payload(self) -> dict:
        return {
            "type": "status",
            "mode": self.mode,
            "session_key": self.session.info.session_key if self.session else None,
            "live_available": self.settings.has_openf1_credentials,
            "feed": self._feed_status,
            "notice": self._notice,
        }

    def welcome_messages(self) -> list[dict]:
        messages = [{"type": "hello", "version": __version__, "speeds": SPEEDS}, self.status_payload()]
        if self.mode == "loading" and self._loading:
            messages.append(self._loading)
        if self.session and self.engine:
            messages.append(self.session.to_payload())
            messages.append(self.session.buffer_payload(complete=False))
            messages.extend(self.engine.snapshot_messages())
        return messages

    async def _broadcast_status(self) -> None:
        await self.hub.broadcast(self.status_payload())

    async def _set_mode(self, mode: str) -> None:
        self.mode = mode
        await self._broadcast_status()

    async def _progress(self, stage: str, fraction: float) -> None:
        self._loading = {"type": "loading", "stage": stage, "progress": round(fraction, 3)}
        await self.hub.broadcast(self._loading)

    async def _fail(self, message: str) -> None:
        """Report a problem; whatever session is on screen stays there."""
        self._notice = message
        self._loading = None
        if self.session is None:
            self.mode = "idle"
        else:
            self.mode = "live" if self.session.live else "replay"
        await self._broadcast_status()
        await self.hub.broadcast({"type": "error", "message": message})

    # ------------------------------------------------------------------ #
    # Commands
    # ------------------------------------------------------------------ #
    async def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd in ("load", "live") and self._startup is not None:
            self._startup.cancel()  # the user's choice wins over the automatic one
        if cmd == "load":
            await self.load_replay(int(msg["session_key"]))
        elif cmd == "live":
            await self.go_live()
        elif self.engine is not None:
            self._playback_command(cmd, msg)

    def _playback_command(self, cmd: str, msg: dict) -> None:
        clock = self.engine.clock
        if cmd in ("play", "pause", "toggle"):
            self._autoplay_pending = False  # the user has taken charge of play/pause
        if cmd == "play":
            clock.play()
        elif cmd == "pause":
            clock.pause()
        elif cmd == "toggle":
            clock.toggle()
        elif cmd == "seek":
            clock.seek(float(msg["t"]))
        elif cmd == "speed":
            clock.set_speed(float(msg["value"]))
        elif cmd == "go_live":
            clock.go_live()
        else:
            log.debug("Ignoring unknown command %r", cmd)
            return
        self.engine.request_state_push()

    # ------------------------------------------------------------------ #
    # Replay
    # ------------------------------------------------------------------ #
    async def load_replay(self, session_key: int) -> None:
        await self._begin_switch(self._run_replay(session_key))

    async def _run_replay(self, session_key: int) -> None:
        await self._set_mode("loading")
        try:
            session, _ = await self.loader.load(session_key, progress=self._progress)
        except asyncio.CancelledError:
            raise
        except OpenF1Error as exc:
            await self._fail(str(exc))
            return
        except Exception as exc:
            log.exception("Failed to load session %s", session_key)
            await self._fail(f"Couldn't load that session: {exc}")
            return

        clock = PlaybackClock(session.timeline_start, session.timeline_end)
        clock.seek(session.default_start)
        self._autoplay_pending = True
        await self._activate(session, clock, "replay")

        async def on_chunk() -> None:
            await self.hub.broadcast(session.buffer_payload(complete=False))
            t = clock.now()
            if self._autoplay_pending and any(lo <= t < hi for lo, hi in session.buffered):
                self._autoplay_pending = False
                clock.play()

        try:
            await self.loader.load_positions(session, on_chunk=on_chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Position download failed for session %s", session_key)
            await self._fail(f"Couldn't download all car positions: {exc}")
            return
        await self.hub.broadcast(session.to_payload())  # drivers/outline may have been filled in
        await self.hub.broadcast(session.buffer_payload(complete=True))
        if self._autoplay_pending:
            clock.play()

    # ------------------------------------------------------------------ #
    # Live
    # ------------------------------------------------------------------ #
    async def go_live(self) -> bool:
        """Switch to the session that's on now. Returns False (keeping whatever
        is currently shown) when live mode isn't possible."""
        try:
            if not self.settings.has_openf1_credentials:
                raise UserFacingError(LIVE_NEEDS_ACCOUNT)
            live = await self.schedule.live_session()
            if live is None:
                raise UserFacingError(await self._no_live_session_message())
        except (UserFacingError, OpenF1Error) as exc:
            await self._fail(str(exc))
            return False
        await self._begin_switch(self._run_live(live))
        return True

    async def _run_live(self, live: dict) -> None:
        await self._set_mode("loading")
        feed = OpenF1LiveFeed(self.settings, self.client.access_token)
        try:
            # Connect first so nothing is missed while history downloads.
            await feed.start()
            feed_started = time.time()
            session, parser = await self.loader.load(live["session_key"], live=True, progress=self._progress)
            clock = PlaybackClock(
                session.timeline_start,
                session.timeline_end,
                live=True,
                live_delay=self.settings.live_delay_s,
            )
            await self._activate(session, clock, "live")
            self._feed = feed
            self._spawn_helper(self._backfill_positions(session, feed_started))
            self._spawn_helper(self._refresh_live_timeline(session, feed_started))
            await self._consume_feed(feed, session, parser)
        except asyncio.CancelledError:
            raise
        except OpenF1Error as exc:
            await self._fail(str(exc))
        except Exception as exc:
            log.exception("Live mode failed")
            await self._fail(f"Live mode stopped: {exc}")
        finally:
            if self._feed is feed:
                self._feed = self._feed_status = None
            await feed.stop()

    async def _no_live_session_message(self) -> str:
        upcoming = await self.schedule.next_session()
        if upcoming is None:
            return "No session is live right now."
        when = datetime.fromtimestamp(upcoming["date_start"]).strftime("%a %d %b, %H:%M")
        return (
            f"No session is live right now. Next up: {upcoming['meeting_name']} — "
            f"{upcoming['name']} ({when} your time)."
        )

    async def _consume_feed(self, feed: OpenF1LiveFeed, session: SessionData, parser: TimingParser) -> None:
        latency = FeedLatency()
        async for topic, row in feed.messages():
            if topic == STATUS_TOPIC:
                self._feed_status = row
                await self._broadcast_status()
                continue
            try:
                await self._apply_live_row(session, parser, topic, row, latency)
            except (KeyError, TypeError, ValueError) as exc:  # one bad message mustn't end live mode
                log.debug("Skipping malformed live message on %s: %s", topic, exc)

    async def _apply_live_row(
        self, session: SessionData, parser: TimingParser, topic: str, row: dict, latency: FeedLatency
    ) -> None:
        if row.get("session_key") not in (None, session.info.session_key):
            return
        endpoint = topic.rsplit("/", 1)[-1]
        if endpoint == "location":
            if parsed := parse_location(row):
                session.store.add_sample(*parsed)
                latency.add(parsed[1])
                if self.engine is not None and (delay := latency.suggested_delay(self.settings.live_delay_s)):
                    self.engine.clock.set_live_delay(delay)
        elif endpoint == "drivers":
            driver = parse_driver(row)
            if session.drivers.get(driver.number) != driver:
                session.drivers[driver.number] = driver
                await self.hub.broadcast(session.to_payload())
        else:
            for event in parser.parse(endpoint, row):
                session.events.add(event)

    async def _backfill_positions(self, session: SessionData, until: float) -> None:
        """Download positions from before we connected, so the live view can rewind."""
        try:
            await self.loader.load_positions(
                session,
                until=until,
                on_chunk=lambda: self.hub.broadcast(session.buffer_payload(complete=False)),
            )
        except OpenF1Error as exc:
            log.warning("Couldn't backfill earlier positions: %s", exc)

    async def _refresh_live_timeline(self, session: SessionData, feed_started: float) -> None:
        while True:
            await asyncio.sleep(LIVE_TIMELINE_REFRESH_S)
            session.mark_buffered(feed_started, time.time())
            session.refresh_derived()
            await self.hub.broadcast(session.to_payload())
            await self.hub.broadcast(session.buffer_payload(complete=False))
