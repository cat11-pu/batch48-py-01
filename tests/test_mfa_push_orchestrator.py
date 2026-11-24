import copy
import threading

import pytest

import mfa_push_orchestrator as mfa

T0 = 1_700_000_000


def make(**kwargs):
    return mfa.MfaPushOrchestrator(**kwargs)


def other_code(record):
    return "01" if record["code"] == "00" else "00"


def test_create_challenge_returns_pending_record_with_matching_code():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)

    assert rec["user_id"] == "alice"
    assert rec["state"] == "pending"
    assert rec["reused"] is False
    assert rec["attempts"] == 0
    assert rec["expires_at"] == T0 + 120
    assert isinstance(rec["code"], str)
    assert len(rec["code"]) == 2
    assert rec["code"].isdigit()
    assert isinstance(rec["challenge_id"], str) and len(rec["challenge_id"]) >= 8
    assert orch.pending_count("alice") == 1
    assert orch.pending_count("bob") == 0
    assert orch.stats()["pushed"] == 1


def test_repeat_create_reuses_the_active_challenge():
    orch = make()
    first = orch.create_challenge("alice", now=T0)
    second = orch.create_challenge("alice", now=T0 + 10)

    assert second["challenge_id"] == first["challenge_id"]
    assert second["code"] == first["code"]
    assert second["expires_at"] == T0 + 120
    assert second["reused"] is True
    assert orch.pending_count("alice") == 1
    assert orch.stats()["pushed"] == 1


def test_matching_code_approves_and_clears_pending():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)
    out = orch.submit_response(rec["challenge_id"], rec["code"], "approve",
                               now=T0 + 5)

    assert out["state"] == "approved"
    assert out["attempts"] == 0
    assert orch.pending_count("alice") == 0
    assert orch.poll_status(rec["challenge_id"], now=T0 + 6)["state"] == "approved"
    assert orch.stats()["approved"] == 1
    assert orch.stats()["failed"] == 0


def test_wrong_code_counts_a_failure_and_keeps_challenge_pending():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)

    with pytest.raises(mfa.MfaPushError) as exc:
        orch.submit_response(rec["challenge_id"], other_code(rec), "approve",
                             now=T0 + 1)
    assert exc.value.code == "MFA_CODE_MISMATCH"

    status = orch.poll_status(rec["challenge_id"], now=T0 + 2)
    assert status["state"] == "pending"
    assert status["attempts"] == 1
    assert orch.pending_count("alice") == 1
    assert orch.stats()["failed"] == 1
    assert orch.stats()["approved"] == 0


def test_malformed_code_is_refused_without_counting_a_failure():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)

    for bad in ("", "7", "123", "ab", 12):
        with pytest.raises(mfa.MfaPushError) as exc:
            orch.submit_response(rec["challenge_id"], bad, "approve", now=T0)
        assert exc.value.code == "MFA_CODE_INVALID"

    status = orch.poll_status(rec["challenge_id"], now=T0 + 1)
    assert status["attempts"] == 0
    assert status["state"] == "pending"
    assert orch.stats()["failed"] == 0


def test_failure_limit_locks_the_user_and_blocks_new_pushes():
    orch = make(max_failures=3, lock_seconds=300)
    rec = orch.create_challenge("alice", now=T0)
    bad = other_code(rec)

    for offset in range(3):
        with pytest.raises(mfa.MfaPushError) as exc:
            orch.submit_response(rec["challenge_id"], bad, "approve",
                                 now=T0 + offset)
        assert exc.value.code == "MFA_CODE_MISMATCH"

    with pytest.raises(mfa.MfaPushError) as exc:
        orch.create_challenge("alice", now=T0 + 3)
    assert exc.value.code == "MFA_USER_LOCKED"
    assert orch.stats()["locked"] == 1


def test_lock_expires_exactly_at_locked_until():
    orch = make(max_failures=2, lock_seconds=60, ttl=30)
    rec = orch.create_challenge("alice", now=T0)
    bad = other_code(rec)
    for offset in range(2):
        with pytest.raises(mfa.MfaPushError):
            orch.submit_response(rec["challenge_id"], bad, "approve",
                                 now=T0 + offset)

    unlocked_at = T0 + 1 + 60
    with pytest.raises(mfa.MfaPushError) as exc:
        orch.create_challenge("alice", now=unlocked_at - 1)
    assert exc.value.code == "MFA_USER_LOCKED"

    fresh = orch.create_challenge("alice", now=unlocked_at)

    assert fresh["reused"] is False
    assert fresh["challenge_id"] != rec["challenge_id"]
    assert fresh["state"] == "pending"


def test_successful_approval_resets_the_failure_counter():
    orch = make(max_failures=2)
    rec = orch.create_challenge("alice", now=T0)
    with pytest.raises(mfa.MfaPushError) as exc:
        orch.submit_response(rec["challenge_id"], other_code(rec), "approve",
                             now=T0)
    assert exc.value.code == "MFA_CODE_MISMATCH"

    assert orch.submit_response(rec["challenge_id"], rec["code"], "approve",
                                now=T0 + 1)["state"] == "approved"

    next_rec = orch.create_challenge("alice", now=T0 + 2)
    assert next_rec["reused"] is False
    with pytest.raises(mfa.MfaPushError) as exc:
        orch.submit_response(next_rec["challenge_id"], other_code(next_rec),
                             "approve", now=T0 + 3)
    assert exc.value.code == "MFA_CODE_MISMATCH"

    follow = orch.create_challenge("alice", now=T0 + 4)
    assert follow["reused"] is True
    assert follow["challenge_id"] == next_rec["challenge_id"]
    assert orch.stats()["failed"] == 2
    assert orch.stats()["locked"] == 0


def test_expire_stale_moves_only_due_pending_challenges():
    orch = make(ttl=120)
    rec = orch.create_challenge("alice", now=T0)

    assert orch.expire_stale(T0 + 119) == 0
    assert orch.poll_status(rec["challenge_id"], now=T0 + 119)["state"] == "pending"
    assert orch.expire_stale(T0 + 120) == 1
    assert orch.poll_status(rec["challenge_id"], now=T0 + 121)["state"] == "expired"
    assert orch.pending_count("alice") == 0
    assert orch.stats()["expired"] == 1

    fresh = orch.create_challenge("alice", now=T0 + 121)
    assert fresh["challenge_id"] != rec["challenge_id"]
    assert orch.stats()["pushed"] == 2


def test_cancel_is_terminal_for_pending_challenges():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)

    out = orch.cancel_challenge(rec["challenge_id"], now=T0 + 1)
    assert out["state"] == "cancelled"
    assert orch.pending_count("alice") == 0
    assert orch.stats()["cancelled"] == 1

    with pytest.raises(mfa.MfaPushError) as exc:
        orch.cancel_challenge(rec["challenge_id"], now=T0 + 2)
    assert exc.value.code == "MFA_INVALID_TRANSITION"
    with pytest.raises(mfa.MfaPushError) as exc:
        orch.submit_response(rec["challenge_id"], rec["code"], "approve",
                             now=T0 + 3)
    assert exc.value.code == "MFA_INVALID_TRANSITION"


def test_unknown_challenge_is_rejected_by_every_entry_point():
    orch = make()
    with pytest.raises(mfa.MfaPushError) as exc:
        orch.poll_status("missing", now=T0)
    assert exc.value.code == "MFA_UNKNOWN_CHALLENGE"

    with pytest.raises(mfa.MfaPushError) as exc:
        orch.submit_response("missing", "00", "approve", now=T0)
    assert exc.value.code == "MFA_UNKNOWN_CHALLENGE"

    with pytest.raises(mfa.MfaPushError) as exc:
        orch.cancel_challenge("missing", now=T0)
    assert exc.value.code == "MFA_UNKNOWN_CHALLENGE"


def test_users_have_isolated_challenges_codes_and_failure_counters():
    orch = make()
    alice = orch.create_challenge("alice", now=T0)
    bob = orch.create_challenge("bob", now=T0)

    assert alice["challenge_id"] != bob["challenge_id"]
    assert orch.pending_count("alice") == 1
    assert orch.pending_count("bob") == 1

    with pytest.raises(mfa.MfaPushError) as exc:
        orch.submit_response(alice["challenge_id"], other_code(alice), "approve",
                             now=T0 + 1)
    assert exc.value.code == "MFA_CODE_MISMATCH"
    assert orch.stats()["failed"] == 1

    assert orch.submit_response(alice["challenge_id"], alice["code"], "approve",
                                now=T0 + 2)["state"] == "approved"
    assert orch.pending_count("bob") == 1
    assert orch.submit_response(bob["challenge_id"], bob["code"], "approve",
                                now=T0 + 3)["state"] == "approved"
    assert orch.stats()["pushed"] == 2
    assert orch.stats()["approved"] == 2


def test_snapshot_restore_keeps_pending_challenge_lock_counters_and_stats():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)
    for offset in range(2):
        with pytest.raises(mfa.MfaPushError):
            orch.submit_response(rec["challenge_id"], other_code(rec), "approve",
                                 now=T0 + offset)

    state = orch.snapshot()
    assert state["stats"]["pushed"] == 1
    assert state["stats"]["failed"] == 2
    frozen = copy.deepcopy(state)
    state["challenges"].clear()
    state["stats"]["pushed"] = 99
    assert orch.stats()["pushed"] == 1

    restored = make()
    restored.restore(frozen)

    assert restored.pending_count("alice") == 1
    assert restored.poll_status(rec["challenge_id"], now=T0 + 2)["state"] == "pending"
    out = restored.submit_response(rec["challenge_id"], rec["code"], "approve",
                                   now=T0 + 3)
    assert out["state"] == "approved"
    assert restored.stats()["failed"] == 2
    assert restored.stats()["approved"] == 1


def test_corrupt_snapshot_is_refused_and_live_state_survives():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)

    broken = [
        None,
        {},
        {"version": 2, "challenges": {}, "users": {}, "stats": {}},
        {"version": 1, "challenges": "nope", "users": {}, "stats": {}},
        {"version": 1, "challenges": {}, "users": "nope", "stats": {}},
        {"version": 1, "challenges": {}, "users": {}},
    ]
    for state in broken:
        with pytest.raises(mfa.MfaPushError) as exc:
            orch.restore(state)
        assert exc.value.code == "MFA_SNAPSHOT_INVALID"

    assert orch.pending_count("alice") == 1
    assert orch.poll_status(rec["challenge_id"], now=T0 + 1)["state"] == "pending"
    assert orch.submit_response(rec["challenge_id"], rec["code"], "approve",
                                now=T0 + 2)["state"] == "approved"


def test_only_one_concurrent_approval_of_a_challenge_wins():
    orch = make()
    rec = orch.create_challenge("alice", now=T0)
    barrier = threading.Barrier(8)
    winners = []
    rejected = []
    guard = threading.Lock()

    def worker():
        barrier.wait()
        try:
            out = orch.submit_response(rec["challenge_id"], rec["code"],
                                       "approve", now=T0 + 1)
        except mfa.MfaPushError as exc:
            with guard:
                rejected.append(exc.code)
        else:
            with guard:
                winners.append(out["state"])

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert winners == ["approved"]
    assert rejected == ["MFA_INVALID_TRANSITION"] * 7
    assert orch.stats()["approved"] == 1
