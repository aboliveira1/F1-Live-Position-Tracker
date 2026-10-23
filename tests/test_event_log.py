from f1tracker.core.event_log import EventLog
from f1tracker.core.models import PositionUpdate


def ev(t: float, pos: int = 1) -> PositionUpdate:
    return PositionUpdate(t, 1, pos)


def test_drains_events_in_time_order():
    log = EventLog()
    log.extend([ev(3), ev(1), ev(2)])
    assert [e.t for e in log.drain_until(2)] == [1, 2]
    assert [e.t for e in log.drain_until(10)] == [3]
    assert log.drain_until(10) == []


def test_late_events_are_not_lost():
    log = EventLog()
    log.extend([ev(1), ev(5)])
    log.drain_until(6)
    log.add(ev(3))  # arrives after the cursor has passed t=3
    assert [e.t for e in log.drain_until(6)] == [3]


def test_rewind_replays_everything():
    log = EventLog()
    log.extend([ev(1), ev(2)])
    log.drain_until(5)
    log.rewind()
    assert [e.t for e in log.drain_until(1.5)] == [1]
