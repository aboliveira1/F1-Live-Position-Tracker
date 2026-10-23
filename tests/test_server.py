"""Web layer: REST endpoints and the websocket protocol, against the recorded race."""

import asyncio
import dataclasses
import time

from fastapi.testclient import TestClient

from f1tracker.data.cache import JsonCache
from f1tracker.data.circuits import CircuitRepository
from f1tracker.data.openf1_client import OpenF1Client
from f1tracker.data.schedule import ScheduleService
from f1tracker.data.session_loader import SessionLoader
from f1tracker.server import app as app_module
from f1tracker.server.app import create_app
from f1tracker.server.controller import LIVE_NEEDS_ACCOUNT
from tests.helpers import UNLIMITED, fake_openf1


def make_app(settings, recorded, **kwargs):
    app = create_app(settings, autostart=False, **kwargs)
    controller = app.state.controller
    transport = fake_openf1(recorded)
    controller.client = OpenF1Client(settings, transport=transport, limiter=UNLIMITED)
    controller.circuits = CircuitRepository(settings, transport=transport)
    controller.loader = SessionLoader(controller.client, controller.circuits, persist=settings.keep_data)
    controller.schedule = ScheduleService(controller.client)
    return app


def receive_until(ws, wanted: set[str], limit: int = 2000) -> dict[str, dict]:
    seen: dict[str, dict] = {}
    for _ in range(limit):
        message = ws.receive_json()
        seen[message["type"]] = message
        if wanted <= seen.keys():
            return seen
    raise AssertionError(f"never received {wanted - seen.keys()}")


def test_status_and_seasons(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client:
        status = client.get("/api/status").json()
        assert status["mode"] == "idle" and status["live_available"] is False
        assert 2023 in client.get("/api/seasons").json()["seasons"]
        assert client.get("/").status_code == 200
        assert "trackmap" in client.get("/js/main.js").text


def test_schedule_groups_sessions_by_meeting(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client:
        data = client.get("/api/schedule?year=2026").json()
    meetings = data["meetings"]
    assert meetings[0]["name"] == "Singapore Grand Prix"
    session = meetings[0]["sessions"][0]
    assert session["status"] == "completed"
    assert session["saved"] is False  # never opened, so not on disk yet
    assert data["locked"] is False and data["error"] is None


def test_schedule_marks_sessions_saved_after_opening_them(settings, recorded):
    settings = dataclasses.replace(settings, keep_data=True)
    with TestClient(make_app(settings, recorded)) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"cmd": "load", "session_key": 11388})
        receive_until(ws, {"session"})
        data = client.get("/api/schedule?year=2026").json()
    assert data["meetings"][0]["sessions"][0]["saved"] is True
    assert data["current_session"] == 11388


def test_schedule_reports_errors_in_the_body(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client:
        data = client.get("/api/schedule?year=2019").json()  # nothing recorded for 2019
    assert data["meetings"] == [] and data["year"] == 2019


def test_websocket_replays_a_session(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client, client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["type"] == "hello"
        assert ws.receive_json()["type"] == "status"

        ws.send_json({"cmd": "load", "session_key": 11388})
        seen = receive_until(ws, {"session", "frame", "state"})
        assert seen["session"]["session"]["meeting_name"] == "Singapore Grand Prix"
        assert seen["session"]["track"]["source"] == "multiviewer"
        for _ in range(2000):  # wait until every position chunk has downloaded
            message = ws.receive_json()
            if message["type"] == "buffer" and message["complete"]:
                break

        race_start = seen["session"]["timeline"]["race_start"]
        ws.send_json({"cmd": "pause"})
        ws.send_json({"cmd": "seek", "t": race_start + 20})
        for _ in range(500):
            frame = ws.receive_json()
            if frame["type"] == "frame" and abs(frame["t"] - (race_start + 20)) < 0.01:
                break
        else:
            raise AssertionError("seek never took effect")
        assert frame["clock"]["playing"] is False
        assert {car[0] for car in frame["cars"]} == {1, 3, 16}


def test_live_without_an_account_explains_and_keeps_the_replay(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"cmd": "load", "session_key": 11388})
        receive_until(ws, {"session", "frame"})

        ws.send_json({"cmd": "live"})
        error = receive_until(ws, {"error"})["error"]
        assert error["message"] == LIVE_NEEDS_ACCOUNT
        # The replay that was showing is still running.
        assert receive_until(ws, {"frame"})["frame"]["clock"]["live"] is False
        assert client.get("/api/status").json()["mode"] == "replay"


def test_a_failed_load_keeps_the_current_session(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"cmd": "load", "session_key": 11388})
        receive_until(ws, {"session", "frame"})

        ws.send_json({"cmd": "load", "session_key": 424242})  # not on OpenF1
        assert "not found" in receive_until(ws, {"error"})["error"]["message"]
        status = client.get("/api/status").json()
        assert (status["mode"], status["session_key"]) == ("replay", 11388)
        assert receive_until(ws, {"frame"})["frame"]["clock"]["live"] is False  # still streaming


def test_concurrent_loads_leave_exactly_one_engine(settings, recorded):
    app = make_app(settings, recorded)
    controller = app.state.controller
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        for _ in range(3):
            ws.send_json({"cmd": "load", "session_key": 11388})
        receive_until(ws, {"session", "buffer"})
        engines = client.portal.call(_running_engine_tasks, controller)
        assert engines == 1


async def _running_engine_tasks(controller) -> int:
    await asyncio.sleep(0.5)  # let any racing loads settle
    return sum(
        1
        for task in asyncio.all_tasks()
        if not task.done() and "PlaybackEngine.run" in repr(task.get_coro())
    )


def test_bad_commands_do_not_break_the_socket(settings, recorded):
    with TestClient(make_app(settings, recorded)) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()
        ws.receive_json()
        ws.send_json({"cmd": "load"})  # missing session_key
        ws.send_json(["not", "a", "command"])
        ws.send_json({"cmd": "load", "session_key": 11388})
        assert receive_until(ws, {"session"})


def test_leftover_session_data_is_deleted_unless_kept(settings, recorded):
    JsonCache(settings.cache_dir / "openf1")  # an up-to-date cache folder
    leftover = settings.cache_dir / "openf1" / "sessions" / "11388" / "old.json.gz"

    def plant():
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.write_bytes(b"x" * 1000)

    plant()
    with TestClient(make_app(dataclasses.replace(settings, keep_data=True), recorded)):
        pass
    assert leftover.exists()  # kept when asked to

    with TestClient(make_app(settings, recorded)):
        assert not leftover.exists()  # removed at startup by default
        plant()
    assert not leftover.exists()  # and again at shutdown


def test_closing_the_last_tab_quits(settings, recorded, monkeypatch):
    monkeypatch.setattr(app_module, "CLOSE_GRACE_S", 0.3)
    quits = []
    app = make_app(settings, recorded, on_last_tab_closed=lambda: quits.append(time.time()))
    with TestClient(app) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
        # A reload: the page reconnects within the grace period, so nothing happens.
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
            time.sleep(0.5)
            assert quits == []
        # Closing for real.
        time.sleep(0.6)
        assert len(quits) == 1


def test_tabs_closing_does_not_quit_without_a_callback(settings, recorded, monkeypatch):
    monkeypatch.setattr(app_module, "CLOSE_GRACE_S", 0.1)
    with TestClient(make_app(settings, recorded)) as client:
        with client.websocket_connect("/ws") as ws:
            ws.receive_json()
        time.sleep(0.3)
        assert client.get("/api/status").status_code == 200
