import copy
import threading

import pytest

import memory_pressure_mediator as mp


def make(**kwargs):
    return mp.MemoryPressureMediator(**kwargs)


def sample(usage, tick, pressure_full=10.0, some10=1.0, procs=(), limit=None):
    body = {
        "usage": usage,
        "pressure": {"some10": some10, "full": pressure_full},
        "tick": tick,
        "procs": list(procs),
    }
    if limit is not None:
        body["limit"] = limit
    return body


def proc(pid, rss, score, tenant="t1"):
    return {"pid": pid, "rss": rss, "score": score, "tenant": tenant}


def test_register_group_reports_configuration_and_rejects_duplicates():
    guard = make(max_groups=2)
    out = guard.register_group("g1", "t1", 1000, 700)
    assert out == {"group": "g1", "tenant": "t1", "limit": 1000, "high": 700}
    assert guard.register_group("g2", "t2", 2000)["high"] == 1800
    assert guard.list_groups() == ["g1", "g2"]

    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.register_group("g1", "t1", 500, 400)
    assert exc.value.code == "MEMORY_GROUP_DUPLICATE"
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.register_group("g3", "t3", 500, 400)
    assert exc.value.code == "MEMORY_GROUP_LIMIT"

    other = make()
    with pytest.raises(mp.MemoryPressureError) as exc:
        other.register_group("", "t3", 500, 400)
    assert exc.value.code == "MEMORY_GROUP_INVALID"
    with pytest.raises(mp.MemoryPressureError) as exc:
        other.register_group("g3", "t3", 500, 700)
    assert exc.value.code == "MEMORY_THRESHOLD_INVALID"


def test_classify_steps_through_normal_elevated_and_critical():
    guard = make()
    guard.register_group("g1", "t1", 1000, 750)
    guard.observe("g1", sample(700, 1))
    assert guard.classify("g1") == "normal"
    guard.observe("g1", sample(800, 2))
    assert guard.classify("g1") == "elevated"
    guard.observe("g1", sample(1000, 3))
    assert guard.classify("g1") == "critical"


def test_growth_projection_of_crossing_is_critical_before_the_limit():
    guard = make()
    guard.register_group("g1", "t1", 1000, 500)
    guard.observe("g1", sample(600, 1))
    assert guard.classify("g1") == "elevated"
    guard.observe("g1", sample(800, 2))
    assert guard.classify("g1") == "critical"
    info = guard.pressure("g1")
    assert info["usage"] == 800
    assert info["growth"] == 200
    assert info["projected"] == 1000
    assert info["samples"] == 2


def test_pressure_indicator_raises_the_level_only_while_growing():
    guard = make()
    guard.register_group("g1", "t1", 1000, 600)
    guard.observe("g1", sample(400, 1, pressure_full=75.0))
    assert guard.classify("g1") == "normal"
    guard.observe("g1", sample(450, 2, pressure_full=75.0))
    assert guard.classify("g1") == "elevated"
    guard.observe("g1", sample(450, 3, pressure_full=75.0))
    assert guard.classify("g1") == "elevated"


def test_observe_refuses_stale_or_malformed_samples():
    guard = make()
    guard.register_group("g1", "t1", 1000, 700)
    guard.observe("g1", sample(500, 5))
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g1", sample(500, 5))
    assert exc.value.code == "MEMORY_SAMPLE_STALE"
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g1", sample(500, 4))
    assert exc.value.code == "MEMORY_SAMPLE_STALE"

    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g1", sample(-1, 6))
    assert exc.value.code == "MEMORY_SAMPLE_INVALID"
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g1", sample(500, 6, limit=0))
    assert exc.value.code == "MEMORY_SAMPLE_INVALID"
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g1", sample(500, 6, pressure_full=120.0))
    assert exc.value.code == "MEMORY_SAMPLE_INVALID"
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g1", sample(500, 6, procs=[proc(7, 10, 5), proc(7, 20, 6)]))
    assert exc.value.code == "MEMORY_SAMPLE_INVALID"
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.observe("g9", sample(500, 6))
    assert exc.value.code == "MEMORY_UNKNOWN_GROUP"


def test_throttle_target_must_sit_below_current_usage():
    guard = make()
    guard.register_group("g1", "t1", 1000, 700)
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.throttle("g1", 500)
    assert exc.value.code == "MEMORY_NO_SAMPLE"
    guard.observe("g1", sample(700, 1))
    out = guard.throttle("g1", 500)
    assert out == {"group": "g1", "target": 500, "throttled": True}
    assert guard.throttle_state("g1") == {
        "group": "g1", "throttled": True, "target": 500, "revision": 1,
    }
    assert guard.throttle("g1", 300)["target"] == 300
    assert guard.throttle_state("g1")["revision"] == 2
    for bad in (0, -5, 700, 900):
        with pytest.raises(mp.MemoryPressureError) as exc:
            guard.throttle("g1", bad)
        assert exc.value.code == "MEMORY_TARGET_INVALID"


def test_reclaim_rechecks_the_watermark_instead_of_trusting_the_throttle():
    guard = make()
    guard.register_group("g1", "t1", 1000, 700)
    guard.observe("g1", sample(900, 1))
    guard.throttle("g1", 500)
    first = guard.reclaim("g1", 100)
    assert first == {"group": "g1", "freed": 100, "usage": 800, "throttled": True}
    assert guard.group_usage("g1") == 800
    second = guard.reclaim("g1", 200)
    assert second == {"group": "g1", "freed": 200, "usage": 600, "throttled": False}
    assert guard.throttle_state("g1")["throttled"] is False
    for bad in (0, -10, 601):
        with pytest.raises(mp.MemoryPressureError) as exc:
            guard.reclaim("g1", bad)
        assert exc.value.code == "MEMORY_RECLAIM_INVALID"
    assert guard.clear_throttle("g1") == {
        "group": "g1", "throttled": False, "target": None,
    }


def test_oom_victim_follows_score_before_resident_size():
    guard = make()
    guard.register_group("g1", "t1", 4096, 3000)
    guard.observe("g1", sample(3000, 1, procs=[
        proc(11, 900, 40),
        proc(12, 300, 70),
    ]))
    victim = guard.pick_oom_victim("g1")
    assert victim == {"pid": 12, "group": "g1", "tenant": "t1", "rss": 300, "score": 70}


def test_oom_victim_stays_inside_the_group_tenant_and_is_never_repeated():
    guard = make()
    guard.register_group("g1", "t1", 4096, 3000)
    guard.observe("g1", sample(3000, 1, procs=[
        proc(21, 100, 50, tenant="t1"),
        proc(22, 900, 90, tenant="t2"),
    ]))
    assert guard.pick_oom_victim("g1")["pid"] == 21
    with pytest.raises(mp.MemoryPressureError) as exc:
        guard.pick_oom_victim("g1")
    assert exc.value.code == "MEMORY_NO_VICTIM"


def test_oom_victim_ties_break_on_size_then_descriptor():
    guard = make()
    guard.register_group("g1", "t1", 4096, 3000)
    guard.observe("g1", sample(3000, 1, procs=[
        proc(31, 400, 50),
        proc(32, 500, 50),
        proc(30, 500, 50),
    ]))
    assert guard.pick_oom_victim("g1")["pid"] == 30
    assert guard.pick_oom_victim("g1")["pid"] == 32
    assert guard.pick_oom_victim("g1")["pid"] == 31


def test_snapshot_restore_keeps_thresholds_window_and_picked_set():
    guard = make()
    guard.register_group("g1", "t1", 2000, 700)
    guard.observe("g1", sample(600, 1, procs=[proc(41, 100, 10), proc(42, 200, 20)]))
    guard.observe("g1", sample(800, 2, procs=[proc(41, 100, 10), proc(42, 200, 20)]))
    guard.throttle("g1", 500)
    assert guard.pick_oom_victim("g1")["pid"] == 42

    state = guard.snapshot()
    assert state["version"] == 1
    assert state["groups"]["g1"]["high"] == 700
    assert state["stats"]["samples"] == 2
    assert state["stats"]["picks"] == 1

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.group_usage("g1") == 800
    assert restored.pressure("g1")["samples"] == 2
    assert restored.pressure("g1")["level"] == "elevated"
    assert restored.throttle_state("g1")["target"] == 500
    assert restored.pick_oom_victim("g1")["pid"] == 41
    assert restored.stats()["samples"] == 2


def test_snapshot_is_detached_from_live_state():
    guard = make()
    guard.register_group("g1", "t1", 1000, 700)
    guard.observe("g1", sample(600, 1, procs=[proc(51, 100, 10)]))
    state = guard.snapshot()
    state["groups"]["g1"]["high"] = 100
    state["stats"]["samples"] = 999
    assert guard.pressure("g1")["high"] == 700
    assert guard.stats()["samples"] == 1
    assert guard.pick_oom_victim("g1")["pid"] == 51


def test_restore_rejects_corrupt_state_without_touching_live_groups():
    guard = make()
    guard.register_group("g1", "t1", 1000, 700)
    guard.observe("g1", sample(600, 1))
    for bad in (None, {}, {"version": 2, "groups": {}, "stats": {}},
                {"version": 1, "groups": [], "stats": {}},
                {"version": 1, "groups": {}, "stats": {"samples": "x"}}):
        with pytest.raises(mp.MemoryPressureError) as exc:
            guard.restore(copy.deepcopy(bad))
        assert exc.value.code == "MEMORY_SNAPSHOT_INVALID"
    assert guard.group_usage("g1") == 600
    assert guard.list_groups() == ["g1"]


def test_stats_count_samples_picks_and_reclaimed_bytes():
    guard = make()
    guard.register_group("g1", "t1", 1000, 700)
    guard.observe("g1", sample(900, 1, procs=[proc(61, 100, 10), proc(62, 100, 20)]))
    guard.observe("g1", sample(950, 2, procs=[proc(61, 100, 10), proc(62, 100, 20)]))
    guard.pick_oom_victim("g1")
    guard.reclaim("g1", 250)
    assert guard.stats() == {"groups": 1, "samples": 2, "picks": 1, "reclaimed": 250}
    guard.reset_stats()
    assert guard.stats() == {"groups": 1, "samples": 0, "picks": 0, "reclaimed": 0}
    guard.pick_oom_victim("g1")
    with pytest.raises(mp.MemoryPressureError):
        guard.pick_oom_victim("g1")
    assert guard.stats()["picks"] == 1


def test_concurrent_picks_and_reclaims_stay_consistent():
    guard = make()
    guard.register_group("g1", "t1", 2000, 1500)
    guard.observe("g1", sample(1000, 1, procs=[
        proc(51, 100, 10), proc(52, 100, 10), proc(53, 100, 10), proc(54, 100, 10),
    ]))
    picks = []
    frees = []
    errors = []
    lock = threading.Lock()

    def picker():
        try:
            victim = guard.pick_oom_victim("g1")
            with lock:
                picks.append(victim["pid"])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            with lock:
                errors.append(repr(exc))

    def reclaimer():
        try:
            out = guard.reclaim("g1", 100)
            with lock:
                frees.append(out["freed"])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=picker) for _ in range(4)]
    threads += [threading.Thread(target=reclaimer) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(picks) == [51, 52, 53, 54]
    assert sum(frees) == 300
    assert guard.group_usage("g1") == 700
    assert guard.stats()["picks"] == 4
