import pytest

from f1tracker.core.clock import PlaybackClock


class FakeWall:
    def __init__(self, now: float = 1_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_replay_plays_pauses_and_seeks():
    wall = FakeWall()
    clock = PlaybackClock(0, 100, wallclock=wall)
    assert clock.now() == 0 and not clock.playing
    clock.play()
    wall.now += 10
    assert clock.now() == pytest.approx(10)
    clock.pause()
    wall.now += 10
    assert clock.now() == pytest.approx(10)
    clock.seek(50)
    assert clock.now() == 50 and not clock.playing


def test_seek_keeps_playing_and_clamps_to_bounds():
    wall = FakeWall()
    clock = PlaybackClock(0, 100, wallclock=wall)
    clock.play()
    clock.seek(-20)
    assert clock.now() == 0 and clock.playing
    clock.seek(500)
    assert clock.now() == 100


def test_speed_scales_playback():
    wall = FakeWall()
    clock = PlaybackClock(0, 1000, wallclock=wall)
    clock.play()
    clock.set_speed(8)
    wall.now += 5
    assert clock.now() == pytest.approx(40)


def test_replay_pauses_at_the_end():
    wall = FakeWall()
    clock = PlaybackClock(0, 10, wallclock=wall)
    clock.play()
    wall.now += 60
    assert clock.now() == 10
    assert not clock.playing


def test_live_clock_follows_the_edge_and_supports_rewind():
    wall = FakeWall(5_000.0)
    clock = PlaybackClock(4_000, 6_000, live=True, live_delay=4.0, wallclock=wall)
    assert clock.following_live and clock.now() == pytest.approx(4_996)

    clock.seek(4_900)  # rewind while playing
    assert not clock.following_live and clock.playing
    wall.now += 10
    assert clock.now() == pytest.approx(4_910)

    clock.set_speed(16)  # catch up quickly ...
    wall.now += 10
    assert clock.now() == pytest.approx(clock.end)
    assert clock.following_live and clock.speed == 1.0  # ... and snap back to the edge


def test_live_pause_turns_into_dvr():
    wall = FakeWall(5_000.0)
    clock = PlaybackClock(4_000, 6_000, live=True, live_delay=2.0, wallclock=wall)
    clock.pause()
    paused_at = clock.now()
    wall.now += 30
    assert clock.now() == paused_at
    clock.go_live()
    assert clock.now() == pytest.approx(wall.now - 2.0)


def test_live_delay_changes_gradually():
    wall = FakeWall(5_000.0)
    clock = PlaybackClock(4_000, 6_000, live=True, live_delay=4.0, wallclock=wall)
    clock.set_live_delay(6.0)
    before = clock.now()
    wall.now += 1.0
    after = clock.now()
    # Session time still moves forward (just slower than real time) — no jump back.
    assert 0 < after - before < 1.0
    wall.now += 60
    clock.now()
    assert clock.live_delay == pytest.approx(6.0)


def test_live_speed_does_not_linger_after_returning_to_the_edge():
    wall = FakeWall(5_000.0)
    clock = PlaybackClock(4_000, 6_000, live=True, live_delay=4.0, wallclock=wall)
    clock.set_speed(8)  # ignored at the live edge
    assert clock.following_live and clock.speed == 1.0
    clock.seek(4_900)
    clock.set_speed(16)
    clock.seek(clock.end)  # drag back to the edge
    clock.seek(clock.now() - 60)  # then rewind a minute
    wall.now += 10
    assert clock.now() == pytest.approx(clock.end - 60, abs=0.5)  # still 60 s behind: 1x, not 16x


def test_jumps_bump_the_epoch():
    wall = FakeWall()
    clock = PlaybackClock(0, 100, wallclock=wall)
    epoch = clock.epoch
    clock.play()
    clock.set_speed(4)
    assert clock.epoch == epoch  # continuous changes keep the epoch
    clock.seek(50)
    assert clock.epoch == epoch + 1
