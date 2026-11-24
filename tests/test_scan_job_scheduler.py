import threading

import pytest

import scan_job_scheduler as sjs


def job(target, profile, priority=None):
    payload = {"target": target, "profile": profile}
    if priority is not None:
        payload["priority"] = priority
    return payload


def make(**kwargs):
    return sjs.ScanJobScheduler(**kwargs)


def test_submit_assigns_increasing_ids_and_default_priority():
    guard = make()
    ids = [
        guard.submit(job("t1", "p1")),
        guard.submit(job("t2", "p1")),
        guard.submit(job("t3", "p1")),
    ]
    assert ids == [1, 2, 3]
    state = guard.job_state(2)
    assert state["job_id"] == 2
    assert state["priority"] == "normal"
    assert state["attempt"] == 0
    assert state["state"] == "pending"
    assert guard.stats()["submitted"] == 3


def test_submit_rejects_duplicate_target_profile_in_flight():
    guard = make()
    assert guard.submit(job("t1", "p1")) == 1
    with pytest.raises(sjs.SchedulerError) as exc:
        guard.submit(job("t1", "p1"))
    assert exc.value.code == "SCAN_DUPLICATE_JOB"
    assert guard.stats()["submitted"] == 1

    leased = guard.lease("w1", 0.0)
    assert [item["job_id"] for item in leased] == [1]
    guard.complete(1, 0.0)
    assert guard.submit(job("t1", "p1")) == 2


def test_submit_rejects_invalid_job():
    guard = make()
    bad_jobs = [
        "t1",
        {},
        {"target": "", "profile": "p1"},
        {"target": "t1", "profile": ""},
        {"target": "t1", "profile": "p1", "priority": "urgent"},
        {"target": "t1", "profile": "p1", "priority": 3},
    ]
    for bad in bad_jobs:
        with pytest.raises(sjs.SchedulerError) as exc:
            guard.submit(bad)
        assert exc.value.code == "SCAN_INVALID_JOB"
    assert guard.stats()["submitted"] == 0
    assert guard.pending() == {"critical": 0, "normal": 0, "low": 0}


def test_lease_orders_by_priority_then_sequence():
    guard = make()
    guard.submit(job("t1", "p1", "low"))
    guard.submit(job("t2", "p1", "critical"))
    guard.submit(job("t3", "p1", "normal"))
    leased = guard.lease("w1", 0.0)
    assert [item["job_id"] for item in leased] == [2, 3, 1]
    assert [item["priority"] for item in leased] == ["critical", "normal", "low"]
    assert all(item["lease_until"] == 60.0 for item in leased)


def test_lease_is_fifo_within_same_priority():
    guard = make()
    for index in range(4):
        guard.submit(job("t%d" % index, "p1"))
    leased = guard.lease("w1", 0.0)
    assert [item["job_id"] for item in leased] == [1, 2, 3, 4]


def test_per_target_limit_caps_concurrent_leases():
    guard = make(per_target_limit=2)
    for profile in ("p1", "p2", "p3"):
        guard.submit(job("shared", profile))

    first = guard.lease("w1", 0.0)
    assert [item["job_id"] for item in first] == [1, 2]
    assert guard.lease("w2", 0.0) == []
    guard.complete(1, 0.0)
    third = guard.lease("w2", 0.0)
    assert [item["job_id"] for item in third] == [3]


def test_worker_and_global_limits_cap_grants():
    guard = make(worker_limit=1, global_limit=2)
    for index in range(3):
        guard.submit(job("t%d" % index, "p1"))

    assert [item["job_id"] for item in guard.lease("w1", 0.0)] == [1]
    assert guard.lease("w1", 0.0) == []
    assert [item["job_id"] for item in guard.lease("w2", 0.0)] == [2]
    assert guard.lease("w3", 0.0) == []
    guard.complete(1, 0.0)
    assert [item["job_id"] for item in guard.lease("w1", 0.0)] == [3]


def test_heartbeat_extends_lease_and_requires_lease():
    guard = make(worker_limit=1)
    guard.submit(job("t1", "p1"))
    guard.submit(job("t2", "p1"))
    leased = guard.lease("w1", 0.0, ttl=10.0)
    assert [item["job_id"] for item in leased] == [1]
    assert leased[0]["lease_until"] == 10.0

    assert guard.heartbeat(1, 5.0, ttl=10.0) == 15.0
    assert guard.requeue_expired(12.0) == []
    assert guard.job_state(1)["state"] == "leased"

    with pytest.raises(sjs.SchedulerError) as exc:
        guard.heartbeat(2, 5.0)
    assert exc.value.code == "SCAN_LEASE_NOT_HELD"
    with pytest.raises(sjs.SchedulerError) as exc:
        guard.heartbeat(999, 5.0)
    assert exc.value.code == "SCAN_JOB_NOT_FOUND"


def test_requeue_expired_returns_ids_and_frees_quota():
    guard = make(worker_limit=2, per_target_limit=1)
    guard.submit(job("shared", "p1"))
    guard.submit(job("shared", "p2"))
    leased = guard.lease("w1", 0.0, ttl=10.0)
    assert [item["job_id"] for item in leased] == [1, 2]

    assert guard.requeue_expired(9.5) == []
    assert guard.requeue_expired(10.0) == [1, 2]
    assert guard.requeue_expired(10.0) == []
    assert guard.job_state(1)["state"] == "pending"
    assert guard.job_state(2)["state"] == "pending"
    again = guard.lease("w1", 10.0)
    assert [item["job_id"] for item in again] == [1, 2]
    assert guard.stats()["requeues"] == 2


def test_complete_releases_quota_and_rejects_stale_ids():
    guard = make(worker_limit=1, per_target_limit=1)
    guard.submit(job("shared", "p1"))
    guard.submit(job("shared", "p2"))
    assert [item["job_id"] for item in guard.lease("w1", 0.0)] == [1]

    done = guard.complete(1, 0.0)
    assert done["job_id"] == 1
    assert done["state"] == "done"
    assert guard.job_state(1)["state"] == "done"
    assert [item["job_id"] for item in guard.lease("w1", 0.0)] == [2]

    with pytest.raises(sjs.SchedulerError) as exc:
        guard.complete(1, 0.0)
    assert exc.value.code == "SCAN_LEASE_NOT_HELD"
    with pytest.raises(sjs.SchedulerError) as exc:
        guard.complete(777, 0.0)
    assert exc.value.code == "SCAN_JOB_NOT_FOUND"
    assert guard.stats()["completed"] == 1


def test_fail_backoff_doubles_and_keeps_job_id():
    guard = make(max_attempts=5, worker_limit=1)
    guard.submit(job("t1", "p1"))
    assert [item["job_id"] for item in guard.lease("w1", 100.0)] == [1]

    first = guard.fail(1, 100.0, "boom")
    assert first == {"job_id": 1, "attempt": 1, "dead": False, "next_at": 102.0}
    assert guard.job_state(1)["state"] == "backoff"
    assert guard.requeue_expired(101.0) == []
    assert guard.requeue_expired(102.0) == [1]

    again = guard.lease("w1", 102.0)
    assert [item["job_id"] for item in again] == [1]
    assert again[0]["attempt"] == 1
    second = guard.fail(1, 200.0, "boom")
    assert second["attempt"] == 2
    assert second["next_at"] == 204.0
    assert guard.job_state(1)["job_id"] == 1


def test_fail_moves_to_dead_letter_and_keeps_reason():
    guard = make(max_attempts=2, worker_limit=2)
    guard.submit(job("t1", "p1"))
    guard.submit(job("t2", "p1"))
    guard.lease("w1", 0.0)
    guard.fail(1, 0.0, "timeout")
    guard.fail(2, 0.0, "timeout")
    assert guard.requeue_expired(2.0) == [1, 2]
    guard.lease("w1", 2.0)
    assert guard.fail(1, 2.0, "timeout") == {
        "job_id": 1, "attempt": 2, "dead": True, "next_at": None,
    }
    assert guard.fail(2, 2.0, "timeout")["dead"] is True

    letters = guard.dead_letters()
    assert [item["job_id"] for item in letters] == [1, 2]
    assert letters[0]["reason"] == "timeout"
    assert guard.job_state(1)["state"] == "dead"
    assert guard.lease("w1", 100.0) == []
    counters = guard.stats()
    assert counters["submitted"] == 2
    assert counters["failed"] == 4
    assert counters["dead"] == 2
    assert counters["requeues"] == 2
    assert guard.pending() == {"critical": 0, "normal": 0, "low": 0}


def test_snapshot_restore_preserves_leases_and_sequence():
    guard = make(worker_limit=1)
    for index in range(3):
        guard.submit(job("t%d" % index, "p1"))
    assert [item["job_id"] for item in guard.lease("w1", 0.0)] == [1]
    state = guard.snapshot()
    assert state["version"] == 1

    clone = make(worker_limit=1)
    clone.restore(state)
    assert clone.job_state(1)["state"] == "leased"
    assert clone.pending() == {"critical": 0, "normal": 2, "low": 0}
    assert clone.dead_letters() == []
    assert clone.complete(1, 0.0)["job_id"] == 1
    assert clone.submit(job("t9", "p1")) == 4


def test_corrupt_snapshot_is_rejected_without_state_change():
    guard = make(worker_limit=1)
    guard.submit(job("t1", "p1"))
    guard.lease("w1", 0.0)
    before = guard.pending()

    for bad in (
        None,
        [],
        {},
        {"version": 9},
        {"version": 1, "jobs": "nope"},
        {"version": 1, "jobs": [], "next_id": -1},
    ):
        with pytest.raises(sjs.SchedulerError) as exc:
            guard.restore(bad)
        assert exc.value.code == "SCAN_SNAPSHOT_INVALID"
    assert guard.pending() == before
    assert guard.job_state(1)["state"] == "leased"


def test_concurrent_submit_and_lease_never_double_dispatch():
    guard = make(worker_limit=8, global_limit=64, per_target_limit=2)
    leased_ids = []
    guard_ids_lock = threading.Lock()
    errors = []

    def submitter(index):
        try:
            for offset in range(5):
                guard.submit(job("t-%d-%d" % (index, offset), "p1"))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def leaser(index):
        try:
            while True:
                batch = guard.lease("w%d" % index, 0.0)
                if not batch:
                    return
                with guard_ids_lock:
                    leased_ids.extend(item["job_id"] for item in batch)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=submitter, args=(i,)) for i in range(4)]
    threads += [threading.Thread(target=leaser, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for index in range(4):
        while True:
            batch = guard.lease("drain-%d" % index, 0.0)
            if not batch:
                break
            leased_ids.extend(item["job_id"] for item in batch)

    assert errors == []
    assert len(leased_ids) == 20
    assert len(set(leased_ids)) == 20
    for job_id in leased_ids:
        guard.complete(job_id, 0.0)
    assert guard.stats()["submitted"] == 20
    assert guard.stats()["completed"] == 20
    assert guard.pending() == {"critical": 0, "normal": 0, "low": 0}
