import threading

import pytest

import ducking_state_guard as dsg


def guard(**kwargs):
    return dsg.DuckingStateGuard(**kwargs)


def staged(**kwargs):
    g = guard(**kwargs)
    g.add_source("host", priority=1)
    return g


def test_new_guard_starts_idle_at_unity_gain():
    g = guard()
    assert g.gain() == 1.0
    assert g.state() == "idle"
    assert g.dominant_source() is None
    assert g.bypassed() is False
    assert g.stats()["advances"] == 0
    assert g.stats()["rejected"] == 0


def test_frames_for_converts_milliseconds_to_sample_counts():
    g = guard()
    assert g.frames_for(20) == 960
    assert g.frames_for(300) == 14400
    assert g.frames_for(0) == 0
    assert g.frames_for(800) == 38400
    assert guard(sample_rate=44100).frames_for(25) == 1103


def test_frames_for_rejects_negative_and_fractional_milliseconds():
    g = guard()
    for bad in (-1, -250, 12.5):
        with pytest.raises(dsg.DuckingError) as exc:
            g.frames_for(bad)
        assert exc.value.code == "DUCK_INVALID_TIMING"
    assert g.stats()["rejected"] == 3


def test_add_source_reports_defaults_and_rejects_bad_arguments():
    g = guard()
    out = g.add_source("host", priority=5)
    assert out == {
        "source_id": "host", "priority": 5, "depth": 0.35, "active": False,
    }
    deeper = g.add_source("sfx", depth=0.8)
    assert deeper["depth"] == 0.8
    with pytest.raises(dsg.DuckingError) as exc:
        g.add_source("host")
    assert exc.value.code == "DUCK_DUPLICATE_SOURCE"
    with pytest.raises(dsg.DuckingError) as exc:
        g.add_source("bad-depth", depth=1.0)
    assert exc.value.code == "DUCK_INVALID_DEPTH"
    with pytest.raises(dsg.DuckingError) as exc:
        g.add_source("bad-priority", priority=-2)
    assert exc.value.code == "DUCK_INVALID_PRIORITY"
    assert g.stats()["sources_added"] == 2


def test_attack_reaches_target_depth_then_holds():
    g = staged()
    g.feed("host", True)
    assert g.state() == "attack"
    mid = g.advance(g.frames_for(10))
    assert 0.65 < mid < 1.0
    assert g.advance(g.frames_for(10)) == 0.65
    assert g.state() == "hold"
    assert g.dominant_source() == "host"


def test_release_waits_out_hold_then_returns_to_unity():
    g = staged()
    g.feed("host", True)
    g.advance(g.frames_for(20) + g.frames_for(300))
    assert g.state() == "hold"
    g.feed("host", False)
    g.advance(g.frames_for(300) - 1)
    assert g.state() == "hold"
    assert g.gain() == 0.65
    g.advance(1)
    assert g.state() == "release"
    assert g.gain() == 0.65
    g.advance(g.frames_for(800))
    assert g.gain() == 1.0
    assert g.state() == "idle"


def test_gain_falls_during_attack_and_rises_during_release():
    g = staged()
    g.feed("host", True)
    falling = [g.advance(96) for _ in range(10)]
    assert falling == sorted(falling, reverse=True)
    assert falling[-1] == 0.65
    g.feed("host", False)
    g.advance(g.frames_for(300))
    rising = [g.advance(3840) for _ in range(10)]
    assert rising == sorted(rising)
    assert rising[-1] == 1.0


def test_remove_source_forgets_it_and_unknown_handles_are_rejected():
    g = staged()
    g.add_source("sfx")
    g.remove_source("sfx")
    with pytest.raises(dsg.DuckingError) as exc:
        g.feed("sfx", True)
    assert exc.value.code == "DUCK_UNKNOWN_SOURCE"
    with pytest.raises(dsg.DuckingError) as exc:
        g.remove_source("ghost")
    assert exc.value.code == "DUCK_UNKNOWN_SOURCE"
    assert g.stats()["sources_removed"] == 1
    assert [s["source_id"] for s in g.snapshot()["sources"]] == ["host"]


def test_bypass_forces_unity_and_freezes_progress():
    g = staged()
    g.feed("host", True)
    g.advance(200)
    frozen = g.gain()
    assert g.state() == "attack"
    g.set_bypass(True)
    assert g.gain() == 1.0
    assert g.state() == "bypass"
    assert g.advance(50000) == 1.0
    g.set_bypass(False)
    assert g.state() == "attack"
    assert g.gain() == frozen
    assert g.stats()["bypass_toggles"] == 2


def test_advance_rejects_negative_frames_while_zero_is_a_noop():
    g = staged()
    g.feed("host", True)
    before = g.gain()
    assert g.advance(0) == before
    with pytest.raises(dsg.DuckingError) as exc:
        g.advance(-10)
    assert exc.value.code == "DUCK_INVALID_TIMING"
    assert g.gain() == before


def test_snapshot_restore_resumes_the_same_ramp():
    g = staged()
    g.feed("host", True)
    g.advance(300)
    snap = g.snapshot()
    assert snap["state"] == "attack"
    assert 0.65 < snap["gain"] < 1.0
    assert snap["sources"][0]["active"] is True
    continued = [g.advance(60) for _ in range(3)]
    g.restore(snap)
    assert g.state() == "attack"
    replay = [g.advance(60) for _ in range(3)]
    assert replay == continued


def test_restore_rejects_malformed_snapshot_and_keeps_state():
    g = staged()
    g.feed("host", True)
    g.advance(100)
    before_gain = g.gain()
    before_state = g.state()
    with pytest.raises(dsg.DuckingError) as exc:
        g.restore({"version": 1, "state": "selftest"})
    assert exc.value.code == "DUCK_SNAPSHOT_INVALID"
    assert g.gain() == before_gain
    assert g.state() == before_state


def test_stats_track_activity_and_reset_keeps_running_state():
    g = staged()
    g.feed("host", True)
    g.advance(100)
    g.set_bypass(True)
    g.set_bypass(False)
    g.feed("host", False)
    s = g.stats()
    assert s["feeds"] == 2
    assert s["advances"] == 1
    assert s["bypass_toggles"] == 2
    assert s["sources_added"] == 1
    running = g.gain()
    g.reset_stats()
    fresh = g.stats()
    assert [fresh[k] for k in dsg.STAT_KEYS] == [0, 0, 0, 0, 0, 0]
    assert g.gain() == running


def test_repeated_activation_does_not_restart_attack():
    g = staged()
    g.feed("host", True)
    g.advance(480)
    g.feed("host", True)
    assert g.state() == "attack"
    assert g.advance(480) == 0.65
    assert g.stats()["feeds"] == 2


def test_concurrent_feed_and_advance_keep_counters_consistent():
    g = guard()
    names = ["s0", "s1", "s2", "s3"]
    for name in names:
        g.add_source(name)

    def worker(name):
        try:
            for _ in range(25):
                g.feed(name, True)
                g.advance(10)
        except Exception as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    errors = []
    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    s = g.stats()
    assert s["feeds"] == 100
    assert s["advances"] == 100
    assert s["sources_added"] == 4
    assert 0.01 <= g.gain() <= 1.0
    assert g.state() in dsg.DUCK_STATES
