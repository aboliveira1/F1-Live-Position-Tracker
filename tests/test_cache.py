from f1tracker.data.cache import LAYOUT_VERSION, JsonCache


def test_round_trip_and_clear(tmp_path):
    cache = JsonCache(tmp_path / "c")
    cache.put("sessions/1", "k", [1, 2])
    assert cache.get("sessions/1", "k") == [1, 2]
    assert cache.has("sessions/1", "k")
    assert cache.clear("sessions") > 0
    assert cache.get("sessions/1", "k") is None


def test_caches_in_an_old_layout_are_discarded_once(tmp_path):
    root = tmp_path / "c"
    (root / "rest").mkdir(parents=True)
    (root / "rest" / "old.json.gz").write_bytes(b"old")
    JsonCache(root)
    assert not (root / "rest").exists()
    assert (root / "LAYOUT").read_text() == LAYOUT_VERSION

    cache = JsonCache(root)
    cache.put("calendar", "k", {"a": 1})
    JsonCache(root)  # same layout: nothing is wiped
    assert cache.get("calendar", "k") == {"a": 1}
