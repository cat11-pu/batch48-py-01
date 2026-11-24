import threading

import pytest

import jit_elevation_broker as eb


def make(**kwargs):
    broker = eb.ElevationBroker(**kwargs)
    broker.register_approver("alice", ["operator"])
    broker.register_approver("dave", ["operator"])
    broker.register_approver("carol", ["viewer"])
    return broker


def open_request(broker, subject="svc-a", ttl=300, now=1000):
    return broker.request_access(subject, "operator", "修复主从延迟", ttl=ttl, now=now)


def approved_request(broker, subject="svc-a", ttl=300, now=1000):
    req = open_request(broker, subject=subject, ttl=ttl, now=now)
    broker.approve(req["request_id"], "alice", now=now + 10)
    broker.approve(req["request_id"], "dave", now=now + 20)
    return req


def test_request_access_validates_and_numbers():
    broker = make()
    req = broker.request_access("svc-a", "operator", "  修复主从延迟  ",
                                ttl=300, now=1000)
    assert req["request_id"] == "req-1"
    assert req["subject"] == "svc-a"
    assert req["role"] == "operator"
    assert req["ttl"] == 300
    assert req["created_at"] == 1000
    assert req["state"] == "pending"
    assert req["approvals"] == []

    with pytest.raises(eb.ElevationError) as exc:
        broker.request_access("svc-b", "superuser", "巡检", now=1000)
    assert exc.value.code == "ELEV_ROLE_INVALID"

    with pytest.raises(eb.ElevationError) as exc:
        broker.request_access("svc-b", "operator", "   ", now=1000)
    assert exc.value.code == "ELEV_REASON_INVALID"

    with pytest.raises(eb.ElevationError) as exc:
        broker.request_access("svc-b", "operator", "巡检", ttl=0, now=1000)
    assert exc.value.code == "ELEV_TTL_INVALID"

    with pytest.raises(eb.ElevationError) as exc:
        broker.request_access("svc-b", "operator", "巡检", ttl=99999, now=1000)
    assert exc.value.code == "ELEV_TTL_INVALID"

    second = broker.request_access("svc-b", "viewer", "只读排查", now=1001)
    assert second["request_id"] == "req-2"
    assert second["ttl"] == 900


def test_duplicate_pending_request_refused():
    broker = make()
    first = open_request(broker)
    with pytest.raises(eb.ElevationError) as exc:
        broker.request_access("svc-a", "operator", "再来一次", now=1001)
    assert exc.value.code == "ELEV_REQUEST_DUPLICATE"

    other = broker.request_access("svc-a", "viewer", "只读", now=1002)
    assert other["request_id"] == "req-2"
    assert broker.request_status(first["request_id"])["state"] == "pending"


def test_two_distinct_approvals_reach_approved():
    broker = make()
    open_request(broker)
    with pytest.raises(eb.ElevationError) as exc:
        broker.redeem("req-1", "sess-a", now=1010)
    assert exc.value.code == "ELEV_APPROVALS_PENDING"

    one = broker.approve("req-1", "alice", now=1010)
    assert one["approvals"] == ["alice"]
    assert one["required"] == 2
    assert one["state"] == "pending"

    with pytest.raises(eb.ElevationError) as exc:
        broker.approve("req-1", "alice", now=1011)
    assert exc.value.code == "ELEV_APPROVER_DUPLICATE"
    assert broker.request_status("req-1")["approvals"] == ["alice"]

    two = broker.approve("req-1", "dave", now=1020)
    assert two["approvals"] == ["alice", "dave"]
    assert two["state"] == "approved"


def test_self_approval_refused():
    broker = make()
    broker.register_approver("svc-a", ["operator"])
    open_request(broker)
    with pytest.raises(eb.ElevationError) as exc:
        broker.approve("req-1", "svc-a", now=1010)
    assert exc.value.code == "ELEV_SELF_APPROVAL"
    assert broker.request_status("req-1")["approvals"] == []


def test_approver_must_hold_requested_role():
    broker = make()
    open_request(broker)
    with pytest.raises(eb.ElevationError) as exc:
        broker.approve("req-1", "carol", now=1010)
    assert exc.value.code == "ELEV_APPROVER_UNAUTHORIZED"

    with pytest.raises(eb.ElevationError) as exc:
        broker.approve("req-1", "mallory", now=1011)
    assert exc.value.code == "ELEV_APPROVER_UNAUTHORIZED"
    assert broker.request_status("req-1")["state"] == "pending"


def test_redeem_binds_session_and_grant_fields():
    broker = make()
    approved_request(broker, ttl=300, now=1000)
    grant = broker.redeem("req-1", "sess-a", now=1100)
    assert grant["grant_id"] == "grant-1"
    assert grant["subject"] == "svc-a"
    assert grant["role"] == "operator"
    assert grant["session_id"] == "sess-a"
    assert grant["issued_at"] == 1100
    assert grant["expires_at"] == 1400
    assert grant["state"] == "active"

    ok = broker.validate("grant-1", "sess-a", now=1200)
    assert ok["grant_id"] == "grant-1"
    assert ok["role"] == "operator"
    assert ok["requested_role"] == "operator"
    assert ok["expires_at"] == 1400
    assert ok["remaining"] == 200

    with pytest.raises(eb.ElevationError) as exc:
        broker.validate("grant-1", "sess-b", now=1200)
    assert exc.value.code == "ELEV_SESSION_MISMATCH"

    with pytest.raises(eb.ElevationError) as exc:
        broker.validate("grant-1", "", now=1200)
    assert exc.value.code == "ELEV_SESSION_REQUIRED"

    with pytest.raises(eb.ElevationError) as exc:
        broker.redeem("req-1", "sess-b", now=1201)
    assert exc.value.code == "ELEV_REQUEST_STATE"


def test_grant_expiry_measured_from_redemption():
    broker = make()
    approved_request(broker, ttl=300, now=1000)
    grant = broker.redeem("req-1", "sess-a", now=1200)
    assert grant["expires_at"] == 1500

    assert broker.validate("grant-1", "sess-a", now=1499)["remaining"] == 1
    with pytest.raises(eb.ElevationError) as exc:
        broker.validate("grant-1", "sess-a", now=1500)
    assert exc.value.code == "ELEV_GRANT_EXPIRED"


def test_denied_request_cannot_be_redeemed():
    broker = make()
    open_request(broker)
    with pytest.raises(eb.ElevationError) as exc:
        broker.deny("req-1", "alice", "   ", now=1010)
    assert exc.value.code == "ELEV_NOTE_INVALID"

    denied = broker.deny("req-1", "alice", "误报", now=1011)
    assert denied["state"] == "denied"

    with pytest.raises(eb.ElevationError) as exc:
        broker.redeem("req-1", "sess-a", now=1012)
    assert exc.value.code == "ELEV_REQUEST_STATE"

    with pytest.raises(eb.ElevationError) as exc:
        broker.approve("req-1", "dave", now=1013)
    assert exc.value.code == "ELEV_REQUEST_STATE"

    with pytest.raises(eb.ElevationError) as exc:
        broker.deny("req-1", "dave", "补充", now=1014)
    assert exc.value.code == "ELEV_REQUEST_STATE"


def test_revoke_invalidates_grant_and_is_idempotent():
    broker = make()
    approved_request(broker)
    broker.redeem("req-1", "sess-a", now=1100)

    revoked = broker.revoke("grant-1", now=1200)
    assert revoked["grant_id"] == "grant-1"
    assert revoked["state"] == "revoked"

    with pytest.raises(eb.ElevationError) as exc:
        broker.validate("grant-1", "sess-a", now=1201)
    assert exc.value.code == "ELEV_GRANT_REVOKED"

    again = broker.revoke("grant-1", now=1202)
    assert again["state"] == "revoked"
    assert len(broker.grants()) == 1


def test_validate_rejects_role_above_grant():
    broker = make()
    approved_request(broker)
    broker.redeem("req-1", "sess-a", now=1100)

    lower = broker.validate("grant-1", "sess-a", role="viewer", now=1200)
    assert lower["role"] == "operator"
    assert lower["requested_role"] == "viewer"

    with pytest.raises(eb.ElevationError) as exc:
        broker.validate("grant-1", "sess-a", role="admin", now=1201)
    assert exc.value.code == "ELEV_ROLE_OUT_OF_SCOPE"


def test_expire_sweeps_only_due_grants():
    broker = make()
    approved_request(broker, subject="svc-a", ttl=100, now=1000)
    broker.redeem("req-1", "sess-a", now=1000)
    approved_request(broker, subject="svc-b", ttl=500, now=1000)
    broker.redeem("req-2", "sess-b", now=1000)

    assert broker.expire(1100) == 1
    with pytest.raises(eb.ElevationError) as exc:
        broker.validate("grant-1", "sess-a", now=1100)
    assert exc.value.code == "ELEV_GRANT_EXPIRED"
    assert broker.validate("grant-2", "sess-b", now=1100)["grant_id"] == "grant-2"
    assert broker.expire(1101) == 0


def test_concurrent_redeem_issues_single_grant():
    broker = make()
    approved_request(broker)
    grants = []
    failures = []
    guard = threading.Lock()

    def worker(index):
        try:
            grant = broker.redeem("req-1", f"sess-{index}", now=1200)
            with guard:
                grants.append(grant)
        except eb.ElevationError as exc:
            with guard:
                failures.append(exc.code)
        except Exception as exc:  # noqa: BLE001 - reported as a failed admission
            with guard:
                failures.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(grants) == 1
    assert len(failures) == 5
    assert all(code == "ELEV_REQUEST_STATE" for code in failures)
    assert len(broker.grants()) == 1


def test_snapshot_is_detached_deep_copy():
    broker = make()
    open_request(broker)
    broker.approve("req-1", "alice", now=1010)

    snap = broker.snapshot()
    assert snap["version"] == 1
    assert "req-1" in snap["requests"]
    snap["requests"]["req-1"]["approvals"].append("ghost")

    result = broker.approve("req-1", "dave", now=1020)
    assert result["approvals"] == ["alice", "dave"]
    assert result["state"] == "approved"


def test_restore_keeps_grants_and_blocks_second_redeem():
    broker = make()
    approved_request(broker, ttl=300, now=1000)
    broker.redeem("req-1", "sess-a", now=1100)
    snap = broker.snapshot()

    restored = eb.ElevationBroker()
    restored.restore(snap)

    assert restored.validate("grant-1", "sess-a", now=1300)["remaining"] == 100
    assert restored.request_status("req-1")["state"] == "redeemed"
    with pytest.raises(eb.ElevationError) as exc:
        restored.redeem("req-1", "sess-a", now=1301)
    assert exc.value.code == "ELEV_REQUEST_STATE"
    assert len(restored.grants()) == 1


def test_restore_rejects_bad_state_and_stats_reset():
    broker = make()
    open_request(broker)
    broker.approve("req-1", "alice", now=1010)
    broker.approve("req-1", "dave", now=1011)
    broker.redeem("req-1", "s1", now=1020)
    broker.revoke("grant-1", now=1030)
    broker.request_access("svc-b", "viewer", "只读", now=1040)
    broker.deny("req-2", "alice", "不需要", now=1050)

    assert broker.stats() == {
        "requests": 2, "approvals": 2, "denials": 1,
        "grants": 1, "revocations": 1,
    }

    with pytest.raises(eb.ElevationError) as exc:
        broker.restore({"version": 99})
    assert exc.value.code == "ELEV_SNAPSHOT_INVALID"
    assert broker.request_status("req-2")["state"] == "denied"

    broker.reset_stats()
    assert broker.stats() == {
        "requests": 0, "approvals": 0, "denials": 0,
        "grants": 0, "revocations": 0,
    }
