import pytest

import occluder_proxy_guard as og

CAM = {"position": (1.0, 1.0, 11.0), "focal": 100.0, "viewport": (100.0, 100.0)}
CUBE = (0.0, 0.0, 0.0, 2.0, 2.0, 2.0)
STAT_KEYS = {"built", "rebuilt", "removed", "occluder_queries",
             "dropped_budget", "dropped_coverage", "frames"}


def box(mesh_id, bounds=CUBE):
    return {"id": mesh_id, "shape": "box", "bounds": bounds}


def hull(mesh_id, triangles=64, bounds=CUBE):
    return {"id": mesh_id, "shape": "hull", "bounds": bounds,
            "hull_triangles": triangles}


def make(**kwargs):
    return og.OccluderProxyGuard(**kwargs)


def test_build_returns_a_box_proxy_and_updates_budget():
    guard = make()
    proxy = guard.build(box("a"))
    assert proxy["mesh_id"] == "a"
    assert proxy["shape"] == "box"
    assert proxy["triangles"] == 12
    assert proxy["generation"] == 1
    assert proxy["stale"] is False
    assert tuple(proxy["bounds"]) == CUBE
    assert guard.proxy_of("a")["triangles"] == 12
    assert guard.budget() == {
        "proxies": 1, "max_proxies": 16, "triangles": 12, "tri_budget": 400,
    }
    assert guard.stats()["built"] == 1


def test_hull_proxy_uses_declared_triangles_and_mesh_shape_is_validated():
    guard = make()
    proxy = guard.build(hull("h"))
    assert proxy["shape"] == "hull"
    assert proxy["triangles"] == 64

    bad_meshes = [
        {"id": "h2", "shape": "hull", "bounds": CUBE},
        {"id": "h3", "shape": "hull", "bounds": CUBE, "hull_triangles": 2},
        {"id": "b2", "shape": "box", "bounds": (2.0, 0.0, 0.0, 0.0, 2.0, 2.0)},
        {"id": "s", "shape": "sphere", "bounds": CUBE},
    ]
    for mesh in bad_meshes:
        with pytest.raises(og.OccluderError) as exc:
            guard.build(mesh)
        assert exc.value.code == "PROXY_MESH_INVALID"
    assert guard.stats()["built"] == 1


def test_duplicate_and_unknown_mesh_operations_are_rejected():
    guard = make()
    guard.build(box("a"))
    with pytest.raises(og.OccluderError) as exc:
        guard.build(box("a"))
    assert exc.value.code == "PROXY_DUPLICATE_MESH"

    with pytest.raises(og.OccluderError) as exc:
        guard.proxy_of("ghost")
    assert exc.value.code == "PROXY_UNKNOWN_MESH"
    with pytest.raises(og.OccluderError) as exc:
        guard.invalidate("ghost")
    assert exc.value.code == "PROXY_UNKNOWN_MESH"
    with pytest.raises(og.OccluderError) as exc:
        guard.rebuild("ghost")
    assert exc.value.code == "PROXY_UNKNOWN_MESH"
    with pytest.raises(og.OccluderError) as exc:
        guard.score("ghost", CAM)
    assert exc.value.code == "PROXY_UNKNOWN_MESH"
    with pytest.raises(og.OccluderError) as exc:
        guard.remove("ghost")
    assert exc.value.code == "PROXY_UNKNOWN_MESH"


def test_coverage_is_computed_from_projected_pixel_area():
    guard = make()
    guard.build(box("a"))
    assert guard.score("a", CAM) == pytest.approx(0.04 * 64.0 / 74.0)
    assert guard.coverage("a") == pytest.approx(0.04)

    distant = {"position": (1.0, 1.0, 101.0), "focal": 100.0,
               "viewport": (100.0, 100.0)}
    guard.score("a", distant)
    assert guard.coverage("a") == pytest.approx(0.0004)


def test_score_prefers_nearer_proxies_of_the_same_size():
    guard = make()
    guard.build(box("mid"))
    guard.build(box("near", (0.0, 0.0, 8.0, 2.0, 2.0, 10.0)))
    near_score = guard.score("near", CAM)
    mid_score = guard.score("mid", CAM)
    assert near_score == pytest.approx(64.0 / 66.0)
    assert mid_score == pytest.approx(0.04 * 64.0 / 74.0)
    assert near_score > mid_score


def test_occluders_are_sorted_by_score_and_limited():
    guard = make()
    guard.build(box("mid"))
    guard.build(box("far", (0.0, 0.0, -10.0, 2.0, 2.0, -8.0)))
    guard.build(box("near", (0.0, 0.0, 8.0, 2.0, 2.0, 10.0)))
    assert guard.occluders(CAM) == ["near", "mid", "far"]
    assert guard.occluders(CAM, limit=2) == ["near", "mid"]
    assert guard.stats()["occluder_queries"] == 2

    with pytest.raises(og.OccluderError) as exc:
        guard.occluders({"position": (0, 0, 0)})
    assert exc.value.code == "PROXY_CAMERA_INVALID"
    with pytest.raises(og.OccluderError) as exc:
        guard.occluders(CAM, limit=0)
    assert exc.value.code == "PROXY_LIMIT_INVALID"


def test_min_coverage_filters_small_proxies():
    guard = make(min_coverage=0.02)
    guard.build(box("mid"))
    guard.build(box("far", (0.0, 0.0, -10.0, 2.0, 2.0, -8.0)))
    guard.build(box("near", (0.0, 0.0, 8.0, 2.0, 2.0, 10.0)))
    assert guard.occluders(CAM) == ["near", "mid"]
    frame = guard.last_frame()
    assert frame["dropped_coverage"] == ["far"]
    assert frame["kept"] == ["near", "mid"]
    assert guard.stats()["dropped_coverage"] == 1


def test_triangle_budget_drops_the_lowest_coverage_first():
    guard = make(tri_budget=30)
    guard.build(box("mid"))
    guard.build(box("far", (0.0, 0.0, -10.0, 2.0, 2.0, -8.0)))
    guard.build(box("near", (0.0, 0.0, 8.0, 2.0, 2.0, 10.0)))
    assert guard.occluders(CAM, limit=8) == ["near", "mid"]
    frame = guard.last_frame()
    assert frame["dropped_budget"] == ["far"]
    assert guard.stats()["dropped_budget"] == 1


def test_proxy_count_and_triangle_budget_have_hard_ceilings():
    guard = make(max_proxies=2, tri_budget=100)
    guard.build(box("a"))
    guard.build(box("b"))
    with pytest.raises(og.OccluderError) as exc:
        guard.build(box("c"))
    assert exc.value.code == "PROXY_LIMIT"
    assert guard.stats()["built"] == 2

    heavy = make(tri_budget=20)
    with pytest.raises(og.OccluderError) as exc:
        heavy.build(hull("h", triangles=64))
    assert exc.value.code == "PROXY_BUDGET"
    assert heavy.stats()["built"] == 0
    assert heavy.budget()["proxies"] == 0


def test_invalidate_marks_stale_and_only_stale_proxies_rebuild():
    guard = make()
    guard.build(box("a"))
    guard.build(box("b"))
    guard.invalidate("a")
    stale = guard.proxy_of("a")
    assert stale["stale"] is True
    assert stale["generation"] == 1

    fresh = guard.rebuild("a")
    assert fresh["stale"] is False
    assert fresh["generation"] == 2
    assert guard.stats()["rebuilt"] == 1
    guard.rebuild("a")
    assert guard.proxy_of("a")["generation"] == 2
    assert guard.stats()["rebuilt"] == 1

    guard.invalidate("a")
    guard.invalidate("b")
    assert guard.rebuild_pending() == 2
    assert guard.stats()["rebuilt"] == 3


def test_rebuild_with_new_bounds_refreshes_the_coverage():
    guard = make()
    guard.build(box("a"))
    assert guard.score("a", CAM) == pytest.approx(0.04 * 64.0 / 74.0)
    guard.invalidate("a", bounds=(-1.0, -1.0, -1.0, 3.0, 3.0, 3.0))
    guard.rebuild("a")
    assert guard.coverage("a") == pytest.approx(0.0)
    guard.score("a", CAM)
    assert guard.coverage("a") == pytest.approx(0.16)


def test_remove_frees_proxy_and_triangle_budget():
    guard = make()
    guard.build(box("a"))
    guard.build(box("b"))
    guard.remove("b")
    assert guard.budget()["proxies"] == 1
    assert guard.budget()["triangles"] == 12
    assert guard.stats()["removed"] == 1
    with pytest.raises(og.OccluderError) as exc:
        guard.proxy_of("b")
    assert exc.value.code == "PROXY_UNKNOWN_MESH"


def test_snapshot_is_detached_and_restore_keeps_coverage():
    guard = make()
    guard.build(box("near", (0.0, 0.0, 8.0, 2.0, 2.0, 10.0)))
    guard.build(box("mid"))
    guard.score("mid", CAM)
    state = guard.snapshot()

    state["proxies"].clear()
    state["stats"]["built"] = 99
    assert guard.stats()["built"] == 2
    assert guard.budget()["proxies"] == 2

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.stats()["built"] == 2
    assert restored.budget() == {
        "proxies": 2, "max_proxies": 16, "triangles": 24, "tri_budget": 400,
    }
    assert restored.coverage("mid") == pytest.approx(0.04)
    restored.invalidate("mid")
    assert restored.rebuild("mid")["generation"] == 2


def test_corrupt_snapshot_leaves_guard_usable():
    guard = make()
    guard.build(box("a"))
    good_stats = {key: 0 for key in STAT_KEYS}
    good_stats["built"] = 1
    bad_states = [
        None,
        {},
        {"version": 2, "proxies": {}, "stats": dict(good_stats)},
        {"version": 1, "proxies": "nope", "stats": dict(good_stats)},
        {"version": 1, "proxies": {}, "stats": {"built": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(og.OccluderError) as exc:
            guard.restore(bad)
        assert exc.value.code == "PROXY_SNAPSHOT_INVALID"

    assert guard.proxy_of("a")["triangles"] == 12
    guard.build(box("b"))
    assert guard.budget()["proxies"] == 2


def test_frame_accounting_and_stats_reset():
    guard = make()
    guard.build(box("near", (0.0, 0.0, 8.0, 2.0, 2.0, 10.0)))
    guard.build(box("mid"))
    guard.begin_frame()
    guard.begin_frame()
    assert guard.stats()["frames"] == 2
    assert guard.occluders(CAM) == ["near", "mid"]
    assert guard.last_frame()["kept"] == ["near", "mid"]

    guard.reset_stats()
    assert set(guard.stats()) == STAT_KEYS
    assert all(value == 0 for value in guard.stats().values())
    assert guard.budget()["proxies"] == 2
