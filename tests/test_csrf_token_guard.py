import copy
import threading

import pytest

import csrf_token_guard as csrf

T0 = 1_700_000_000
ORIGIN = "https://app.example.com"


def make(**kwargs):
    return csrf.CsrfTokenGuard(**kwargs)


def opened(**kwargs):
    guard = make(**kwargs)
    session = guard.create_session("s1", now=T0)
    return guard, session


def issue(guard, action, **overrides):
    base = {"origin": ORIGIN, "one_time": True, "now": T0}
    base.update(overrides)
    return guard.issue_token("s1", action, **base)


def verify(guard, token, cookie, action, **overrides):
    base = {"action": action, "origin": ORIGIN, "cookie_token": cookie,
            "now": T0 + 1}
    base.update(overrides)
    return guard.verify_token("s1", token, **base)


def test_create_session_and_issue_token_bind_action_origin_and_ttl():
    guard = make()
    session = guard.create_session("s1", now=T0)

    assert session["session_id"] == "s1"
    assert session["epoch"] == 1
    assert isinstance(session["session_token"], str)
    assert len(session["session_token"]) >= 16

    with pytest.raises(csrf.CsrfError) as exc:
        guard.create_session("s1", now=T0)
    assert exc.value.code == "CSRF_SESSION_EXISTS"

    token = guard.issue_token("s1", "transfer", origin=ORIGIN, one_time=True,
                              now=T0)
    assert token["session_id"] == "s1"
    assert token["action"] == "transfer"
    assert token["origin"] == ORIGIN
    assert token["one_time"] is True
    assert isinstance(token["token"], str) and len(token["token"]) >= 16
    assert guard.token_count("s1") == 1
    assert guard.stats()["issued"] == 1


def test_one_time_token_is_consumed_by_a_successful_verification():
    guard, session = opened()
    token = issue(guard, "transfer")

    out = verify(guard, token["token"], session["session_token"], "transfer")
    assert out["status"] == "ok"
    assert guard.stats()["verified"] == 1
    assert guard.stats()["consumed"] == 1
    assert guard.token_count("s1") == 1


def test_reusable_token_can_be_verified_repeatedly():
    guard, session = opened()
    token = issue(guard, "read", one_time=False)

    for offset in (1, 2):
        out = verify(guard, token["token"], session["session_token"], "read",
                     now=T0 + offset)
        assert out["status"] == "ok"

    assert guard.stats()["verified"] == 2
    assert guard.stats()["consumed"] == 0


def test_unknown_token_is_rejected():
    guard, session = opened()

    with pytest.raises(csrf.CsrfError) as exc:
        verify(guard, "not-a-real-token", session["session_token"], "read")
    assert exc.value.code == "CSRF_TOKEN_UNKNOWN"
    assert guard.stats()["rejected"] == 1


def test_origin_mismatch_has_its_own_reason_code():
    guard, session = opened()
    token = issue(guard, "transfer")

    with pytest.raises(csrf.CsrfError) as exc:
        verify(guard, token["token"], session["session_token"], "transfer",
               origin="https://evil.example")
    assert exc.value.code == "CSRF_ORIGIN_MISMATCH"

    out = verify(guard, token["token"], session["session_token"], "transfer")
    assert out["status"] == "ok"
    assert guard.stats()["rejected"] == 1
    assert guard.stats()["verified"] == 1


def test_double_submit_cookie_must_match_the_session_token():
    guard, session = opened()
    token = issue(guard, "transfer")

    with pytest.raises(csrf.CsrfError) as exc:
        verify(guard, token["token"], "wrong-cookie", "transfer")
    assert exc.value.code == "CSRF_DOUBLE_SUBMIT_MISMATCH"

    out = verify(guard, token["token"], session["session_token"], "transfer")
    assert out["status"] == "ok"


def test_expired_token_is_refused_and_purged():
    guard, session = opened(token_ttl=10)
    token = issue(guard, "read", one_time=False)

    assert verify(guard, token["token"], session["session_token"], "read",
                  now=T0 + 9)["status"] == "ok"
    with pytest.raises(csrf.CsrfError) as exc:
        verify(guard, token["token"], session["session_token"], "read",
               now=T0 + 10)
    assert exc.value.code == "CSRF_TOKEN_EXPIRED"

    assert guard.purge_expired(T0 + 10) == 1
    assert guard.token_count("s1") == 0
    assert guard.stats()["rejected"] == 1


def test_consumed_one_time_token_cannot_be_replayed():
    guard, session = opened()
    token = issue(guard, "transfer")

    assert verify(guard, token["token"], session["session_token"],
                  "transfer")["status"] == "ok"
    with pytest.raises(csrf.CsrfError) as exc:
        verify(guard, token["token"], session["session_token"], "transfer")
    assert exc.value.code == "CSRF_TOKEN_CONSUMED"
    assert guard.stats()["verified"] == 1
    assert guard.stats()["consumed"] == 1
    assert guard.stats()["rejected"] == 1


def test_session_token_table_enforces_its_capacity():
    guard, session = opened(max_tokens_per_session=3, token_ttl=10)
    for index in range(3):
        issue(guard, f"action-{index}", one_time=False)

    with pytest.raises(csrf.CsrfError) as exc:
        issue(guard, "action-overflow", one_time=False)
    assert exc.value.code == "CSRF_TOKEN_CAPACITY"
    assert guard.token_count("s1") == 3

    assert guard.purge_expired(T0 + 10) == 3
    issue(guard, "fresh", one_time=False, now=T0 + 10)
    assert guard.token_count("s1") == 1


def test_revocation_clears_tokens_and_is_idempotent():
    guard, session = opened()
    first = issue(guard, "one", one_time=False)
    issue(guard, "two", one_time=False)

    assert guard.revoke_for_session("s1") == 2
    assert guard.token_count("s1") == 0

    with pytest.raises(csrf.CsrfError) as exc:
        verify(guard, first["token"], session["session_token"], "one")
    assert exc.value.code == "CSRF_SESSION_REVOKED"
    with pytest.raises(csrf.CsrfError) as exc:
        issue(guard, "three")
    assert exc.value.code == "CSRF_SESSION_REVOKED"

    assert guard.revoke_for_session("s1") == 0
    assert guard.stats()["revoked"] == 2


def test_rotation_advances_epoch_and_issues_a_new_session_token():
    guard, session = opened()
    old_cookie = session["session_token"]

    out = guard.rotate_session_token("s1", now=T0)
    assert out["session_id"] == "s1"
    assert out["epoch"] == 2
    assert out["session_token"] != old_cookie

    token = issue(guard, "transfer")
    verified = verify(guard, token["token"], out["session_token"], "transfer")
    assert verified["status"] == "ok"
    assert guard.stats()["rotated"] == 1


def test_sessions_do_not_share_tokens():
    guard = make()
    first = guard.create_session("s1", now=T0)
    second = guard.create_session("s2", now=T0)
    token = guard.issue_token("s1", "transfer", origin=ORIGIN, one_time=True,
                              now=T0)

    with pytest.raises(csrf.CsrfError) as exc:
        guard.verify_token("s2", token["token"], action="transfer",
                           origin=ORIGIN, cookie_token=second["session_token"],
                           now=T0 + 1)
    assert exc.value.code == "CSRF_TOKEN_UNKNOWN"
    assert guard.token_count("s1") == 1
    assert guard.token_count("s2") == 0
    assert first["session_token"] != second["session_token"]


def test_snapshot_restore_keeps_consumed_state_and_epochs():
    guard, session = opened()
    reusable = issue(guard, "read", one_time=False)
    onetime = issue(guard, "write", one_time=True)
    assert verify(guard, onetime["token"], session["session_token"],
                  "write")["status"] == "ok"

    state = guard.snapshot()
    assert state["stats"]["issued"] == 2
    assert state["stats"]["consumed"] == 1
    frozen = copy.deepcopy(state)
    state["sessions"].clear()
    state["stats"]["issued"] = 99
    assert guard.stats()["issued"] == 2

    restored = make()
    restored.restore(frozen)
    assert restored.stats()["verified"] == 1

    out = restored.verify_token(
        "s1", reusable["token"], action="read", origin=ORIGIN,
        cookie_token=session["session_token"], now=T0 + 2,
    )
    assert out["status"] == "ok"
    with pytest.raises(csrf.CsrfError) as exc:
        restored.verify_token(
            "s1", onetime["token"], action="write", origin=ORIGIN,
            cookie_token=session["session_token"], now=T0 + 2,
        )
    assert exc.value.code == "CSRF_TOKEN_CONSUMED"
    assert restored.stats()["issued"] == 2
    assert restored.stats()["verified"] == 2


def test_corrupt_snapshot_is_refused_and_live_state_survives():
    guard, session = opened()
    token = issue(guard, "read", one_time=False)

    broken = [
        None,
        {},
        {"version": 2, "sessions": {}, "stats": {}},
        {"version": 1, "sessions": "nope", "stats": {}},
        {"version": 1, "sessions": {}, "stats": "nope"},
        {"version": 1, "sessions": {}},
    ]
    for state in broken:
        with pytest.raises(csrf.CsrfError) as exc:
            guard.restore(state)
        assert exc.value.code == "CSRF_SNAPSHOT_INVALID"

    assert guard.token_count("s1") == 1
    assert verify(guard, token["token"], session["session_token"],
                  "read")["status"] == "ok"


def test_concurrent_verification_of_one_time_token_has_one_winner():
    guard, session = opened()
    token = issue(guard, "transfer")
    barrier = threading.Barrier(10)
    winners = []
    rejected = []
    guard_lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            out = verify(guard, token["token"], session["session_token"],
                         "transfer")
        except csrf.CsrfError as exc:
            with guard_lock:
                rejected.append(exc.code)
        else:
            with guard_lock:
                winners.append(out["status"])

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert winners == ["ok"]
    assert rejected == ["CSRF_TOKEN_CONSUMED"] * 9
    assert guard.stats()["verified"] == 1
    assert guard.stats()["consumed"] == 1
    assert guard.stats()["rejected"] == 9
