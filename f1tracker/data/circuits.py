"""Circuit layouts (outline, corners, marshal sectors).

The primary source is the MultiViewer circuit API, which is also what OpenF1
links to from its ``meetings`` endpoint. Its coordinates are in the same
system as the car positions, so they overlay directly.
"""

from __future__ import annotations

import asyncio
import logging

import httpx
import numpy as np

from .. import __version__
from ..config import Settings
from ..core.models import Corner, TrackLayout
from .cache import JsonCache

log = logging.getLogger(__name__)


def nearest_index(xs: np.ndarray, ys: np.ndarray, x: float, y: float) -> int:
    return int(np.argmin((xs - x) ** 2 + (ys - y) ** 2))


def parse_multiviewer(data: dict) -> TrackLayout:
    xs = np.asarray(data["x"], dtype=float)
    ys = np.asarray(data["y"], dtype=float)
    corners = [
        Corner(
            number=int(c["number"]),
            letter=c.get("letter") or "",
            x=float(c["trackPosition"]["x"]),
            y=float(c["trackPosition"]["y"]),
            angle=float(c.get("angle") or 0.0),
        )
        for c in data.get("corners", [])
    ]
    marshal_starts = [
        nearest_index(xs, ys, s["trackPosition"]["x"], s["trackPosition"]["y"])
        for s in data.get("marshalSectors", [])
    ]
    return TrackLayout(
        x=xs.round(1).tolist(),
        y=ys.round(1).tolist(),
        rotation=float(data.get("rotation") or 0.0),
        corners=corners,
        marshal_sector_starts=marshal_starts,
        source="multiviewer",
    )


def outline_from_samples(t: np.ndarray, x: np.ndarray, y: np.ndarray, start: float, end: float) -> TrackLayout | None:
    """Fallback outline traced from one car's positions over a single lap."""
    mask = (t >= start) & (t <= end) & ~np.isnan(x)
    if mask.sum() < 50:
        return None
    return TrackLayout(
        x=x[mask].round(1).tolist(),
        y=y[mask].round(1).tolist(),
        rotation=0.0,
        corners=[],
        marshal_sector_starts=[],
        source="car positions",
    )


class CircuitRepository:
    """Fetches and caches circuit layouts, falling back to earlier years' layouts."""

    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None):
        self._base_url = settings.multiviewer_url
        self._cache = JsonCache(settings.cache_dir / "circuits")
        self._http = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": f"f1tracker/{__version__}"},
            transport=transport,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def layout(self, circuit_key: int, year: int) -> TrackLayout | None:
        for candidate_year in (year, year - 1, year - 2):
            data = await self._fetch(circuit_key, candidate_year)
            if data and data.get("x"):
                try:
                    return parse_multiviewer(data)
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("Unusable circuit data for %s/%s (%s)", circuit_key, candidate_year, exc)
        log.warning("No circuit layout available for circuit %s", circuit_key)
        return None

    async def _fetch(self, circuit_key: int, year: int) -> dict | None:
        key = f"{circuit_key}/{year}"
        cached = await asyncio.to_thread(self._cache.get, "multiviewer", key)
        if cached is not None:
            return cached
        try:
            resp = await self._http.get(f"{self._base_url}/{circuit_key}/{year}")
        except httpx.TransportError as exc:
            log.warning("Circuit API unreachable (%s)", exc.__class__.__name__)
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None
        if not isinstance(data, dict):
            return None
        await asyncio.to_thread(self._cache.put, "multiviewer", key, data)
        return data
