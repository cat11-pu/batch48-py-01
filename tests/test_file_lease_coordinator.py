import threading

import pytest

import file_lease_coordinator as fl


def test_exclusive_acquire_returns_lease_and_holder():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    assert lease.key == "index.db"
    assert lease.owner == "writer-a"
    assert lease.mode == "exclusive"
    assert lease.acquired_at == 100
    assert lease.expires_at == 110
    assert lease.lease_id.startswith("lease-")
    assert coord.holder("index.db") == [{
        "lease_id": lease.lease_id,
        "owner": "writer-a",
        "mode": "exclusive",
        "expires_at": 110,
    }]
    assert coord.holder("other") == []


def test_second_exclusive_owner_is_queued_and_denied():
    coord = fl.LeaseCoordinator()
    first = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    with pytest.raises(fl.LeaseError) as exc:
        coord.acquire("index.db", "writer-b", "exclusive", 10, 101)
    assert exc.value.code == "LEASE_BUSY"
    assert [item["owner"] for item in coord.holder("index.db")] == ["writer-a"]
    assert coord.holder("index.db")[0]["lease_id"] == first.lease_id
    assert [w["owner"] for w in coord.waiters("index.db")] == ["writer-b"]
    assert coord.stats()["denied"] == 1
    assert coord.stats()["waited"] == 1


def test_same_owner_reacquire_is_idempotent():
    coord = fl.LeaseCoordinator()
    first = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    again = coord.acquire("index.db", "writer-a", "exclusive", 10, 105)
    assert again == first
    assert len(coord.holder("index.db")) == 1
    assert coord.waiters("index.db") == []
    stats = coord.stats()
    assert stats["granted"] == 1
    assert stats["denied"] == 0


def test_shared_leases_coexist_on_one_key():
    coord = fl.LeaseCoordinator()
    one = coord.acquire("report.csv", "reader-a", "shared", 10, 100)
    two = coord.acquire("report.csv", "reader-b", "shared", 10, 100)
    assert one.lease_id != two.lease_id
    holders = coord.holder("report.csv")
    assert [item["owner"] for item in holders] == ["reader-a", "reader-b"]
    assert all(item["mode"] == "shared" for item in holders)
    assert coord.stats()["granted"] == 2


def test_shared_lease_blocks_exclusive_requester():
    coord = fl.LeaseCoordinator()
    coord.acquire("report.csv", "reader-a", "shared", 10, 100)
    with pytest.raises(fl.LeaseError) as exc:
        coord.acquire("report.csv", "writer-c", "exclusive", 10, 100)
    assert exc.value.code == "LEASE_BUSY"
    assert [item["owner"] for item in coord.holder("report.csv")] == ["reader-a"]


def test_shared_holder_upgrade_waits_instead_of_jumping_queue():
    coord = fl.LeaseCoordinator()
    coord.acquire("report.csv", "reader-a", "shared", 10, 100)
    coord.acquire("report.csv", "reader-b", "shared", 10, 100)
    with pytest.raises(fl.LeaseError) as exc:
        coord.acquire("report.csv", "reader-a", "exclusive", 10, 104)
    assert exc.value.code == "LEASE_BUSY"
    with pytest.raises(fl.LeaseError) as exc:
        coord.acquire("report.csv", "reader-a", "exclusive", 10, 105)
    assert exc.value.code == "LEASE_BUSY"
    assert [w["owner"] for w in coord.waiters("report.csv")] == ["reader-a"]
    assert len(coord.holder("report.csv")) == 2


def test_release_requires_the_owning_process():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    with pytest.raises(fl.LeaseError) as exc:
        coord.release(lease.lease_id, "writer-b")
    assert exc.value.code == "LEASE_OWNER_MISMATCH"
    assert [item["owner"] for item in coord.holder("index.db")] == ["writer-a"]
    assert coord.release(lease.lease_id, "writer-a") is True
    assert coord.holder("index.db") == []
    with pytest.raises(fl.LeaseError) as exc:
        coord.release(lease.lease_id, "writer-a")
    assert exc.value.code == "LEASE_UNKNOWN_LEASE"


def test_release_grants_only_the_fifo_head():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    for owner in ("writer-b", "writer-c"):
        with pytest.raises(fl.LeaseError):
            coord.acquire("index.db", owner, "exclusive", 10, 100)
    assert [w["owner"] for w in coord.waiters("index.db")] == ["writer-b", "writer-c"]
    coord.release(lease.lease_id, "writer-a")
    assert [item["owner"] for item in coord.holder("index.db")] == ["writer-b"]
    assert [w["owner"] for w in coord.waiters("index.db")] == ["writer-c"]


def test_expire_reports_ids_in_sorted_order():
    coord = fl.LeaseCoordinator()
    short = coord.acquire("a.dat", "owner-a", "exclusive", 2, 100)
    long = coord.acquire("b.dat", "owner-b", "exclusive", 10, 100)
    assert coord.expire(101) == []
    assert coord.expire(102) == [short.lease_id]
    assert coord.holder("a.dat") == []
    assert [item["lease_id"] for item in coord.holder("b.dat")] == [long.lease_id]
    assert coord.stats()["expired"] == 1


def test_expire_wakes_only_the_first_waiter():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 5, 100)
    for owner in ("writer-b", "writer-c"):
        with pytest.raises(fl.LeaseError):
            coord.acquire("index.db", owner, "exclusive", 30, 100)
    assert coord.expire(105) == [lease.lease_id]
    assert [item["owner"] for item in coord.holder("index.db")] == ["writer-b"]
    assert [w["owner"] for w in coord.waiters("index.db")] == ["writer-c"]


def test_renew_extends_but_cannot_revive_expired_lease():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    renewed = coord.renew(lease.lease_id, 105)
    assert renewed.lease_id == lease.lease_id
    assert renewed.expires_at == 115
    assert coord.holder("index.db")[0]["expires_at"] == 115
    with pytest.raises(fl.LeaseError) as exc:
        coord.renew(lease.lease_id, 115)
    assert exc.value.code == "LEASE_EXPIRED"
    assert len(coord.holder("index.db")) == 1
    assert coord.expire(115) == [lease.lease_id]
    assert coord.stats()["renewed"] == 1


def test_snapshot_restore_keeps_holders_and_queue_order():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    for owner in ("writer-b", "writer-c"):
        with pytest.raises(fl.LeaseError):
            coord.acquire("index.db", owner, "exclusive", 10, 100)
    fresh = fl.LeaseCoordinator()
    fresh.restore(coord.snapshot())
    assert fresh.holder("index.db") == [{
        "lease_id": lease.lease_id,
        "owner": "writer-a",
        "mode": "exclusive",
        "expires_at": 110,
    }]
    assert [w["owner"] for w in fresh.waiters("index.db")] == ["writer-b", "writer-c"]
    assert fresh.expire(110) == [lease.lease_id]
    assert [item["owner"] for item in fresh.holder("index.db")] == ["writer-b"]
    assert [w["owner"] for w in fresh.waiters("index.db")] == ["writer-c"]


def test_restore_rejects_broken_state_and_keeps_current_state():
    coord = fl.LeaseCoordinator()
    coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    with pytest.raises(fl.LeaseError) as exc:
        coord.restore({"leases": 7})
    assert exc.value.code == "LEASE_SNAPSHOT_INVALID"
    assert [item["owner"] for item in coord.holder("index.db")] == ["writer-a"]


def test_snapshot_is_a_deep_copy():
    coord = fl.LeaseCoordinator()
    lease = coord.acquire("index.db", "writer-a", "exclusive", 10, 100)
    state = coord.snapshot()
    state["leases"][lease.lease_id]["expires_at"] = 0
    state["leases"][lease.lease_id]["owner"] = "intruder"
    assert coord.holder("index.db")[0]["expires_at"] == 110
    assert coord.holder("index.db")[0]["owner"] == "writer-a"


def test_concurrent_acquire_yields_one_exclusive_winner():
    coord = fl.LeaseCoordinator()
    barrier = threading.Barrier(10)
    results = []
    lock = threading.Lock()

    def worker(index):
        barrier.wait()
        try:
            value = coord.acquire("index.db", "writer-%d" % index, "exclusive", 10, 100)
            value = value.lease_id
        except fl.LeaseError as exc:
            value = exc.code
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures
            value = type(exc).__name__
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [value for value in results if value.startswith("lease-")]
    assert len(winners) == 1
    assert results.count("LEASE_BUSY") == 9
    assert [item["lease_id"] for item in coord.holder("index.db")] == winners
