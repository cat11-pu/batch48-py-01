import copy
import threading

import pytest

import tfo_cookie_guard as tf

A = "203.0.113.5"
B = "198.51.100.7"


def make_guard(**kwargs):
    return tf.TfoCookieGuard(**kwargs)


def test_issue_requires_a_current_key():
    guard = make_guard()
    with pytest.raises(tf.TfoError) as exc:
        guard.issue(A, now_ms=0)
    assert exc.value.code == "TFO_NO_KEY"
    assert guard.stats()["issued"] == 0

    guard.rotate_key("k1")
    issued = guard.issue(A, now_ms=1000)
    assert set(issued) == {"cookie", "key_id", "expires_ms"}
    assert issued["key_id"] == "k1"
    assert issued["expires_ms"] == 61000
    assert len(issued["cookie"]) == 64
    assert issued["cookie"] == issued["cookie"].lower()
    int(issued["cookie"], 16)
    assert guard.stats()["issued"] == 1


def test_verify_accepts_matching_cookie():
    guard = make_guard()
    guard.rotate_key("k1")
    cookie = guard.issue(A, now_ms=0)["cookie"]

    assert guard.verify(A, cookie, now_ms=1000) == {
        "ok": True, "key_id": "k1", "reason": "ok",
    }
    stats = guard.stats()
    assert stats["verified"] == 1
    assert stats["accepted"] == 1
    assert stats["rejected"] == 0


def test_verify_rejects_address_mismatch():
    guard = make_guard()
    guard.rotate_key("k1")
    cookie = guard.issue(A, now_ms=0)["cookie"]

    out = guard.verify(B, cookie, now_ms=10)
    assert out["ok"] is False
    assert out["reason"] == "address_mismatch"
    assert guard.stats()["rejected"] == 1


def test_verify_rejects_expired_cookie():
    guard = make_guard()
    guard.rotate_key("k1")
    cookie = guard.issue(A, now_ms=0)["cookie"]

    assert guard.verify(A, cookie, now_ms=59999)["ok"] is True
    out = guard.verify(A, cookie, now_ms=60000)
    assert out["ok"] is False
    assert out["reason"] == "expired"


def test_verify_rejects_tampered_and_malformed_cookies():
    guard = make_guard()
    guard.rotate_key("k1")
    cookie = guard.issue(A, now_ms=0)["cookie"]

    tampered = cookie[:-1] + ("0" if cookie[-1] != "0" else "1")
    out = guard.verify(A, tampered, now_ms=10)
    assert out["ok"] is False
    assert out["reason"] == "bad_signature"

    for bad in ("zz", "00" * 20, ""):
        out = guard.verify(A, bad, now_ms=10)
        assert out["ok"] is False
        assert out["reason"] == "malformed"


def test_rotation_keeps_previous_key_within_grace():
    guard = make_guard(key_grace_ms=30000)
    assert guard.rotate_key("k1", now_ms=0) == {
        "key_id": "k1", "generation": 1, "previous": None,
    }
    old = guard.issue(A, now_ms=100)
    assert guard.rotate_key("k2", now_ms=1000) == {
        "key_id": "k2", "generation": 2, "previous": "k1",
    }

    assert guard.list_keys() == [
        {"key_id": "k1", "generation": 1, "state": "previous",
         "grace_until_ms": 31000},
        {"key_id": "k2", "generation": 2, "state": "active",
         "grace_until_ms": None},
    ]
    assert guard.verify(A, old["cookie"], now_ms=20000) == {
        "ok": True, "key_id": "k1", "reason": "ok",
    }
    assert guard.issue(A, now_ms=2500)["key_id"] == "k2"


def test_revoked_key_rejects_previous_cookie():
    guard = make_guard()
    guard.rotate_key("k1", now_ms=0)
    cookie = guard.issue(A, now_ms=0)["cookie"]
    guard.rotate_key("k2", now_ms=500)

    guard.revoke_key("k1")
    out = guard.verify(A, cookie, now_ms=600)
    assert out["ok"] is False
    assert out["reason"] == "key_revoked"

    assert guard.list_keys() == [
        {"key_id": "k1", "generation": 1, "state": "revoked",
         "grace_until_ms": None},
        {"key_id": "k2", "generation": 2, "state": "active",
         "grace_until_ms": None},
    ]
    with pytest.raises(tf.TfoError) as exc:
        guard.revoke_key("missing")
    assert exc.value.code == "TFO_UNKNOWN_KEY"


def test_duplicate_key_is_rejected_on_rotation():
    guard = make_guard()
    guard.rotate_key("k1")
    with pytest.raises(tf.TfoError) as exc:
        guard.rotate_key("k1")
    assert exc.value.code == "TFO_DUPLICATE_KEY"
    assert [item["key_id"] for item in guard.list_keys()] == ["k1"]

    guard.revoke_key("k1")
    with pytest.raises(tf.TfoError) as exc:
        guard.rotate_key("k1")
    assert exc.value.code == "TFO_DUPLICATE_KEY"


def test_rate_limit_is_per_address_and_slides():
    guard = make_guard(rate_limit=2, rate_window_ms=1000)
    guard.rotate_key("k1")

    guard.issue(A, now_ms=0)
    guard.issue(A, now_ms=100)
    with pytest.raises(tf.TfoError) as exc:
        guard.issue(A, now_ms=200)
    assert exc.value.code == "TFO_RATE_LIMITED"

    stats = guard.stats()
    assert stats["issued"] == 2
    assert stats["rate_limited"] == 1
    assert stats["rejected"] == 1

    assert guard.issue(A, now_ms=1200)["key_id"] == "k1"
    assert guard.issue(B, now_ms=200)["key_id"] == "k1"


def test_sfo_deliverable_only_with_valid_cookie():
    guard = make_guard()
    guard.rotate_key("k1")
    cookie = guard.issue(A, now_ms=0)["cookie"]

    assert guard.sfo_deliverable(A, cookie, now_ms=10) is True
    assert guard.sfo_deliverable(B, cookie, now_ms=10) is False
    assert guard.sfo_deliverable(A, cookie, now_ms=60000) is False
    assert guard.sfo_deliverable(A, "zz", now_ms=10) is False


def test_cookie_is_deterministic_for_same_address_and_time():
    guard = make_guard()
    guard.rotate_key("k1")
    first = guard.issue(A, now_ms=1000)
    second = guard.issue(A, now_ms=1000)
    later = guard.issue(A, now_ms=2000)

    assert first == second
    assert first["expires_ms"] == 61000
    assert later["cookie"] != first["cookie"]
    assert later["expires_ms"] == 62000


def test_snapshot_is_deep_copy_and_rejects_malformed_restore():
    guard = make_guard()
    guard.rotate_key("k1", now_ms=0)
    guard.issue(A, now_ms=10)
    guard.rotate_key("k2", now_ms=100)

    snap = guard.snapshot()
    assert set(snap) == {"version", "keys", "rate", "stats"}
    assert snap["version"] == 1

    snap["keys"]["k1"]["grace_until_ms"] = 1
    snap["stats"]["issued"] = 99
    snap["rate"].clear()

    keys = guard.list_keys()
    assert keys[0]["grace_until_ms"] == 30100
    assert guard.stats()["issued"] == 1

    bad_states = [
        {"version": 2},
        {"version": 1},
        {"version": 1, "keys": [], "rate": {}, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(tf.TfoError) as exc:
            guard.restore(bad)
        assert exc.value.code == "TFO_SNAPSHOT_INVALID"
    assert guard.list_keys()[0]["grace_until_ms"] == 30100


def test_restore_preserves_keys_grace_and_rate_state():
    guard = make_guard()
    guard.rotate_key("k1", now_ms=0)
    old = guard.issue(A, now_ms=100)
    guard.rotate_key("k2", now_ms=500)
    guard.issue(A, now_ms=600)
    frozen = guard.stats()
    snap = copy.deepcopy(guard.snapshot())

    revived = make_guard()
    revived.restore(snap)
    assert revived.list_keys() == guard.list_keys()
    assert revived.stats() == frozen
    assert revived.verify(A, old["cookie"], now_ms=700) == {
        "ok": True, "key_id": "k1", "reason": "ok",
    }
    assert revived.issue(A, now_ms=3000)["key_id"] == "k2"


def test_stats_and_reset():
    guard = make_guard()
    guard.rotate_key("k1")
    issued = guard.issue(A, now_ms=1000)
    guard.verify(A, issued["cookie"], now_ms=2000)
    guard.verify(B, issued["cookie"], now_ms=2000)
    guard.verify(A, "zz", now_ms=2000)

    stats = guard.stats()
    assert set(stats) == {"issued", "verified", "accepted",
                          "rejected", "rotated", "rate_limited"}
    assert stats["issued"] == 1
    assert stats["accepted"] == 1
    assert stats["rejected"] == 2
    assert stats["rotated"] == 1
    assert stats["rate_limited"] == 0
    guard.reset_stats()
    assert guard.stats() == {
        "issued": 0, "verified": 0, "accepted": 0,
        "rejected": 0, "rotated": 0, "rate_limited": 0,
    }


def test_concurrent_issue_and_verify_are_counted_once():
    guard = make_guard(rate_limit=3, rate_window_ms=1000)
    guard.rotate_key("k1")
    errors = []
    barrier = threading.Barrier(8)

    def worker(index):
        try:
            barrier.wait(timeout=10)
            for round_no in range(20):
                addr = "10.9.%d.%d" % (index, round_no + 1)
                issued = guard.issue(addr, now_ms=round_no * 10)
                checked = guard.verify(addr, issued["cookie"],
                                       now_ms=round_no * 10 + 5)
                assert checked["ok"] is True
        except Exception as exc:  # noqa: BLE001 - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    stats = guard.stats()
    assert stats["issued"] == 160
    assert stats["verified"] == 160
    assert stats["accepted"] == 160
