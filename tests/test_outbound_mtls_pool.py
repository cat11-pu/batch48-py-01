import copy
import threading

import pytest

import outbound_mtls_pool as mtls

T0 = 1_700_000_000
TARGET = "db.internal:5432"
SNI = "db.internal"
SUBJECT = "CN=db.internal"


def make(**kwargs):
    return mtls.OutboundMtlsPool(**kwargs)


def ready(**kwargs):
    pool = make(**kwargs)
    pool.register_peer(TARGET, subject=SUBJECT)
    pool.register_identity("id-a", tenant="t1", fingerprint="fp-a",
                           not_after=T0 + 3600)
    return pool


def acquire(pool, identity="id-a", tenant="t1", sni=SNI, wait_ms=0, now=T0):
    return pool.acquire(tenant, TARGET, sni=sni, identity_id=identity,
                        expected_subject=SUBJECT, now=now, wait_ms=wait_ms)


def test_acquire_opens_a_connection_with_the_full_key():
    pool = ready()
    identity = pool.register_identity("id-b", tenant="t1", fingerprint="fp-b",
                                      not_after=T0 + 3600)
    assert identity["identity_id"] == "id-b"
    assert identity["tenant"] == "t1"
    assert identity["fingerprint"] == "fp-b"
    assert identity["not_after"] == T0 + 3600

    lease = acquire(pool)
    assert lease["queued"] is False
    assert lease["reused"] is False
    assert lease["tenant"] == "t1"
    assert lease["target"] == TARGET
    assert lease["sni"] == SNI
    assert lease["identity_id"] == "id-a"
    assert lease["fingerprint"] == "fp-a"
    assert lease["in_flight"] == 1

    state = pool.connection_state(lease["conn_id"])
    assert state["state"] == "active"
    assert state["in_flight"] == 1
    assert state["fingerprint"] == "fp-a"


def test_tenants_never_share_a_connection():
    pool = make()
    pool.register_peer(TARGET, subject=SUBJECT)
    pool.register_identity("id-a", tenant="t1", fingerprint="fp-shared",
                           not_after=T0 + 3600)
    pool.register_identity("id-b", tenant="t2", fingerprint="fp-shared",
                           not_after=T0 + 3600)

    first = acquire(pool, identity="id-a", tenant="t1")
    second = acquire(pool, identity="id-b", tenant="t2")

    assert first["conn_id"] != second["conn_id"]
    assert first["fingerprint"] == second["fingerprint"] == "fp-shared"


def test_sni_is_part_of_the_connection_key():
    pool = ready()

    first = acquire(pool, sni="db.internal")
    second = acquire(pool, sni="db-ro.internal")

    assert first["conn_id"] != second["conn_id"]


def test_release_is_idempotent_and_idle_connections_are_reused():
    pool = ready()
    lease = acquire(pool)

    out = pool.release(lease["lease_id"], now=T0 + 1)
    assert out["released"] is True
    state = pool.connection_state(lease["conn_id"])
    assert state["state"] == "idle"
    assert state["in_flight"] == 0

    again = pool.release(lease["lease_id"], now=T0 + 2)
    assert again["released"] is False

    reused = acquire(pool, now=T0 + 3)
    assert reused["reused"] is True
    assert reused["conn_id"] == lease["conn_id"]


def test_per_target_connection_cap_rejects_extra_keys():
    pool = ready(target_limit=4)

    for index in range(4):
        lease = acquire(pool, sni=f"sni-{index}")
        assert lease["reused"] is False

    with pytest.raises(mtls.MtlsPoolError) as exc:
        acquire(pool, sni="sni-4")
    assert exc.value.code == "MTLS_TARGET_SATURATED"


def test_waiting_acquire_is_promoted_when_a_slot_is_closed():
    pool = make(target_limit=1)
    pool.register_peer(TARGET, subject=SUBJECT)
    pool.register_identity("id-a", tenant="t1", fingerprint="fp-a",
                           not_after=T0 + 3600)
    pool.register_identity("id-b", tenant="t1", fingerprint="fp-b",
                           not_after=T0 + 3600)

    first = acquire(pool, identity="id-a")
    queued = acquire(pool, identity="id-b", wait_ms=500)
    assert queued["queued"] is True
    assert queued["position"] == 1
    assert queued["deadline"] == T0 + 500
    assert pool.waiters(TARGET) == 1

    out = pool.release(first["lease_id"], now=T0 + 1, close=True)
    assert out["released"] is True
    promoted = out["promoted"]
    assert promoted is not None
    assert promoted["fingerprint"] == "fp-b"
    assert promoted["identity_id"] == "id-b"
    assert pool.waiters(TARGET) == 0

    assert pool.release(promoted["lease_id"], now=T0 + 2)["released"] is True


def test_waiters_expire_at_their_deadline():
    pool = make(target_limit=1)
    pool.register_peer(TARGET, subject=SUBJECT)
    pool.register_identity("id-a", tenant="t1", fingerprint="fp-a",
                           not_after=T0 + 3600)
    pool.register_identity("id-b", tenant="t1", fingerprint="fp-b",
                           not_after=T0 + 3600)

    acquire(pool, identity="id-a")
    queued = acquire(pool, identity="id-b", wait_ms=100)
    assert queued["queued"] is True
    assert queued["deadline"] == T0 + 100

    assert pool.expire_waiters(T0 + 99) == 0
    assert pool.waiters(TARGET) == 1
    assert pool.expire_waiters(T0 + 100) == 1
    assert pool.waiters(TARGET) == 0

    with pytest.raises(mtls.MtlsPoolError) as exc:
        acquire(pool, identity="id-b", wait_ms=0)
    assert exc.value.code == "MTLS_TARGET_SATURATED"


def test_identity_lookup_checks_tenant_conflict_and_expiry():
    pool = make()
    pool.register_peer(TARGET, subject=SUBJECT)
    pool.register_identity("id-a", tenant="t1", fingerprint="fp-a",
                           not_after=T0 + 10)
    pool.register_identity("id-b", tenant="t2", fingerprint="fp-b",
                           not_after=T0 + 10)

    with pytest.raises(mtls.MtlsPoolError) as exc:
        acquire(pool, identity="id-missing", tenant="t1")
    assert exc.value.code == "MTLS_UNKNOWN_IDENTITY"

    with pytest.raises(mtls.MtlsPoolError) as exc:
        acquire(pool, identity="id-b", tenant="t1")
    assert exc.value.code == "MTLS_IDENTITY_TENANT_MISMATCH"

    with pytest.raises(mtls.MtlsPoolError) as exc:
        pool.register_identity("id-a", tenant="t1", fingerprint="fp-a",
                               not_after=T0 + 10)
    assert exc.value.code == "MTLS_IDENTITY_CONFLICT"

    with pytest.raises(mtls.MtlsPoolError) as exc:
        acquire(pool, identity="id-a", tenant="t1", now=T0 + 10)
    assert exc.value.code == "MTLS_IDENTITY_EXPIRED"


def test_target_limit_validation_and_override():
    pool = ready()
    for bad in (0, -3):
        with pytest.raises(mtls.MtlsPoolError) as exc:
            pool.set_target_limit(TARGET, bad)
        assert exc.value.code == "MTLS_INVALID_LIMIT"

    assert pool.set_target_limit(TARGET, 1) == 1
    first = acquire(pool, sni=SNI)
    with pytest.raises(mtls.MtlsPoolError) as exc:
        acquire(pool, sni="db-ro.internal")
    assert exc.value.code == "MTLS_TARGET_SATURATED"
    assert pool.release(first["lease_id"], now=T0 + 1, close=True)["released"] is True
    assert acquire(pool, sni="db-ro.internal", now=T0 + 2)["sni"] == "db-ro.internal"


def test_rotation_drains_old_connections_and_new_requests_use_new_fingerprint():
    pool = ready()
    lease = acquire(pool)

    rotated = pool.rotate_identity("id-a", "fp-b", now=T0 + 10, overlap=60)
    assert rotated["identity_id"] == "id-a"
    assert rotated["fingerprint"] == "fp-b"
    assert rotated["previous_fingerprint"] == "fp-a"
    assert rotated["drain_deadline"] == T0 + 70
    assert pool.connection_state(lease["conn_id"])["state"] == "draining"

    assert pool.release(lease["lease_id"], now=T0 + 11)["released"] is True
    assert pool.connection_state(lease["conn_id"])["state"] == "closed"

    fresh = acquire(pool, now=T0 + 12)
    assert fresh["fingerprint"] == "fp-b"
    assert fresh["conn_id"] != lease["conn_id"]
    assert pool.connection_state(fresh["conn_id"])["state"] == "active"


def test_drain_target_closes_idle_connections_and_retires_in_flight_ones():
    pool = ready()
    first = acquire(pool, sni=SNI)
    second = acquire(pool, sni="db-ro.internal")

    assert pool.release(first["lease_id"], now=T0 + 1)["released"] is True
    assert pool.drain_target(TARGET, now=T0 + 2) == 1
    assert pool.connection_state(first["conn_id"])["state"] == "closed"
    assert pool.connection_state(second["conn_id"])["state"] == "draining"

    assert pool.release(second["lease_id"], now=T0 + 3)["released"] is True
    assert pool.connection_state(second["conn_id"])["state"] == "closed"
    assert pool.drain_target(TARGET, now=T0 + 4) == 0


def test_release_of_an_unknown_lease_is_rejected():
    pool = ready()
    with pytest.raises(mtls.MtlsPoolError) as exc:
        pool.release("missing-lease", now=T0)
    assert exc.value.code == "MTLS_UNKNOWN_LEASE"


def test_snapshot_restore_keeps_idle_connections_and_leases():
    pool = ready()
    first = acquire(pool, sni=SNI)
    second = acquire(pool, sni="db-ro.internal")
    assert pool.release(first["lease_id"], now=T0 + 1)["released"] is True

    state = pool.snapshot()
    assert state["stats"]["created"] == 2
    frozen = copy.deepcopy(state)
    state["connections"].clear()
    state["stats"]["created"] = 99

    restored = make()
    restored.restore(frozen)

    again = restored.acquire("t1", TARGET, sni=SNI, identity_id="id-a",
                             expected_subject=SUBJECT, now=T0 + 2)
    assert again["reused"] is True
    assert again["conn_id"] == first["conn_id"]
    assert restored.release(second["lease_id"], now=T0 + 3)["released"] is True


def test_corrupt_snapshot_is_refused_and_live_state_survives():
    pool = ready()
    lease = acquire(pool)

    broken = [
        None,
        {},
        {"version": 2, "connections": {}, "stats": {}},
        {"version": 1, "connections": "nope", "stats": {}},
        {"version": 1, "connections": {}, "stats": "nope"},
        {"version": 1, "connections": {}},
    ]
    for state in broken:
        with pytest.raises(mtls.MtlsPoolError) as exc:
            pool.restore(state)
        assert exc.value.code == "MTLS_SNAPSHOT_INVALID"

    assert pool.connection_state(lease["conn_id"])["state"] == "active"
    assert pool.release(lease["lease_id"], now=T0 + 1)["released"] is True


def test_concurrent_acquire_of_the_same_key_shares_one_connection():
    pool = ready()
    barrier = threading.Barrier(8)
    leases = []
    errors = []
    guard = threading.Lock()

    def worker():
        barrier.wait()
        try:
            lease = acquire(pool)
        except Exception as exc:  # pragma: no cover - reported through errors
            with guard:
                errors.append(repr(exc))
        else:
            with guard:
                leases.append(lease)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len({lease["conn_id"] for lease in leases}) == 1
    conn_id = leases[0]["conn_id"]
    assert pool.connection_state(conn_id)["in_flight"] == 8

    for lease in leases:
        assert pool.release(lease["lease_id"], now=T0 + 1)["released"] is True
    assert pool.connection_state(conn_id)["in_flight"] == 0
