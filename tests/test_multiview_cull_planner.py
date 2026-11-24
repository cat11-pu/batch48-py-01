import threading

import pytest

import multiview_cull_planner as mcp


def perspective(position=(0.0, 0.0, 5.0), target=(0.0, 0.0, 0.0),
               fov=90.0, near=1.0, far=20.0, aspect=1.0, layers=0xFFFF):
    return {"kind": "perspective", "position": position, "target": target,
            "up": (0.0, 1.0, 0.0), "fov_deg": fov, "aspect": aspect,
            "near": near, "far": far, "layers": layers}


def orthographic(position=(0.0, 0.0, 5.0), target=(0.0, 0.0, 0.0),
                 half_height=5.0, near=1.0, far=20.0, aspect=1.0,
                 layers=0xFFFF):
    return {"kind": "ortho", "position": position, "target": target,
            "up": (0.0, 1.0, 0.0), "half_height": half_height,
            "aspect": aspect, "near": near, "far": far, "layers": layers}


def box(center, half):
    return {"min": (center[0] - half, center[1] - half, center[2] - half),
            "max": (center[0] + half, center[1] + half, center[2] + half)}


def make(**kwargs):
    return mcp.MultiviewCullPlanner(**kwargs)


def test_add_view_reports_key_and_rejects_duplicate():
    planner = make()
    out = planner.add_view("v0", perspective())
    assert out["view_id"] == "v0"
    assert out["kind"] == "perspective"
    assert out["key"] == planner.view_key("v0")
    assert planner.views() == [out]

    with pytest.raises(mcp.CullError) as err:
        planner.add_view("v0", perspective(target=(1.0, 0.0, 0.0)))
    assert err.value.code == "CULL_VIEW_DUPLICATE"
    assert len(planner.views()) == 1


def test_add_view_rejects_malformed_camera():
    planner = make()
    bad = perspective()
    del bad["target"]
    with pytest.raises(mcp.CullError) as err:
        planner.add_view("v0", bad)
    assert err.value.code == "CULL_CAMERA_INVALID"

    with pytest.raises(mcp.CullError) as err:
        planner.add_view("v0", perspective(fov=0.0))
    assert err.value.code == "CULL_CAMERA_INVALID"

    with pytest.raises(mcp.CullError) as err:
        planner.add_view("v0", perspective(near=5.0, far=5.0))
    assert err.value.code == "CULL_CAMERA_INVALID"

    with pytest.raises(mcp.CullError) as err:
        planner.add_view("v0", {"kind": "fisheye"})
    assert err.value.code == "CULL_CAMERA_INVALID"


def test_set_object_reports_bounds_and_rejects_bad_box():
    planner = make()
    first = planner.set_object("o1", box((0.0, 0.0, 0.0), 1.0))
    assert first["obj_id"] == "o1"
    assert first["layers"] == 1
    assert first["replaced"] is False
    second = planner.set_object("o1", box((0.0, 0.0, 0.0), 2.0))
    assert second["replaced"] is True
    assert len(planner.objects()) == 1

    with pytest.raises(mcp.CullError) as err:
        planner.set_object("o2", {"min": (1.0, 1.0, 1.0),
                                  "max": (0.0, 0.0, 0.0)})
    assert err.value.code == "CULL_BOUNDS_INVALID"


def test_view_and_object_limits_and_removals():
    planner = make(max_views=2, max_objects=2)
    planner.add_view("v0", perspective())
    planner.add_view("v1", perspective(target=(0.0, 0.0, 1.0)))
    with pytest.raises(mcp.CullError) as err:
        planner.add_view("v2", perspective())
    assert err.value.code == "CULL_VIEW_LIMIT"

    planner.set_object("o1", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("o2", box((0.0, 0.0, 0.0), 2.0))
    with pytest.raises(mcp.CullError) as err:
        planner.set_object("o3", box((0.0, 0.0, 0.0), 1.0))
    assert err.value.code == "CULL_OBJECT_LIMIT"

    assert planner.remove_object("o2") is True
    assert planner.remove_view("v1") is True
    with pytest.raises(mcp.CullError) as err:
        planner.remove_object("o2")
    assert err.value.code == "CULL_OBJECT_UNKNOWN"
    with pytest.raises(mcp.CullError) as err:
        planner.remove_view("v1")
    assert err.value.code == "CULL_VIEW_UNKNOWN"


def test_cull_sorts_views_and_objects_ascending():
    planner = make()
    planner.add_view("vb", perspective(target=(0.0, 0.0, 1.0)))
    planner.add_view("va", perspective())
    planner.set_object("o2", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("o1", box((0.0, 0.0, 0.0), 1.0))
    out = planner.cull()
    assert [entry["view_id"] for entry in out["views"]] == ["va", "vb"]
    assert out["views"][0]["objects"] == ["o1", "o2"]
    assert out["views"][1]["objects"] == ["o1", "o2"]


def test_cull_drops_objects_behind_beyond_and_outside_frustum():
    planner = make()
    planner.add_view("v0", perspective())
    planner.set_object("inside", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("behind", box((0.0, 0.0, 10.0), 1.0))
    planner.set_object("beyond", box((0.0, 0.0, -30.0), 1.0))
    planner.set_object("side", box((100.0, 0.0, 0.0), 1.0))
    out = planner.cull()
    assert out["views"][0]["objects"] == ["inside"]
    assert out["masks"] == {"inside": 1, "behind": 0, "beyond": 0, "side": 0}
    assert out["visible"] == 1


def test_cull_respects_layer_masks():
    planner = make()
    planner.add_view("v0", perspective(layers=0b01))
    planner.set_object("ui", box((0.0, 0.0, 0.0), 1.0), layers=0b10)
    planner.set_object("world", box((0.0, 0.0, 0.0), 1.0), layers=0b01)
    out = planner.cull()
    assert out["views"][0]["objects"] == ["world"]
    assert out["masks"] == {"ui": 0, "world": 1}


def test_cull_builds_per_object_view_bitmask():
    planner = make()
    planner.add_view("v0", perspective())
    planner.add_view("v1", perspective(position=(0.0, 0.0, -5.0)))
    planner.set_object("both", box((2.0, 0.0, 0.0), 0.5))
    planner.set_object("front", box((0.0, 0.0, 8.0), 0.5))
    planner.set_object("back", box((0.0, 0.0, -8.0), 0.5))
    out = planner.cull()
    assert out["views"][0]["objects"] == ["back", "both"]
    assert out["views"][1]["objects"] == ["both", "front"]
    assert out["masks"] == {"both": 0b11, "front": 0b10, "back": 0b01}


def test_cull_with_no_objects_returns_empty_lists_and_masks():
    planner = make()
    planner.add_view("v0", perspective())
    out = planner.cull()
    assert out["views"] == [{"view_id": "v0", "objects": []}]
    assert out["masks"] == {}
    assert out["visible"] == 0


def test_lod_for_selects_level_from_perspective_screen_ratio():
    planner = make()
    planner.add_view("v0", perspective())
    planner.set_object("huge", box((0.0, 0.0, 0.0), 3.0))
    planner.set_object("branch", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("rock", box((0.0, 0.0, 0.0), 0.5))
    planner.set_object("pebble", box((0.0, 0.0, 0.0), 0.1))
    assert planner.lod_for("huge", "v0") == 0
    assert planner.lod_for("branch", "v0") == 1
    assert planner.lod_for("rock", "v0") == 2
    assert planner.lod_for("pebble", "v0") == 3


def test_lod_for_selects_level_from_orthographic_half_height():
    planner = make()
    planner.add_view("v0", orthographic(half_height=5.0))
    planner.set_object("branch", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("pebble", box((0.0, 0.0, 0.0), 0.1))
    assert planner.lod_for("branch", "v0") == 1
    assert planner.lod_for("pebble", "v0") == 3


def test_stable_views_groups_equivalent_cameras():
    planner = make()
    planner.add_view("v0", perspective())
    planner.add_view("v1", perspective(target=(0.0, 0.0, 1.0)))
    planner.add_view("v2", perspective())
    assert planner.view_key("v0") == planner.view_key("v2")
    assert planner.view_key("v0") != planner.view_key("v1")
    assert planner.stable_views() == [["v0", "v2"], ["v1"]]


def test_cull_is_deterministic_and_stats_accumulate():
    planner = make()
    planner.add_view("v0", perspective())
    planner.set_object("o1", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("o2", box((0.0, 0.0, 10.0), 1.0))
    first = planner.cull()
    second = planner.cull()
    assert first == second
    assert planner.stats() == {"views": 1, "objects": 2,
                               "culls": 2, "instances": 2}


def test_snapshot_restore_keeps_views_objects_and_masks():
    planner = make()
    planner.add_view("v0", perspective())
    planner.set_object("o1", box((0.0, 0.0, 0.0), 1.0))
    planner.set_object("o2", box((0.0, 0.0, 10.0), 1.0))
    before_views = planner.views()
    before_objects = planner.objects()
    before_cull = planner.cull()
    before_stats = planner.stats()
    state = planner.snapshot()

    planner.remove_object("o1")
    planner.add_view("v9", perspective(target=(0.0, 0.0, 1.0)))
    planner.cull()
    planner.restore(state)

    assert planner.views() == before_views
    assert planner.objects() == before_objects
    assert planner.cull() == before_cull
    assert planner.stats()["views"] == before_stats["views"]

    with pytest.raises(mcp.CullError) as err:
        planner.restore({"views": 7})
    assert err.value.code == "CULL_SNAPSHOT_INVALID"
    assert planner.views() == before_views


def test_concurrent_set_object_and_cull_keep_masks_consistent():
    planner = make()
    planner.add_view("v0", perspective())
    planner.add_view("v1", perspective(position=(0.0, 0.0, -5.0)))
    planner.set_object("seed", box((0.0, 0.0, 0.0), 1.0))

    errors = []
    stop = threading.Event()

    def writer():
        try:
            step = 0
            while not stop.is_set() and step < 60:
                planner.set_object(f"o{step}", box((0.0, 0.0, 0.0), 1.0))
                step += 1
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def reader():
        try:
            for _ in range(60):
                out = planner.cull()
                view_ids = [entry["view_id"] for entry in out["views"]]
                for index, entry in enumerate(out["views"]):
                    assert entry["objects"] == sorted(entry["objects"])
                    listed = set(entry["objects"])
                    expected = {obj for obj, mask in out["masks"].items()
                                if mask & (1 << index)}
                    assert listed == expected
                assert view_ids == sorted(view_ids)
                assert out["visible"] == sum(
                    len(entry["objects"]) for entry in out["views"])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    thread_w = threading.Thread(target=writer)
    thread_r = threading.Thread(target=reader)
    thread_w.start()
    thread_r.start()
    thread_w.join()
    stop.set()
    thread_r.join()

    assert errors == []
