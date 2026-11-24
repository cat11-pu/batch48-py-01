import threading

import pytest

import resource_handle_ledger as rl


def make(**kwargs):
    return rl.ResourceHandleLedger(**kwargs)


def destroy(ledger, handle):
    ledger.begin_frame()
    ledger.retain(handle)
    ledger.release(handle)
    ledger.end_frame()
    return ledger.reclaim()


def test_create_allocates_increasing_ids_and_reports_resource():
    ledger = make()
    first = ledger.create("vertex-buffer", kind="buffer", size=1024)
    assert first["id"] == 1
    assert first["generation"] == 1
    assert first["state"] == "live"
    assert first["refcount"] == 0
    second = ledger.create("albedo", kind="texture", size=2048)
    assert second["id"] == 2
    info = ledger.resource_info(first)
    assert info["name"] == "vertex-buffer"
    assert info["kind"] == "buffer"
    assert info["size"] == 1024
    assert info["generation"] == 1
    assert ledger.stats()["resources_created"] == 2


def test_create_validates_name_kind_and_size():
    ledger = make()
    bad_calls = [
        lambda: ledger.create(""),
        lambda: ledger.create(None),
        lambda: ledger.create(5),
        lambda: ledger.create("x" * 65),
        lambda: ledger.create("ok", kind="mesh"),
        lambda: ledger.create("ok", size=-1),
        lambda: ledger.create("ok", size=1.5),
        lambda: ledger.create("ok", size=2 ** 31),
    ]
    for call in bad_calls:
        with pytest.raises(rl.LedgerError) as exc:
            call()
        assert exc.value.code == "LEDGER_INVALID_RESOURCE"
    assert ledger.stats()["rejected"] == len(bad_calls)
    assert ledger.stats()["resources_created"] == 0
    assert ledger.leak_report()["entries"] == []


def test_duplicate_live_name_is_refused():
    ledger = make()
    first = ledger.create("albedo")
    with pytest.raises(rl.LedgerError) as exc:
        ledger.create("albedo", kind="texture", size=64)
    assert exc.value.code == "LEDGER_DUPLICATE_NAME"
    assert exc.value.detail["name"] == "albedo"
    assert ledger.resource_info(first)["name"] == "albedo"
    assert ledger.stats()["resources_created"] == 1


def test_resource_capacity_is_a_hard_cap():
    ledger = make(max_resources=2)
    ledger.create("a")
    ledger.create("b")
    with pytest.raises(rl.LedgerError) as exc:
        ledger.create("c")
    assert exc.value.code == "LEDGER_RESOURCE_LIMIT"
    assert exc.value.detail["live"] == 2
    assert exc.value.detail["max_resources"] == 2
    assert [e["name"] for e in ledger.leak_report()["entries"]] == ["a", "b"]


def test_retain_and_release_track_refcount():
    ledger = make()
    handle = ledger.create("albedo")
    assert ledger.retain(handle)["refcount"] == 1
    assert ledger.retain(handle, count=2)["refcount"] == 3
    assert ledger.release(handle)["refcount"] == 2
    info = ledger.resource_info(handle)
    assert info["refcount"] == 2
    assert info["state"] == "live"
    assert ledger.stats()["handles_retained"] == 3
    assert ledger.stats()["handles_released"] == 1
    with pytest.raises(rl.LedgerError) as exc:
        ledger.retain(handle, count=0)
    assert exc.value.code == "LEDGER_INVALID_COUNT"


def test_release_to_zero_defers_destruction_until_frame_end():
    ledger = make()
    ledger.begin_frame()
    handle = ledger.create("albedo")
    ledger.retain(handle)
    ledger.release(handle)
    assert ledger.resource_info(handle)["state"] == "pending"
    assert ledger.reclaim()["count"] == 0
    assert ledger.resource_info(handle)["state"] == "pending"
    assert ledger.end_frame()["completed_frame"] == 1
    assert ledger.reclaim()["reclaimed"] == ["albedo"]
    assert ledger.resource_info(handle)["state"] == "destroyed"


def test_double_release_is_refused():
    ledger = make()
    handle = ledger.create("albedo")
    ledger.retain(handle, count=2)
    assert ledger.release(handle)["refcount"] == 1
    assert ledger.release(handle)["state"] == "pending"
    with pytest.raises(rl.LedgerError) as exc:
        ledger.release(handle)
    assert exc.value.code == "LEDGER_DOUBLE_RELEASE"
    assert ledger.stats()["rejected"] == 1
    assert ledger.stats()["handles_released"] == 2


def test_retain_after_release_is_refused():
    ledger = make()
    handle = ledger.create("albedo")
    ledger.retain(handle)
    ledger.release(handle)
    with pytest.raises(rl.LedgerError) as exc:
        ledger.retain(handle)
    assert exc.value.code == "LEDGER_RELEASED_HANDLE"
    with pytest.raises(rl.LedgerError) as exc:
        ledger.retain(handle, count=3)
    assert exc.value.code == "LEDGER_RELEASED_HANDLE"
    assert ledger.validate(handle)["reason"] == "HANDLE_RELEASED"


def test_reclaim_requires_the_releasing_frame_to_complete():
    ledger = make()
    ledger.begin_frame()
    handle = ledger.create("albedo")
    ledger.retain(handle)
    ledger.release(handle)
    assert ledger.begin_frame()["frame"] == 2
    assert ledger.reclaim()["count"] == 0
    assert ledger.end_frame()["completed_frame"] == 1
    assert ledger.reclaim()["reclaimed"] == ["albedo"]
    assert ledger.end_frame()["completed_frame"] == 2
    assert ledger.stats()["frames_begun"] == 2
    assert ledger.stats()["frames_completed"] == 2


def test_validate_reports_unknown_and_released_reasons():
    ledger = make()
    handle = ledger.create("albedo")
    good = ledger.validate(handle)
    assert good["valid"] is True
    assert good["reason"] is None
    assert good["name"] == "albedo"

    unknown = ledger.validate({"id": 99, "generation": 1})
    assert unknown["valid"] is False
    assert unknown["reason"] == "HANDLE_UNKNOWN"

    ledger.retain(handle)
    ledger.release(handle)
    released = ledger.validate(handle)
    assert released["valid"] is False
    assert released["reason"] == "HANDLE_RELEASED"
    assert released["name"] == "albedo"


def test_slot_reuse_bumps_generation_and_stales_old_handle():
    ledger = make()
    old = ledger.create("albedo")
    destroy(ledger, old)
    fresh = ledger.create("albedo")
    assert fresh["id"] == old["id"]
    assert fresh["generation"] == old["generation"] + 1
    stale = ledger.validate(old)
    assert stale["valid"] is False
    assert stale["reason"] == "HANDLE_STALE"
    with pytest.raises(rl.LedgerError) as exc:
        ledger.resource_info(old)
    assert exc.value.code == "LEDGER_INVALID_HANDLE"
    assert exc.value.detail["reason"] == "HANDLE_STALE"
    assert ledger.validate(fresh)["valid"] is True


def test_device_lost_invalidates_handles_and_queues_live_resources():
    ledger = make()
    first = ledger.create("a")
    second = ledger.create("b")
    ledger.begin_frame()
    ledger.retain(first)
    ledger.release(first)
    result = ledger.device_lost()
    assert result["invalidated"] == 2
    assert result["queued"] == 1
    assert ledger.rebuild_queue() == ["b"]
    assert ledger.validate(first)["reason"] == "DEVICE_LOST"
    assert ledger.validate(second)["reason"] == "DEVICE_LOST"
    assert ledger.stats()["device_lost_events"] == 1
    fresh = ledger.create("c")
    assert ledger.validate(fresh)["valid"] is True


def test_leak_report_lists_outstanding_resources():
    ledger = make()
    first = ledger.create("alpha", kind="buffer", size=16)
    ledger.create("beta", kind="texture", size=32)
    ledger.retain(first)
    beta = {"id": 2, "generation": 1}
    assert ledger.resource_info(beta)["name"] == "beta"
    ledger.retain(beta)
    ledger.release(beta)
    report = ledger.leak_report()
    assert [entry["name"] for entry in report["entries"]] == ["alpha", "beta"]
    assert report["total"] == 2
    assert {entry["state"] for entry in report["entries"]} == {"live", "pending"}


def test_snapshot_is_deep_copy_and_restore_rejects_bad_state():
    ledger = make()
    handle = ledger.create("alpha")
    ledger.retain(handle)
    snapshot = ledger.snapshot()
    ledger.create("beta")
    ledger.restore(snapshot)
    assert ledger.validate(handle)["valid"] is True
    assert ledger.validate({"id": 2, "generation": 1})["reason"] == "HANDLE_UNKNOWN"

    snapshot["slots"][0]["name"] = "hacked"
    assert ledger.resource_info(handle)["name"] == "alpha"

    broken = dict(snapshot)
    broken["slots"] = "not-a-list"
    with pytest.raises(rl.LedgerError) as exc:
        ledger.restore(broken)
    assert exc.value.code == "LEDGER_SNAPSHOT_INVALID"
    assert ledger.resource_info(handle)["name"] == "alpha"
    assert ledger.resource_info(handle)["refcount"] == 1


def test_concurrent_retain_and_release_are_exact():
    ledger = make(max_resources=1)
    handle = ledger.create("shared", kind="buffer", size=4096)
    barrier = threading.Barrier(4)
    retained = threading.Barrier(4)
    errors = []

    def worker():
        barrier.wait()
        try:
            for _ in range(50):
                ledger.retain(handle)
            retained.wait()
            for _ in range(50):
                ledger.release(handle)
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    stats = ledger.stats()
    assert stats["handles_retained"] == 200
    assert stats["handles_released"] == 200
    info = ledger.resource_info(handle)
    assert info["refcount"] == 0
    assert info["state"] == "pending"
