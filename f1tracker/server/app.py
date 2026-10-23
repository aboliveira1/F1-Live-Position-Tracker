"""FastAPI application: REST endpoints, the websocket stream and the web UI."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from ..config import FIRST_SEASON, WEB_DIR, Settings
from ..data.openf1_client import OpenF1Error
from .controller import TrackerController
from .hub import ClientHub

log = logging.getLogger(__name__)

# After the last browser tab disconnects, wait this long before quitting, so a
# page reload (which briefly drops the connection) doesn't close the app.
CLOSE_GRACE_S = 6.0


def create_app(
    settings: Settings,
    *,
    startup_session: int | None = None,
    startup_live: bool = False,
    autostart: bool = True,
    on_last_tab_closed: Callable[[], None] | None = None,
) -> FastAPI:
    """Build the app. ``on_last_tab_closed`` is called once every browser tab
    showing the tracker has been closed (the launcher uses it to quit)."""
    hub = ClientHub()
    controller = TrackerController(settings, hub)
    close_watch: set[asyncio.Task] = set()

    async def quit_if_still_closed() -> None:
        await asyncio.sleep(CLOSE_GRACE_S)
        if hub.client_count == 0:
            log.info("Browser tab closed; shutting down")
            on_last_tab_closed()

    def tab_closed() -> None:
        if on_last_tab_closed is not None and hub.client_count == 0:
            task = asyncio.create_task(quit_if_still_closed())
            close_watch.add(task)
            task.add_done_callback(close_watch.discard)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if autostart:
            controller.begin_startup(session_key=startup_session, live=startup_live)
        yield
        await controller.shutdown()

    app = FastAPI(title="F1 Live Track Positions", lifespan=lifespan)
    app.state.controller = controller

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/status")
    async def status() -> dict:
        return controller.status_payload()

    @app.get("/api/seasons")
    async def seasons() -> dict:
        current = time.gmtime().tm_year
        return {"seasons": list(range(current, FIRST_SEASON - 1, -1))}

    @app.get("/api/schedule")
    async def schedule(year: int | None = None) -> dict:
        """A season's calendar plus what's on now, for the session picker.

        Errors are reported in the body (not as HTTP errors) so the picker can
        still show whatever part did load, e.g. the live session during a lockout.
        """
        year = year or time.gmtime().tm_year
        meetings, error, upcoming = [], None, None
        try:
            meetings = await controller.schedule.season(year)
        except OpenF1Error as exc:
            error = str(exc)
        try:
            upcoming = await controller.schedule.next_session()
        except OpenF1Error:
            pass
        return {
            "year": year,
            "meetings": meetings,
            "error": error,
            "next_session": upcoming,
            "locked": controller.client.locked_out,
            "keeps_data": controller.settings.keep_data,
            "live_available": controller.settings.has_openf1_credentials,
            "current_session": controller.session.info.session_key if controller.session else None,
        }

    @app.websocket("/ws")
    async def stream(websocket: WebSocket) -> None:
        await hub.connect(websocket)
        try:
            for message in controller.welcome_messages():
                await hub.send(websocket, message)
            while True:
                command = await websocket.receive_json()
                if isinstance(command, dict):
                    try:
                        await controller.handle(command)
                    except (KeyError, TypeError, ValueError) as exc:
                        log.warning("Bad command %r: %s", command, exc)
        except WebSocketDisconnect:
            pass
        finally:
            hub.disconnect(websocket)
            tab_closed()

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
