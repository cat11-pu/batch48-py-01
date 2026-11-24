import copy
import math
import threading

import pytest

import material_graph_evaluator as mg


def test_add_node_and_duplicate_identity():
    graph = mg.MaterialGraphEvaluator()
    node = graph.add_node("n1", "add", {"a": 1, "b": 2})
    assert node["id"] == "n1"
    assert node["op"] == "add"
    assert graph.add_node("v1", "vec3", {"x": 1, "y": 2, "z": 3})["op"] == "vec3"
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.add_node("n1", "add", {"a": 0, "b": 0})
    assert exc.value.code == "MATERIAL_DUPLICATE_NODE"
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.evaluate("ghost")
    assert exc.value.code == "MATERIAL_UNKNOWN_NODE"
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.connect("ghost", "a", "n1")
    assert exc.value.code == "MATERIAL_UNKNOWN_NODE"


def test_unknown_port_and_unsupported_op():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.add_node("n2", "add", {"z": 1})
    assert exc.value.code == "MATERIAL_UNKNOWN_PORT"
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.connect("n1", "z", "n1")
    assert exc.value.code == "MATERIAL_UNKNOWN_PORT"
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.add_node("n3", "frobnicate")
    assert exc.value.code == "MATERIAL_OP_UNSUPPORTED"


def test_scalar_chain_uses_float32_steps():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": 0.1, "b": 0.2})
    graph.add_node("n2", "mul", {"a": ("n1", "out"), "b": 4})
    assert graph.evaluate("n1") == pytest.approx(0.3, abs=1e-6)
    assert graph.evaluate("n2") == pytest.approx(1.2, abs=1e-6)


def test_vector_dot_scale_and_length():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("v1", "vec3", {"x": 1, "y": 2, "z": 3})
    graph.add_node("v2", "vec3", {"x": 4, "y": 5, "z": 6})
    graph.add_node("d", "dot", {"a": ("v1", "out"), "b": ("v2", "out")})
    graph.add_node("s", "scale", {"v": ("v1", "out"), "s": 2})
    graph.add_node("l", "length", {"v": ("v1", "out")})
    assert graph.evaluate("d") == pytest.approx(32.0)
    assert graph.evaluate("s") == [pytest.approx(2.0), pytest.approx(4.0),
                                   pytest.approx(6.0)]
    assert graph.evaluate("l") == pytest.approx(math.sqrt(14.0))


def test_vec4_clamp_mix_and_neg():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("v4", "vec4", {"x": 1, "y": 2, "z": 3, "w": 4})
    graph.add_node("c", "clamp", {"v": [-1.0, 0.5, 2.0], "lo": 0.0, "hi": 1.0})
    graph.add_node("m", "mix", {"a": [1.0, 2.0], "b": [3.0, 4.0], "t": 0.25})
    graph.add_node("n", "neg", {"a": [1.0, -2.0]})
    assert graph.evaluate("v4") == [1.0, 2.0, 3.0, 4.0]
    assert graph.evaluate("c") == [0.0, 0.5, 1.0]
    assert graph.evaluate("m") == [1.5, 2.5]
    assert graph.evaluate("n") == [-1.0, 2.0]


def test_dimension_mismatch_reports_node_and_port():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": [1.0, 2.0], "b": [1.0, 2.0, 3.0]})
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.evaluate("n1")
    assert exc.value.code == "MATERIAL_DIM_MISMATCH"
    assert exc.value.detail["node"] == "n1"
    assert exc.value.detail["port"] == "b"


def test_cycle_is_rejected_when_edge_is_added():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("a", "add", {"a": 1, "b": 2})
    graph.add_node("b", "add", {"a": 3, "b": 4})
    graph.connect("a", "a", "b")
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.connect("b", "a", "a")
    assert exc.value.code == "MATERIAL_CYCLE"
    assert isinstance(exc.value.detail, (list, tuple))
    assert set(exc.value.detail) == {"a", "b"}

    solo = mg.MaterialGraphEvaluator()
    solo.add_node("s", "add", {"a": 1, "b": 2})
    with pytest.raises(mg.MaterialGraphError) as exc:
        solo.connect("s", "a", "s")
    assert exc.value.code == "MATERIAL_CYCLE"


def test_node_and_input_limits():
    graph = mg.MaterialGraphEvaluator(max_nodes=3, max_inputs=4)
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    graph.add_node("n2", "add", {"a": 1, "b": 2})
    graph.add_node("n3", "add", {"a": 1, "b": 2})
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.add_node("n4", "add", {"a": 1, "b": 2})
    assert exc.value.code == "MATERIAL_NODE_LIMIT"

    narrow = mg.MaterialGraphEvaluator(max_inputs=3)
    with pytest.raises(mg.MaterialGraphError) as exc:
        narrow.add_node("wide", "vec4", {"x": 1, "y": 2, "z": 3, "w": 4})
    assert exc.value.code == "MATERIAL_INPUT_LIMIT"


def test_evaluation_depth_limit():
    graph = mg.MaterialGraphEvaluator(max_depth=3)
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    graph.add_node("n2", "add", {"a": ("n1", "out"), "b": 1})
    graph.add_node("n3", "add", {"a": ("n2", "out"), "b": 1})
    graph.add_node("n4", "add", {"a": ("n3", "out"), "b": 1})
    assert graph.evaluate("n3") == pytest.approx(5.0)
    with pytest.raises(mg.MaterialGraphError) as exc:
        graph.evaluate("n4")
    assert exc.value.code == "MATERIAL_DEPTH_LIMIT"


def test_set_input_invalidates_downstream_results():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    graph.add_node("n2", "add", {"a": ("n1", "out"), "b": 10})
    assert graph.evaluate("n2") == pytest.approx(13.0)
    graph.set_input("n1", "a", 5)
    assert graph.evaluate("n1") == pytest.approx(7.0)
    assert graph.evaluate("n2") == pytest.approx(17.0)


def test_repeated_evaluation_reuses_cached_version():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    graph.add_node("n2", "add", {"a": ("n1", "out"), "b": 4})
    assert set(graph.stats()) == {
        "evaluations", "cache_hits", "folds", "invalidations",
    }
    assert graph.evaluate("n1") == pytest.approx(3.0)
    after_first = graph.stats()
    assert graph.evaluate("n1") == pytest.approx(3.0)
    after_second = graph.stats()
    assert after_second["evaluations"] == after_first["evaluations"]
    assert after_second["cache_hits"] == after_first["cache_hits"] + 1
    assert graph.evaluate("n2") == pytest.approx(7.0)
    assert graph.evaluate("n2") == pytest.approx(7.0)


def test_fold_constants_counts_constant_subgraph():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    graph.add_node("n2", "mul", {"a": 3, "b": 4})
    graph.add_node("n3", "add", {"a": ("n1", "out"), "b": ("n2", "out")})
    assert graph.fold_constants() == 2
    assert graph.evaluate("n3") == pytest.approx(15.0)
    assert graph.fold_constants() == 0


def test_evaluate_all_lists_nodes_in_ascending_order():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n3", "add", {"a": 3, "b": 0})
    graph.add_node("n1", "add", {"a": 1, "b": 0})
    graph.add_node("n2", "add", {"a": 2, "b": 0})
    results = graph.evaluate_all()
    assert list(results.keys()) == ["n1", "n2", "n3"]
    assert results["n1"] == pytest.approx(1.0)
    assert results["n2"] == pytest.approx(2.0)
    assert results["n3"] == pytest.approx(3.0)


def test_snapshot_restore_keeps_graph_and_is_detached():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("n1", "add", {"a": 1, "b": 2})
    graph.add_node("n2", "mul", {"a": ("n1", "out"), "b": 3})
    graph.evaluate("n2")
    state = graph.snapshot()
    pristine = copy.deepcopy(state)
    assert set(state) == {"version", "nodes", "stats"}
    state["nodes"].clear()
    assert graph.evaluate("n2") == pytest.approx(9.0)

    restored = mg.MaterialGraphEvaluator()
    restored.restore(pristine)
    assert restored.evaluate("n1") == pytest.approx(3.0)
    assert restored.evaluate("n2") == pytest.approx(9.0)
    restored.set_input("n1", "a", 10)
    assert restored.evaluate("n2") == pytest.approx(36.0)


def test_concurrent_evaluate_and_set_input_see_complete_versions():
    graph = mg.MaterialGraphEvaluator()
    graph.add_node("a", "add", {"a": 1.0, "b": 0.0})
    graph.add_node("b", "add", {"a": ("a", "out"), "b": 2.0})
    errors = []
    results = []

    def worker(worker_id):
        try:
            for step in range(30):
                value = 1.0 if (worker_id + step) % 2 == 0 else 2.0
                graph.set_input("a", "a", value)
                results.append(graph.evaluate("b"))
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append((worker_id, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(results) == 120
    for value in results:
        assert value in (3.0, 4.0)
