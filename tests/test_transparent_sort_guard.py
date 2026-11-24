import pytest

import transparent_sort_guard as tsg

STAT_KEYS = {"frames", "reused_order", "reordered_objects", "degraded",
             "depth_updates"}


def make(**kwargs):
    return tsg.TransparentSortGuard(**kwargs)


def ids(draws):
    return [entry["object_id"] for entry in draws]


def test_add_reports_bucket_and_key_and_rejects_duplicates():
    guard = make()
    result = guard.add("a", depth=0.0)
    assert result["object_id"] == "a"
    assert result["bucket"] == 0
    assert result["keys"] == [guard.key_for("a")]
    assert guard.object_count() == 1
    assert guard.bucket_of("a") == 0

    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.add("a", depth=10.0)
    assert exc.value.code == "SORT_DUPLICATE_OBJECT"
    assert guard.object_count() == 1


def test_depth_is_quantized_into_the_configured_bucket_count():
    guard = make(depth_bits=8)
    guard.add("a", depth=0.0)
    assert guard.bucket_of("a") == 0
    assert guard.update_depth("a", 1000.0) == 255
    assert guard.update_depth("a", 500.0) == 127
    assert guard.update_depth("a", 999.0) == 254
    assert guard.update_depth("a", 250.0) == 63
    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.update_depth("ghost", 10.0)
    assert exc.value.code == "SORT_UNKNOWN_OBJECT"


def test_sort_returns_far_to_near_with_ascending_keys():
    guard = make()
    guard.add("a", depth=900.0)
    guard.add("b", depth=500.0)
    guard.add("c", depth=100.0)
    draws = guard.sort()
    assert ids(draws) == ["a", "b", "c"]
    keys = [entry["key"] for entry in draws]
    assert keys == sorted(keys)
    assert all(entry["side"] == 0 for entry in draws)
    assert guard.stats()["frames"] == 1


def test_objects_in_the_same_bucket_keep_insertion_order():
    guard = make()
    guard.add("a", depth=100.0)
    guard.add("b", depth=100.0)
    guard.add("c", depth=900.0)
    assert ids(guard.sort()) == ["c", "a", "b"]
    assert guard.bucket_of("a") == guard.bucket_of("b")


def test_layer_outranks_depth():
    guard = make()
    guard.add("near0", layer=0, depth=100.0)
    guard.add("far1", layer=1, depth=900.0)
    draws = guard.sort()
    assert ids(draws) == ["near0", "far1"]
    assert draws[0]["key"] < draws[1]["key"]


def test_material_batch_outranks_depth_inside_a_layer():
    guard = make()
    guard.add("a", material="glass", depth=900.0)
    guard.add("b", material="water", depth=100.0)
    guard.add("c", material="glass", depth=50.0)
    assert ids(guard.sort()) == ["a", "c", "b"]


def test_double_sided_material_emits_two_adjacent_draws():
    guard = make()
    guard.add("a", double_sided=True, depth=500.0)
    guard.add("b", depth=300.0)
    draws = guard.sort()
    assert ids(draws) == ["a", "a", "b"]
    assert [entry["side"] for entry in draws] == [0, 1, 0]
    assert draws[1]["key"] - draws[0]["key"] == 1
    assert guard.keys("a") == [draws[0]["key"], draws[1]["key"]]
    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.key_for("b", side=1)
    assert exc.value.code == "SORT_SIDE_INVALID"


def test_key_layout_is_fixed_layer_batch_depth_index():
    guard = make(depth_bits=8)
    guard.add("a", layer=3, depth=0.0)
    key = guard.key_for("a")
    assert key < 2 ** 64
    assert key >> 56 == 3
    assert (key >> 44) & 0xFFF == 0
    assert (key >> 36) & 0xFF == 255
    assert key & ((1 << 36) - 1) == 0

    guard.add("b", material="mat2", depth=1000.0)
    assert (guard.key_for("b") >> 44) & 0xFFF == 1


def test_unmodified_sort_is_reused_without_reordering():
    guard = make()
    guard.add("a", depth=900.0)
    guard.add("b", depth=100.0)
    first = guard.sort()
    assert guard.stats()["reordered_objects"] == 2
    second = guard.sort()
    assert second == first
    assert guard.stats()["reused_order"] == 1
    assert guard.stats()["reordered_objects"] == 2
    assert guard.stats()["frames"] == 2


def test_single_depth_change_reorders_globally():
    guard = make()
    guard.add("a", depth=900.0)
    guard.add("b", depth=700.0)
    guard.add("c", depth=100.0)
    assert ids(guard.sort()) == ["a", "b", "c"]
    assert guard.update_depth("b", 50.0) == 12
    draws = guard.sort()
    assert ids(draws) == ["a", "c", "b"]
    keys = [entry["key"] for entry in draws]
    assert keys == sorted(keys)
    assert guard.stats()["reordered_objects"] == 5
    assert guard.stats()["depth_updates"] == 1


def test_sort_budget_overflow_degrades_to_coarse_bucket_order():
    guard = make(sort_budget=1)
    guard.add("b", depth=500.0)
    guard.add("a", depth=501.0)
    guard.add("z", depth=900.0)
    assert guard.bucket_of("a") == guard.bucket_of("b")
    assert ids(guard.sort()) == ["z", "b", "a"]

    guard.update_depth("b", 700.0)
    guard.update_depth("a", 700.0)
    draws = guard.sort()
    assert ids(draws) == ["z", "a", "b"]
    assert guard.stats()["degraded"] == 1


def test_object_cap_and_basic_field_validation():
    guard = make(max_objects=2)
    guard.add("a", depth=0.0)
    guard.add("b", depth=1.0)
    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.add("c", depth=2.0)
    assert exc.value.code == "SORT_LIMIT"
    guard.remove("b")
    guard.add("c", depth=2.0)
    assert guard.object_count() == 2

    fresh = make()
    with pytest.raises(tsg.TransparentSortError) as exc:
        fresh.add("")
    assert exc.value.code == "SORT_OBJECT_INVALID"
    with pytest.raises(tsg.TransparentSortError) as exc:
        fresh.add("x", layer=256)
    assert exc.value.code == "SORT_LAYER_INVALID"
    with pytest.raises(tsg.TransparentSortError) as exc:
        fresh.add("x", depth=-1.0)
    assert exc.value.code == "SORT_DEPTH_OUT_OF_RANGE"


def test_depth_range_side_and_unknown_object_errors():
    guard = make()
    guard.add("a", depth=0.0)
    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.update_depth("a", 1001.0)
    assert exc.value.code == "SORT_DEPTH_OUT_OF_RANGE"
    for side in (1, 2):
        with pytest.raises(tsg.TransparentSortError) as exc:
            guard.key_for("a", side=side)
        assert exc.value.code == "SORT_SIDE_INVALID"
    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.key_for("ghost")
    assert exc.value.code == "SORT_UNKNOWN_OBJECT"
    with pytest.raises(tsg.TransparentSortError) as exc:
        guard.remove("ghost")
    assert exc.value.code == "SORT_UNKNOWN_OBJECT"


def test_snapshot_restore_preserves_order_keys_and_counters():
    guard = make()
    guard.add("a", depth=900.0)
    guard.add("b", depth=500.0)
    before = guard.sort()
    state = guard.snapshot()
    state["objects"].clear()
    state["stats"]["frames"] = 99
    assert guard.object_count() == 2
    assert guard.stats()["frames"] == 1

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.object_count() == 2
    after = restored.sort()
    assert after == before
    assert restored.stats()["frames"] == 2
    assert restored.stats()["reused_order"] == 1

    assert restored.update_depth("b", 950.0) == 242
    assert ids(restored.sort()) == ["b", "a"]


def test_corrupt_snapshot_is_rejected_and_stats_reset():
    guard = make()
    guard.add("a", depth=900.0)
    guard.add("b", depth=100.0)
    first = guard.sort()
    good_stats = {key: 0 for key in STAT_KEYS}
    good_stats["frames"] = 1
    bad_states = [
        None,
        {},
        {"version": 2, "objects": {}, "materials": {}, "counter": 0,
         "stats": dict(good_stats)},
        {"version": 1, "objects": "nope", "materials": {}, "counter": 0,
         "stats": dict(good_stats)},
        {"version": 1, "objects": {}, "materials": {}, "counter": 0,
         "stats": {"frames": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(tsg.TransparentSortError) as exc:
            guard.restore(bad)
        assert exc.value.code == "SORT_SNAPSHOT_INVALID"

    assert ids(guard.sort()) == ids(first)
    guard.reset_stats()
    assert set(guard.stats()) == STAT_KEYS
    assert all(value == 0 for value in guard.stats().values())
