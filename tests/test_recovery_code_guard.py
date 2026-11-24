import copy
import string
import threading

import pytest

import recovery_code_guard as rc


def make(**kwargs):
    return rc.RecoveryCodeGuard(**kwargs)


def test_generate_returns_unique_codes_and_status():
    guard = make()
    batch = guard.generate("alice")
    assert batch["account"] == "alice"
    assert batch["epoch"] == 1
    assert batch["expires_at"] is None
    codes = batch["codes"]
    assert len(codes) == 10
    assert len(set(codes)) == 10
    allowed = set(string.ascii_uppercase + string.digits)
    for code in codes:
        assert len(code) == 10
        assert set(code) <= allowed
    state = guard.status("alice")
    assert state == {
        "account": "alice", "epoch": 1, "remaining": 10,
        "used": 0, "failures": 0, "locked": False,
    }


def test_redeem_normalises_input_and_consumes_code():
    guard = make()
    batch = guard.generate("alice")
    code = batch["codes"][0]
    messy = " %s-%s " % (code[:3].lower(), code[3:].lower())
    result = guard.redeem("alice", messy)
    assert result["account"] == "alice"
    assert result["status"] == "ok"
    assert result["epoch"] == 1
    assert result["remaining"] == 9
    state = guard.status("alice")
    assert state["used"] == 1
    assert state["remaining"] == 9


def test_used_code_returns_used_error():
    guard = make()
    batch = guard.generate("alice")
    code = batch["codes"][0]
    guard.redeem("alice", code)
    with pytest.raises(rc.RecoveryCodeError) as exc:
        guard.redeem("alice", code)
    assert exc.value.code == "RC_CODE_USED"
    assert guard.remaining("alice") == 9


def test_generate_twice_is_rejected():
    guard = make()
    batch = guard.generate("alice")
    with pytest.raises(rc.RecoveryCodeError) as exc:
        guard.generate("alice")
    assert exc.value.code == "RC_BATCH_EXISTS"
    assert guard.redeem("alice", batch["codes"][0])["status"] == "ok"


def test_regenerate_advances_epoch_and_reissues():
    guard = make()
    guard.generate("alice")
    first_batch = guard.regenerate("alice")
    assert first_batch["epoch"] == 2
    assert len(first_batch["codes"]) == 10
    state = guard.status("alice")
    assert state["epoch"] == 2
    assert state["remaining"] == 10
    assert state["failures"] == 0
    assert guard.redeem("alice", first_batch["codes"][0])["remaining"] == 9


def test_time_expired_batch_rejected():
    guard = make(code_ttl=100)
    batch = guard.generate("alice", now=0)
    assert batch["expires_at"] == 100
    with pytest.raises(rc.RecoveryCodeError) as exc:
        guard.redeem("alice", batch["codes"][0], now=100)
    assert exc.value.code == "RC_CODE_EXPIRED"
    assert guard.status("alice", now=100)["remaining"] == 10
    assert guard.redeem("alice", batch["codes"][0], now=99)["status"] == "ok"


def test_failure_counter_leads_to_lock():
    guard = make(max_failures=3)
    batch = guard.generate("alice")
    first = guard.redeem("alice", "AAAAAAAAAA", now=0)
    second = guard.redeem("alice", "BBBBBBBBBB", now=1)
    assert first["status"] == "invalid"
    assert first["attempts"] == 1
    assert second["attempts"] == 2
    with pytest.raises(rc.RecoveryCodeError) as exc:
        guard.redeem("alice", "CCCCCCCCCC", now=2)
    assert exc.value.code == "RC_LOCKED"
    with pytest.raises(rc.RecoveryCodeError) as locked:
        guard.redeem("alice", batch["codes"][0], now=2)
    assert locked.value.code == "RC_LOCKED"
    assert guard.status("alice", now=2)["locked"] is True


def test_lock_window_expires():
    guard = make(max_failures=2, lock_window=300)
    batch = guard.generate("alice")
    guard.redeem("alice", "AAAAAAAAAA", now=0)
    with pytest.raises(rc.RecoveryCodeError):
        guard.redeem("alice", "BBBBBBBBBB", now=0)
    with pytest.raises(rc.RecoveryCodeError) as still:
        guard.redeem("alice", batch["codes"][0], now=299)
    assert still.value.code == "RC_LOCKED"
    result = guard.redeem("alice", batch["codes"][0], now=300)
    assert result["status"] == "ok"
    assert guard.status("alice", now=300)["failures"] == 0


def test_unlock_clears_counter_and_lock():
    guard = make(max_failures=3)
    guard.generate("alice")
    guard.redeem("alice", "AAAAAAAAAA")
    guard.redeem("alice", "BBBBBBBBBB")
    assert guard.status("alice")["failures"] == 2
    guard.unlock("alice")
    assert guard.status("alice")["failures"] == 0
    assert guard.status("alice")["locked"] is False
    assert guard.redeem("alice", "CCCCCCCCCC")["status"] == "invalid"
    assert guard.redeem("alice", "DDDDDDDDDD")["status"] == "invalid"


def test_success_resets_failure_counter():
    guard = make(max_failures=3)
    batch = guard.generate("alice")
    guard.redeem("alice", "AAAAAAAAAA")
    guard.redeem("alice", "BBBBBBBBBB")
    assert guard.redeem("alice", batch["codes"][0])["status"] == "ok"
    guard.redeem("alice", "CCCCCCCCCC")
    guard.redeem("alice", "DDDDDDDDDD")
    assert guard.redeem("alice", batch["codes"][1])["status"] == "ok"
    assert guard.status("alice")["failures"] == 0


def test_unknown_account_mirrors_failure_shape():
    guard = make()
    guard.generate("alice")
    ghost = guard.redeem("ghost", "ABCDEFGHIJ")
    known_bad = guard.redeem("alice", "ABCDEFGHIJ")
    assert set(ghost) == set(known_bad)
    assert ghost["status"] == known_bad["status"] == "invalid"
    assert ghost["attempts"] == 0
    assert ghost["locked"] is False
    with pytest.raises(rc.RecoveryCodeError) as status:
        guard.status("ghost")
    assert status.value.code == "RC_ACCOUNT_UNKNOWN"
    with pytest.raises(rc.RecoveryCodeError) as remaining:
        guard.remaining("ghost")
    assert remaining.value.code == "RC_ACCOUNT_UNKNOWN"


def test_invalid_code_and_batch_config_rejected():
    guard = make()
    guard.generate("alice")
    for bad in ("", "   ", "X" * 129):
        with pytest.raises(rc.RecoveryCodeError) as exc:
            guard.redeem("alice", bad)
        assert exc.value.code == "RC_CODE_INVALID"
    for config in ({"code_count": 0}, {"code_count": 33}, {"code_length": 4}):
        broken = make(**config)
        with pytest.raises(rc.RecoveryCodeError) as exc:
            broken.generate("bob")
        assert exc.value.code == "RC_COUNT_INVALID"


def test_snapshot_restore_preserves_epoch_and_usage():
    guard = make()
    batch = guard.generate("alice")
    guard.redeem("alice", batch["codes"][0])
    state = guard.snapshot()
    assert state["version"] == 1
    assert set(state) >= {"accounts"}

    clone = make()
    clone.restore(copy.deepcopy(state))
    restored = clone.status("alice")
    assert restored["epoch"] == 1
    assert restored["remaining"] == 9
    assert clone.redeem("alice", batch["codes"][1])["remaining"] == 8
    state["accounts"].clear()
    assert guard.status("alice")["remaining"] == 9


def test_restore_rejects_malformed_state():
    guard = make()
    batch = guard.generate("alice")
    bad_states = [
        None,
        {},
        {"version": 2, "accounts": {}},
        {"version": 1, "accounts": []},
        {"version": 1, "accounts": {"alice": []}},
    ]
    for bad in bad_states:
        with pytest.raises(rc.RecoveryCodeError) as exc:
            guard.restore(bad)
        assert exc.value.code == "RC_SNAPSHOT_INVALID"
    assert guard.redeem("alice", batch["codes"][0])["status"] == "ok"


def test_concurrent_redeem_distinct_codes_all_succeed():
    guard = make()
    batch = guard.generate("alice")
    errors = []
    results = []

    def worker(index):
        try:
            results.append(guard.redeem("alice", batch["codes"][index]))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [item["status"] for item in results] == ["ok"] * 8
    assert guard.remaining("alice") == 2
    assert guard.status("alice")["used"] == 8
