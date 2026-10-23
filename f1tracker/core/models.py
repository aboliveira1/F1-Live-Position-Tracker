"""Domain model shared by every data source, the playback engine and the web layer.

Conventions
-----------
* All timestamps are UTC epoch seconds (``float``).
* Track coordinates are in the F1 timing system's native units (decimetres),
  so car positions and the circuit outline line up without conversion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Union

# A gap can be a number of seconds, a label such as "+1 LAP", or unknown.
Gap = Union[float, str, None]


# --------------------------------------------------------------------------- #
# Static session metadata
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class SessionInfo:
    session_key: int
    meeting_key: int
    year: int
    session_name: str  # "Race", "Sprint", "Qualifying", "Practice 1", ...
    session_type: str  # "Race", "Qualifying", "Practice"
    meeting_name: str  # "Singapore Grand Prix"
    circuit_key: int
    circuit_name: str
    country_name: str
    country_code: str
    country_flag_url: str | None
    location: str
    date_start: float
    date_end: float
    gmt_offset_s: int

    @property
    def is_race(self) -> bool:
        """True for Grands Prix and Sprints (both have session_type "Race")."""
        return self.session_type == "Race"

    def to_dict(self) -> dict:
        return {**asdict(self), "is_race": self.is_race}


@dataclass(frozen=True, slots=True)
class Driver:
    number: int
    code: str  # three-letter abbreviation, e.g. "VER"
    first_name: str
    last_name: str
    full_name: str
    team_name: str
    team_colour: str  # "#RRGGBB"
    headshot_url: str | None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Corner:
    number: int
    letter: str
    x: float
    y: float
    angle: float  # degrees; direction in which to place the corner label


@dataclass(frozen=True, slots=True)
class TrackLayout:
    x: list[float]
    y: list[float]
    rotation: float  # degrees to rotate the map for its canonical orientation
    corners: list[Corner]
    # Index into (x, y) at which each marshal sector starts, in sector order.
    marshal_sector_starts: list[int]
    source: str  # where the outline came from, e.g. "multiviewer"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Time-stamped timing events
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, slots=True)
class PositionUpdate:
    t: float
    driver: int
    position: int


@dataclass(frozen=True, slots=True)
class IntervalUpdate:
    t: float
    driver: int
    gap_to_leader: Gap
    interval: Gap


@dataclass(frozen=True, slots=True)
class LapStarted:
    t: float
    driver: int
    lap: int


@dataclass(frozen=True, slots=True)
class LapCompleted:
    t: float
    driver: int
    lap: int
    duration: float
    sectors: tuple[float | None, float | None, float | None]
    pit_out_lap: bool


@dataclass(frozen=True, slots=True)
class StintStarted:
    t: float
    driver: int
    stint: int
    compound: str | None
    lap_start: int
    tyre_age_at_start: int


@dataclass(frozen=True, slots=True)
class PitStop:
    t: float  # pit-lane entry
    driver: int
    lap: int | None
    lane_duration: float | None  # pit entry to pit exit
    stop_duration: float | None  # stationary time


@dataclass(frozen=True, slots=True)
class RaceControlMessage:
    t: float
    category: str
    flag: str | None
    scope: str | None
    sector: int | None
    driver: int | None
    message: str
    lap: int | None


@dataclass(frozen=True, slots=True)
class WeatherUpdate:
    t: float
    air_temp: float | None
    track_temp: float | None
    humidity: float | None
    pressure: float | None
    rainfall: bool
    wind_speed: float | None
    wind_direction: float | None


@dataclass(frozen=True, slots=True)
class DriverRetired:
    t: float
    driver: int


TimingEvent = Union[
    PositionUpdate,
    IntervalUpdate,
    LapStarted,
    LapCompleted,
    StintStarted,
    PitStop,
    RaceControlMessage,
    WeatherUpdate,
    DriverRetired,
]
