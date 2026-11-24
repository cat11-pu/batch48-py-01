import copy
import threading

import pytest

import revocation_drain_barrier as rev

T0 = 1_700_000_000


def make(**kwargs):
    return rev.RevocationDrainBarrier(**kwargs)


def ready(**kwargs):
    barrier = make(**kwargs)
    barrier.configure_tenant("t1", 120)
    return barrier


def test_configure_reports_effective_window_and_open_state():
    barrier = make()
    out = barrier.configure_tenant("t1", 120)

    assert out["tenant"] == "t1"
    assert out["drain_window"] == 120

    status = barrier.drain_status("t1", now=T0)
    assert status["tenant"] == "t1"
    assert status["state"] == "open"
    assert status["in_flight"] == 0
    assert status["drain_deadline"] is None
    assert status["remaining"] == 0


def test_window_must_be_positive_and_is_capped_by_hard_cap():
    barrier = make(hard_cap=600)
    for bad in (0, -5):
        with pytest.raises(rev.RevocationError) as exc:
            barrier.configure_tenant("t1", bad)
        assert exc.value.code == "REVOCATION_INVALID_WINDOW"

    out = barrier.configure_tenant("t1", 5000)
    assert out["drain_window"] == 600

    barrier.issue_token("t1", "alice", now=T0)
    barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)
    status = barrier.drain_status("t1", now=T0 + 1)
    assert status["drain_deadline"] == T0 + 600
    assert status["remaining"] == 599


def test_token_issue_before_revocation_uses_generation_zero():
    barrier = ready()
    token = barrier.issue_token("t1", "alice", now=T0)

    assert token["tenant"] == "t1"
    assert token["subject"] == "alice"
    assert token["issued_at"] == T0
    assert isinstance(token["token_id"], str) and len(token["token_id"]) >= 8
    assert barrier.stats()["issued"] == 1


def test_lease_before_revocation_binds_request_and_expiry():
    barrier = ready(lease_ttl=60)
    lease = barrier.acquire_lease("t1", "alice", "req-1", now=T0)

    assert lease["tenant"] == "t1"
    assert lease["subject"] == "alice"
    assert lease["request_id"] == "req-1"
    assert lease["generation"] == 0
    assert isinstance(lease["expires_at"], int)
    assert isinstance(lease["lease_id"], str) and len(lease["lease_id"]) >= 8
    assert barrier.drain_status("t1", now=T0)["in_flight"] == 1
    assert barrier.stats()["leased"] == 1


def test_begin_revocation_advances_generation_and_starts_drain():
    barrier = ready()
    barrier.issue_token("t1", "alice", now=T0)
    barrier.acquire_lease("t1", "alice", "req-1", now=T0)

    out = barrier.begin_revocation("t1", now=T0)
    assert out["tenant"] == "t1"
    assert out["generation"] == 1
    assert out["drain_deadline"] == T0 + 120

    status = barrier.drain_status("t1", now=T0 + 10)
    assert status["state"] == "draining"
    assert status["in_flight"] == 1
    assert status["drain_deadline"] == T0 + 120
    assert status["remaining"] == 110


def test_issue_token_is_refused_once_revocation_begins():
    barrier = ready()
    barrier.begin_revocation("t1", now=T0)

    with pytest.raises(rev.RevocationError) as exc:
        barrier.issue_token("t1", "alice", now=T0 + 1)
    assert exc.value.code == "REVOCATION_ACTIVE"
    assert barrier.stats()["rejected"] == 1
    assert barrier.stats()["issued"] == 0


def test_new_leases_are_refused_once_revocation_begins():
    barrier = ready()
    barrier.begin_revocation("t1", now=T0)

    with pytest.raises(rev.RevocationError) as exc:
        barrier.acquire_lease("t1", "alice", "req-1", now=T0 + 1)
    assert exc.value.code == "REVOCATION_DRAINING"
    assert barrier.stats()["rejected"] == 1
    assert barrier.drain_status("t1", now=T0 + 1)["in_flight"] == 0


def test_in_flight_lease_survives_until_released():
    barrier = ready()
    lease = barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)

    assert barrier.drain_status("t1", now=T0 + 5)["in_flight"] == 1

    assert barrier.release_lease("t1", lease["lease_id"]) is True
    status = barrier.drain_status("t1", now=T0 + 6)
    assert status["in_flight"] == 0
    assert status["state"] == "draining"


def test_release_is_idempotent_and_unknown_lease_is_rejected():
    barrier = ready()
    lease = barrier.acquire_lease("t1", "alice", "req-1", now=T0)

    assert barrier.release_lease("t1", lease["lease_id"]) is True
    assert barrier.release_lease("t1", lease["lease_id"]) is False
    assert barrier.stats()["released"] == 1
    assert barrier.drain_status("t1", now=T0)["in_flight"] == 0

    with pytest.raises(rev.RevocationError) as exc:
        barrier.release_lease("t1", "missing")
    assert exc.value.code == "REVOCATION_UNKNOWN_LEASE"


def test_enforce_closes_leases_at_their_expiry_boundary():
    barrier = ready(lease_ttl=60)
    barrier.issue_token("t1", "alice", now=T0)
    lease = barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)

    out = barrier.enforce("t1", now=T0 + 59)
    assert out["closed"] == 0
    assert out["state"] == "draining"
    assert barrier.drain_status("t1", now=T0 + 59)["in_flight"] == 1

    out = barrier.enforce("t1", now=T0 + 60)
    assert out["closed"] == 1
    assert out["state"] == "draining"
    assert barrier.drain_status("t1", now=T0 + 60)["in_flight"] == 0
    assert barrier.stats()["enforced"] == 1


def test_enforce_at_drain_deadline_hard_cuts_tokens():
    barrier = ready(lease_ttl=600)
    barrier.configure_tenant("t1", 30)
    token = barrier.issue_token("t1", "alice", now=T0)
    barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)

    assert barrier.token_state(token["token_id"], now=T0 + 10)["usable"] is True

    out = barrier.enforce("t1", now=T0 + 29)
    assert out["closed"] == 0
    assert out["state"] == "draining"

    out = barrier.enforce("t1", now=T0 + 30)
    assert out["closed"] == 1
    assert out["state"] == "enforced"
    assert barrier.drain_status("t1", now=T0 + 30)["state"] == "enforced"
    assert barrier.token_state(token["token_id"], now=T0 + 30)["usable"] is False


def test_enforce_is_idempotent_and_keeps_barrier_closed():
    barrier = ready(lease_ttl=600)
    barrier.configure_tenant("t1", 30)
    barrier.issue_token("t1", "alice", now=T0)
    barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)
    barrier.enforce("t1", now=T0 + 30)

    again = barrier.enforce("t1", now=T0 + 31)
    assert again["closed"] == 0
    assert again["state"] == "enforced"

    with pytest.raises(rev.RevocationError) as exc:
        barrier.issue_token("t1", "alice", now=T0 + 31)
    assert exc.value.code == "REVOCATION_ACTIVE"
    with pytest.raises(rev.RevocationError) as exc:
        barrier.acquire_lease("t1", "alice", "req-2", now=T0 + 31)
    assert exc.value.code == "REVOCATION_DRAINING"


def test_snapshot_restore_keeps_generation_leases_and_counters():
    barrier = ready()
    barrier.issue_token("t1", "alice", now=T0)
    lease = barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)

    state = barrier.snapshot()
    assert state["stats"]["issued"] == 1
    assert state["stats"]["leased"] == 1
    frozen = copy.deepcopy(state)
    state["leases"].clear()
    state["stats"]["issued"] = 99
    assert barrier.stats()["issued"] == 1

    restored = make()
    restored.restore(frozen)

    status = restored.drain_status("t1", now=T0 + 5)
    assert status["state"] == "draining"
    assert status["in_flight"] == 1
    assert status["drain_deadline"] == T0 + 120
    assert restored.stats()["issued"] == 1
    assert restored.release_lease("t1", lease["lease_id"]) is True


def test_corrupt_snapshot_is_refused_and_live_state_survives():
    barrier = ready()
    lease = barrier.acquire_lease("t1", "alice", "req-1", now=T0)
    barrier.begin_revocation("t1", now=T0)

    broken = [
        None,
        {},
        {"version": 2, "tenants": {}, "leases": {}, "stats": {}},
        {"version": 1, "tenants": "nope", "leases": {}, "stats": {}},
        {"version": 1, "tenants": {}, "leases": "nope", "stats": {}},
        {"version": 1, "tenants": {}, "leases": {}},
    ]
    for state in broken:
        with pytest.raises(rev.RevocationError) as exc:
            barrier.restore(state)
        assert exc.value.code == "REVOCATION_SNAPSHOT_INVALID"

    assert barrier.drain_status("t1", now=T0 + 1)["in_flight"] == 1
    assert barrier.release_lease("t1", lease["lease_id"]) is True


def test_concurrent_acquire_and_release_keep_counters_exact():
    barrier = ready(lease_ttl=600)
    barrier.configure_tenant("t1", 300)
    barrier.issue_token("t1", "alice", now=T0)

    start = threading.Barrier(10)
    lease_ids = []
    errors = []
    guard = threading.Lock()

    def acquirer(index):
        start.wait()
        try:
            lease = barrier.acquire_lease("t1", f"user-{index}", f"req-{index}",
                                          now=T0)
        except Exception as exc:  # pragma: no cover - reported through errors
            with guard:
                errors.append(repr(exc))
        else:
            with guard:
                lease_ids.append(lease["lease_id"])

    threads = [threading.Thread(target=acquirer, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(lease_ids)) == 10
    assert barrier.drain_status("t1", now=T0)["in_flight"] == 10
    assert barrier.stats()["leased"] == 10

    stop = threading.Barrier(4)
    released = []

    def releaser(lease_id):
        stop.wait()
        released.append(barrier.release_lease("t1", lease_id))

    threads = [threading.Thread(target=releaser, args=(lid,)) for lid in lease_ids[:4]]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert released == [True, True, True, True]
    assert barrier.drain_status("t1", now=T0 + 1)["in_flight"] == 6
    assert barrier.stats()["released"] == 4
