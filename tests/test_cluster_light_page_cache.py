import copy
import threading

import pytest

import cluster_light_page_cache as clc


def light(index, x, y, z, radius=0.1, intensity=1.0):
    return {
        "index": index,
        "position": (x, y, z),
        "radius": radius,
        "intensity": intensity,
    }


def test_grid_registration_and_page_code():
    cache = clc.ClusterLightPageCache()
    out = cache.set_grid(16, 16, 4)
    assert out["grid"] == [16, 16, 4]
    assert out["page_size"] == 8
    assert out["pages"] == 4
    assert cache.page_code(0, 0, 0) == "g16x16x4-s8-p0.0.0"
    assert cache.page_code(1, 1, 0) == "g16x16x4-s8-p1.1.0"
    assert cache.set_page_size(4) == 16
    assert cache.page_code(1, 1, 0) == "g16x16x4-s4-p1.1.0"


def test_invalid_grid_and_page_size_leave_state_intact():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(16, 16, 4)
    with pytest.raises(clc.ClusterLightError) as exc:
        cache.set_grid(0, 4, 4)
    assert exc.value.code == "CLUSTER_GRID_INVALID"
    with pytest.raises(clc.ClusterLightError) as exc:
        cache.set_grid(4, -1, 4)
    assert exc.value.code == "CLUSTER_GRID_INVALID"
    with pytest.raises(clc.ClusterLightError) as exc:
        cache.set_page_size(0)
    assert exc.value.code == "CLUSTER_PAGE_SIZE_INVALID"
    assert cache.page_code(0, 0, 0) == "g16x16x4-s8-p0.0.0"


def test_page_table_limit_rejects_without_replacing_grid():
    cache = clc.ClusterLightPageCache(max_pages=8)
    cache.set_grid(16, 16, 4)
    with pytest.raises(clc.ClusterLightError) as exc:
        cache.set_grid(32, 32, 32)
    assert exc.value.code == "CLUSTER_PAGE_LIMIT"
    assert cache.page_code(0, 0, 0) == "g16x16x4-s8-p0.0.0"


def test_upsert_marks_pages_dirty_and_rebuild_commits():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    assert cache.set_page_size(4) == 8
    assert cache.upsert_light(light(0, 1, 1, 1, intensity=3.0)) == 1
    assert cache.rebuild(False) == 8
    assert cache.page(0, 0, 0) == [0]
    assert cache.page(1, 1, 1) == []
    stats = cache.stats()
    assert stats["lights"] == 1
    assert stats["dirty_pages"] == 0
    assert stats["hot_pages"] == 1


def test_sphere_spanning_pages_appears_once_per_page():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    assert cache.upsert_light(light(5, 4, 4, 4, radius=0.5, intensity=2.0)) == 8
    assert cache.rebuild(True) == 8
    for ix in range(2):
        for iy in range(2):
            for iz in range(2):
                contents = cache.page(ix, iy, iz)
                assert contents == [5]


def test_light_fully_outside_grid_touches_no_page():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    assert cache.upsert_light(light(9, 20, 20, 20, radius=1.0)) == 0
    assert cache.rebuild(True) == 8
    for ix in range(2):
        for iy in range(2):
            for iz in range(2):
                assert cache.page(ix, iy, iz) == []
    assert cache.stats()["hot_pages"] == 0
    assert cache.stats()["lights"] == 1


def test_page_capacity_overflow_is_counted_not_overwritten():
    cache = clc.ClusterLightPageCache(page_capacity=64)
    cache.set_grid(4, 4, 4)
    cache.set_page_size(4)
    for index in range(70):
        cache.upsert_light(light(index, 1, 1, 1, intensity=70.0 - index))
    assert cache.rebuild(True) == 1
    assert cache.page(0, 0, 0) == list(range(64))
    assert cache.stats()["overflow_pages"] == 1
    assert cache.stats()["lights"] == 70


def test_dirty_only_rebuild_touches_just_dirty_pages():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    assert cache.rebuild(False) == 8
    cache.upsert_light(light(0, 1, 1, 1))
    assert cache.rebuild(True) == 1
    assert cache.page(1, 1, 1) == []
    cache.upsert_light(light(1, 5, 1, 1))
    assert cache.rebuild(True) == 1
    assert cache.page(1, 0, 0) == [1]
    assert cache.rebuild(False) == 8


def test_page_coordinate_out_of_range_is_rejected():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    for coords in ((2, 0, 0), (0, -1, 0), (0, 0, 5)):
        with pytest.raises(clc.ClusterLightError) as exc:
            cache.page(*coords)
        assert exc.value.code == "CLUSTER_PAGE_RANGE"


def test_stats_shape_and_reset_keeps_pages():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    cache.upsert_light(light(0, 1, 1, 1, intensity=4.0))
    cache.rebuild(True)
    stats = cache.stats()
    assert set(stats) == {
        "lights", "dirty_pages", "hot_pages", "rebuilds", "overflow_pages",
    }
    assert stats["lights"] == 1
    assert stats["hot_pages"] == 1
    assert stats["rebuilds"] == 1
    cache.reset_stats()
    cleared = cache.stats()
    assert cleared["rebuilds"] == 0
    assert cleared["overflow_pages"] == 0
    assert cleared["lights"] == 1
    assert cleared["hot_pages"] == 1
    assert cache.page(0, 0, 0) == [0]


def test_remove_and_update_light_clears_old_pages():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    cache.upsert_light(light(3, 1, 1, 1))
    cache.rebuild(True)
    assert cache.remove_light(3) == 1
    assert cache.rebuild(True) == 1
    assert cache.page(0, 0, 0) == []

    cache.upsert_light(light(7, 1, 1, 1))
    cache.rebuild(True)
    assert cache.page(0, 0, 0) == [7]
    cache.upsert_light(light(7, 5, 5, 5))
    cache.rebuild(True)
    assert cache.page(0, 0, 0) == []
    assert cache.page(1, 1, 1) == [7]


def test_snapshot_restore_reproduces_pages_and_is_detached():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    cache.upsert_light(light(0, 1, 1, 1, intensity=2.5))
    cache.rebuild(False)
    state = cache.snapshot()
    pristine = copy.deepcopy(state)
    assert set(state) == {"version", "grid", "page_size", "lights", "pages", "stats"}
    state["lights"].clear()
    state["pages"].clear()
    assert cache.stats()["lights"] == 1
    assert cache.page(0, 0, 0) == [0]

    restored = clc.ClusterLightPageCache()
    restored.restore(pristine)
    assert restored.page_code(0, 0, 0) == "g8x8x8-s4-p0.0.0"
    assert restored.page(0, 0, 0) == [0]
    assert restored.rebuild(False) == 8
    assert restored.page(0, 0, 0) == [0]


def test_corrupt_snapshot_is_refused_and_live_state_survives():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    cache.upsert_light(light(0, 1, 1, 1))
    cache.rebuild(False)
    bad_states = [
        None,
        {},
        {"version": 2, "grid": [8, 8, 8], "page_size": 4,
         "lights": {}, "pages": {}, "stats": {}},
        {"version": 1, "grid": "eight", "page_size": 4,
         "lights": {}, "pages": {}, "stats": {}},
        {"version": 1, "grid": [8, 8, 8], "page_size": 0,
         "lights": {}, "pages": {}, "stats": {}},
        {"version": 1, "grid": [8, 8, 8], "page_size": 4,
         "lights": "nope", "pages": {}, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(clc.ClusterLightError) as exc:
            cache.restore(bad)
        assert exc.value.code == "CLUSTER_SNAPSHOT_INVALID"
    assert cache.page(0, 0, 0) == [0]


def test_concurrent_upsert_and_page_reads_see_whole_lists():
    cache = clc.ClusterLightPageCache(page_capacity=256)
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    cache.rebuild(False)
    errors = []
    observed = []

    def worker(worker_id):
        try:
            for step in range(20):
                cache.upsert_light(
                    light(worker_id * 20 + step, 1.0, 1.0, 1.0,
                          intensity=float(step))
                )
                for _ in range(3):
                    contents = cache.page(0, 0, 0)
                    observed.append(len(contents))
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append((worker_id, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert cache.rebuild(True) == 1
    final = cache.page(0, 0, 0)
    assert len(final) == 160
    assert len(set(final)) == 160
    assert all(length <= 256 for length in observed)


def test_repeated_rebuild_is_deterministic():
    cache = clc.ClusterLightPageCache()
    cache.set_grid(8, 8, 8)
    cache.set_page_size(4)
    cache.upsert_light(light(0, 1, 1, 1, intensity=1.0))
    cache.upsert_light(light(1, 5, 5, 5, intensity=2.0))
    assert cache.rebuild(False) == 8
    first = {(ix, iy, iz): cache.page(ix, iy, iz)
             for ix in range(2) for iy in range(2) for iz in range(2)}
    assert cache.rebuild(False) == 8
    second = {(ix, iy, iz): cache.page(ix, iy, iz)
              for ix in range(2) for iy in range(2) for iz in range(2)}
    assert first == second

    twin = clc.ClusterLightPageCache()
    twin.set_grid(8, 8, 8)
    twin.set_page_size(4)
    twin.upsert_light(light(1, 5, 5, 5, intensity=2.0))
    twin.upsert_light(light(0, 1, 1, 1, intensity=1.0))
    twin.rebuild(False)
    for coords, contents in first.items():
        assert twin.page(*coords) == contents
        assert twin.page_code(*coords) == cache.page_code(*coords)
