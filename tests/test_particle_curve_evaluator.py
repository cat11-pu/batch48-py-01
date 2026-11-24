import copy
import threading

import pytest

import particle_curve_evaluator as pc


def linear_keys():
    return [
        {"time": 0.0, "value": 0.0, "mode": "linear"},
        {"time": 2.0, "value": 10.0, "mode": "linear"},
    ]


def step_keys():
    return [
        {"time": 0.0, "value": 10.0, "mode": "step"},
        {"time": 2.0, "value": 20.0, "mode": "step"},
    ]


def test_set_curve_and_eval_linear_between_keys():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("spark", linear_keys())
    assert evaluator.eval("spark", 0.5) == pytest.approx(2.5)
    assert evaluator.eval("spark", 1.5) == pytest.approx(7.5)
    assert evaluator.eval("spark", 0.0) == pytest.approx(0.0)


def test_eval_clamps_outside_key_range():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("spark", linear_keys())
    assert evaluator.eval("spark", -3.0) == pytest.approx(0.0)
    assert evaluator.eval("spark", 9.0) == pytest.approx(10.0)


def test_step_mode_holds_left_value_inside_segment():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("burst", step_keys())
    assert evaluator.eval("burst", 0.5) == pytest.approx(10.0)
    assert evaluator.eval("burst", 1.999) == pytest.approx(10.0)
    assert evaluator.eval("burst", 5.0) == pytest.approx(20.0)


def test_invalid_key_layout_rejected():
    evaluator = pc.ParticleCurveEvaluator()
    bad_layouts = [
        [],
        [{"time": 2.0, "value": 1.0, "mode": "linear"},
         {"time": 1.0, "value": 2.0, "mode": "linear"}],
        [{"time": 1.0, "value": 1.0, "mode": "linear"},
         {"time": 1.0, "value": 2.0, "mode": "linear"}],
        [{"time": 0.0, "value": 1.0, "mode": "ease"}],
    ]
    for layout in bad_layouts:
        with pytest.raises(pc.ParticleError) as exc:
            evaluator.set_curve("spark", layout)
        assert exc.value.code == "PARTICLE_KEY_INVALID"
    assert evaluator.list_modules() == []


def test_key_and_module_limits():
    evaluator = pc.ParticleCurveEvaluator()
    keys = [{"time": float(index), "value": 0.0, "mode": "linear"} for index in range(257)]
    with pytest.raises(pc.ParticleError) as exc:
        evaluator.set_curve("too-many", keys)
    assert exc.value.code == "PARTICLE_KEY_LIMIT"

    for index in range(64):
        evaluator.set_curve(f"m{index}", linear_keys())
    assert len(evaluator.list_modules()) == 64
    with pytest.raises(pc.ParticleError) as exc:
        evaluator.set_curve("m64", linear_keys())
    assert exc.value.code == "PARTICLE_MODULE_LIMIT"

    evaluator.set_curve("m0", step_keys())
    assert evaluator.eval("m0", 0.5) == pytest.approx(10.0)


def test_unknown_module_rejected():
    evaluator = pc.ParticleCurveEvaluator()
    for call in (lambda: evaluator.eval("ghost", 0.0),
                 lambda: evaluator.curve_info("ghost")):
        with pytest.raises(pc.ParticleError) as exc:
            call()
        assert exc.value.code == "PARTICLE_UNKNOWN_MODULE"
    assert evaluator.remove_curve("ghost") is False


def test_curve_info_and_list_modules():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("zeta", linear_keys())
    evaluator.set_curve("alpha", step_keys())
    assert evaluator.list_modules() == ["alpha", "zeta"]

    info = evaluator.curve_info("zeta")
    assert info["module_id"] == "zeta"
    assert info["key_count"] == 2
    assert info["start"] == 0.0
    assert info["end"] == 2.0


def test_remove_curve():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("spark", linear_keys())
    assert evaluator.remove_curve("spark") is True
    assert evaluator.list_modules() == []
    with pytest.raises(pc.ParticleError) as exc:
        evaluator.eval("spark", 1.0)
    assert exc.value.code == "PARTICLE_UNKNOWN_MODULE"


def test_emission_budget_carries_fraction_remainder():
    evaluator = pc.ParticleCurveEvaluator()
    frames = [evaluator.emission_budget("thruster", 1.5, 100) for _ in range(21)]
    assert frames[:7] == [0, 0, 0, 0, 0, 0, 1]
    assert sum(frames) == 3

    state = evaluator.budget_state("thruster")
    assert state["emitted"] == 3
    assert state["dropped"] == 0
    assert 0.0 <= state["remainder"] < 1.0


def test_emission_budget_is_frame_rate_independent():
    evaluator = pc.ParticleCurveEvaluator()
    total_30 = sum(evaluator.emission_budget("a", 10.0, 1000.0 / 30.0)
                   for _ in range(30))
    total_60 = sum(evaluator.emission_budget("b", 10.0, 1000.0 / 60.0)
                   for _ in range(60))
    assert abs(total_30 - total_60) <= 1
    assert total_30 >= 9
    assert total_60 >= 9


def test_budget_state_and_reset():
    evaluator = pc.ParticleCurveEvaluator()
    assert evaluator.emission_budget("em", 2.5, 250) == 0
    assert evaluator.emission_budget("em", 2.5, 250) == 1
    state = evaluator.budget_state("em")
    assert state["emitted"] == 1
    assert state["remainder"] == pytest.approx(0.25)
    assert state["dropped"] == 0

    evaluator.reset_budget("em")
    assert evaluator.budget_state("em") == {"remainder": 0.0, "emitted": 0, "dropped": 0}
    assert evaluator.budget_state("fresh") == {"remainder": 0.0, "emitted": 0, "dropped": 0}


def test_remap_time_modes_and_validation():
    evaluator = pc.ParticleCurveEvaluator()
    assert evaluator.remap_time(2500.0, 0.0, 1000.0, "loop") == pytest.approx(500.0)
    assert evaluator.remap_time(-200.0, 0.0, 1000.0, "clamp") == pytest.approx(0.0)
    assert evaluator.remap_time(1200.0, 0.0, 1000.0, "clamp") == pytest.approx(1000.0)

    with pytest.raises(pc.ParticleError) as exc:
        evaluator.remap_time(500.0, 0.0, 1000.0, "bounce")
    assert exc.value.code == "PARTICLE_REMAP_MODE"
    with pytest.raises(pc.ParticleError) as exc:
        evaluator.remap_time(5.0, 1000.0, 1000.0, "clamp")
    assert exc.value.code == "PARTICLE_TIME_INVALID"


def test_random_stream_is_reproducible_per_seed_and_index():
    evaluator = pc.ParticleCurveEvaluator()
    first = [evaluator.random_stream(7, index) for index in range(20)]
    second = [evaluator.random_stream(7, index) for index in range(20)]
    assert first == second
    assert all(0.0 <= value < 1.0 for value in first)
    assert len(set(first)) > 1

    other = pc.ParticleCurveEvaluator()
    assert [other.random_stream(7, index) for index in range(20)] == first
    assert first[0] != evaluator.random_stream(8, 0)


def test_snapshot_restore_preserves_curves_and_budgets():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("spark", linear_keys())
    evaluator.emission_budget("em", 2.5, 250)
    state = evaluator.snapshot()
    assert state["version"] == 1
    pristine = copy.deepcopy(state)

    state["curves"].clear()
    assert evaluator.eval("spark", 1.0) == pytest.approx(5.0)

    restored = pc.ParticleCurveEvaluator()
    restored.restore(pristine)
    assert restored.eval("spark", 1.0) == pytest.approx(5.0)
    assert restored.list_modules() == ["spark"]
    assert restored.budget_state("em") == evaluator.budget_state("em")

    with pytest.raises(pc.ParticleError) as exc:
        restored.restore({"version": 4})
    assert exc.value.code == "PARTICLE_SNAPSHOT_INVALID"
    assert restored.list_modules() == ["spark"]


def test_concurrent_eval_and_curve_swap():
    evaluator = pc.ParticleCurveEvaluator()
    evaluator.set_curve("ring", [{"time": 0.0, "value": 10.0, "mode": "linear"}])
    observed = []
    errors = []

    def writer(value):
        try:
            for _ in range(150):
                evaluator.set_curve("ring",
                                    [{"time": 0.0, "value": value, "mode": "linear"}])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(150):
                observed.append(evaluator.eval("ring", 0.5))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(10.0,)),
               threading.Thread(target=writer, args=(20.0,)),
               threading.Thread(target=reader),
               threading.Thread(target=reader)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(observed) == 300
    assert set(observed) <= {10.0, 20.0}
