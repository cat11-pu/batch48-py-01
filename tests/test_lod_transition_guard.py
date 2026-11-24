import pytest

import lod_transition_guard as lg

STAT_KEYS = {"selections", "transitions", "instant_switches", "completed",
             "cancelled", "forced"}


def make(**kwargs):
    return lg.LodTransitionGuard(**kwargs)


def stable(guard, entity, ratio):
    return guard.select_lod(entity, ratio)


def test_screen_ratio_selects_level_and_shadow_is_one_coarser():
    guard = make()
    assert stable(guard, "a", 0.8) == 0
    assert stable(guard, "b", 0.3) == 1
    assert stable(guard, "c", 0.1) == 2
    assert stable(guard, "d", 0.01) == 3
    for name in ("a", "b", "c", "d"):
        info = guard.transition_of(name)
        assert info["phase"] == "stable"
        assert info["from"] is None
        assert info["alpha"] == 1.0
    assert guard.draw_state("a") == [
        {"pass": "main", "level": 0, "alpha": 1.0},
        {"pass": "shadow", "level": 1, "alpha": 1.0},
    ]
    assert guard.stats()["selections"] == 4
    assert guard.stats()["transitions"] == 0


def test_hysteresis_band_keeps_the_current_level():
    guard = make()
    assert stable(guard, "e", 0.8) == 0
    assert stable(guard, "e", 0.45) == 0
    assert stable(guard, "e", 0.40) == 0
    assert guard.transition_of("e")["phase"] == "stable"
    assert guard.budget_left() == 2
    assert guard.stats()["transitions"] == 0
    assert guard.stats()["instant_switches"] == 0


def test_crossing_the_band_starts_a_budgeted_transition():
    guard = make()
    assert stable(guard, "e", 0.8) == 0
    assert stable(guard, "e", 0.39) == 1
    assert guard.transition_of("e") == {
        "phase": "transition", "level": 1, "from": 0, "to": 1,
        "alpha": 0.0, "forced": False,
    }
    assert guard.budget_left() == 1
    assert guard.stats()["transitions"] == 1


def test_transition_draws_both_levels_and_shadows_with_complementary_alpha():
    guard = make()
    stable(guard, "e", 0.8)
    stable(guard, "e", 0.39)
    assert guard.draw_state("e") == [
        {"pass": "main", "level": 0, "alpha": 1.0},
        {"pass": "main", "level": 1, "alpha": 0.0},
        {"pass": "shadow", "level": 1, "alpha": 1.0},
        {"pass": "shadow", "level": 2, "alpha": 0.0},
    ]

    state = guard.advance("e", 2)
    assert state["alpha"] == 0.5
    draws = guard.draw_state("e")
    assert [entry["pass"] for entry in draws] == ["main", "main", "shadow", "shadow"]
    assert draws[0]["alpha"] == 0.5
    assert draws[1]["alpha"] == 0.5
    assert draws[0]["alpha"] + draws[1]["alpha"] == 1.0
    assert draws[2]["alpha"] + draws[3]["alpha"] == 1.0


def test_transition_completes_at_the_declared_duration():
    guard = make()
    stable(guard, "e", 0.8)
    stable(guard, "e", 0.39)
    state = guard.advance("e", 4)
    assert state == {
        "phase": "stable", "level": 1, "from": None, "to": None,
        "alpha": 1.0, "forced": False,
    }
    assert guard.draw_state("e") == [
        {"pass": "main", "level": 1, "alpha": 1.0},
        {"pass": "shadow", "level": 2, "alpha": 1.0},
    ]
    assert guard.stats()["completed"] == 1
    guard.advance("e")
    assert guard.stats()["completed"] == 1


def test_exhausted_budget_switches_immediately():
    guard = make(transition_budget=0)
    stable(guard, "e", 0.8)
    assert stable(guard, "e", 0.39) == 1
    assert guard.transition_of("e")["phase"] == "stable"
    assert guard.budget_left() == 0
    assert guard.stats()["instant_switches"] == 1
    assert guard.stats()["transitions"] == 0


def test_transition_budget_is_shared_across_entities():
    guard = make(transition_budget=2)
    for name in ("a", "b", "c"):
        stable(guard, name, 0.8)
    assert stable(guard, "a", 0.39) == 1
    assert stable(guard, "b", 0.39) == 1
    assert stable(guard, "c", 0.39) == 1
    assert guard.transition_of("a")["phase"] == "transition"
    assert guard.transition_of("b")["phase"] == "transition"
    assert guard.transition_of("c")["phase"] == "stable"
    assert guard.budget_left() == 0
    assert guard.stats()["transitions"] == 2
    assert guard.stats()["instant_switches"] == 1


def test_shadow_offset_clamps_at_the_coarsest_level():
    guard = make(shadow_offset=2)
    stable(guard, "a", 0.8)
    stable(guard, "b", 0.1)
    stable(guard, "c", 0.01)
    assert guard.draw_state("a")[1]["level"] == 2
    assert guard.draw_state("b")[1]["level"] == 3
    assert guard.draw_state("c")[1]["level"] == 3

    plain = make()
    stable(plain, "d", 0.01)
    assert plain.draw_state("d")[1]["level"] == 3


def test_refining_hysteresis_uses_the_target_threshold():
    guard = make()
    assert stable(guard, "e", 0.1) == 2
    assert stable(guard, "e", 0.25) == 2
    assert guard.transition_of("e")["phase"] == "stable"
    assert stable(guard, "e", 0.65) == 0
    info = guard.transition_of("e")
    assert info["phase"] == "transition"
    assert info["from"] == 2
    assert info["to"] == 0
    draws = guard.draw_state("e")
    assert draws[2]["level"] == 3
    assert draws[3]["level"] == 1


def test_forced_level_skips_hysteresis_but_keeps_the_transition():
    guard = make()
    stable(guard, "e", 0.1)
    assert guard.force_lod("e", 0) == 0
    info = guard.transition_of("e")
    assert info["phase"] == "transition"
    assert info["from"] == 2
    assert info["to"] == 0
    assert info["forced"] is True
    assert guard.stats()["forced"] == 1
    assert guard.budget_left() == 1

    for bad in (4, -1):
        with pytest.raises(lg.LodError) as exc:
            guard.force_lod("e", bad)
        assert exc.value.code == "LOD_LEVEL_INVALID"
    assert guard.stats()["forced"] == 1


def test_clear_force_requires_an_active_override():
    guard = make()
    stable(guard, "e", 0.8)
    with pytest.raises(lg.LodError) as exc:
        guard.clear_force("e")
    assert exc.value.code == "LOD_NOT_FORCED"

    guard.force_lod("e", 1)
    guard.clear_force("e")
    assert guard.transition_of("e")["forced"] is False

    with pytest.raises(lg.LodError) as exc:
        guard.draw_state("ghost")
    assert exc.value.code == "LOD_UNKNOWN_ENTITY"
    with pytest.raises(lg.LodError) as exc:
        guard.transition_of("ghost")
    assert exc.value.code == "LOD_UNKNOWN_ENTITY"


def test_bad_inputs_and_constructor_validation():
    guard = make()
    for bad_ratio in (-0.1, "big"):
        with pytest.raises(lg.LodError) as exc:
            guard.select_lod("e", bad_ratio)
        assert exc.value.code == "LOD_RATIO_INVALID"

    stable(guard, "e", 0.8)
    with pytest.raises(lg.LodError) as exc:
        guard.advance("e", 0)
    assert exc.value.code == "LOD_FRAMES_INVALID"
    with pytest.raises(lg.LodError) as exc:
        guard.advance("ghost")
    assert exc.value.code == "LOD_UNKNOWN_ENTITY"

    for kwargs in ({"levels": 3}, {"transition_frames": 0},
                   {"transition_budget": -1},
                   {"thresholds": (0.5, 0.5, 0.05), "levels": 4}):
        with pytest.raises(lg.LodError) as exc:
            make(**kwargs)
        assert exc.value.code == "LOD_CONFIG_INVALID"


def test_snapshot_restore_keeps_half_done_transitions_and_budget():
    guard = make(transition_budget=4)
    stable(guard, "e1", 0.8)
    stable(guard, "e1", 0.39)
    guard.advance("e1", 2)
    stable(guard, "e2", 0.1)
    guard.force_lod("e2", 3)

    state = guard.snapshot()
    state["entities"].clear()
    state["budget"] = 99
    state["stats"]["selections"] = 0
    assert guard.budget_left() == 2
    assert guard.transition_of("e1")["alpha"] == 0.5
    assert guard.transition_of("e2")["forced"] is True

    restored = make(transition_budget=4)
    restored.restore(guard.snapshot())
    assert restored.budget_left() == 2
    first = restored.transition_of("e1")
    assert first["phase"] == "transition"
    assert first["from"] == 0
    assert first["to"] == 1
    assert first["alpha"] == 0.5
    assert restored.transition_of("e2")["forced"] is True

    assert restored.advance("e1")["alpha"] == 0.75
    done = restored.advance("e1")
    assert done["phase"] == "stable"
    assert done["level"] == 1
    assert restored.stats()["completed"] == 1


def test_corrupt_snapshot_leaves_the_transition_running():
    guard = make()
    stable(guard, "e", 0.8)
    stable(guard, "e", 0.39)
    good_stats = {key: 0 for key in STAT_KEYS}
    good_stats["transitions"] = 1
    bad_states = [
        None,
        {},
        {"version": 2, "entities": {}, "budget": 2, "stats": dict(good_stats)},
        {"version": 1, "entities": "nope", "budget": 2, "stats": dict(good_stats)},
        {"version": 1, "entities": {}, "budget": 2,
         "stats": {"selections": 2}},
    ]
    for bad in bad_states:
        with pytest.raises(lg.LodError) as exc:
            guard.restore(bad)
        assert exc.value.code == "LOD_SNAPSHOT_INVALID"

    assert guard.transition_of("e")["phase"] == "transition"
    assert guard.advance("e", 4)["level"] == 1
    assert guard.stats()["transitions"] == 1


def test_budget_reset_and_stats_reset():
    guard = make(transition_budget=1)
    stable(guard, "a", 0.8)
    stable(guard, "a", 0.39)
    stable(guard, "b", 0.8)
    stable(guard, "b", 0.39)
    assert guard.budget_left() == 0
    assert guard.stats()["instant_switches"] == 1

    assert guard.reset_budget() == 1
    assert guard.budget_left() == 1
    stable(guard, "c", 0.8)
    stable(guard, "c", 0.39)
    assert guard.stats()["transitions"] == 2
    assert guard.budget_left() == 0

    assert guard.reset_budget(0) == 0
    with pytest.raises(lg.LodError) as exc:
        guard.reset_budget(-1)
    assert exc.value.code == "LOD_BUDGET_INVALID"
    assert guard.budget_left() == 0

    guard.reset_stats()
    assert set(guard.stats()) == STAT_KEYS
    assert all(value == 0 for value in guard.stats().values())
