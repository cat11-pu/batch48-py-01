import copy
import threading

import pytest

import texture_stream_budget as tsb


def test_register_and_base_conflict():
    budget = tsb.TextureStreamBudget()
    info = budget.register("tex", 4096)
    assert info["texture_id"] == "tex"
    assert info["base_bytes"] == 4096
    assert info["mips"] == 15
    assert budget.register("tex", 4096) == info
    with pytest.raises(tsb.TextureStreamError) as exc:
        budget.register("tex", 2048)
    assert exc.value.code == "TEXTURE_BASE_CONFLICT"
    for bad in (0, -16):
        with pytest.raises(tsb.TextureStreamError) as exc:
            budget.register("bad", bad)
        assert exc.value.code == "TEXTURE_BASE_INVALID"


def test_request_validation():
    budget = tsb.TextureStreamBudget(max_mip=4)
    budget.register("tex", 1024)
    for bad_mip in (-1, 4, 9):
        with pytest.raises(tsb.TextureStreamError) as exc:
            budget.request("tex", bad_mip, 1.0)
        assert exc.value.code == "TEXTURE_MIP_RANGE"
    with pytest.raises(tsb.TextureStreamError) as exc:
        budget.request("tex", 1, -0.5)
    assert exc.value.code == "TEXTURE_ERROR_INVALID"
    with pytest.raises(tsb.TextureStreamError) as exc:
        budget.request("ghost", 1, 1.0)
    assert exc.value.code == "TEXTURE_UNKNOWN_TEXTURE"
    record = budget.request("tex", 2, 7.5)
    assert record == {
        "texture_id": "tex", "mip": 2, "screen_error": 7.5, "merged": False,
    }


def test_upload_plan_follows_priority_order():
    budget = tsb.TextureStreamBudget()
    budget.register("t1", 1000)
    budget.register("t2", 1000)
    budget.request("t1", 1, 1.0)
    budget.request("t2", 1, 9.0)
    planned = budget.plan(10000)
    assert [item["texture_id"] for item in planned["uploads"]] == ["t2", "t1"]
    assert planned["uploads"][0]["from_mip"] == -1
    assert planned["uploads"][0]["bytes"] == 500
    assert planned["used_bytes"] == 1000
    assert planned["evictions"] == []
    assert planned["deferred"] == []


def test_over_budget_request_waits_for_next_frame():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 1000)
    budget.request("tex", 1, 4.0)
    first = budget.plan(100)
    assert first["uploads"] == []
    assert first["deferred"] == ["tex"]
    assert budget.resident("tex") == -1
    assert budget.stats()["deferred"] == 1

    second = budget.plan(500)
    assert len(second["uploads"]) == 1
    assert budget.resident("tex") == 1
    assert second["deferred"] == []


def test_duplicate_requests_collapse_to_one_upload():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 1000)
    assert budget.request("tex", 2, 5.0)["merged"] is False
    assert budget.request("tex", 1, 3.0)["merged"] is True
    planned = budget.plan(10000)
    assert len(planned["uploads"]) == 1
    assert planned["uploads"][0]["to_mip"] == 1
    assert planned["uploads"][0]["bytes"] == 500
    assert budget.stats()["merged"] == 1


def test_already_resident_request_is_a_hit():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 1000)
    budget.request("tex", 1, 2.0)
    budget.plan(10000)
    budget.request("tex", 1, 8.0)
    planned = budget.plan(10000)
    assert planned["uploads"] == []
    assert budget.stats()["hits"] == 1
    assert budget.resident("tex") == 1


def test_eviction_drops_exactly_one_mip_level():
    budget = tsb.TextureStreamBudget()
    budget.register("a", 1000)
    budget.register("b", 1000)
    budget.request("a", 2, 9.0)
    budget.request("b", 2, 1.0)
    first = budget.plan(600)
    assert len(first["uploads"]) == 2
    assert budget.resident("a") == 2
    assert budget.resident("b") == 2

    budget.request("a", 0, 9.0)
    second = budget.plan(1000)
    assert second["evictions"] == [{
        "texture_id": "b", "from_mip": 2, "to_mip": 3, "bytes": 125,
    }]
    assert budget.resident("a") == 0
    assert budget.resident("b") == 3
    assert second["resident_bytes"] == 1125


def test_pinned_texture_keeps_its_mip():
    budget = tsb.TextureStreamBudget()
    budget.register("a", 1000)
    budget.register("b", 1000)
    budget.request("a", 2, 9.0)
    budget.request("b", 2, 1.0)
    budget.plan(600)
    assert budget.pin("b") is True
    assert budget.pin("b") is True
    assert budget.pinned() == ["b"]

    budget.request("a", 0, 9.0)
    planned = budget.plan(1000)
    assert planned["evictions"] == []
    assert budget.resident("b") == 2

    assert budget.unpin("b") is True
    assert budget.unpin("b") is False
    assert budget.pinned() == []


def test_budget_bounds_are_enforced():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 1000)
    budget.request("tex", 1, 1.0)
    with pytest.raises(tsb.TextureStreamError) as exc:
        budget.plan(64 * 1024 * 1024 + 1)
    assert exc.value.code == "TEXTURE_BUDGET_EXCEEDED"
    for bad in (0, -1, "lots"):
        with pytest.raises(tsb.TextureStreamError) as exc:
            budget.plan(bad)
        assert exc.value.code == "TEXTURE_BUDGET_INVALID"
    assert budget.resident("tex") == -1


def test_texture_count_and_mip_ceiling():
    budget = tsb.TextureStreamBudget(max_textures=2, max_mip=4)
    budget.register("a", 500)
    budget.register("b", 500)
    with pytest.raises(tsb.TextureStreamError) as exc:
        budget.register("c", 500)
    assert exc.value.code == "TEXTURE_TEXTURE_LIMIT"
    with pytest.raises(tsb.TextureStreamError) as exc:
        budget.request("a", 4, 1.0)
    assert exc.value.code == "TEXTURE_MIP_RANGE"


def test_stats_shape_and_reset():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 1000)
    budget.request("tex", 1, 5.0)
    budget.request("tex", 1, 3.0)
    budget.plan(10000)
    budget.request("tex", 1, 2.0)
    budget.plan(10000)
    stats = budget.stats()
    assert set(stats) == {
        "requests", "merged", "hits", "uploads", "evictions", "deferred",
    }
    assert stats == {
        "requests": 3, "merged": 1, "hits": 1,
        "uploads": 1, "evictions": 0, "deferred": 0,
    }
    budget.reset_stats()
    assert budget.stats() == {
        "requests": 0, "merged": 0, "hits": 0,
        "uploads": 0, "evictions": 0, "deferred": 0,
    }


def test_snapshot_restore_keeps_residency_and_pins():
    budget = tsb.TextureStreamBudget()
    budget.register("a", 2000)
    budget.register("b", 1000)
    budget.request("a", 1, 5.0)
    budget.request("b", 2, 1.0)
    budget.plan(10000)
    budget.pin("b")
    state = budget.snapshot()
    pristine = copy.deepcopy(state)
    assert set(state) == {
        "version", "textures", "resident", "pins", "pending", "stats",
    }
    state["resident"].clear()
    state["pins"].clear()
    assert budget.resident("a") == 1
    assert budget.pinned() == ["b"]

    restored = tsb.TextureStreamBudget()
    restored.restore(pristine)
    assert restored.resident("a") == 1
    assert restored.resident("b") == 2
    assert restored.pinned() == ["b"]
    assert restored.stats()["uploads"] == 2


def test_corrupt_snapshot_is_refused_without_state_change():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 1000)
    budget.request("tex", 1, 1.0)
    budget.plan(10000)
    good_entry = {"base_bytes": 1000}
    bad_states = [
        None,
        {},
        {"version": 2, "textures": {}, "resident": {}, "pins": [],
         "pending": {}, "stats": {}},
        {"version": 1, "textures": "nope", "resident": {}, "pins": [],
         "pending": {}, "stats": {}},
        {"version": 1, "textures": {"tex": good_entry}, "resident": "nope",
         "pins": [], "pending": {}, "stats": {}},
        {"version": 1, "textures": {"tex": good_entry}, "resident": {"tex": 1},
         "pins": "nope", "pending": {}, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(tsb.TextureStreamError) as exc:
            budget.restore(bad)
        assert exc.value.code == "TEXTURE_SNAPSHOT_INVALID"
    assert budget.resident("tex") == 1


def test_default_budget_and_mip_byte_math():
    budget = tsb.TextureStreamBudget()
    budget.register("tex", 10000)
    budget.request("tex", 4, 0.0)
    planned = budget.plan()
    assert planned["used_bytes"] == 625
    assert planned["uploads"][0]["bytes"] == 625
    assert planned["uploads"][0]["to_mip"] == 4
    assert budget.resident("tex") == 4


def test_concurrent_requests_and_plans_keep_entries_consistent():
    budget = tsb.TextureStreamBudget()
    for index in range(4):
        budget.register(f"t{index}", 1000)
    budget.plan(4096)
    errors = []
    plans = []

    def worker(worker_id):
        try:
            for step in range(25):
                budget.request(f"t{worker_id}", step % 3, float(step))
                plans.append(budget.plan(4096))
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append((worker_id, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(plans) == 100
    for planned in plans:
        for item in planned["uploads"]:
            assert item["to_mip"] in range(15)
            assert item["from_mip"] != item["to_mip"]
            assert item["bytes"] == max(1, 1000 >> item["to_mip"])
        for item in planned["evictions"]:
            assert item["to_mip"] == item["from_mip"] + 1
    assert budget.resident("t0") in range(-1, 15)
