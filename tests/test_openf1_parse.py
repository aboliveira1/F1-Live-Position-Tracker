import math

import pytest

from f1tracker.core.models import LapCompleted, LapStarted, StintStarted
from f1tracker.data.openf1_parse import (
    TimingParser,
    parse_driver,
    parse_gmt_offset,
    parse_location,
    parse_locations,
    parse_session,
    parse_time,
    retirements,
    total_laps,
)


def test_parse_time_handles_openf1_formats():
    assert parse_time("2026-10-11T12:03:39.077000+00:00") == pytest.approx(1791720219.077)
    assert parse_time("2026-10-11T12:03:39+00:00") == pytest.approx(1791720219.0)
    assert parse_time(None) is None


@pytest.mark.parametrize("raw, seconds", [("08:00:00", 28800), ("-05:00:00", -18000), (None, 0)])
def test_gmt_offset(raw, seconds):
    assert parse_gmt_offset(raw) == seconds


def test_session_and_driver_from_recorded_rows(recorded):
    rows = recorded["openf1"]
    info = parse_session(rows["sessions"][0], rows["meetings"][0])
    assert (info.meeting_name, info.session_name, info.is_race) == ("Singapore Grand Prix", "Race", True)
    assert info.gmt_offset_s == 8 * 3600
    driver = parse_driver(next(r for r in rows["drivers"] if r["driver_number"] == 3))
    assert driver.code == "VER" and driver.team_colour.startswith("#")


def test_origin_location_means_no_position():
    row = {"date": "2026-10-11T12:00:00+00:00", "driver_number": 1, "x": 0, "y": 0, "z": 0}
    _, _, x, y = parse_location(row)
    assert math.isnan(x) and math.isnan(y)


def test_bulk_locations_are_grouped_per_driver(recorded):
    per_driver = parse_locations(recorded["openf1"]["location"])
    assert set(per_driver) == {1, 3, 16}
    t, x, _ = per_driver[3]
    assert len(t) == len(x) > 100


def test_laps_produce_start_and_completion_events():
    parser = TimingParser()
    events = parser.parse(
        "laps",
        {
            "driver_number": 3,
            "lap_number": 2,
            "date_start": "2026-10-11T12:05:21.752000+00:00",
            "lap_duration": 98.0,
            "duration_sector_1": 30.0,
            "duration_sector_2": 40.0,
            "duration_sector_3": 28.0,
            "is_pit_out_lap": False,
        },
    )
    start, done = events
    assert isinstance(start, LapStarted) and isinstance(done, LapCompleted)
    assert done.t - start.t == pytest.approx(98.0)
    assert done.sectors == (30.0, 40.0, 28.0)


def test_stints_are_placed_at_the_start_of_their_first_lap():
    parser = TimingParser()
    parser.parse("laps", {"driver_number": 44, "lap_number": 19, "date_start": "2026-10-11T12:34:30+00:00"})
    (first,) = parser.parse("stints", {"driver_number": 44, "stint_number": 1, "lap_start": 1, "compound": "MEDIUM"})
    (second,) = parser.parse("stints", {"driver_number": 44, "stint_number": 2, "lap_start": 19, "compound": "HARD"})
    assert isinstance(second, StintStarted)
    assert first.t == 0.0
    assert second.t == parse_time("2026-10-11T12:34:30+00:00")
    # Unknown lap start without a live fallback: the stint can't be placed in time.
    assert parser.parse("stints", {"driver_number": 44, "stint_number": 3, "lap_start": 40}) == []


def test_live_parser_falls_back_to_now():
    parser = TimingParser(fallback_time=lambda: 123.0)
    (stint,) = parser.parse("stints", {"driver_number": 1, "stint_number": 2, "lap_start": 30, "compound": "SOFT"})
    assert stint.t == 123.0


def test_gap_labels_are_kept_as_text():
    (event,) = TimingParser().parse(
        "intervals",
        {"date": "2026-10-11T13:00:00+00:00", "driver_number": 7, "gap_to_leader": "+1 LAP", "interval": 0.4},
    )
    assert event.gap_to_leader == "+1 LAP" and event.interval == 0.4


def test_malformed_rows_are_skipped():
    assert TimingParser().parse("position", {"driver_number": 1}) == []
    assert TimingParser().parse("unknown_endpoint", {}) == []


def test_results_give_total_laps_and_retirements(recorded):
    results = recorded["openf1"]["session_result"]
    assert total_laps(results) == 62
    events = [LapStarted(100.0, 1, 5), LapCompleted(200.0, 1, 5, 100.0, (None, None, None), False)]
    retired = retirements([{"driver_number": 1, "dnf": True}], events)
    assert [(e.driver, e.t) for e in retired] == [(1, 320.0)]
