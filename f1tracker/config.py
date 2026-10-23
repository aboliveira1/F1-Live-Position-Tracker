"""Application settings, read from environment variables and an optional ``.env`` file."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB_DIR = Path(__file__).resolve().parent / "web"
FIRST_SEASON = 2023  # OpenF1 coverage starts here


def _load_dotenv(path: Path) -> None:
    """Minimal ``.env`` reader: KEY=VALUE lines, ``#`` comments, optional quotes.

    Values already present in the real environment take precedence.
    """
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), value)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8050
    open_browser: bool = True

    cache_dir: Path = PROJECT_ROOT / ".cache"
    # Keep downloaded session data (positions, timing) on disk between runs.
    # Off by default: data lives in memory while you watch and is never
    # written out. Turning it on makes reopening a session instant and lets
    # saved sessions play while OpenF1 is locked during live sessions.
    keep_data: bool = False

    openf1_base_url: str = "https://api.openf1.org/v1"
    openf1_token_url: str = "https://api.openf1.org/token"
    openf1_mqtt_host: str = "mqtt.openf1.org"
    openf1_mqtt_port: int = 8883
    openf1_username: str | None = None
    openf1_password: str | None = None

    multiviewer_url: str = "https://api.multiviewer.app/api/v1/circuits"

    # How often the engine pushes car positions to the browser.
    tick_hz: float = 20.0
    # How far behind "now" the live view renders. Feed messages arrive with
    # jitter; rendering slightly in the past lets us interpolate smoothly.
    live_delay_s: float = 4.0

    @property
    def has_openf1_credentials(self) -> bool:
        return bool(self.openf1_username and self.openf1_password)


def load_settings(**overrides) -> Settings:
    _load_dotenv(PROJECT_ROOT / ".env")
    values = dict(
        openf1_username=os.environ.get("OPENF1_USERNAME") or None,
        openf1_password=os.environ.get("OPENF1_PASSWORD") or None,
        live_delay_s=_env_float("F1TRACKER_LIVE_DELAY", Settings.live_delay_s),
        tick_hz=_env_float("F1TRACKER_TICK_HZ", Settings.tick_hz),
        keep_data=os.environ.get("F1TRACKER_KEEP_DATA", "").strip().lower() in ("1", "true", "yes"),
    )
    if port := os.environ.get("F1TRACKER_PORT"):
        values["port"] = int(port)
    values.update({k: v for k, v in overrides.items() if v is not None})
    return Settings(**values)
