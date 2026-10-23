import math

import numpy as np
import pytest

from f1tracker.core.trajectories import (
    HOLD_LAST_POSITION_S,
    MAX_INTERPOLATION_GAP_S,
    DriverTrack,
    TrajectoryStore,
)


def make_track(t, x, y=None) -> DriverTrack:
    track = DriverTrack(capacity=2)
    track.extend(np.array(t, float), np.array(x, float), np.array(y if y is not None else x, float))
    return track


def test_interpolates_linearly_between_samples():
    track = make_track([0.0, 1.0], [0.0, 100.0], [10.0, 30.0])
    assert track.position_at(0.25) == pytest.approx((25.0, 15.0))


def test_before_first_sample_is_hidden():
    assert make_track([10.0, 11.0], [1.0, 2.0]).position_at(9.0) is None


def test_holds_last_position_briefly_then_hides():
    track = make_track([0.0, 1.0], [0.0, 5.0])
    assert track.position_at(1.0 + HOLD_LAST_POSITION_S / 2) == (5.0, 5.0)
    assert track.position_at(1.0 + HOLD_LAST_POSITION_S + 0.1) is None


def test_nan_samples_hide_the_car():
    track = make_track([0.0, 1.0, 2.0], [math.nan, math.nan, 4.0])
    assert track.position_at(0.5) is None
    assert track.position_at(2.0) == (4.0, 4.0)


def test_does_not_interpolate_across_long_gaps():
    gap = MAX_INTERPOLATION_GAP_S + 5
    track = make_track([0.0, gap], [0.0, 1000.0])
    assert track.position_at(1.0) == (0.0, 0.0)  # still holding the old position
    assert track.position_at(gap / 2) is None


def test_out_of_order_batches_are_merged_and_deduplicated():
    track = make_track([5.0, 6.0, 7.0], [50.0, 60.0, 70.0])
    track.extend(np.array([1.0, 6.0, 2.0]), np.array([10.0, 60.0, 20.0]), np.array([10.0, 60.0, 20.0]))
    assert track.times.tolist() == [1.0, 2.0, 5.0, 6.0, 7.0]
    assert track.xs.tolist() == [10.0, 20.0, 50.0, 60.0, 70.0]


def test_grows_beyond_initial_capacity():
    track = DriverTrack(capacity=2)
    for i in range(100):
        track.extend(np.array([float(i)]), np.array([float(i)]), np.array([0.0]))
    assert len(track) == 100
    assert track.position_at(50.5) == pytest.approx((50.5, 0.0))


def test_store_reports_every_visible_car():
    store = TrajectoryStore()
    store.add_samples(1, [0.0, 1.0], [0.0, 10.0], [0.0, 0.0])
    store.add_samples(44, [0.0, 1.0], [5.0, 5.0], [0.0, 10.0])
    store.add_sample(16, 50.0, 1.0, 1.0)  # not on track yet at t=0.5
    positions = {n: (x, y) for n, x, y in store.positions_at(0.5)}
    assert positions == {1: pytest.approx((5.0, 0.0)), 44: pytest.approx((5.0, 5.0))}
    assert store.time_range() == (0.0, 50.0)
