import random

from f1tracker.core.models import (
    IntervalUpdate,
    LapCompleted,
    LapStarted,
    PitStop,
    PositionUpdate,
    RaceControlMessage,
    StintStarted,
)
from f1tracker.core.race_state import (
    GREEN,
    SC,
    VSC,
    VSC_ENDING,
    RaceState,
    classify_track_status,
    status_periods,
)


def rc(t, message, category="Other", flag=None, scope=None, sector=None) -> RaceControlMessage:
    return RaceControlMessage(t, category, flag, scope, sector, None, message, None)


def test_result_does_not_depend_on_arrival_order():
    events = [
        PositionUpdate(10, 1, 2),
        PositionUpdate(20, 1, 1),
        PositionUpdate(15, 44, 1),
        PositionUpdate(25, 44, 2),
        IntervalUpdate(10, 1, 1.5, 1.5),
        IntervalUpdate(30, 1, 0.0, 0.0),
        LapStarted(5, 1, 1),
        LapStarted(95, 1, 2),
        LapCompleted(95, 1, 1, 90.0, (30.0, 30.0, 30.0), False),
        LapCompleted(185, 1, 2, 88.5, (29.0, 30.0, 29.5), False),
    ]
    in_order = RaceState([1, 44], is_race=True)
    for e in events:
        in_order.apply(e)
    shuffled = RaceState([1, 44], is_race=True)
    for e in random.Random(7).sample(events, len(events)):
        shuffled.apply(e)
    assert in_order.leaderboard(200) == shuffled.leaderboard(200)
    leader = in_order.leaderboard(200)[0]
    assert (leader["driver"], leader["lap"], leader["last_lap"], leader["best_lap"]) == (1, 2, 88.5, 88.5)


def test_track_status_follows_safety_car_messages():
    state = RaceState([], is_race=True)
    state.apply(rc(1, "VSC DEPLOYED", "SafetyCar"))
    assert state.track_status == VSC
    state.apply(rc(2, "VSC ENDING", "SafetyCar"))
    assert state.track_status == VSC_ENDING
    state.apply(rc(3, "TRACK CLEAR", "Flag", "CLEAR", "Track"))
    assert state.track_status == GREEN
    state.apply(rc(4, "SAFETY CAR DEPLOYED", "SafetyCar"))
    assert state.track_status == SC


def test_sector_yellows_clear_individually_or_with_the_track():
    state = RaceState([], is_race=True)
    state.apply(rc(1, "YELLOW IN TRACK SECTOR 4", "Flag", "YELLOW", "Sector", 4))
    state.apply(rc(2, "DOUBLE YELLOW IN TRACK SECTOR 9", "Flag", "DOUBLE YELLOW", "Sector", 9))
    assert state.yellow_sectors() == [4, 9]
    state.apply(rc(3, "CLEAR IN TRACK SECTOR 4", "Flag", "CLEAR", "Sector", 4))
    assert state.yellow_sectors() == [9]
    state.apply(rc(4, "TRACK CLEAR", "Flag", "CLEAR", "Track"))
    assert state.yellow_sectors() == []


def test_classify_ignores_unrelated_messages():
    assert classify_track_status(rc(1, "CAR 1 TIME DELETED")) is None
    assert classify_track_status(rc(1, "BLUE FLAG", "Flag", "BLUE", "Driver")) is None


def test_status_periods_merge_ending_phase_into_the_period():
    messages = [
        rc(100, "VSC DEPLOYED", "SafetyCar"),
        rc(150, "VSC ENDING", "SafetyCar"),
        rc(160, "TRACK CLEAR", "Flag", "CLEAR", "Track"),
        rc(300, "SAFETY CAR DEPLOYED", "SafetyCar"),
    ]
    assert status_periods(messages, end=400) == [(100, 160, "VSC"), (300, 400, "SC")]


def test_tyre_age_and_pit_window():
    state = RaceState([16], is_race=True)
    state.apply(StintStarted(0, 16, 1, "MEDIUM", 1, 3))
    state.apply(LapStarted(100, 16, 5))
    state.apply(PitStop(500, 16, 18, 24.0, 2.4))
    state.apply(StintStarted(524, 16, 2, "HARD", 19, 0))
    state.apply(LapStarted(600, 16, 20))
    row = state.leaderboard(510)[0]
    assert (row["compound"], row["tyre_age"], row["pit_stops"], row["in_pit"]) == ("HARD", 1, 1, True)
    assert state.leaderboard(530)[0]["in_pit"] is False


def test_feed_excludes_blue_flags_and_is_newest_first():
    state = RaceState([], is_race=True)
    state.apply(rc(1, "GREEN LIGHT - PIT EXIT OPEN", "Flag", "GREEN", "Track"))
    state.apply(rc(2, "WAVED BLUE FLAG FOR CAR 18", "Flag", "BLUE", "Driver"))
    state.apply(rc(3, "RACE START"))
    feed = state.snapshot(10)["race_control"]
    assert [m["message"] for m in feed] == ["RACE START", "GREEN LIGHT - PIT EXIT OPEN"]


def test_unknown_drivers_are_added_on_the_fly():
    state = RaceState([1], is_race=False)
    state.apply(PositionUpdate(1, 99, 1))
    assert [row["driver"] for row in state.leaderboard(2)] == [99, 1]


def test_reset_clears_everything():
    state = RaceState([1], is_race=True)
    state.apply(PositionUpdate(1, 1, 3))
    state.apply(rc(1, "RED FLAG", "Flag", "RED", "Track"))
    state.reset()
    assert state.leaderboard(2)[0]["position"] is None
    assert state.track_status == "NONE"
