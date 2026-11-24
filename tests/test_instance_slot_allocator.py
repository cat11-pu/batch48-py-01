import pytest

import instance_slot_allocator as ia


def alloc(**overrides):
    kwargs = {
        "reclaim_cooldown_ms": 60000,
        "reentry_cooldown_ms": 30000,
    }
    kwargs.update(overrides)
    return ia.InstanceSlotAllocator(**kwargs)


def dungeon(a, dungeon_id="crypt", capacity=3, max_slots=2, reset_every_ms=3600000, anchor_ms=0):
    return a.RegisterDungeon(
        dungeon_id,
        capacity=capacity,
        max_slots=max_slots,
        reset_every_ms=reset_every_ms,
        anchor_ms=anchor_ms,
    )


def test_register_dungeon_and_acquire_first_instance():
    a = alloc()
    out = dungeon(a)
    assert out["dungeon_id"] == "crypt"
    assert out["capacity"] == 3
    assert out["max_slots"] == 2
    inst = a.Acquire("crypt", ["p1", "p2"], 1000)
    assert inst["instance_id"] == "crypt#1"
    assert inst["dungeon_id"] == "crypt"
    assert inst["party"] == ["p1", "p2"]
    assert inst["capacity"] == 3
    assert inst["members"] == []
    assert inst["state"] == "open"
    assert inst["created_at"] == 1000
    assert a.Stats()["acquired"] == 1


def test_acquire_reuses_bound_party_instance():
    a = alloc()
    dungeon(a)
    first = a.Acquire("crypt", ["p1", "p2"], 1000)
    second = a.Acquire("crypt", ["p1", "p2"], 2000)
    assert second == first
    assert a.Stats()["acquired"] == 1


def test_acquire_rejects_oversized_party():
    a = alloc()
    dungeon(a, capacity=2)
    with pytest.raises(ia.InstanceError) as exc:
        a.Acquire("crypt", ["p1", "p2", "p3"], 1000)
    assert exc.value.code == "INSTANCE_PARTY_TOO_LARGE"
    assert a.Stats()["acquired"] == 0


def test_acquire_rejects_when_pool_is_full():
    a = alloc()
    dungeon(a, max_slots=2)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Acquire("crypt", ["p3", "p4"], 1000)
    with pytest.raises(ia.InstanceError) as exc:
        a.Acquire("crypt", ["p5", "p6"], 1000)
    assert exc.value.code == "INSTANCE_POOL_FULL"
    assert a.Stats()["acquired"] == 2


def test_enter_requires_party_membership():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    with pytest.raises(ia.InstanceError) as exc:
        a.Enter("crypt#1", "p3", 1000)
    assert exc.value.code == "INSTANCE_FOREIGN_PLAYER"
    assert a.State("crypt#1")["members"] == []
    assert a.Stats()["entered"] == 0


def test_enter_is_idempotent_for_member():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    first = a.Enter("crypt#1", "p1", 1000)
    assert first["instance_id"] == "crypt#1"
    assert first["player"] == "p1"
    assert first["members"] == ["p1"]
    assert first["capacity"] == 3
    second = a.Enter("crypt#1", "p1", 1500)
    assert second["members"] == ["p1"]
    assert a.Stats()["entered"] == 1


def test_enter_rejects_when_instance_is_full():
    a = alloc()
    dungeon(a, capacity=2)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Enter("crypt#1", "p1", 1000)
    a.Enter("crypt#1", "p2", 1000)
    with pytest.raises(ia.InstanceError) as exc:
        a.Enter("crypt#1", "p3", 1000)
    assert exc.value.code == "INSTANCE_FULL"
    assert a.Stats()["entered"] == 2


def test_leave_frees_capacity_for_follow_up_checks():
    a = alloc()
    dungeon(a, capacity=2)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Enter("crypt#1", "p1", 1000)
    a.Enter("crypt#1", "p2", 1000)
    out = a.Leave("crypt#1", "p1", 5000)
    assert out["members"] == ["p2"]
    assert out["state"] == "open"
    assert a.Stats()["left"] == 1
    with pytest.raises(ia.InstanceError) as exc:
        a.Enter("crypt#1", "p3", 6000)
    assert exc.value.code == "INSTANCE_FOREIGN_PLAYER"


def test_leave_rejects_non_member():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    with pytest.raises(ia.InstanceError) as exc:
        a.Leave("crypt#1", "p3", 1000)
    assert exc.value.code == "INSTANCE_NOT_MEMBER"
    assert a.Stats()["left"] == 0


def test_reentry_cooldown_blocks_immediate_return():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2", "p3"], 1000)
    a.Enter("crypt#1", "p1", 1000)
    a.Enter("crypt#1", "p2", 1000)
    a.Leave("crypt#1", "p1", 2000)
    assert a.Cooldown(["p1", "p2", "p3"], 2000) == 30000
    with pytest.raises(ia.InstanceError) as exc:
        a.Enter("crypt#1", "p1", 5000)
    assert exc.value.code == "INSTANCE_REENTRY_COOLDOWN"
    out = a.Enter("crypt#1", "p1", 32000)
    assert out["members"] == ["p2", "p1"]
    assert a.Stats()["entered"] == 3


def test_reset_clears_members_and_keeps_party_binding():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Enter("crypt#1", "p1", 1000)
    a.Enter("crypt#1", "p2", 1000)
    out = a.Reset("crypt#1")
    assert out["instance_id"] == "crypt#1"
    assert out["members"] == []
    assert out["state"] == "open"
    assert out["party"] == ["p1", "p2"]
    assert out["capacity"] == 3
    again = a.Enter("crypt#1", "p1", 3000)
    assert again["members"] == ["p1"]


def test_sweep_closes_expired_instance_and_frees_slot():
    a = alloc()
    dungeon(a, max_slots=1)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    closed = a.Sweep(10 ** 12)
    assert closed == ["crypt#1"]
    assert a.State("crypt#1")["state"] == "closed"
    assert a.Stats()["reclaimed"] == 1
    nxt = a.Acquire("crypt", ["p3", "p4"], 10 ** 12)
    assert nxt["instance_id"] == "crypt#2"
    assert a.Stats()["acquired"] == 2


def test_reclaim_cooldown_blocks_same_party_reacquire():
    a = alloc()
    dungeon(a, max_slots=1)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Sweep(10 ** 12)
    with pytest.raises(ia.InstanceError) as exc:
        a.Acquire("crypt", ["p1", "p2"], 10 ** 12)
    assert exc.value.code == "INSTANCE_COOLDOWN_ACTIVE"
    assert a.Cooldown(["p1", "p2"], 10 ** 12) == 60000


def test_snapshot_restore_preserves_members_and_cooldowns():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Enter("crypt#1", "p1", 1000)
    a.Enter("crypt#1", "p2", 1000)
    a.Leave("crypt#1", "p1", 2000)
    stats_before = a.Stats()
    snap = a.Snapshot()
    a.Reset("crypt#1")
    assert a.State("crypt#1")["members"] == []
    fresh = alloc()
    fresh.Restore(snap)
    state = fresh.State("crypt#1")
    assert state["instance_id"] == "crypt#1"
    assert state["dungeon_id"] == "crypt"
    assert state["party"] == ["p1", "p2"]
    assert state["capacity"] == 3
    assert state["members"] == ["p2"]
    assert state["state"] == "open"
    assert state["created_at"] == 1000
    assert fresh.Cooldown(["p1"], 2000) == 30000
    assert fresh.Stats() == stats_before


def test_restore_rejects_malformed_snapshot():
    a = alloc()
    dungeon(a)
    a.Acquire("crypt", ["p1", "p2"], 1000)
    a.Enter("crypt#1", "p1", 1000)
    with pytest.raises(ia.InstanceError) as exc:
        a.Restore({"instances": []})
    assert exc.value.code == "INSTANCE_SNAPSHOT_INVALID"
    assert a.State("crypt#1")["members"] == ["p1"]
    assert a.Stats()["acquired"] == 1
