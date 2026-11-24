import copy
import threading

import pytest

import skin_influence_pruner as sip


def test_add_vertex_and_duplicate_identity():
    pruner = sip.SkinInfluencePruner()
    record = pruner.add_vertex(1, [(0, 0.5), (1, 0.5)])
    assert record["vertex_id"] == 1
    assert [(bone, weight) for bone, weight in record["influences"]] == [
        (0, 0.5), (1, 0.5),
    ]
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.add_vertex(1, [(2, 1.0)])
    assert exc.value.code == "SKIN_DUPLICATE_VERTEX"
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.prune(99, 2)
    assert exc.value.code == "SKIN_UNKNOWN_VERTEX"
    assert pruner.remove_vertex(1) is True
    assert pruner.remove_vertex(1) is False
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.vertex(1)
    assert exc.value.code == "SKIN_UNKNOWN_VERTEX"


def test_add_vertex_validates_bones_and_weights():
    pruner = sip.SkinInfluencePruner()
    for bone in (-1, 65536):
        with pytest.raises(sip.SkinInfluenceError) as exc:
            pruner.add_vertex(1, [(bone, 1.0)])
        assert exc.value.code == "SKIN_BONE_RANGE"
    for weight in (-0.1, float("nan"), float("inf")):
        with pytest.raises(sip.SkinInfluenceError) as exc:
            pruner.add_vertex(1, [(0, weight)])
        assert exc.value.code == "SKIN_WEIGHT_INVALID"
    assert pruner.stats()["vertices"] == 0


def test_influence_and_vertex_limits():
    narrow = sip.SkinInfluencePruner(max_influences=2)
    with pytest.raises(sip.SkinInfluenceError) as exc:
        narrow.add_vertex(1, [(0, 1.0), (1, 1.0), (2, 1.0)])
    assert exc.value.code == "SKIN_INFLUENCE_LIMIT"
    assert narrow.stats()["vertices"] == 0

    tiny = sip.SkinInfluencePruner(max_vertices=1)
    tiny.add_vertex(1, [(0, 1.0)])
    with pytest.raises(sip.SkinInfluenceError) as exc:
        tiny.add_vertex(2, [(1, 1.0)])
    assert exc.value.code == "SKIN_VERTEX_LIMIT"


def test_prune_keeps_heaviest_bones():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(3, 5.0), (1, 2.0), (2, 3.0)])
    kept = pruner.prune(1, max_bones=2)
    assert [bone for bone, _ in kept] == [3, 2]
    assert [weight for _, weight in kept] == pytest.approx([0.625, 0.375])


def test_prune_renormalizes_survivors_to_unit_sum():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(5, 4.0), (2, 3.0), (9, 1.0)])
    kept = pruner.prune(1, max_bones=3)
    assert [bone for bone, _ in kept] == [5, 2, 9]
    weights = [weight for _, weight in kept]
    assert weights == pytest.approx([0.5, 0.375, 0.125])
    assert sum(weights) == pytest.approx(1.0, abs=1e-6)


def test_normalize_function_handles_regular_and_degenerate_inputs():
    pruner = sip.SkinInfluencePruner()
    assert pruner.normalize([1, 3]) == pytest.approx([0.25, 0.75])
    assert pruner.normalize([2.0, 2.0]) == pytest.approx([0.5, 0.5])
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.normalize([0.0, 0.0])
    assert exc.value.code == "SKIN_ZERO_WEIGHT"
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.normalize([1.0, -1.0])
    assert exc.value.code == "SKIN_WEIGHT_INVALID"
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.normalize([])
    assert exc.value.code == "SKIN_WEIGHT_INVALID"


def test_prune_max_bones_argument_is_bounded():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(0, 1.0), (1, 1.0)])
    for bad in (0, -1, 33, "two"):
        with pytest.raises(sip.SkinInfluenceError) as exc:
            pruner.prune(1, max_bones=bad)
        assert exc.value.code == "SKIN_MAX_BONES_INVALID"
    assert len(pruner.prune(1, max_bones=2)) == 2


def test_zero_weight_vertex_is_rejected_and_recorded():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(0, 0.0), (1, 0.0)])
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.prune(1, max_bones=2)
    assert exc.value.code == "SKIN_ZERO_WEIGHT"
    recorded = pruner.errors()
    assert recorded[1]["code"] == "SKIN_ZERO_WEIGHT"
    assert pruner.kept(1) is None
    assert pruner.stats()["rejected_vertices"] == 1
    assert pruner.stats()["affected_vertices"] == 0


def test_prune_does_not_mutate_stored_influences():
    pruner = sip.SkinInfluencePruner()
    raw = [(0, 4.0), (1, 3.0), (2, 2.0), (3, 1.0)]
    pruner.add_vertex(1, raw)
    kept = pruner.prune(1, max_bones=2)
    assert [bone for bone, _ in kept] == [0, 1]
    stored = pruner.vertex(1)["influences"]
    assert [(bone, weight) for bone, weight in stored] == raw


def test_same_input_prunes_identically_twice():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(0, 3.0), (1, 2.0), (2, 1.0)])
    first = pruner.prune(1, max_bones=2)
    second = pruner.prune(1, max_bones=2)
    assert first == second
    assert [bone for bone, _ in first] == [0, 1]
    assert [weight for _, weight in first] == pytest.approx([0.6, 0.4])


def test_remap_bones_updates_indices_and_counts_vertices():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(1, 0.5), (2, 0.5)])
    assert pruner.remap_bones({1: 5, 2: 6}) == 1
    bones = sorted(bone for bone, _ in pruner.vertex(1)["influences"])
    assert bones == [5, 6]
    kept = pruner.prune(1, max_bones=2)
    assert sorted(bone for bone, _ in kept) == [5, 6]
    assert sum(weight for _, weight in kept) == pytest.approx(1.0, abs=1e-6)
    assert pruner.remap_bones({5: 5}) == 0
    with pytest.raises(sip.SkinInfluenceError) as exc:
        pruner.remap_bones({5: 70000})
    assert exc.value.code == "SKIN_BONE_RANGE"
    unchanged = sorted(bone for bone, _ in pruner.vertex(1)["influences"])
    assert unchanged == [5, 6]


def test_stats_shape_values_and_reset():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(0, 4.0), (1, 3.0), (2, 1.0)])
    pruner.prune(1, max_bones=2)
    stats = pruner.stats()
    assert set(stats) == {
        "vertices", "pruned_influences", "affected_vertices",
        "rejected_vertices", "prunes",
    }
    assert stats == {
        "vertices": 1, "pruned_influences": 1, "affected_vertices": 1,
        "rejected_vertices": 0, "prunes": 1,
    }
    pruner.reset_stats()
    cleared = pruner.stats()
    assert cleared["prunes"] == 0
    assert cleared["pruned_influences"] == 0
    assert cleared["affected_vertices"] == 0
    assert cleared["vertices"] == 1
    assert pruner.kept(1) is not None


def test_snapshot_restore_keeps_vertices_kept_and_errors():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(0, 4.0), (1, 3.0), (2, 1.0)])
    pruner.add_vertex(2, [(3, 0.0), (4, 0.0)])
    kept = pruner.prune(1, max_bones=2)
    with pytest.raises(sip.SkinInfluenceError):
        pruner.prune(2, max_bones=2)
    state = pruner.snapshot()
    pristine = copy.deepcopy(state)
    assert set(state) == {"version", "vertices", "kept", "errors", "stats"}
    state["vertices"].clear()
    state["kept"].clear()
    assert pruner.vertex(1)["vertex_id"] == 1
    assert pruner.kept(1) is not None

    restored = sip.SkinInfluencePruner()
    restored.restore(pristine)
    assert restored.prune(1, max_bones=2) == kept
    assert restored.errors()[2]["code"] == "SKIN_ZERO_WEIGHT"
    assert restored.stats()["vertices"] == 2


def test_corrupt_snapshot_is_refused_without_state_change():
    pruner = sip.SkinInfluencePruner()
    pruner.add_vertex(1, [(0, 1.0), (1, 1.0)])
    pruner.prune(1, max_bones=1)
    good_vertices = {"1": {"influences": [[0, 1.0]]}}
    bad_states = [
        None,
        {},
        {"version": 2, "vertices": {}, "kept": {}, "errors": {}, "stats": {}},
        {"version": 1, "vertices": "nope", "kept": {}, "errors": {}, "stats": {}},
        {"version": 1, "vertices": good_vertices, "kept": "nope",
         "errors": {}, "stats": {}},
        {"version": 1, "vertices": good_vertices, "kept": {},
         "errors": "nope", "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(sip.SkinInfluenceError) as exc:
            pruner.restore(bad)
        assert exc.value.code == "SKIN_SNAPSHOT_INVALID"
    assert pruner.vertex(1)["vertex_id"] == 1


def test_concurrent_prune_and_remap_see_consistent_mapping():
    pruner = sip.SkinInfluencePruner()
    for vertex_id in range(4):
        pruner.add_vertex(
            vertex_id,
            [(vertex_id, 2.0), (10 + vertex_id, 1.0), (20 + vertex_id, 1.0)],
        )
    errors = []
    outputs = []

    def worker(worker_id):
        try:
            for _ in range(20):
                outputs.append(pruner.prune(worker_id, max_bones=2))
                pruner.remap_bones({worker_id: 100 + worker_id})
                outputs.append(pruner.prune(worker_id, max_bones=2))
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append((worker_id, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(outputs) == 160
    for kept in outputs:
        assert 1 <= len(kept) <= 2
        assert sum(weight for _, weight in kept) == pytest.approx(1.0, abs=1e-6)
        assert all(0 <= bone <= 65535 for bone, _ in kept)
