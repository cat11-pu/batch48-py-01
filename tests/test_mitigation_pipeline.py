import copy
import threading

import pytest

import mitigation_pipeline as mp


def test_unarmored_target_takes_full_damage():
    pipe = mp.MitigationPipeline()
    out = pipe.register_target("dummy", hp=250)
    assert out["target"] == "dummy"
    assert out["hp"] == 250
    assert pipe.effective_hp("dummy") == 250
    assert pipe.layers("dummy") == []
    result = pipe.resolve("dummy", 90, "physical")
    assert result["incoming"] == 90
    assert result["final"] == 90
    assert result["absorbed"] == 0
    assert result["shield_remaining"] == 0
    assert result["layers_applied"] == []
    assert result["capped"] is False
    assert pipe.set_hp("dummy", 200)["hp"] == 200
    assert pipe.effective_hp("dummy") == 200


def test_armor_applies_before_elemental_resist():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 30})
    pipe.add_layer("t", {"layer_id": "flame", "kind": "resist",
                         "damage_type": "fire", "percent": 0.5})
    fire = pipe.resolve("t", 100, "fire")
    assert fire["final"] == 35
    assert fire["layers_applied"] == ["plate", "flame"]
    physical = pipe.resolve("t", 100, "physical")
    assert physical["final"] == 70
    assert physical["layers_applied"] == ["plate"]


def test_penetration_eats_armor_before_flat_reduction():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "pierce", "kind": "penetration",
                         "amount": 15})
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 20})
    assert pipe.resolve("t", 100, "physical")["final"] == 95
    pipe.add_layer("t", {"layer_id": "crush", "kind": "penetration",
                         "amount": 30})
    heavy = pipe.resolve("t", 100, "physical")
    assert heavy["final"] == 100
    assert heavy["layers_applied"] == ["pierce", "crush", "plate"]


def test_resist_layers_stack_additively_and_clamp():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "ward1", "kind": "resist",
                         "damage_type": "physical", "percent": 0.6})
    pipe.add_layer("t", {"layer_id": "ward2", "kind": "resist",
                         "damage_type": "physical", "percent": 0.6})
    stacked = pipe.resolve("t", 100, "physical")
    assert stacked["final"] == 0
    assert stacked["layers_applied"] == ["ward1", "ward2"]
    pipe.remove_layer("t", "ward2")
    single = pipe.resolve("t", 100, "physical")
    assert single["final"] == 40
    assert single["layers_applied"] == ["ward1"]


def test_multiplier_layers_apply_multiplicatively():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "m1", "kind": "multiplier",
                         "percent": 0.5})
    pipe.add_layer("t", {"layer_id": "m2", "kind": "multiplier",
                         "percent": 0.5})
    assert pipe.resolve("t", 100, "physical")["final"] == 25
    pipe.remove_layer("t", "m2")
    assert pipe.resolve("t", 100, "physical")["final"] == 50


def test_cap_enforces_a_damage_floor():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 200})
    pipe.add_layer("t", {"layer_id": "capper", "kind": "cap", "percent": 0.7})
    heavy = pipe.resolve("t", 100, "physical")
    assert heavy["final"] == 30
    assert heavy["capped"] is True
    light = pipe.resolve("t", 50, "physical")
    assert light["final"] == 15
    assert light["capped"] is True


def test_layers_applied_reports_fixed_settlement_order():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "pen", "kind": "penetration", "amount": 5})
    pipe.add_layer("t", {"layer_id": "arm", "kind": "armor", "amount": 20})
    pipe.add_layer("t", {"layer_id": "res", "kind": "resist",
                         "damage_type": "fire", "percent": 0.5})
    pipe.add_layer("t", {"layer_id": "mul", "kind": "multiplier",
                         "percent": 0.2})
    pipe.add_layer("t", {"layer_id": "cap", "kind": "cap", "percent": 0.9})
    out = pipe.resolve("t", 100, "fire")
    assert out["layers_applied"] == ["pen", "arm", "res", "mul", "cap"]
    assert out["final"] == 33
    assert out["capped"] is False


def test_remove_layer_recomputes_and_unknown_layer_fails():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 30})
    assert pipe.resolve("t", 100, "physical")["final"] == 70
    out = pipe.remove_layer("t", "plate")
    assert out["removed"] == "plate"
    assert out["layers"] == []
    assert pipe.resolve("t", 100, "physical")["final"] == 100
    with pytest.raises(mp.MitigationError) as exc:
        pipe.remove_layer("t", "plate")
    assert exc.value.code == "MITIGATION_UNKNOWN_LAYER"


def test_invalid_layers_are_rejected():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    with pytest.raises(mp.MitigationError) as exc:
        pipe.add_layer("t", {"kind": "armor", "amount": 5})
    assert exc.value.code == "MITIGATION_INVALID_LAYER"
    with pytest.raises(mp.MitigationError) as exc:
        pipe.add_layer("t", {"layer_id": "x", "kind": "shield", "amount": 5})
    assert exc.value.code == "MITIGATION_INVALID_LAYER"
    with pytest.raises(mp.MitigationError) as exc:
        pipe.add_layer("t", {"layer_id": "r", "kind": "resist",
                             "damage_type": "fire", "percent": 1.5})
    assert exc.value.code == "MITIGATION_INVALID_LAYER"
    pipe.add_layer("t", {"layer_id": "dup", "kind": "armor", "amount": 5})
    with pytest.raises(mp.MitigationError) as exc:
        pipe.add_layer("t", {"layer_id": "dup", "kind": "armor", "amount": 9})
    assert exc.value.code == "MITIGATION_DUPLICATE_LAYER"
    assert len(pipe.layers("t")) == 1


def test_shield_absorbs_from_the_remaining_damage():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 10})
    added = pipe.add_shield("t", "fire", 40)
    assert added["shield_type"] == "fire"
    assert added["shield_remaining"] == 40
    first = pipe.resolve("t", 100, "fire")
    assert first["absorbed"] == 40
    assert first["final"] == 50
    assert first["shield_remaining"] == 0
    second = pipe.resolve("t", 100, "fire")
    assert second["absorbed"] == 0
    assert second["final"] == 90


def test_effective_hp_sums_life_and_shield_balances():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_shield("t", "fire", 30)
    pipe.add_shield("t", "physical", 20)
    assert pipe.effective_hp("t") == 150
    pipe.set_hp("t", 120)
    assert pipe.effective_hp("t") == 170
    absorbed = pipe.shield_absorb("t", 25, "physical")
    assert absorbed == 20
    assert pipe.effective_hp("t") == 150


def test_snapshot_restore_reproduces_resolution():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=140)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 25})
    pipe.add_layer("t", {"layer_id": "m", "kind": "multiplier", "percent": 0.2})
    pipe.add_shield("t", "fire", 15)
    state = pipe.snapshot()
    pristine = copy.deepcopy(state)
    state["targets"]["t"]["layers"].clear()
    assert len(pipe.layers("t")) == 2
    restored = mp.MitigationPipeline()
    restored.restore(pristine)
    assert restored.layers("t") == pipe.layers("t")
    assert restored.effective_hp("t") == pipe.effective_hp("t")
    assert restored.resolve("t", 100, "fire") == pipe.resolve("t", 100, "fire")


def test_invalid_snapshot_is_rejected_without_changing_state():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 10})
    bad_states = [
        None,
        {},
        {"version": 2, "targets": {}},
        {"version": 1, "targets": "nope"},
        {"version": 1, "targets": {"t": {"hp": 100, "layers": "nope",
                                         "shields": {}}}},
    ]
    for bad in bad_states:
        with pytest.raises(mp.MitigationError) as exc:
            pipe.restore(bad)
        assert exc.value.code == "MITIGATION_SNAPSHOT_INVALID"
    assert len(pipe.layers("t")) == 1
    assert pipe.resolve("t", 100, "physical")["final"] == 90


def test_target_and_amount_validation():
    pipe = mp.MitigationPipeline()
    with pytest.raises(mp.MitigationError) as exc:
        pipe.resolve("ghost", 10, "physical")
    assert exc.value.code == "MITIGATION_UNKNOWN_TARGET"
    pipe.register_target("t", hp=100)
    with pytest.raises(mp.MitigationError) as exc:
        pipe.resolve("t", -5, "physical")
    assert exc.value.code == "MITIGATION_INVALID_AMOUNT"
    with pytest.raises(mp.MitigationError) as exc:
        pipe.add_layer("ghost", {"layer_id": "a", "kind": "armor", "amount": 1})
    assert exc.value.code == "MITIGATION_UNKNOWN_TARGET"
    with pytest.raises(mp.MitigationError) as exc:
        pipe.add_shield("t", "fire", -3)
    assert exc.value.code == "MITIGATION_INVALID_AMOUNT"
    with pytest.raises(mp.MitigationError) as exc:
        pipe.set_hp("t", -1)
    assert exc.value.code == "MITIGATION_INVALID_AMOUNT"


def test_concurrent_resolve_and_remove_keep_results_consistent():
    pipe = mp.MitigationPipeline()
    pipe.register_target("t", hp=100)
    pipe.add_layer("t", {"layer_id": "plate", "kind": "armor", "amount": 30})
    results = []
    errors = []
    barrier = threading.Barrier(6)

    def resolver_worker():
        barrier.wait()
        try:
            for _ in range(6):
                results.append(pipe.resolve("t", 100, "physical"))
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    def toggler_worker():
        barrier.wait()
        for _ in range(6):
            pipe.remove_layer("t", "plate")
            pipe.add_layer("t", {"layer_id": "plate", "kind": "armor",
                                 "amount": 30})

    threads = [threading.Thread(target=resolver_worker) for _ in range(5)]
    threads.append(threading.Thread(target=toggler_worker))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(results) == 30
    for result in results:
        if result["layers_applied"] == ["plate"]:
            assert result["final"] == 70
        else:
            assert result["layers_applied"] == []
            assert result["final"] == 100
