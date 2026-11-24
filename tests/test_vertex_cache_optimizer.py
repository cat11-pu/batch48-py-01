import json
import threading

import pytest

import vertex_cache_optimizer as vc


def mesh():
    vertices = list(range(8))
    triangles = [
        {"id": 1, "submesh": "body", "indices": [0, 1, 2], "winding": "ccw"},
        {"id": 2, "submesh": "body", "indices": [3, 4, 5], "winding": "cw"},
        {"id": 3, "submesh": "body", "indices": [6, 7, 0], "winding": "ccw"},
        {"id": 4, "submesh": "body", "indices": [1, 2, 3], "winding": "ccw"},
        {"id": 5, "submesh": "body", "indices": [4, 5, 6], "winding": "cw"},
    ]
    return vertices, triangles


def loaded(**kwargs):
    optimizer = vc.VertexCacheOptimizer(**kwargs)
    optimizer.load_mesh(*mesh())
    return optimizer


def test_load_mesh_validates_structure():
    optimizer = vc.VertexCacheOptimizer()
    vertices, triangles = mesh()
    bad_meshes = [
        ([], triangles),
        ([0, 0, 1], triangles),
        ([0, -1, 2], triangles),
        ([0, 1.5, 2], triangles),
        (vertices, []),
        (vertices, [{"id": 0, "submesh": "body", "indices": [0, 1, 2]}]),
        (vertices, [{"id": 1, "submesh": "", "indices": [0, 1, 2]}]),
        (vertices, [{"id": 1, "submesh": "body", "indices": [0, 1]}]),
        (vertices, [{"id": 1, "submesh": "body", "indices": [0, 1, "2"]}]),
        (vertices, [{"id": 1, "submesh": "body", "indices": [0, 1, 2], "winding": "strip"}]),
    ]
    for vlist, tlist in bad_meshes:
        with pytest.raises(vc.VertexCacheError) as exc:
            optimizer.load_mesh(vlist, tlist)
        assert exc.value.code == "VCO_INVALID_MESH"
    assert optimizer.stats()["rejected"] == len(bad_meshes)
    assert optimizer.mesh_info()["triangles"] == 0


def test_load_mesh_rejects_duplicate_triangle_and_bad_index():
    optimizer = vc.VertexCacheOptimizer()
    vertices, triangles = mesh()
    duplicate = triangles + [{"id": 2, "submesh": "body", "indices": [0, 1, 2]}]
    with pytest.raises(vc.VertexCacheError) as exc:
        optimizer.load_mesh(vertices, duplicate)
    assert exc.value.code == "VCO_DUPLICATE_TRIANGLE"
    assert exc.value.detail["id"] == 2

    bad_index = triangles + [{"id": 9, "submesh": "body", "indices": [0, 1, 99]}]
    with pytest.raises(vc.VertexCacheError) as exc:
        optimizer.load_mesh(vertices, bad_index)
    assert exc.value.code == "VCO_INDEX_OUT_OF_RANGE"
    assert exc.value.detail["index"] == 99


def test_load_mesh_reports_info_and_submeshes():
    optimizer = vc.VertexCacheOptimizer()
    vertices = list(range(8))
    triangles = [
        {"id": 1, "submesh": "body", "indices": [0, 1, 2]},
        {"id": 2, "submesh": "body", "indices": [1, 2, 3]},
        {"id": 3, "submesh": "glass", "indices": [4, 5, 6]},
    ]
    info = optimizer.load_mesh(vertices, triangles)
    assert info["vertices"] == 8
    assert info["triangles"] == 3
    assert info["optimized"] is False
    assert info["submeshes"] == [
        {"name": "body", "triangles": 2},
        {"name": "glass", "triangles": 1},
    ]
    assert optimizer.submeshes() == info["submeshes"]
    assert optimizer.original_order() == [1, 2, 3]
    assert optimizer.optimized_order() == []
    assert optimizer.stats()["meshes_loaded"] == 1


def test_set_cache_size_validation_and_plan_invalidation():
    optimizer = loaded(max_cache_size=32)
    optimizer.optimize()
    assert optimizer.mesh_info()["optimized"] is True
    assert optimizer.set_cache_size(4) == {"cache_size": 4, "optimized": False}
    assert optimizer.cache_size() == 4
    assert optimizer.optimized_order() == []
    assert optimizer.mesh_info()["optimized"] is False

    for bad in (-1, 33, 65, "8", 2.5, None):
        with pytest.raises(vc.VertexCacheError) as exc:
            optimizer.set_cache_size(bad)
        assert exc.value.code == "VCO_INVALID_CACHE_SIZE"
    assert optimizer.cache_size() == 4
    assert optimizer.stats()["rejected"] == 6


def test_mesh_operations_require_loaded_mesh():
    optimizer = vc.VertexCacheOptimizer()
    calls = [
        optimizer.optimize,
        optimizer.measure_hit_rate,
        optimizer.restore_order,
    ]
    for call in calls:
        with pytest.raises(vc.VertexCacheError) as exc:
            call()
        assert exc.value.code == "VCO_NO_MESH"
    assert optimizer.original_order() == []
    assert optimizer.optimized_order() == []


def test_triangle_and_vertex_caps_are_hard():
    vertices, triangles = mesh()
    tiny_mesh = vc.VertexCacheOptimizer(max_triangles=3)
    with pytest.raises(vc.VertexCacheError) as exc:
        tiny_mesh.load_mesh(vertices, triangles)
    assert exc.value.code == "VCO_TOO_MANY_TRIANGLES"
    assert exc.value.detail["max_triangles"] == 3

    tiny_vertices = vc.VertexCacheOptimizer(max_vertices=4)
    with pytest.raises(vc.VertexCacheError) as exc:
        tiny_vertices.load_mesh(vertices, triangles)
    assert exc.value.code == "VCO_TOO_MANY_VERTICES"
    assert exc.value.detail["vertices"] == 8
    assert tiny_vertices.mesh_info()["vertices"] == 0


def test_optimize_returns_permutation_of_all_triangles():
    optimizer = loaded()
    report = optimizer.optimize()
    order = report["order"]
    assert sorted(order) == optimizer.original_order()
    assert len(set(order)) == 5
    assert report["reused"] is False
    assert report["cache_hits"] + report["cache_misses"] == 15
    assert optimizer.optimized_order() == order
    assert optimizer.mesh_info()["optimized"] is True


def test_optimize_is_deterministic_across_instances():
    first = loaded()
    second = loaded()
    first_report = first.optimize()
    second_report = second.optimize()
    assert json.dumps(first_report["order"]) == json.dumps(second_report["order"])
    assert first_report["cache_hits"] == second_report["cache_hits"]
    assert first_report["cache_misses"] == second_report["cache_misses"]
    assert first.measure_hit_rate()["hit_rate"] == second.measure_hit_rate()["hit_rate"]


def test_optimize_improves_hit_rate_on_reused_vertices():
    optimizer = loaded()
    optimizer.set_cache_size(3)
    original = optimizer.measure_hit_rate(optimizer.original_order())
    assert original["cache_hits"] == 0
    assert original["cache_misses"] == 15
    assert original["hit_rate"] == 0.0
    optimized = optimizer.optimize()
    assert optimized["cache_hits"] > original["cache_hits"]
    assert optimized["hit_rate"] > 0.0
    checked = optimizer.measure_hit_rate(optimizer.optimized_order())
    assert checked["cache_hits"] == optimized["cache_hits"]
    assert checked["cache_misses"] == optimized["cache_misses"]


def test_optimize_reuses_plan_until_cache_size_changes():
    optimizer = loaded()
    first = optimizer.optimize()
    second = optimizer.optimize()
    assert second["reused"] is True
    assert second["order"] == first["order"]
    assert optimizer.stats()["optimizations"] == 1

    optimizer.set_cache_size(2)
    third = optimizer.optimize()
    assert third["reused"] is False
    assert optimizer.stats()["optimizations"] == 2
    assert third["cache_size"] == 2
    assert third["cache_hits"] + third["cache_misses"] == 15


def test_restore_order_returns_original_and_clears_plan():
    optimizer = loaded()
    optimizer.optimize()
    restored = optimizer.restore_order()
    assert restored["restored"] is True
    assert restored["order"] == [1, 2, 3, 4, 5]
    assert optimizer.optimized_order() == []
    assert optimizer.mesh_info()["optimized"] is False
    assert optimizer.stats()["restores"] == 1


def test_measure_hit_rate_validates_order():
    optimizer = loaded()
    bad_orders = [
        [1, 2, 3],
        [1, 2, 3, 4, 5, 6],
        [1, 2, 3, 4, 4],
        [1, 2, 3, 4, 99],
        [1, 2, 3, 4, "5"],
        "12345",
    ]
    for order in bad_orders:
        with pytest.raises(vc.VertexCacheError) as exc:
            optimizer.measure_hit_rate(order)
        assert exc.value.code == "VCO_INVALID_ORDER"
    assert optimizer.stats()["rejected"] == len(bad_orders)


def test_measure_hit_rate_counts_fifo_hits_and_misses():
    optimizer = vc.VertexCacheOptimizer()
    optimizer.set_cache_size(3)
    vertices = [0, 1, 2, 3]
    triangles = [
        {"id": 1, "submesh": "body", "indices": [0, 1, 2]},
        {"id": 2, "submesh": "body", "indices": [1, 2, 3]},
    ]
    optimizer.load_mesh(vertices, triangles)
    report = optimizer.measure_hit_rate([1, 2])
    assert report["cache_hits"] == 2
    assert report["cache_misses"] == 4
    assert report["hit_rate"] == pytest.approx(2 / 6)
    assert report["cache_size"] == 3
    assert optimizer.stats()["cache_simulations"] == 1


def test_snapshot_restore_and_invalid_state():
    optimizer = loaded()
    plan = optimizer.optimize()["order"]
    snapshot = optimizer.snapshot()
    optimizer.load_mesh([0, 1, 2], [{"id": 7, "submesh": "other", "indices": [0, 1, 2]}])
    optimizer.restore(snapshot)
    assert optimizer.mesh_info()["triangles"] == 5
    assert optimizer.optimized_order() == plan
    assert optimizer.stats()["meshes_loaded"] == 1

    snapshot["triangles"][0]["submesh"] = "hacked"
    assert optimizer.submeshes()[0]["name"] == "body"

    broken = dict(snapshot)
    broken["triangles"] = "not-a-list"
    with pytest.raises(vc.VertexCacheError) as exc:
        optimizer.restore(broken)
    assert exc.value.code == "VCO_SNAPSHOT_INVALID"
    assert optimizer.optimized_order() == plan
    assert optimizer.stats()["meshes_loaded"] == 1


def test_concurrent_optimize_returns_single_plan():
    optimizer = loaded()
    barrier = threading.Barrier(4)
    results = []
    errors = []

    def worker():
        barrier.wait()
        try:
            results.append(optimizer.optimize())
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert len(results) == 4
    orders = {json.dumps(report["order"]) for report in results}
    assert len(orders) == 1
    assert optimizer.stats()["optimizations"] == 1
    assert results[0]["cache_hits"] + results[0]["cache_misses"] == 15
