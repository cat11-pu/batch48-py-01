import copy
import threading

import pytest

import audit_retention_guard as rg

DAY = 86400


def make(**kwargs):
    return rg.RetentionGuard(**kwargs)


def seeded(**kwargs):
    guard = make(**kwargs)
    guard.set_policy("t1", "access", 1)
    return guard


def test_policy_versions_advance_and_missing_policies_are_reported():
    guard = make()
    first = guard.set_policy("t1", "access", 1)
    assert first == {"tenant": "t1", "category": "access", "days": 1, "version": 1}
    second = guard.set_policy("t1", "access", 30)
    assert second["version"] == 2
    assert second["days"] == 30
    assert guard.policy_for("t1", "access")["days"] == 30
    for bad in (0, 3651, "30", None):
        with pytest.raises(rg.RetentionError) as exc:
            guard.set_policy("t1", "other", bad)
        assert exc.value.code == "RETENTION_POLICY_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.set_policy("t1", "", 10)
    assert exc.value.code == "RETENTION_POLICY_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.policy_for("t1", "other")
    assert exc.value.code == "RETENTION_POLICY_MISSING"


def test_record_expiry_is_derived_from_event_time_and_policy():
    guard = seeded()
    record = guard.add_record("t1", "access", 100, "r1")
    assert record["id"] == "r1"
    assert record["tenant"] == "t1"
    assert record["category"] == "access"
    assert record["at"] == 100
    assert record["expires"] == 100 + DAY
    with pytest.raises(rg.RetentionError) as exc:
        guard.add_record("t1", "access", 200, "r1")
    assert exc.value.code == "RETENTION_DUPLICATE_RECORD"
    with pytest.raises(rg.RetentionError) as exc:
        guard.add_record("t1", "nope", 200)
    assert exc.value.code == "RETENTION_POLICY_MISSING"
    with pytest.raises(rg.RetentionError) as exc:
        guard.add_record("t1", "access", "200")
    assert exc.value.code == "RETENTION_TIMESTAMP_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.add_record("t1", "access", 200, "")
    assert exc.value.code == "RETENTION_RECORD_INVALID"


def test_record_capacity_is_a_hard_ceiling():
    guard = seeded(max_records=2)
    guard.add_record("t1", "access", 10, "r1")
    guard.add_record("t1", "access", 20, "r2")
    with pytest.raises(rg.RetentionError) as exc:
        guard.add_record("t1", "access", 30, "r3")
    assert exc.value.code == "RETENTION_RECORD_LIMIT"
    assert guard.stats()["records"] == 2


def test_query_is_tenant_scoped_ordered_and_window_bounded():
    guard = seeded()
    guard.set_policy("t2", "access", 1)
    guard.add_record("t1", "access", 100, "r1")
    guard.add_record("t1", "access", 300, "r3")
    guard.add_record("t1", "access", 200, "r2")
    guard.add_record("t2", "access", 150, "x1")
    rows = guard.query("t1", 100, 250)
    assert [row["id"] for row in rows] == ["r1", "r2"]
    assert [row["id"] for row in guard.query("t1", 0, 1000)] == ["r1", "r2", "r3"]
    assert [row["id"] for row in guard.query("t2", 0, 1000)] == ["x1"]
    with pytest.raises(rg.RetentionError) as exc:
        guard.query("t9", 0, 1000)
    assert exc.value.code == "RETENTION_UNKNOWN_TENANT"
    with pytest.raises(rg.RetentionError) as exc:
        guard.query("t1", 300, 200)
    assert exc.value.code == "RETENTION_WINDOW_INVALID"


def test_purge_only_destroys_events_past_their_own_expiry():
    guard = seeded()
    guard.add_record("t1", "access", 0, "r1")
    guard.add_record("t1", "access", 50000, "r2")
    receipt = guard.purge(90000)
    assert receipt["receipt_id"] == "purge-1"
    assert receipt["at"] == 90000
    assert receipt["count"] == 1
    assert receipt["removed"] == ["r1"]
    assert receipt["blocked_count"] == 0
    assert receipt["tenants"] == {"t1": 1}
    assert [row["id"] for row in guard.query("t1", 0, 100000)] == ["r2"]
    assert guard.stats()["purged"] == 1


def test_purge_receipts_are_retrievable_evidence():
    guard = seeded()
    guard.add_record("t1", "access", 0, "r1")
    first = guard.purge(90000)
    assert guard.describe_receipt("purge-1") == {
        "receipt_id": "purge-1", "at": 90000, "count": 1,
        "removed": ["r1"], "blocked": [], "blocked_count": 0,
        "tenants": {"t1": 1},
    }
    second = guard.purge(90000)
    assert second["receipt_id"] == "purge-2"
    assert second["count"] == 0
    with pytest.raises(rg.RetentionError) as exc:
        guard.describe_receipt("purge-9")
    assert exc.value.code == "RETENTION_RECEIPT_UNKNOWN"


def test_tenant_scoped_hold_blocks_purge_until_released():
    guard = seeded()
    guard.add_record("t1", "access", 0, "r1")
    held = guard.hold("t1", "tenant", 100000, "litigation")
    assert held == {"hold_id": "hold-1", "tenant": "t1", "scope": "tenant",
                    "until": 100000, "reason": "litigation", "status": "active"}
    receipt = guard.purge(90000)
    assert receipt["count"] == 0
    assert receipt["blocked"] == ["r1"]
    assert receipt["blocked_count"] == 1
    assert [row["id"] for row in guard.query("t1", 0, 100000)] == ["r1"]

    guard.release(held["hold_id"])
    assert guard.purge(90000)["removed"] == ["r1"]
    assert guard.query("t1", 0, 100000) == []


def test_category_scoped_hold_only_protects_its_category():
    guard = make()
    guard.set_policy("t1", "catA", 1)
    guard.set_policy("t1", "catB", 1)
    guard.add_record("t1", "catA", 0, "a1")
    guard.add_record("t1", "catB", 0, "b1")
    guard.hold("t1", "category:catA", 100000, "case-42")
    receipt = guard.purge(90000)
    assert receipt["removed"] == ["b1"]
    assert receipt["blocked"] == ["a1"]
    assert [row["id"] for row in guard.query("t1", 0, 100000)] == ["a1"]


def test_hold_scope_cannot_be_borrowed_across_tenants():
    guard = make()
    guard.set_policy("t1", "catA", 10)
    guard.set_policy("t2", "catB", 10)
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold("t1", "category:catB", 1000, "cross")
    assert exc.value.code == "RETENTION_HOLD_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold("t1", "everything", 1000, "wide")
    assert exc.value.code == "RETENTION_HOLD_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold("t1", "tenant", 0, "past")
    assert exc.value.code == "RETENTION_HOLD_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold("t1", "tenant", 1000, "")
    assert exc.value.code == "RETENTION_HOLD_INVALID"
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold("t8", "tenant", 1000, "unknown")
    assert exc.value.code == "RETENTION_UNKNOWN_TENANT"


def test_identical_holds_are_idempotent():
    guard = seeded()
    first = guard.hold("t1", "tenant", 100000, "legal")
    again = guard.hold("t1", "tenant", 100000, "legal")
    assert again["hold_id"] == first["hold_id"] == "hold-1"
    assert guard.stats()["holds_active"] == 1
    other = guard.hold("t1", "tenant", 200000, "legal")
    assert other["hold_id"] == "hold-2"
    assert guard.stats()["holds_active"] == 2


def test_release_is_single_use_and_reported():
    guard = seeded()
    held = guard.hold("t1", "tenant", 100000, "legal")
    out = guard.release(held["hold_id"])
    assert out == {"hold_id": "hold-1", "status": "released", "released_at": 0}
    assert guard.hold_status("hold-1")["status"] == "released"
    with pytest.raises(rg.RetentionError) as exc:
        guard.release("hold-1")
    assert exc.value.code == "RETENTION_HOLD_CLOSED"
    with pytest.raises(rg.RetentionError) as exc:
        guard.release("hold-9")
    assert exc.value.code == "RETENTION_HOLD_UNKNOWN"
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold_status("hold-9")
    assert exc.value.code == "RETENTION_HOLD_UNKNOWN"


def test_active_hold_capacity_is_capped():
    guard = make(max_holds=2)
    for category in ("catA", "catB", "catC"):
        guard.set_policy("t1", category, 10)
    guard.hold("t1", "tenant", 1000, "one")
    guard.hold("t1", "category:catA", 1000, "two")
    with pytest.raises(rg.RetentionError) as exc:
        guard.hold("t1", "category:catB", 1000, "three")
    assert exc.value.code == "RETENTION_HOLD_LIMIT"


def test_clock_rollback_stops_a_second_earlier_purge():
    guard = seeded()
    guard.add_record("t1", "access", 0, "r1")
    guard.purge(1000)
    with pytest.raises(rg.RetentionError) as exc:
        guard.purge(999)
    assert exc.value.code == "RETENTION_CLOCK_ROLLBACK"
    assert guard.stats()["receipts"] == 1
    assert guard.stats()["records"] == 1


def test_snapshot_restore_preserves_policies_holds_receipts():
    guard = seeded()
    guard.set_policy("t1", "access", 10)
    guard.add_record("t1", "access", 0, "r1")
    guard.add_record("t1", "access", 100, "r2")
    guard.hold("t1", "tenant", 200000, "legal")
    guard.purge(100)

    state = guard.snapshot()
    assert state["version"] == 1
    assert state["last_purge_at"] == 100
    state["policies"].clear()
    state["records"].clear()
    assert guard.policy_for("t1", "access")["days"] == 10
    assert len(guard.query("t1", 0, 1000)) == 2

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.stats()["records"] == 2
    assert restored.stats()["holds_active"] == 1
    assert restored.stats()["receipts"] == 1
    assert restored.policy_for("t1", "access")["version"] == 2
    assert len(restored.query("t1", 0, 1000)) == 2
    with pytest.raises(rg.RetentionError) as exc:
        restored.purge(50)
    assert exc.value.code == "RETENTION_CLOCK_ROLLBACK"

    receipt = restored.purge(100 + 10 * DAY)
    assert receipt["removed"] == ["r1", "r2"]
    assert restored.query("t1", 0, 1000) == []

    for bad in (None, {}, {"version": 2}, {"version": 1, "records": "x"}):
        with pytest.raises(rg.RetentionError) as exc:
            restored.restore(copy.deepcopy(bad))
        assert exc.value.code == "RETENTION_SNAPSHOT_INVALID"
    assert restored.stats()["records"] == 0


def test_concurrent_purge_and_query_never_show_half_deleted_records():
    guard = seeded()
    ids = [f"r{i}" for i in range(40)]
    for index, rid in enumerate(ids):
        guard.add_record("t1", "access", index, rid)

    seen = []
    removed = []
    errors = []
    lock = threading.Lock()

    def reader():
        try:
            for _ in range(5):
                rows = guard.query("t1", 0, 100)
                with lock:
                    seen.extend(row["id"] for row in rows)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            with lock:
                errors.append(repr(exc))

    def purger():
        try:
            receipt = guard.purge(90000)
            with lock:
                removed.extend(receipt["removed"])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads += [threading.Thread(target=purger) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert set(seen) <= set(ids)
    assert sorted(set(removed)) == sorted(ids)
    assert guard.query("t1", 0, 100) == []
    assert guard.stats()["records"] == 0
