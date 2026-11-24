import threading

import pytest

import cooldown_charge_tracker as cct


def make(**kwargs):
    settings = {"gcd_ms": 100, "haste_cap": 0.5}
    settings.update(kwargs)
    return cct.CooldownTracker(**settings)


def test_register_validates_and_reports_definition():
    tracker = make()
    assert tracker.register("dash", cooldown_ms=1000, charges=2,
                            recovery_ms=400) == {
        "skill": "dash",
        "cooldown_ms": 1000,
        "charges": 2,
        "recovery_ms": 400,
        "gcd_ms": 100,
    }
    with pytest.raises(cct.CooldownError) as exc:
        tracker.register("dash", cooldown_ms=1000)
    assert exc.value.code == "CD_DUPLICATE_SKILL"
    with pytest.raises(cct.CooldownError) as exc:
        tracker.register("zero", cooldown_ms=0)
    assert exc.value.code == "CD_INVALID_ARGUMENT"
    with pytest.raises(cct.CooldownError) as exc:
        tracker.register("none", cooldown_ms=100, charges=0)
    assert exc.value.code == "CD_INVALID_ARGUMENT"
    with pytest.raises(cct.CooldownError) as exc:
        tracker.ready("ghost", 0)
    assert exc.value.code == "CD_UNKNOWN_SKILL"


def test_use_consumes_one_charge_and_reports_timers():
    tracker = make()
    tracker.register("strike", cooldown_ms=1000, charges=1)
    out = tracker.use("strike", 0)
    assert out["skill"] == "strike"
    assert out["charges"] == 0
    assert out["recovery_at"] == 1000
    assert out["gcd_ready_at"] == 100
    assert tracker.charges("strike") == 0
    assert tracker.ready("strike", 999) is False
    assert tracker.remaining("strike", 0) == 1000


def test_use_without_charges_is_denied_without_side_effects():
    tracker = make()
    tracker.register("strike", cooldown_ms=1000, charges=1)
    tracker.use("strike", 0)
    with pytest.raises(cct.CooldownError) as exc:
        tracker.use("strike", 100)
    assert exc.value.code == "CD_NO_CHARGES"
    assert tracker.charges("strike") == 0
    assert tracker.remaining("strike", 100) == 900
    assert tracker.stats()["denied"] == 1


def test_global_cooldown_blocks_other_skills_without_resetting_cooldowns():
    tracker = make(gcd_ms=500)
    tracker.register("a", cooldown_ms=1000)
    tracker.register("b", cooldown_ms=1000)
    tracker.use("a", 0)
    assert tracker.ready("b", 100) is False
    with pytest.raises(cct.CooldownError) as exc:
        tracker.use("b", 100)
    assert exc.value.code == "CD_GLOBAL_COOLDOWN"
    assert tracker.ready("b", 500) is True
    assert tracker.remaining("a", 500) == 500
    assert tracker.use("b", 500)["gcd_ready_at"] == 1000


def test_haste_is_capped_for_new_cooldowns():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    assert tracker.apply_haste(0.8) == {"haste": 0.5, "cap": 0.5}
    tracker.use("x", 0)
    assert tracker.remaining("x", 0) == 500
    assert tracker.advance(499)["recovered"] == {}
    assert tracker.advance(500)["recovered"] == {"x": 1}
    assert tracker.charges("x") == 1


def test_haste_accumulates_but_running_cooldowns_are_not_recomputed():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    tracker.use("x", 0)
    tracker.apply_haste(0.3)
    tracker.apply_haste(0.3)
    assert tracker.apply_haste(0.1)["haste"] == 0.5
    assert tracker.remaining("x", 0) == 1000
    assert tracker.advance(1000)["recovered"] == {"x": 1}


def test_pause_freezes_timers_and_resume_shifts_deadlines():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    tracker.use("x", 0)
    assert tracker.pause(100)["paused_at"] == 100
    assert tracker.remaining("x", 5000) == 900
    assert tracker.ready("x", 5000) is False
    assert tracker.resume(400)["resumed_at"] == 400
    assert tracker.remaining("x", 400) == 900
    assert tracker.remaining("x", 1000) == 300
    assert tracker.advance(1299)["recovered"] == {}
    assert tracker.advance(1300)["recovered"] == {"x": 1}


def test_pause_state_errors_are_reported():
    tracker = make()
    tracker.register("x", cooldown_ms=100)
    with pytest.raises(cct.CooldownError) as exc:
        tracker.resume(10)
    assert exc.value.code == "CD_NOT_PAUSED"
    tracker.pause(10)
    with pytest.raises(cct.CooldownError) as exc:
        tracker.pause(20)
    assert exc.value.code == "CD_ALREADY_PAUSED"
    with pytest.raises(cct.CooldownError) as exc:
        tracker.resume(5)
    assert exc.value.code == "CD_STALE_TIME"


def test_advance_recovers_partial_charges_at_exact_times():
    tracker = make()
    tracker.register("x", cooldown_ms=1000, charges=4, recovery_ms=500)
    tracker.use("x", 0)
    tracker.use("x", 100)
    assert tracker.charges("x") == 2
    assert tracker.advance(499)["recovered"] == {}
    assert tracker.advance(500)["recovered"] == {"x": 1}
    assert tracker.charges("x") == 3
    assert tracker.advance(999)["recovered"] == {}
    assert tracker.advance(1000)["recovered"] == {"x": 1}
    assert tracker.charges("x") == 4


def test_each_use_consumes_exactly_one_layer():
    tracker = make()
    tracker.register("x", cooldown_ms=1000, charges=3, recovery_ms=2000)
    assert tracker.use("x", 0)["charges"] == 2
    assert tracker.use("x", 100)["charges"] == 1
    assert tracker.use("x", 200)["charges"] == 0
    with pytest.raises(cct.CooldownError) as exc:
        tracker.use("x", 300)
    assert exc.value.code == "CD_NO_CHARGES"
    assert tracker.charges("x") == 0


def test_backwards_time_is_rejected():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    tracker.use("x", 100)
    with pytest.raises(cct.CooldownError) as exc:
        tracker.use("x", 50)
    assert exc.value.code == "CD_STALE_TIME"
    with pytest.raises(cct.CooldownError) as exc:
        tracker.advance(99)
    assert exc.value.code == "CD_STALE_TIME"
    assert tracker.charges("x") == 0


def test_snapshot_restore_preserves_remaining_and_charges():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    tracker.use("x", 0)
    tracker.advance(300)
    state = tracker.snapshot()
    assert tracker.remaining("x", 300) == 700
    tracker.advance(1000)
    assert tracker.charges("x") == 1
    tracker.restore(state)
    assert tracker.charges("x") == 0
    assert tracker.remaining("x", 300) == 700
    assert tracker.advance(999)["recovered"] == {}
    assert tracker.advance(1000)["recovered"] == {"x": 1}


def test_invalid_snapshot_leaves_state_untouched():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    tracker.use("x", 0)
    with pytest.raises(cct.CooldownError) as exc:
        tracker.restore({"skills": "nope"})
    assert exc.value.code == "CD_SNAPSHOT_INVALID"
    assert tracker.charges("x") == 0
    assert tracker.remaining("x", 0) == 1000


def test_concurrent_use_on_last_charge_accepts_one():
    tracker = make()
    tracker.register("x", cooldown_ms=1000, charges=1)
    barrier = threading.Barrier(8)
    successes = []
    errors = []
    guard = threading.Lock()

    def press():
        barrier.wait()
        try:
            out = tracker.use("x", 0)
        except Exception as exc:  # noqa: BLE001 - test records the domain code
            with guard:
                errors.append(getattr(exc, "code", type(exc).__name__))
        else:
            with guard:
                successes.append(out)

    threads = [threading.Thread(target=press) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert len(errors) == 7
    assert set(errors) <= {"CD_NO_CHARGES", "CD_GLOBAL_COOLDOWN"}
    assert tracker.charges("x") == 0


def test_stats_counters_and_reset():
    tracker = make()
    tracker.register("x", cooldown_ms=1000)
    tracker.use("x", 0)
    with pytest.raises(cct.CooldownError) as exc:
        tracker.use("x", 100)
    assert exc.value.code == "CD_NO_CHARGES"
    tracker.advance(1000)
    assert tracker.stats() == {
        "uses": 1,
        "denied": 1,
        "recovered": 1,
        "pauses": 0,
    }
    tracker.pause(1000)
    assert tracker.stats()["pauses"] == 1
    tracker.resume(1100)
    tracker.reset_stats()
    assert tracker.stats() == {
        "uses": 0,
        "denied": 0,
        "recovered": 0,
        "pauses": 0,
    }
