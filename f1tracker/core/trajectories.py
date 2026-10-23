"""Time-indexed car position storage with interpolation.

Replays bulk-load a whole session; live feeds append samples as they arrive.
Both paths end up in the same structure, so the engine never needs to know
where the data came from.
"""

from __future__ import annotations

import math

import numpy as np

# Don't draw a straight line across a hole in the data longer than this.
MAX_INTERPOLATION_GAP_S = 10.0
# Keep showing a car at its last known spot for this long after its newest sample.
HOLD_LAST_POSITION_S = 2.5


class DriverTrack:
    """Growable, time-sorted (t, x, y) history for a single car.

    Missing positions (the feed reports 0,0 while a car is in the garage)
    are stored as NaN so interpolation naturally hides the car.
    """

    __slots__ = ("_t", "_x", "_y", "_n")

    def __init__(self, capacity: int = 4096) -> None:
        self._t = np.empty(capacity, dtype=np.float64)
        self._x = np.empty(capacity, dtype=np.float64)
        self._y = np.empty(capacity, dtype=np.float64)
        self._n = 0

    def __len__(self) -> int:
        return self._n

    @property
    def times(self) -> np.ndarray:
        return self._t[: self._n]

    @property
    def xs(self) -> np.ndarray:
        return self._x[: self._n]

    @property
    def ys(self) -> np.ndarray:
        return self._y[: self._n]

    def extend(self, t: np.ndarray, x: np.ndarray, y: np.ndarray) -> None:
        """Add samples. Fast path when they are newer than everything stored."""
        if len(t) == 0:
            return
        t = np.asarray(t, dtype=np.float64)
        x = np.asarray(x, dtype=np.float64)
        y = np.asarray(y, dtype=np.float64)
        if len(t) > 1 and np.any(np.diff(t) < 0):
            order = np.argsort(t, kind="stable")
            t, x, y = t[order], x[order], y[order]

        if self._n == 0 or t[0] > self._t[self._n - 1]:
            self._append(t, x, y)
        else:
            self._merge(t, x, y)

    def _append(self, t: np.ndarray, x: np.ndarray, y: np.ndarray) -> None:
        needed = self._n + len(t)
        if needed > len(self._t):
            capacity = max(needed, 2 * len(self._t))
            for name in ("_t", "_x", "_y"):
                grown = np.empty(capacity, dtype=np.float64)
                grown[: self._n] = getattr(self, name)[: self._n]
                setattr(self, name, grown)
        self._t[self._n : needed] = t
        self._x[self._n : needed] = x
        self._y[self._n : needed] = y
        self._n = needed

    def _merge(self, t: np.ndarray, x: np.ndarray, y: np.ndarray) -> None:
        all_t = np.concatenate([self.times, t])
        all_x = np.concatenate([self.xs, x])
        all_y = np.concatenate([self.ys, y])
        order = np.argsort(all_t, kind="stable")
        all_t, all_x, all_y = all_t[order], all_x[order], all_y[order]
        # Drop exact duplicate timestamps (e.g. overlapping downloads).
        keep = np.ones(len(all_t), dtype=bool)
        keep[1:] = np.diff(all_t) > 0
        all_t, all_x, all_y = all_t[keep], all_x[keep], all_y[keep]
        self._n = 0
        self._append(all_t, all_x, all_y)

    def position_at(self, when: float) -> tuple[float, float] | None:
        n = self._n
        if n == 0:
            return None
        t = self._t
        i = int(np.searchsorted(t[:n], when, side="right"))
        if i == 0:
            return None  # before the first sample
        prev = i - 1
        x0, y0 = self._x[prev], self._y[prev]
        if math.isnan(x0):
            return None
        if i == n:  # after the newest sample
            return (x0, y0) if when - t[prev] <= HOLD_LAST_POSITION_S else None

        gap = t[i] - t[prev]
        x1, y1 = self._x[i], self._y[i]
        if gap > MAX_INTERPOLATION_GAP_S or math.isnan(x1):
            return (x0, y0) if when - t[prev] <= HOLD_LAST_POSITION_S else None
        frac = (when - t[prev]) / gap
        return (x0 + (x1 - x0) * frac, y0 + (y1 - y0) * frac)


class TrajectoryStore:
    """Position histories for every car in a session."""

    def __init__(self) -> None:
        self._tracks: dict[int, DriverTrack] = {}
        self.sample_count = 0

    def __contains__(self, driver: int) -> bool:
        return driver in self._tracks

    @property
    def drivers(self) -> list[int]:
        return sorted(self._tracks)

    def track(self, driver: int) -> DriverTrack | None:
        return self._tracks.get(driver)

    def add_samples(self, driver: int, t, x, y) -> None:
        """Add a batch of samples for one driver (any order, may overlap)."""
        track = self._tracks.get(driver)
        if track is None:
            track = self._tracks[driver] = DriverTrack()
        track.extend(t, x, y)
        self.sample_count += len(t)

    def add_sample(self, driver: int, t: float, x: float, y: float) -> None:
        self.add_samples(driver, np.array([t]), np.array([x]), np.array([y]))

    def positions_at(self, when: float) -> list[tuple[int, float, float]]:
        """Interpolated position of every visible car at ``when``."""
        out = []
        for driver, track in self._tracks.items():
            pos = track.position_at(when)
            if pos is not None:
                out.append((driver, pos[0], pos[1]))
        return out

    def time_range(self) -> tuple[float, float] | None:
        ranges = [(tr.times[0], tr.times[-1]) for tr in self._tracks.values() if len(tr)]
        if not ranges:
            return None
        return min(r[0] for r in ranges), max(r[1] for r in ranges)
