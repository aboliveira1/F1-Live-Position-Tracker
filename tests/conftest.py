"""Shared fixtures: a recorded slice of the 2026 Singapore GP served by a fake OpenF1."""

from __future__ import annotations

import pytest

from f1tracker.config import Settings
from tests.helpers import fake_openf1, load_recording


@pytest.fixture(scope="session")
def recorded() -> dict:
    return load_recording()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(cache_dir=tmp_path / "cache", open_browser=False)


@pytest.fixture
def openf1_transport(recorded):
    return fake_openf1(recorded)
