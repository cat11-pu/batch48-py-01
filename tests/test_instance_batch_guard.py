import threading

import pytest

import instance_batch_guard as ibg

STAT_KEYS = {"created", "deleted", "committed_ranges", "commits",
             "compactions", "moved_slots"}


def make(**kwargs):
    return ibg.InstanceBatchGuard(**kwargs)


def add(guard, instance_id, mesh="m1", material="mat1", attrs=None):
    return guard.create(instance_id, mesh=mesh, material=material, attrs=attrs)


def test_create_assigns_lowest_free_slots_and_lookup():
    guard = make()
    assert add(guard, "a") == {"instance_id": "a", "slot": 0}
    assert add(guard, "b", mesh="m2") == {"instance_id": "b", "slot": 1}
    assert add(guard, "c", mesh="m3") == {"instance_id": "c", "slot": 2}
    assert guard.slot_of("a") == 0
    assert guard.slot_of("c") == 2
    assert guard.instance_at(0) == "a"
    assert guard.instance_at(2) == "c"
    assert guard.instance_at(9) is None
    assert guard.stats()["created"] == 3


def test_duplicate_and_unknown_instance_are_rejected():
    guard = make()
    add(guard, "a")
    with pytest.raises(ibg.InstanceBatchError) as exc:
        add(guard, "a")
    assert exc.value.code == "DUPLICATE_INSTANCE"
    assert guard.stats()["created"] == 1
    with pytest.raises(ibg.InstanceBatchError) as exc:
        guard.slot_of("ghost")
    assert exc.value.code == "UNKNOWN_INSTANCE"
    with pytest.raises(ibg.InstanceBatchError) as exc:
        guard.update("ghost", {"x": 1})
    assert exc.value.code == "UNKNOWN_INSTANCE"
    with pytest.raises(ibg.InstanceBatchError) as exc:
        guard.delete("ghost")
    assert exc.value.code == "UNKNOWN_INSTANCE"


def test_delete_reuses_the_lowest_freed_slot_first():
    guard = make()
    add(guard, "a")
    add(guard, "b")
    add(guard, "c")
    assert guard.delete("b") == 1
    assert guard.delete("a") == 0
    assert guard.instance_at(0) is None
    assert add(guard, "d")["slot"] == 0
    assert add(guard, "e")["slot"] == 1
    assert guard.stats()["deleted"] == 2
    assert guard.stats()["created"] == 5


def test_dirty_ranges_merge_and_commit_clears_them():
    guard = make()
    add(guard, "a")
    add(guard, "b")
    add(guard, "c")
    assert guard.dirty_ranges() == [(0, 3)]
    assert guard.commit() == [(0, 3)]
    assert guard.dirty_ranges() == []
    assert guard.commit() == []
    assert guard.stats()["commits"] == 2
    assert guard.stats()["committed_ranges"] == 1

    guard.update("a", {"pos": 1})
    guard.update("c", {"pos": 2})
    assert guard.dirty_ranges() == [(0, 1), (2, 3)]
    assert guard.commit() == [(0, 1), (2, 3)]
    assert guard.stats()["committed_ranges"] == 3


def test_merge_gap_bridges_clean_slots_within_one_range():
    guard = make(merge_gap=1)
    for name in ("a", "b", "c", "d"):
        add(guard, name)
    guard.commit()
    guard.update("a", {"v": 1})
    guard.update("c", {"v": 2})
    assert guard.dirty_ranges() == [(0, 3)]

    guard.commit()
    guard.update("a", {"v": 3})
    guard.update("d", {"v": 4})
    assert guard.dirty_ranges() == [(0, 1), (3, 4)]
    assert guard.commit() == [(0, 1), (3, 4)]


def test_slot_limit_and_constructor_validation():
    guard = make(max_slots=2)
    add(guard, "a")
    add(guard, "b")
    with pytest.raises(ibg.InstanceBatchError) as exc:
        add(guard, "c")
    assert exc.value.code == "SLOT_LIMIT"
    assert guard.stats()["created"] == 2
    assert guard.instance_at(2) is None

    for kwargs in ({"max_slots": 0}, {"max_batches": 0}, {"merge_gap": -1}):
        with pytest.raises(ibg.InstanceBatchError) as exc:
            make(**kwargs)
        assert exc.value.code == "BATCH_CONFIG_INVALID"


def test_batches_split_on_mesh_material_and_slot_gaps():
    guard = make()
    add(guard, "a", mesh="m1", material="mat1")
    add(guard, "b", mesh="m1", material="mat1")
    add(guard, "c", mesh="m2", material="mat1")
    assert guard.batches() == [
        {"mesh": "m1", "material": "mat1", "start": 0, "end": 2},
        {"mesh": "m2", "material": "mat1", "start": 2, "end": 3},
    ]
    add(guard, "d", mesh="m2", material="mat1")
    assert guard.batches()[1]["end"] == 4

    guard.delete("b")
    assert guard.batches() == [
        {"mesh": "m1", "material": "mat1", "start": 0, "end": 1},
        {"mesh": "m2", "material": "mat1", "start": 2, "end": 4},
    ]

    add(guard, "e", mesh="m1", material="mat2")
    assert guard.batches() == [
        {"mesh": "m1", "material": "mat1", "start": 0, "end": 1},
        {"mesh": "m1", "material": "mat2", "start": 1, "end": 2},
        {"mesh": "m2", "material": "mat1", "start": 2, "end": 4},
    ]


def test_batch_ceiling_rejects_without_touching_state():
    guard = make(max_batches=2)
    add(guard, "a", mesh="m1")
    add(guard, "b", mesh="m2")
    with pytest.raises(ibg.InstanceBatchError) as exc:
        add(guard, "c", mesh="m3")
    assert exc.value.code == "BATCH_LIMIT"
    assert guard.stats()["created"] == 2
    assert guard.instance_at(2) is None
    with pytest.raises(ibg.InstanceBatchError) as exc:
        guard.slot_of("c")
    assert exc.value.code == "UNKNOWN_INSTANCE"

    assert add(guard, "d", mesh="m2")["slot"] == 2
    assert guard.batches() == [
        {"mesh": "m1", "material": "mat1", "start": 0, "end": 1},
        {"mesh": "m2", "material": "mat1", "start": 1, "end": 3},
    ]


def test_update_validates_attributes_and_marks_only_its_slot():
    guard = make()
    add(guard, "a", attrs={"pos": 0})
    add(guard, "b", attrs={"pos": 1})
    guard.commit()
    assert guard.update("a", {"pos": 5}) == 0
    assert guard.dirty_ranges() == [(0, 1)]
    assert guard.stats()["created"] == 2

    with pytest.raises(ibg.InstanceBatchError) as exc:
        guard.update("a", ["pos", 5])
    assert exc.value.code == "ATTR_INVALID"
    with pytest.raises(ibg.InstanceBatchError) as exc:
        add(guard, "c", attrs="nope")
    assert exc.value.code == "ATTR_INVALID"
    with pytest.raises(ibg.InstanceBatchError) as exc:
        guard.create("c", mesh="", material="mat1")
    assert exc.value.code == "ATTR_INVALID"
    assert guard.stats()["created"] == 2


def test_fragmentation_reports_holes_inside_the_used_span():
    guard = make()
    assert guard.fragmentation() == 0.0
    for name in ("a", "b", "c", "d", "e"):
        add(guard, name)
    guard.delete("b")
    guard.delete("d")
    assert guard.fragmentation() == pytest.approx(2 / 5)
    assert guard.instance_at(1) is None
    assert guard.instance_at(3) is None


def test_compact_renumbers_slots_and_marks_all_dirty():
    guard = make()
    for name in ("a", "b", "c", "d", "e"):
        add(guard, name)
    guard.commit()
    guard.delete("b")
    guard.delete("d")

    result = guard.compact()
    assert result == {"moved": 2, "slots": 3}
    assert guard.slot_of("a") == 0
    assert guard.slot_of("c") == 1
    assert guard.slot_of("e") == 2
    assert guard.fragmentation() == 0.0
    assert guard.dirty_ranges() == [(0, 3)]
    assert guard.stats()["compactions"] == 1
    assert guard.stats()["moved_slots"] == 2


def test_compact_leaves_one_contiguous_batch_per_group():
    guard = make()
    for name in ("a", "b", "c"):
        add(guard, name, mesh="m1", material="mat1")
    for name in ("d", "e", "f"):
        add(guard, name, mesh="m2", material="mat2")
    guard.delete("b")
    guard.delete("e")
    result = guard.compact()
    assert result == {"moved": 3, "slots": 4}
    assert guard.slot_of("a") == 0
    assert guard.slot_of("c") == 1
    assert guard.slot_of("d") == 2
    assert guard.slot_of("f") == 3
    assert guard.batches() == [
        {"mesh": "m1", "material": "mat1", "start": 0, "end": 2},
        {"mesh": "m2", "material": "mat2", "start": 2, "end": 4},
    ]
    assert guard.fragmentation() == 0.0


def test_snapshot_is_detached_and_restore_keeps_pending_dirty():
    guard = make()
    add(guard, "a", attrs={"pos": 0})
    add(guard, "b", attrs={"pos": 1})
    add(guard, "c", attrs={"pos": 2})
    guard.update("b", {"pos": 9})
    state = guard.snapshot()

    state["slots"].clear()
    state["stats"]["created"] = 99
    assert guard.stats()["created"] == 3
    assert guard.dirty_ranges() == [(0, 3)]

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.stats()["created"] == 3
    assert restored.slot_of("b") == 1
    assert restored.instance_at(2) == "c"
    assert restored.dirty_ranges() == [(0, 3)]
    assert restored.commit() == [(0, 3)]
    assert restored.dirty_ranges() == []


def test_corrupt_snapshot_leaves_live_state_usable():
    guard = make()
    add(guard, "a")
    good_stats = {key: 0 for key in STAT_KEYS}
    bad_states = [
        None,
        {},
        {"version": 2, "slots": {}, "stats": dict(good_stats)},
        {"version": 1, "slots": "nope", "stats": dict(good_stats)},
        {"version": 1, "slots": {}, "stats": {"created": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(ibg.InstanceBatchError) as exc:
            guard.restore(bad)
        assert exc.value.code == "SNAPSHOT_INVALID"

    assert guard.stats()["created"] == 1
    assert guard.slot_of("a") == 0
    assert guard.dirty_ranges() == [(0, 1)]


def test_concurrent_creates_never_share_a_slot_and_stats_reset():
    guard = make(max_slots=64, max_batches=8)
    errors = []

    def worker(index):
        try:
            for step in range(4):
                guard.create(f"c{index}-{step}", mesh="mesh", material="mat")
        except Exception as exc:  # pragma: no cover - surfaced via errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["created"] == 40
    slots = {guard.slot_of(f"c{i}-{s}") for i in range(10) for s in range(4)}
    assert slots == set(range(40))
    assert guard.batches() == [
        {"mesh": "mesh", "material": "mat", "start": 0, "end": 40},
    ]

    guard.reset_stats()
    assert guard.stats() == {key: 0 for key in STAT_KEYS}
    assert len(guard.snapshot()["slots"]) == 40
