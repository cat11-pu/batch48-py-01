import copy
import threading

import pytest

import key_phase_guard as kp


def started(**kwargs):
    guard = kp.KeyPhaseGuard(**kwargs)
    guard.start(now=0)
    return guard


def test_start_initializes_key_phase():
    guard = kp.KeyPhaseGuard()
    result = guard.start(now=0)
    assert result == {"recv_epoch": 1, "send_epoch": 1, "retained": [1]}
    phase = guard.key_phase()
    assert phase["recv_epoch"] == 1
    assert phase["send_epoch"] == 1
    assert phase["retained"] == [{"epoch": 1, "expires_at": None}]
    assert phase["pending_send_epoch"] is None


def test_start_twice_is_refused():
    guard = started()
    with pytest.raises(kp.KeyPhaseError) as exc:
        guard.start(now=10)
    assert exc.value.code == "KP_ALREADY_STARTED"
    assert guard.stats()["rejected"] == 1
    assert guard.key_phase()["recv_epoch"] == 1


def test_operations_before_start_are_refused():
    guard = kp.KeyPhaseGuard()
    calls = [
        lambda: guard.encrypt(0),
        lambda: guard.decrypt(1, 0),
        lambda: guard.request_update(now=0),
        lambda: guard.on_key_update(now=0),
        lambda: guard.key_phase(),
    ]
    for call in calls:
        with pytest.raises(kp.KeyPhaseError) as exc:
            call()
        assert exc.value.code == "KP_NOT_STARTED"
    assert guard.stats()["rejected"] == len(calls)


def test_encrypt_validates_sequence():
    guard = started()
    bad_sequences = [-1, 8, 9, "1", 1.0, None]
    for seq in bad_sequences:
        with pytest.raises(kp.KeyPhaseError) as exc:
            guard.encrypt(seq)
        assert exc.value.code == "KP_SEQ_OUT_OF_RANGE"
    assert guard.stats()["rejected"] == len(bad_sequences)
    assert guard.encrypt(0) == {"epoch": 1, "seq": 0}
    assert guard.stats()["records_encrypted"] == 1


def test_encrypt_rejects_reused_sequence():
    guard = started()
    assert guard.encrypt(0)["seq"] == 0
    assert guard.encrypt(1)["seq"] == 1
    for seq in (0, 1):
        with pytest.raises(kp.KeyPhaseError) as exc:
            guard.encrypt(seq)
        assert exc.value.code == "KP_SEQ_REUSED"
    assert guard.stats()["records_encrypted"] == 2
    assert guard.stats()["rejected"] == 2


def test_request_update_advances_receive_epoch():
    guard = started()
    result = guard.request_update(now=1000)
    assert result["recv_epoch"] == 2
    assert result["send_epoch"] == 1
    assert result["retained"] == [1, 2]
    phase = guard.key_phase()
    assert phase["retained"] == [
        {"epoch": 1, "expires_at": 3000},
        {"epoch": 2, "expires_at": None},
    ]
    record = guard.decrypt(1, 0, now=1000)
    assert record == {"status": "ok", "epoch": 1, "seq": 0}
    assert guard.stats()["records_decrypted"] == 1


def test_update_minimum_interval_is_enforced():
    guard = started()
    guard.request_update(now=1000)
    with pytest.raises(kp.KeyPhaseError) as exc:
        guard.request_update(now=1200)
    assert exc.value.code == "KP_UPDATE_TOO_SOON"
    assert exc.value.detail["elapsed_ms"] == 200
    assert exc.value.detail["min_update_interval_ms"] == 500
    assert guard.request_update(now=1500)["recv_epoch"] == 3
    assert guard.stats()["updates_accepted"] == 2
    assert guard.stats()["updates_rejected"] == 1


def test_update_count_limit_is_enforced():
    guard = started(max_updates=2)
    guard.request_update(now=1000)
    guard.request_update(now=3000)
    with pytest.raises(kp.KeyPhaseError) as exc:
        guard.request_update(now=5000)
    assert exc.value.code == "KP_UPDATE_LIMIT"
    assert exc.value.detail["max_updates"] == 2
    assert guard.stats()["updates_accepted"] == 2
    assert guard.key_phase()["recv_epoch"] == 3


def test_on_key_update_switches_send_epoch():
    guard = started()
    guard.encrypt(0)
    guard.encrypt(1)
    result = guard.on_key_update(now=1000)
    assert result == {"deferred": False, "send_epoch": 2, "pending_send_epoch": None}
    assert guard.key_phase()["send_epoch"] == 2
    assert guard.encrypt(0) == {"epoch": 2, "seq": 0}
    assert guard.encrypt(1) == {"epoch": 2, "seq": 1}
    assert guard.stats()["records_encrypted"] == 4
    assert guard.stats()["updates_accepted"] == 1


def test_decrypt_future_epoch_is_refused():
    guard = started()
    for epoch in (2, 9, 0, -1):
        with pytest.raises(kp.KeyPhaseError) as exc:
            guard.decrypt(epoch, 0)
        assert exc.value.code == "KP_EPOCH_UNKNOWN"
    assert guard.stats()["rejected"] == 4
    assert guard.decrypt(1, 0)["status"] == "ok"


def test_decrypt_replay_in_current_epoch():
    guard = started()
    assert guard.decrypt(1, 5)["status"] == "ok"
    with pytest.raises(kp.KeyPhaseError) as exc:
        guard.decrypt(1, 5)
    assert exc.value.code == "KP_SEQ_REUSED"
    assert exc.value.detail["seq"] == 5
    assert guard.decrypt(1, 6)["status"] == "ok"
    assert guard.stats()["records_decrypted"] == 2


def test_expire_drops_retained_epoch():
    guard = started()
    guard.request_update(now=1000)
    assert guard.expire(now=2999) == {"expired": []}
    assert guard.decrypt(1, 0, now=2999)["status"] == "ok"
    assert guard.expire(now=3000) == {"expired": [1]}
    phase = guard.key_phase()
    assert phase["retained"] == [{"epoch": 2, "expires_at": None}]
    late = guard.decrypt(1, 1, now=3001)
    assert late == {"status": "discarded", "reason": "KP_KEY_EXPIRED",
                    "epoch": 1, "seq": 1}
    assert guard.stats()["discarded"] == 1


def test_snapshot_restore_and_invalid_state():
    guard = started()
    guard.request_update(now=1000)
    guard.decrypt(2, 0)
    snapshot = guard.snapshot()
    guard.decrypt(2, 1)
    with pytest.raises(kp.KeyPhaseError):
        guard.decrypt(2, 1)
    guard.restore(copy.deepcopy(snapshot))
    assert guard.decrypt(2, 1)["status"] == "ok"
    assert guard.key_phase()["recv_epoch"] == 2

    snapshot["seen"][2].append(7)
    assert guard.decrypt(2, 2)["status"] == "ok"

    broken = copy.deepcopy(snapshot)
    broken["seen"] = "not-a-dict"
    with pytest.raises(kp.KeyPhaseError) as exc:
        guard.restore(broken)
    assert exc.value.code == "KP_SNAPSHOT_INVALID"
    assert guard.key_phase()["recv_epoch"] == 2
    assert guard.decrypt(2, 3)["status"] == "ok"


def test_stats_counters_and_reset():
    guard = started()
    guard.encrypt(0)
    guard.encrypt(1)
    guard.encrypt(2)
    guard.decrypt(1, 0)
    guard.decrypt(1, 1)
    guard.request_update(now=1000)
    assert guard.stats() == {
        "updates_accepted": 1,
        "updates_rejected": 0,
        "records_encrypted": 3,
        "records_decrypted": 2,
        "discarded": 0,
        "rejected": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "updates_accepted": 0,
        "updates_rejected": 0,
        "records_encrypted": 0,
        "records_decrypted": 0,
        "discarded": 0,
        "rejected": 0,
    }
    assert guard.key_phase()["recv_epoch"] == 2


def test_concurrent_decrypt_counts_are_exact():
    guard = started(seq_limit=100)
    barrier = threading.Barrier(4)
    errors = []

    def worker(offset):
        barrier.wait()
        try:
            for seq in range(offset * 25, offset * 25 + 25):
                assert guard.decrypt(1, seq)["status"] == "ok"
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert guard.stats()["records_decrypted"] == 100
    with pytest.raises(kp.KeyPhaseError) as exc:
        guard.decrypt(1, 0)
    assert exc.value.code == "KP_SEQ_REUSED"
