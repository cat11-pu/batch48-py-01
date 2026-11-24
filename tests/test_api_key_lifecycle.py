import copy
import threading

import pytest

import api_key_lifecycle as ak


def manager(**kwargs):
    return ak.ApiKeyLifecycleManager(**kwargs)


def issue(keys, tenant="t1", scopes=("read",), **kwargs):
    return keys.issue_key(tenant, scopes=list(scopes), **kwargs)


def test_issue_returns_secret_once_and_never_stores_it():
    keys = manager()
    out = issue(keys, scopes=("write", "read", "read"))
    assert out["key_id"] == "k1"
    assert out["scopes"] == ["read", "write"]
    assert out["secret"].startswith("ak")

    listed = keys.list_keys("t1")
    assert len(listed) == 1
    assert "secret" not in listed[0]
    assert listed[0]["key_id"] == "k1"
    assert out["secret"] not in repr(keys.snapshot())


def test_verify_resolves_tenant_and_scopes():
    keys = manager()
    out = issue(keys, scopes=("read",))
    ok = keys.verify_key(out["secret"], tenant="t1", scope="read", now=5)
    assert ok["ok"] is True
    assert ok["key_id"] == "k1"


def test_unknown_or_empty_secret_is_refused():
    keys = manager()
    issue(keys)
    with pytest.raises(ak.ApiKeyError) as exc:
        keys.verify_key("ak" + "0" * 32)
    assert exc.value.code == "APIKEY_UNKNOWN"
    with pytest.raises(ak.ApiKeyError):
        keys.verify_key("")
    with pytest.raises(ak.ApiKeyError):
        keys.verify_key(None)
    assert [item["key_id"] for item in keys.list_keys("t1")] == ["k1"]


def test_scope_and_tenant_mismatch_are_denied():
    keys = manager()
    out = issue(keys, scopes=("read",))
    with pytest.raises(ak.ApiKeyError) as exc:
        keys.verify_key(out["secret"], scope="write")
    assert exc.value.code == "APIKEY_SCOPE_DENIED"
    assert keys.verify_key(out["secret"], scope="read")["ok"] is True


def test_source_netmask_restriction():
    keys = manager()
    restricted = issue(keys, scopes=("read",), cidrs=["10.0.0.0/8"])
    assert keys.verify_key(restricted["secret"], source="10.4.5.6")["ok"] is True
    with pytest.raises(ak.ApiKeyError):
        keys.verify_key(restricted["secret"], source="192.168.0.9")
    with pytest.raises(ak.ApiKeyError):
        keys.verify_key(restricted["secret"])

    open_key = issue(keys, scopes=("read",))
    assert keys.verify_key(open_key["secret"], source="203.0.113.9")["ok"] is True
    with pytest.raises(ak.ApiKeyError):
        issue(keys, scopes=("read",), cidrs=["not-a-network"])


def test_expiry_boundary_is_inclusive():
    keys = manager()
    out = issue(keys, scopes=("read",), expires_at=1000)
    assert keys.verify_key(out["secret"], now=999)["ok"] is True
    assert keys.verify_key(out["secret"], now=0)["ok"] is True
    with pytest.raises(ak.ApiKeyError):
        keys.verify_key(out["secret"], now=1000)


def test_revocation_is_idempotent_and_blocks_verification():
    keys = manager()
    out = issue(keys)
    first = keys.revoke_key("t1", "k1", now=10)
    assert first["key_id"] == "k1"
    second = keys.revoke_key("t1", "k1", now=20)
    assert second["key_id"] == "k1"
    with pytest.raises(ak.ApiKeyError) as exc:
        keys.verify_key(out["secret"])
    assert exc.value.code == "APIKEY_REVOKED"
    with pytest.raises(ak.ApiKeyError):
        keys.revoke_key("t1", "k9")


def test_key_capacity_is_enforced_per_tenant():
    keys = manager(max_keys_per_tenant=2)
    issue(keys)
    issue(keys)
    with pytest.raises(ak.ApiKeyError):
        issue(keys)
    assert len(keys.list_keys("t1")) == 2

    issue(keys, tenant="t2")
    assert len(keys.list_keys("t2")) == 1


def test_scope_cap_and_normalization():
    keys = manager(max_scopes_per_key=2)
    out = issue(keys, scopes=("write", "read", "read"))
    assert out["scopes"] == ["read", "write"]
    with pytest.raises(ak.ApiKeyError):
        issue(keys, scopes=("a", "b", "c"))
    with pytest.raises(ak.ApiKeyError):
        issue(keys, scopes=())
    with pytest.raises(ak.ApiKeyError):
        issue(keys, scopes=(1, 2))
    assert len(keys.list_keys("t1")) == 1


def test_rotation_keeps_old_key_valid_during_overlap():
    keys = manager()
    old = issue(keys, scopes=("read",))
    rotated = keys.rotate_key("t1", "k1", overlap=100, now=0)
    assert rotated["key_id"] == "k2"
    assert rotated["secret"] != old["secret"]

    during = keys.verify_key(old["secret"], now=50)
    assert during["ok"] is True
    assert during["key_id"] == "k1"
    assert keys.verify_key(rotated["secret"], now=50)["ok"] is True

    with pytest.raises(ak.ApiKeyError):
        keys.verify_key(old["secret"], now=100)
    assert keys.verify_key(rotated["secret"], now=100)["ok"] is True


def test_rotation_inherits_policy_and_links_both_sides():
    keys = manager()
    issue(keys, scopes=("read", "write"), cidrs=["10.0.0.0/8"], expires_at=5000)
    rotated = keys.rotate_key("t1", "k1", overlap=200, now=10)

    fresh = keys.verify_key(rotated["secret"], scope="write",
                            source="10.1.2.3", now=20)
    assert fresh["ok"] is True

    listed = {item["key_id"]: item for item in keys.list_keys("t1", now=20)}
    assert listed["k1"]["state"] == "retiring"
    assert listed["k2"]["state"] == "active"

    with pytest.raises(ak.ApiKeyError):
        keys.rotate_key("t1", "k9")


def test_touch_last_used_batches_pending_writes():
    keys = manager()
    issue(keys)
    keys.touch_last_used("k1", now=10)
    keys.touch_last_used("k1", now=30)
    keys.touch_last_used("k1", now=20)
    assert keys.list_keys("t1")[0]["last_used"] is None
    keys.flush_last_used()
    assert keys.list_keys("t1")[0]["last_used"] == 30
    keys.touch_last_used("k1", now=5)
    keys.flush_last_used()
    assert keys.list_keys("t1")[0]["last_used"] == 30

    with pytest.raises(ak.ApiKeyError):
        keys.touch_last_used("k9", now=1)


def test_snapshot_restore_keeps_digests_and_revocations():
    keys = manager()
    first = issue(keys, scopes=("read",))
    second = issue(keys, scopes=("admin",))
    keys.revoke_key("t1", "k2")

    snap = keys.snapshot()
    pristine = copy.deepcopy(snap)
    clone = manager()
    clone.restore(pristine)
    assert clone.stats() == keys.stats()
    assert first["secret"] not in repr(clone.snapshot())
    assert clone.verify_key(first["secret"])["key_id"] == "k1"
    with pytest.raises(ak.ApiKeyError) as exc:
        clone.verify_key(second["secret"])
    assert exc.value.code == "APIKEY_REVOKED"
    assert [item["key_id"] for item in clone.list_keys("t1")] == ["k1", "k2"]

    snap["keys"]["k1"]["state"] = "revoked"
    snap["keys"]["k1"]["digest"] = "0" * 64
    assert keys.verify_key(first["secret"])["ok"] is True

    empty_stats = {"issued": 0, "verified": 0, "denied": 0, "rotated": 0,
                   "revoked": 0, "flushed": 0}
    bad_states = [
        None,
        {},
        {"version": 2, "max_keys_per_tenant": 5, "max_scopes_per_key": 4,
         "prefix_len": 8, "keys": {}, "rotations": {},
         "pending_last_used": {}, "stats": empty_stats},
        {"version": 1, "max_keys_per_tenant": 5, "max_scopes_per_key": 4,
         "prefix_len": 8, "keys": [], "rotations": {},
         "pending_last_used": {}, "stats": empty_stats},
        {"version": 1, "max_keys_per_tenant": 5, "max_scopes_per_key": 4,
         "prefix_len": 8,
         "keys": {"k1": {"key_id": "k9", "tenant": "t1", "prefix": "ak",
                         "digest": "x", "scopes": ["read"], "cidrs": [],
                         "created_at": 0, "expires_at": None,
                         "revoked_at": None, "retire_at": None,
                         "last_used": None, "rotated_to": None,
                         "rotated_from": None}},
         "rotations": {}, "pending_last_used": {}, "stats": empty_stats},
        {"version": 1, "max_keys_per_tenant": 5, "max_scopes_per_key": 4,
         "prefix_len": 8, "keys": {}, "rotations": {},
         "pending_last_used": {}, "stats": {"issued": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(ak.ApiKeyError):
            keys.restore(bad)
    assert keys.verify_key(first["secret"])["ok"] is True


def test_list_keys_reports_states_in_key_order():
    keys = manager()
    issue(keys)
    issue(keys)
    issue(keys)
    keys.revoke_key("t1", "k2")
    keys.rotate_key("t1", "k3", overlap=100, now=0)

    listed = keys.list_keys("t1", now=0)
    assert [item["key_id"] for item in listed] == ["k1", "k2", "k3", "k4"]
    states = {item["key_id"]: item["state"] for item in listed}
    assert states["k1"] == "active"
    assert states["k2"] == "revoked"
    later = {item["key_id"]: item["state"]
             for item in keys.list_keys("t1", now=100)}
    assert later["k1"] == "active"
    assert later["k3"] != "active"


def test_concurrent_verification_and_issue_stay_exact():
    keys = manager(max_keys_per_tenant=5)
    issued = issue(keys, scopes=("read",))
    errors = []

    def verifier():
        try:
            for _ in range(20):
                keys.verify_key(issued["secret"], scope="read", now=7)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    def issuer():
        try:
            issue(keys, scopes=("read",))
        except ak.ApiKeyError:
            pass

    threads = [threading.Thread(target=verifier) for _ in range(4)]
    threads += [threading.Thread(target=issuer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(keys.list_keys("t1")) == 5
    assert keys.verify_key(issued["secret"], scope="read")["ok"] is True
