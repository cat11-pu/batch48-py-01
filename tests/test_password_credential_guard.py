import threading

import pytest

import password_credential_guard as pg


def make(**kwargs):
    guard = pg.PasswordCredentialGuard(**kwargs)
    guard.rotate_pepper("p1", b"pepper-one-secret-0001")
    return guard


def test_register_and_verify_roundtrip():
    guard = make()
    record = guard.register("alice", "oldpass123", now=10.0)
    assert record["account"] == "alice"
    assert record["algorithm"] == "sha256"
    assert record["iterations"] == 2000
    assert len(record["salt"]) >= 16
    assert record["pepper_id"] == "p1"
    assert record["created_at"] == 10.0

    ok = guard.verify("alice", "oldpass123", now=20.0)
    assert ok == {
        "account": "alice", "status": "ok", "attempts": 0,
        "locked": False, "upgraded": False,
    }
    bad = guard.verify("alice", "wrongpass1", now=21.0)
    assert bad["status"] == "invalid"
    assert bad["attempts"] == 1
    assert bad["locked"] is False


def test_password_policy_rejects_weak_input():
    guard = make()
    for weak in ("short1", "nodigitshere", " leading1space", "a1" * 70):
        with pytest.raises(pg.PasswordGuardError) as exc:
            guard.register("alice", weak)
        assert exc.value.code == "PWD_WEAK"
    assert guard.stats()["accounts"] == 0


def test_duplicate_registration_is_rejected():
    guard = make()
    guard.register("alice", "oldpass123")
    with pytest.raises(pg.PasswordGuardError) as exc:
        guard.register("alice", "another123")
    assert exc.value.code == "PWD_ACCOUNT_EXISTS"
    assert guard.verify("alice", "oldpass123")["status"] == "ok"
    assert guard.stats()["accounts"] == 1


def test_unknown_account_mirrors_failure_shape():
    guard = make()
    guard.register("alice", "oldpass123")
    ghost = guard.verify("ghost", "whatever123")
    known_bad = guard.verify("alice", "wrongpass1")
    assert set(ghost) == set(known_bad)
    assert ghost["status"] == known_bad["status"] == "invalid"
    assert ghost["locked"] is False
    assert ghost["attempts"] == 0
    assert guard.stats()["accounts"] == 1


def test_failed_attempts_lock_account_for_window():
    guard = make(max_failures=3, lock_window=300)
    guard.register("alice", "oldpass123")
    first = guard.verify("alice", "badpass001", now=0.0)
    second = guard.verify("alice", "badpass002", now=1.0)
    third = guard.verify("alice", "badpass003", now=2.0)
    assert [first["status"], second["status"], third["status"]] == [
        "invalid", "invalid", "locked",
    ]
    assert third["attempts"] == 3
    state = guard.lock_state("alice", now=2.0)
    assert state["locked"] is True
    assert state["until"] == 302.0

    assert guard.verify("alice", "oldpass123", now=301.0)["status"] == "locked"
    after = guard.verify("alice", "oldpass123", now=302.0)
    assert after["status"] == "ok"
    assert after["attempts"] == 0


def test_success_resets_failure_counter():
    guard = make(max_failures=3)
    guard.register("alice", "oldpass123")
    guard.verify("alice", "badpass001")
    guard.verify("alice", "badpass002")
    assert guard.verify("alice", "oldpass123")["status"] == "ok"
    guard.verify("alice", "badpass003")
    guard.verify("alice", "badpass004")
    assert guard.verify("alice", "oldpass123")["status"] == "ok"
    assert guard.lock_state("alice")["attempts"] == 0


def test_policy_raise_upgrades_record_in_place():
    guard = make()
    guard.register("alice", "oldpass123")
    with pytest.raises(pg.PasswordGuardError) as low:
        guard.set_policy(iterations=10)
    assert low.value.code == "PWD_POLICY_INVALID"
    with pytest.raises(pg.PasswordGuardError) as algo:
        guard.set_policy(algorithm="md5")
    assert algo.value.code == "PWD_POLICY_INVALID"
    assert guard.policy()["iterations"] == 2000

    assert guard.set_policy(iterations=4000)["iterations"] == 4000
    upgraded = guard.verify("alice", "oldpass123")
    assert upgraded["status"] == "ok"
    assert upgraded["upgraded"] is True
    assert guard.params("alice")["iterations"] == 4000
    again = guard.verify("alice", "oldpass123")
    assert again["upgraded"] is False
    assert guard.verify("alice", "wrongpass1")["status"] == "invalid"


def test_pepper_rotation_keeps_old_pepper_usable():
    guard = make()
    guard.register("alice", "oldpass123")
    rotated = guard.rotate_pepper("p2", b"pepper-two-secret-0002")
    assert rotated["pepper_id"] == "p2"
    assert guard.active_pepper() == "p2"
    assert guard.verify("alice", "oldpass123")["status"] == "ok"
    assert guard.register("bob", "bobpass123")["pepper_id"] == "p2"


def test_revoked_pepper_blocks_bound_records():
    guard = make()
    guard.register("alice", "oldpass123")
    assert guard.revoke_pepper("p1") is True
    with pytest.raises(pg.PasswordGuardError) as exc:
        guard.verify("alice", "oldpass123")
    assert exc.value.code == "PWD_PEPPER_UNKNOWN"
    assert guard.revoke_pepper("p1") is False


def test_change_password_requires_current_password():
    guard = make()
    guard.register("alice", "oldpass123")
    with pytest.raises(pg.PasswordGuardError) as mismatch:
        guard.change_password("alice", "wrongold1", "newpass123")
    assert mismatch.value.code == "PWD_OLD_MISMATCH"
    changed = guard.change_password("alice", "oldpass123", "newpass123", now=5.0)
    assert changed["account"] == "alice"
    assert changed["changed_at"] == 5.0
    assert guard.verify("alice", "oldpass123")["status"] == "invalid"
    assert guard.verify("alice", "newpass123")["status"] == "ok"
    with pytest.raises(pg.PasswordGuardError) as weak:
        guard.change_password("alice", "newpass123", "short")
    assert weak.value.code == "PWD_WEAK"
    assert guard.verify("alice", "newpass123")["status"] == "ok"


def test_lock_state_and_unlock():
    guard = make(max_failures=3)
    with pytest.raises(pg.PasswordGuardError) as unknown:
        guard.lock_state("ghost")
    assert unknown.value.code == "PWD_ACCOUNT_UNKNOWN"
    guard.register("alice", "oldpass123")
    guard.verify("alice", "badpass001")
    guard.verify("alice", "badpass002")
    state = guard.lock_state("alice")
    assert state == {
        "account": "alice", "attempts": 2, "locked": False, "until": None,
    }
    guard.unlock("alice")
    assert guard.lock_state("alice")["attempts"] == 0
    assert guard.verify("alice", "oldpass123")["status"] == "ok"


def test_history_and_stats_caps():
    guard = make()
    guard.register("alice", "oldpass123")
    guard.verify("alice", "oldpass123", now=1.0)
    guard.verify("alice", "wrongpass1", now=2.0)
    guard.verify("alice", "oldpass123", now=3.0)
    entries = guard.history("alice")
    assert [entry["status"] for entry in entries] == ["ok", "invalid", "ok"]
    assert all(set(entry) == {"at", "status", "upgraded"} for entry in entries)
    assert guard.stats() == {"accounts": 1, "verifications": 2, "failures": 1}


def test_snapshot_restore_preserves_records_and_lock():
    guard = make(max_failures=3)
    guard.register("alice", "oldpass123")
    guard.register("bob", "bobpass123")
    for attempt in range(3):
        guard.verify("alice", "badpass00%d" % attempt, now=0.0)
    state = guard.snapshot()
    assert state["version"] == 1
    assert set(state) >= {"policy", "peppers", "accounts", "stats"}

    clone = pg.PasswordCredentialGuard(max_failures=3)
    clone.restore(state)
    assert clone.verify("alice", "oldpass123")["status"] == "locked"
    assert clone.verify("bob", "bobpass123")["status"] == "ok"
    assert clone.stats()["accounts"] == 2
    state["accounts"].clear()
    assert guard.verify("bob", "bobpass123")["status"] == "ok"


def test_restore_rejects_malformed_state():
    guard = make()
    guard.register("alice", "oldpass123")
    bad_states = [
        None,
        {},
        {"version": 2, "policy": {}, "peppers": {}, "accounts": {}, "stats": {}},
        {"version": 1, "policy": [], "peppers": {}, "accounts": {}, "stats": {}},
        {"version": 1, "policy": {}, "peppers": {}, "accounts": [], "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(pg.PasswordGuardError) as exc:
            guard.restore(bad)
        assert exc.value.code == "PWD_SNAPSHOT_INVALID"
    assert guard.verify("alice", "oldpass123")["status"] == "ok"


def test_concurrent_register_and_verify_are_consistent():
    guard = make()
    errors = []

    def worker(index):
        try:
            account = "user-%d" % index
            password = "passwd%04d" % index
            guard.register(account, password)
            assert guard.verify(account, password)["status"] == "ok"
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats() == {"accounts": 8, "verifications": 8, "failures": 0}
