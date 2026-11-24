import base64
import copy
import hashlib
import threading

import pytest

import authcode_exchange_guard as ac

NOW = "2026-11-24T09:00:00+00:00"
EXPIRED_AT = "2026-11-24T09:10:01+00:00"
REDIRECT = "https://app.example.com/callback"
VERIFIER = "verifier-abcdefghijklmnopqrstuvwxyz-0123456789"


def make_guard(**kwargs):
    return ac.AuthCodeExchangeGuard(**kwargs)


def challenge_for(verifier):
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def issue(guard, *, client_id="web", redirect=REDIRECT, verifier=VERIFIER,
          scopes=("profile", "openid"), now=NOW, method="S256"):
    return guard.issue_code(client_id=client_id, redirect_uri=redirect,
                            challenge=challenge_for(verifier), method=method,
                            scopes=list(scopes), now=now)


def redeem(guard, code, *, client_id="web", redirect=REDIRECT,
           verifier=VERIFIER, now=NOW):
    return guard.redeem(code=code, client_id=client_id, redirect_uri=redirect,
                        verifier=verifier, now=now)


def test_issue_and_redeem_s256_success():
    guard = make_guard()
    code = issue(guard)
    assert isinstance(code, str) and len(code) >= 16
    out = redeem(guard, code)
    assert out["client_id"] == "web"
    assert out["scopes"] == ["openid", "profile"]
    assert out["code"] == code
    assert isinstance(out["token_id"], str) and out["token_id"]
    assert guard.token_usable(out["token_id"]) is True
    assert guard.inspect_code(code)["consumed"] is True
    assert guard.stats() == {"issued": 1, "redeemed": 1, "replays": 0,
                             "revoked_tokens": 0, "purged": 0}


def test_redeem_wrong_client_is_refused():
    guard = make_guard()
    code = issue(guard)
    with pytest.raises(ac.AuthCodeError) as exc:
        redeem(guard, code, client_id="spa")
    assert exc.value.code == "AUTHCODE_CLIENT_MISMATCH"
    assert guard.inspect_code(code)["consumed"] is False
    assert redeem(guard, code)["client_id"] == "web"
    assert guard.stats()["redeemed"] == 1


def test_redeem_wrong_verifier_is_refused():
    guard = make_guard()
    code = issue(guard)
    with pytest.raises(ac.AuthCodeError) as exc:
        redeem(guard, code, verifier="another-verifier-value")
    assert exc.value.code == "AUTHCODE_PKCE_MISMATCH"
    assert guard.inspect_code(code)["consumed"] is False
    assert guard.token_usable(redeem(guard, code)["token_id"]) is True


def test_redeem_expired_code_is_refused():
    guard = make_guard(ttl=600)
    code = issue(guard)
    with pytest.raises(ac.AuthCodeError) as exc:
        redeem(guard, code, now=EXPIRED_AT)
    assert exc.value.code == "AUTHCODE_EXPIRED"
    assert guard.inspect_code(code)["consumed"] is False
    assert guard.stats()["redeemed"] == 0


def test_replay_revokes_derived_tokens():
    guard = make_guard()
    code = issue(guard)
    token_id = redeem(guard, code)["token_id"]
    with pytest.raises(ac.AuthCodeError) as exc:
        redeem(guard, code)
    assert exc.value.code == "AUTHCODE_REPLAY"
    assert guard.token_usable(token_id) is False
    assert guard.inspect_code(code)["token_ids"] == [token_id]
    assert guard.stats()["replays"] == 1
    assert guard.stats()["revoked_tokens"] == 1
    assert guard.stats()["redeemed"] == 1


def test_inspect_code_reports_detached_state():
    guard = make_guard()
    code = issue(guard)
    before = guard.inspect_code(code)
    assert before["code"] == code
    assert before["client_id"] == "web"
    assert before["redirect_uri"] == REDIRECT
    assert before["scopes"] == ["openid", "profile"]
    assert before["method"] == "S256"
    assert before["issued_at"] == NOW
    assert before["consumed"] is False
    assert before["token_ids"] == []

    before["scopes"].append("admin")
    before["token_ids"].append("fake")
    assert guard.inspect_code(code)["scopes"] == ["openid", "profile"]
    assert guard.inspect_code(code)["token_ids"] == []


def test_inspect_unknown_code_is_rejected():
    guard = make_guard()
    with pytest.raises(ac.AuthCodeError) as exc:
        guard.inspect_code("no-such-code")
    assert exc.value.code == "AUTHCODE_UNKNOWN"


def test_issue_rejects_invalid_input():
    guard = make_guard()
    bad_calls = [
        dict(client_id="", redirect_uri=REDIRECT, challenge="abc", method="S256"),
        dict(client_id="web", redirect_uri="", challenge="abc", method="S256"),
        dict(client_id="web", redirect_uri=REDIRECT, challenge="", method="S256"),
        dict(client_id="web", redirect_uri=REDIRECT, challenge="abc", method="S512"),
    ]
    for kwargs in bad_calls:
        with pytest.raises(ac.AuthCodeError) as exc:
            guard.issue_code(scopes=["openid"], now=NOW, **kwargs)
        assert exc.value.code == "AUTHCODE_INVALID_REQUEST"
    assert guard.stats()["issued"] == 0


def test_redeem_rejects_invalid_input():
    guard = make_guard()
    code = issue(guard)
    for kwargs in (dict(code="", client_id="web", redirect_uri=REDIRECT, verifier=VERIFIER),
                   dict(code=code, client_id="", redirect_uri=REDIRECT, verifier=VERIFIER),
                   dict(code=code, client_id="web", redirect_uri=REDIRECT, verifier="")):
        with pytest.raises(ac.AuthCodeError) as exc:
            guard.redeem(now=NOW, **kwargs)
        assert exc.value.code == "AUTHCODE_INVALID_REQUEST"
    assert guard.inspect_code(code)["consumed"] is False


def test_purge_expired_reclaims_codes():
    guard = make_guard(ttl=600)
    first = issue(guard, client_id="a")
    issue(guard, client_id="b")
    issue(guard, client_id="c")
    assert guard.purge_expired(now=EXPIRED_AT) == 3
    assert guard.purge_expired(now=EXPIRED_AT) == 0
    with pytest.raises(ac.AuthCodeError) as exc:
        guard.inspect_code(first)
    assert exc.value.code == "AUTHCODE_UNKNOWN"
    assert guard.stats()["purged"] == 3


def test_code_table_capacity_is_capped():
    guard = make_guard(max_codes=2)
    issue(guard, client_id="a")
    issue(guard, client_id="b")
    with pytest.raises(ac.AuthCodeError) as exc:
        issue(guard, client_id="c")
    assert exc.value.code == "AUTHCODE_CAPACITY_EXCEEDED"
    assert guard.stats()["issued"] == 2

    guard.purge_expired(now=EXPIRED_AT)
    late = "2026-11-24T09:20:00+00:00"
    fresh = issue(guard, client_id="c", now=late)
    assert guard.stats()["issued"] == 3
    assert redeem(guard, fresh, client_id="c", now=late)["client_id"] == "c"


def test_revoke_derived_is_explicit_and_idempotent():
    guard = make_guard()
    code = issue(guard)
    token_id = redeem(guard, code)["token_id"]
    assert guard.token_usable(token_id) is True
    assert guard.revoke_derived(code) == 1
    assert guard.token_usable(token_id) is False
    assert guard.revoke_derived(code) == 0
    assert guard.stats()["revoked_tokens"] == 1
    with pytest.raises(ac.AuthCodeError) as exc:
        guard.revoke_derived("missing-code")
    assert exc.value.code == "AUTHCODE_UNKNOWN"


def test_snapshot_restore_preserves_codes_and_tokens():
    guard = make_guard()
    first = issue(guard, client_id="a")
    token_id = redeem(guard, first, client_id="a")["token_id"]
    second = issue(guard, client_id="b")
    state = guard.snapshot()
    pristine = copy.deepcopy(state)

    state["stats"]["issued"] = 999
    assert guard.stats()["issued"] == 2

    restored = make_guard()
    restored.restore(pristine)
    assert restored.stats()["issued"] == 2
    assert restored.token_usable(token_id) is True
    assert restored.inspect_code(second)["consumed"] is False
    assert restored.redeem(code=second, client_id="b", redirect_uri=REDIRECT,
                           verifier=VERIFIER, now=NOW)["client_id"] == "b"


def test_restore_rejects_malformed_state_without_touching_live_data():
    guard = make_guard()
    code = issue(guard)
    bad_states = [None, [], {}, {"version": 99}, "not-a-state", {"version": 1}]
    for bad in bad_states:
        with pytest.raises(ac.AuthCodeError) as exc:
            guard.restore(bad)
        assert exc.value.code == "AUTHCODE_SNAPSHOT_INVALID"
    assert guard.inspect_code(code)["consumed"] is False
    assert guard.stats()["issued"] == 1


def test_concurrent_redeem_has_single_winner():
    guard = make_guard()
    code = issue(guard)
    outcomes = []
    errors = []
    barrier = threading.Barrier(16)

    def worker():
        try:
            barrier.wait(timeout=5)
            outcomes.append(("ok", redeem(guard, code)["token_id"]))
        except ac.AuthCodeError as exc:
            errors.append(exc.code)
        except Exception as exc:  # pragma: no cover - unexpected failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(outcomes) == 1
    assert errors == ["AUTHCODE_REPLAY"] * 15
    assert guard.stats()["redeemed"] == 1
    assert guard.stats()["replays"] == 15
    assert guard.stats()["revoked_tokens"] == 1
    assert guard.inspect_code(code)["consumed"] is True
    assert guard.token_usable(outcomes[0][1]) is False
