import copy
import threading

import pytest

import visibility_buffer_resolver as vb


def make(width=4, height=4, **kwargs):
    resolver = vb.VisibilityBufferResolver(**kwargs)
    resolver.resize(width, height)
    return resolver


def triangle(tid=1, material="rock", params=None, pixels=None):
    return {
        "id": tid,
        "material": material,
        "params": {} if params is None else params,
        "pixels": pixels if pixels is not None else [
            {"x": 0, "y": 0, "depth": 0.5, "coverage": 1.0},
        ],
    }


def pixel(x, y, depth=0.5, coverage=1.0):
    return {"x": x, "y": y, "depth": depth, "coverage": coverage}


def test_resize_sets_dimensions_and_bumps_generation():
    resolver = vb.VisibilityBufferResolver()
    assert resolver.resize(4, 3) == {"width": 4, "height": 3, "generation": 1}
    assert resolver.generation() == 1
    assert resolver.resize(5, 2)["generation"] == 2
    assert resolver.buffered_triangles() == []

    bad_sizes = [(0, 3), (3, 0), (-1, 2), (513, 2), (2, 513), (2, "3")]
    for width, height in bad_sizes:
        with pytest.raises(vb.VisibilityError) as exc:
            resolver.resize(width, height)
        assert exc.value.code == "VBUF_INVALID_SIZE"
    assert resolver.stats()["rejected"] == len(bad_sizes)
    assert resolver.generation() == 2


def test_buffer_operations_require_resize():
    resolver = vb.VisibilityBufferResolver()
    calls = [
        lambda: resolver.submit_triangle(triangle()),
        lambda: resolver.resolve_pixel(0, 0),
        lambda: resolver.resolve_region(0, 0, 1, 1),
        lambda: resolver.clear(),
    ]
    for call in calls:
        with pytest.raises(vb.VisibilityError) as exc:
            call()
        assert exc.value.code == "VBUF_NOT_RESIZED"
    assert resolver.stats()["triangles_submitted"] == 0


def test_submit_triangle_validates_payload():
    resolver = make()
    bad_triangles = [
        {"id": 1, "material": "", "params": {}, "pixels": [pixel(0, 0)]},
        {"id": -1, "material": "rock", "params": {}, "pixels": [pixel(0, 0)]},
        {"id": 1, "material": "rock", "params": "nope", "pixels": [pixel(0, 0)]},
        {"id": 1, "material": "rock", "params": {}, "pixels": []},
        {"id": 1, "material": "rock", "params": {}, "pixels": [pixel(0, 0, depth=0.0)]},
        {"id": 1, "material": "rock", "params": {}, "pixels": [pixel(0, 0, coverage=0.0)]},
        {"id": 1, "material": "rock", "params": {}, "pixels": [pixel(0, 0, coverage=1.5)]},
        {"id": 1, "material": "rock", "params": {},
         "pixels": [{"x": 0, "y": 0, "depth": "0.5", "coverage": 1.0}]},
        {"id": 1, "material": "rock", "params": {}},
    ]
    for bad in bad_triangles:
        with pytest.raises(vb.VisibilityError) as exc:
            resolver.submit_triangle(bad)
        assert exc.value.code == "VBUF_INVALID_TRIANGLE"
    assert resolver.buffered_triangles() == []
    assert resolver.stats()["rejected"] == len(bad_triangles)
    assert resolver.stats()["triangles_submitted"] == 0


def test_duplicate_triangle_id_is_refused():
    resolver = make()
    resolver.submit_triangle(triangle(1, pixels=[pixel(0, 0)]))
    with pytest.raises(vb.VisibilityError) as exc:
        resolver.submit_triangle(triangle(1, pixels=[pixel(1, 1)]))
    assert exc.value.code == "VBUF_DUPLICATE_TRIANGLE"
    assert resolver.buffered_triangles() == [1]


def test_out_of_bounds_pixel_rejects_whole_triangle():
    resolver = make(4, 4)
    straddling = triangle(2, pixels=[pixel(0, 0), pixel(4, 0, depth=0.4)])
    with pytest.raises(vb.VisibilityError) as exc:
        resolver.submit_triangle(straddling)
    assert exc.value.code == "VBUF_OUT_OF_BOUNDS"
    assert resolver.buffered_triangles() == []
    assert resolver.resolve_pixel(0, 0)["triangle"] is None

    with pytest.raises(vb.VisibilityError) as exc:
        resolver.submit_triangle(triangle(3, pixels=[pixel(0, -1)]))
    assert exc.value.code == "VBUF_OUT_OF_BOUNDS"


def test_triangle_capacity_is_a_hard_cap():
    resolver = make(4, 4, max_triangles=2)
    resolver.submit_triangle(triangle(1, pixels=[pixel(0, 0)]))
    resolver.submit_triangle(triangle(2, pixels=[pixel(1, 0)]))
    with pytest.raises(vb.VisibilityError) as exc:
        resolver.submit_triangle(triangle(3, pixels=[pixel(2, 0)]))
    assert exc.value.code == "VBUF_TOO_MANY_TRIANGLES"
    assert exc.value.detail["triangle_count"] == 2
    assert exc.value.detail["max_triangles"] == 2
    assert resolver.buffered_triangles() == [1, 2]


def test_nearest_depth_wins_regardless_of_submission_order():
    submissions = {
        1: triangle(1, material="near", pixels=[pixel(0, 0, depth=0.25)]),
        2: triangle(2, material="far", pixels=[pixel(0, 0, depth=0.75)]),
    }
    for order in ((1, 2), (2, 1)):
        resolver = make(2, 2)
        for tid in order:
            resolver.submit_triangle(copy.deepcopy(submissions[tid]))
        cell = resolver.resolve_pixel(0, 0)
        assert cell["triangle"] == 1
        assert cell["material"] == "near"
        assert cell["depth"] == pytest.approx(0.25)
        assert cell["coverage"] == pytest.approx(1.0)


def test_later_triangle_only_replaces_when_closer():
    resolver = make()
    resolver.submit_triangle(triangle(1, pixels=[pixel(1, 1, depth=0.4)]))
    resolver.submit_triangle(triangle(2, material="sand", pixels=[pixel(1, 1, depth=0.6)]))
    assert resolver.resolve_pixel(1, 1)["triangle"] == 1

    resolver.submit_triangle(triangle(3, material="water", pixels=[pixel(1, 1, depth=0.1)]))
    cell = resolver.resolve_pixel(1, 1)
    assert cell["triangle"] == 3
    assert cell["material"] == "water"
    assert cell["depth"] == pytest.approx(0.1)


def test_untouched_pixel_resolves_to_background():
    resolver = make()
    resolver.submit_triangle(triangle(1, pixels=[pixel(0, 0)]))
    empty = resolver.resolve_pixel(2, 2)
    assert empty == {
        "x": 2,
        "y": 2,
        "triangle": None,
        "material": None,
        "params": {},
        "depth": None,
        "coverage": 0.0,
        "generation": resolver.generation(),
    }


def test_pixel_and_region_queries_reject_bad_coordinates():
    resolver = make(4, 4)
    for coords in ((4, 0), (0, 4), (-1, 0), (0, -1)):
        with pytest.raises(vb.VisibilityError) as exc:
            resolver.resolve_pixel(*coords)
        assert exc.value.code == "VBUF_OUT_OF_BOUNDS"

    bad_regions = [(3, 0, 2, 1), (0, 0, 4, 1), (-1, 0, 1, 1), (0, 0, 1, 4)]
    for region in bad_regions:
        with pytest.raises(vb.VisibilityError) as exc:
            resolver.resolve_region(*region)
        assert exc.value.code == "VBUF_INVALID_REGION"


def test_region_matches_per_pixel_resolution():
    resolver = make(3, 3)
    resolver.submit_triangle(triangle(1, pixels=[pixel(0, 0, depth=0.5)]))
    resolver.submit_triangle(triangle(2, pixels=[pixel(1, 1, depth=0.2)]))
    resolver.submit_triangle(triangle(3, pixels=[pixel(2, 2, depth=0.9)]))

    region = resolver.resolve_region(0, 0, 2, 2)
    assert region["x0"] == 0
    assert region["y0"] == 0
    assert region["x1"] == 2
    assert region["y1"] == 2
    assert region["generation"] == resolver.generation()
    assert len(region["cells"]) == 3
    assert all(len(row) == 3 for row in region["cells"])
    assert region["cells"][0][0]["triangle"] == 1
    assert region["cells"][1][1]["triangle"] == 2
    assert region["cells"][2][2]["triangle"] == 3
    assert region["cells"][0][1]["triangle"] is None

    for y in range(3):
        for x in range(3):
            cell = resolver.resolve_pixel(x, y)
            assert region["cells"][y][x]["triangle"] == cell["triangle"]
            assert region["cells"][y][x]["depth"] == cell["depth"]
    assert resolver.stats()["regions_resolved"] == 1

    single = resolver.resolve_region(1, 1, 1, 1)
    assert single["cells"] == [[region["cells"][1][1]]]


def test_resolve_cache_is_keyed_by_generation():
    resolver = make()
    resolver.submit_triangle(triangle(1, params={"tint": [1, 2, 3]}))
    first = resolver.resolve_pixel(0, 0)
    first["params"]["oops"] = 1
    second = resolver.resolve_pixel(0, 0)
    assert "oops" not in second["params"]
    assert resolver.stats()["pixels_resolved"] == 2
    assert resolver.stats()["cache_hits"] == 1

    resolver.submit_triangle(triangle(2, material="sand",
                                       pixels=[pixel(0, 0, depth=0.1)]))
    third = resolver.resolve_pixel(0, 0)
    assert third["triangle"] == 2
    assert third["generation"] == resolver.generation()
    assert resolver.stats()["cache_hits"] == 1


def test_clear_empties_buffer_and_keeps_dimensions():
    resolver = make(5, 2)
    resolver.submit_triangle(triangle(1, pixels=[pixel(0, 0)]))
    before = resolver.generation()
    cleared = resolver.clear()
    assert cleared == {"width": 5, "height": 2, "generation": before + 1}
    assert resolver.buffered_triangles() == []
    assert resolver.resolve_pixel(0, 0)["triangle"] is None
    resolver.submit_triangle(triangle(1, pixels=[pixel(0, 0)]))
    assert resolver.buffered_triangles() == [1]


def test_snapshot_restore_and_corrupt_state():
    resolver = make()
    resolver.submit_triangle(triangle(1, material="rock", params={"uv": [1, 2]},
                                      pixels=[pixel(0, 0, depth=0.5)]))
    state = resolver.snapshot()
    pristine = copy.deepcopy(state)
    assert pristine["version"] == 1
    assert pristine["width"] == 4
    assert pristine["height"] == 4
    assert len(pristine["triangles"]) == 1

    state["triangles"][0]["material"] = "hacked"
    assert resolver.resolve_pixel(0, 0)["material"] == "rock"

    restored = vb.VisibilityBufferResolver()
    restored.restore(pristine)
    assert restored.generation() == resolver.generation()
    cell = restored.resolve_pixel(0, 0)
    assert cell["triangle"] == 1
    assert cell["material"] == "rock"
    assert cell["params"] == {"uv": [1, 2]}

    good = copy.deepcopy(pristine)
    bad_states = [
        None,
        {},
        {"version": 2, "width": 4, "height": 4, "generation": 1,
         "triangles": [], "stats": good["stats"]},
        {"version": 1, "width": 0, "height": 4, "generation": 1,
         "triangles": [], "stats": good["stats"]},
        {"version": 1, "width": 4, "height": 4, "generation": 1,
         "triangles": [], "stats": {"triangles_submitted": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(vb.VisibilityError) as exc:
            restored.restore(bad)
        assert exc.value.code == "VBUF_SNAPSHOT_INVALID"
    assert restored.resolve_pixel(0, 0)["triangle"] == 1


def test_concurrent_submissions_of_disjoint_triangles_all_land():
    resolver = make(8, 8)
    errors = []

    def worker(index):
        try:
            for round_no in range(2):
                tid = index * 2 + round_no + 1
                resolver.submit_triangle(
                    triangle(tid, pixels=[pixel(index, round_no, depth=0.5)]))
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(resolver.buffered_triangles()) == list(range(1, 17))
    assert resolver.stats()["triangles_submitted"] == 16
    assert resolver.resolve_pixel(3, 1)["triangle"] == 8
    assert resolver.resolve_region(0, 0, 7, 1)["cells"][1][3]["triangle"] == 8
