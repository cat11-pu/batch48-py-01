import threading

import pytest

import crowd_avoidance_scheduler as cas


def make(**kwargs):
    settings = {
        "neighbor_budget": 32,
        "cell_size": 2.0,
        "stuck_epsilon": 0.01,
    }
    settings.update(kwargs)
    return cas.AvoidanceScheduler(**settings)


def test_register_validates_radius_position_and_priority():
    scheduler = make()
    assert scheduler.register(
        "a", radius=0.5, priority=3, position=(1.0, 2.0),
        goal=(5.0, 2.0), max_speed=2.0,
    ) == {
        "unit": "a",
        "radius": 0.5,
        "priority": 3,
        "position": (1.0, 2.0),
        "goal": (5.0, 2.0),
        "max_speed": 2.0,
    }
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.register("a")
    assert exc.value.code == "AV_DUPLICATE_UNIT"
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.register("zero", radius=0.0)
    assert exc.value.code == "AV_INVALID_UNIT"
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.register("bad", position=(float("nan"), 0.0))
    assert exc.value.code == "AV_INVALID_POSITION"
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.register("floaty", priority=1.5)
    assert exc.value.code == "AV_INVALID_UNIT"


def test_state_and_set_goal_report_current_configuration():
    scheduler = make()
    scheduler.register("a", radius=0.4, priority=2, position=(1.0, 1.0))
    assert scheduler.state("a") == {
        "unit": "a",
        "radius": 0.4,
        "priority": 2,
        "position": (1.0, 1.0),
        "goal": None,
        "max_speed": 1.0,
    }
    assert scheduler.set_goal("a", (4.0, 1.0))["goal"] == (4.0, 1.0)
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.set_goal("ghost", (0.0, 0.0))
    assert exc.value.code == "AV_UNKNOWN_UNIT"
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.state("ghost")
    assert exc.value.code == "AV_UNKNOWN_UNIT"


def test_neighbors_filters_by_radius_and_sorts_by_distance():
    scheduler = make()
    scheduler.register("a", position=(0.0, 0.0))
    scheduler.register("far", position=(3.5, 0.0))
    scheduler.register("near", position=(1.0, 0.0))
    assert scheduler.neighbors("a", 3.0) == ["near"]
    assert scheduler.neighbors("a", 4.0) == ["near", "far"]


def test_tick_rejects_non_positive_time_steps():
    scheduler = make()
    scheduler.register("a")
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.tick(dt_ms=0)
    assert exc.value.code == "AV_INVALID_DT"
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.tick(dt_ms=-16)
    assert exc.value.code == "AV_INVALID_DT"


def test_unit_moves_toward_goal_within_speed_budget():
    scheduler = make()
    scheduler.register("a", position=(0.0, 0.0), goal=(10.0, 0.0),
                       max_speed=1.0)
    report = scheduler.tick(dt_ms=100)
    assert report["frame"] == 1
    assert report["moved"]["a"] == pytest.approx([0.1, 0.0])
    assert scheduler.position("a") == pytest.approx((0.1, 0.0))


def test_unit_without_goal_stays_in_place():
    scheduler = make()
    scheduler.register("a", position=(1.0, 1.0))
    report = scheduler.tick(dt_ms=100)
    assert report["moved"]["a"] == [0.0, 0.0]
    assert scheduler.position("a") == (1.0, 1.0)


def test_high_priority_unit_is_not_displaced():
    scheduler = make()
    scheduler.register("elite", priority=9, position=(0.0, 0.0))
    scheduler.register("grunt", priority=1, position=(0.9, 0.0))
    scheduler.tick(dt_ms=16)
    assert scheduler.position("elite") == (0.0, 0.0)
    assert scheduler.position("grunt")[0] > 0.95


def test_overlap_uses_sum_of_radii():
    scheduler = make()
    scheduler.register("a", radius=0.5, priority=1, position=(0.0, 0.0))
    scheduler.register("b", radius=0.5, priority=0, position=(0.9, 0.0))
    scheduler.tick(dt_ms=16)
    ax, ay = scheduler.position("a")
    bx, by = scheduler.position("b")
    distance = ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5
    assert distance >= 0.99
    assert distance <= 1.01


def test_stuck_detects_missing_displacement_over_window():
    scheduler = make()
    scheduler.register("a", position=(0.0, 0.0))
    for _ in range(5):
        scheduler.tick(dt_ms=10)
    assert scheduler.stuck("a", 5) is True


def test_moving_unit_is_not_stuck_and_short_history_is_unknown():
    scheduler = make()
    scheduler.register("a", position=(0.0, 0.0), goal=(10.0, 0.0),
                       max_speed=1.0)
    scheduler.tick(dt_ms=10)
    assert scheduler.stuck("a", 5) is False
    for _ in range(5):
        scheduler.tick(dt_ms=10)
    assert scheduler.stuck("a", 5) is False


def test_zero_budget_evaluates_no_pairs():
    scheduler = make()
    scheduler.register("a", priority=1, position=(0.0, 0.0))
    scheduler.register("b", priority=0, position=(0.9, 0.0))
    report = scheduler.tick(dt_ms=16, budget=0)
    assert report["pairs_evaluated"] == 0
    assert scheduler.position("a") == (0.0, 0.0)
    assert scheduler.position("b") == (0.9, 0.0)


def test_unregister_removes_unit_from_all_paths():
    scheduler = make()
    scheduler.register("a")
    scheduler.register("b", position=(1.0, 0.0))
    assert scheduler.unregister("a") == {"unit": "a", "removed": True}
    assert scheduler.neighbors("b", 10.0) == []
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.tick(units=["a"])
    assert exc.value.code == "AV_UNKNOWN_UNIT"
    assert scheduler.unregister("a") == {"unit": "a", "removed": False}


def test_snapshot_restore_resumes_frame_and_positions():
    scheduler = make()
    scheduler.register("a", position=(0.0, 0.0), goal=(10.0, 0.0),
                       max_speed=1.0)
    scheduler.tick(dt_ms=100)
    scheduler.tick(dt_ms=100)
    position_before = scheduler.position("a")
    state = scheduler.snapshot()
    scheduler.tick(dt_ms=100)
    assert scheduler.position("a") != position_before
    scheduler.restore(state)
    assert scheduler.position("a") == position_before
    report = scheduler.tick(dt_ms=100)
    assert report["frame"] == 3
    assert scheduler.position("a") == pytest.approx((0.3, 0.0))


def test_invalid_snapshot_leaves_state_untouched():
    scheduler = make()
    scheduler.register("a", position=(2.0, 1.0))
    with pytest.raises(cas.CrowdError) as exc:
        scheduler.restore({"units": 9})
    assert exc.value.code == "AV_SNAPSHOT_INVALID"
    assert scheduler.position("a") == (2.0, 1.0)
    assert scheduler.neighbors("a", 10.0) == []


def test_concurrent_ticks_serialize_without_lost_frames():
    scheduler = make()
    scheduler.register("a", position=(0.0, 0.0), goal=(10.0, 0.0),
                       max_speed=1.0)
    barrier = threading.Barrier(10)
    frames = []
    errors = []
    guard = threading.Lock()

    def frame():
        barrier.wait()
        try:
            report = scheduler.tick(dt_ms=10)
        except Exception as exc:  # noqa: BLE001 - test records the domain code
            with guard:
                errors.append(getattr(exc, "code", type(exc).__name__))
        else:
            with guard:
                frames.append(report["frame"])

    threads = [threading.Thread(target=frame) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert sorted(frames) == list(range(1, 11))
    assert scheduler.position("a") == pytest.approx((0.1, 0.0))
