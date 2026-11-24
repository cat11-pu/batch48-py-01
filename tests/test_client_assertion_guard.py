import base64
import copy
import hashlib
import hmac
import json
import threading

import pytest

import client_assertion_guard as ca

ENDPOINT = "https://idp.example.com/token"
SECRET = b"client-secret-one"
NOW = 1000


def make_guard(**kwargs):
    return ca.ClientAssertionGuard(**kwargs)


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text):
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def assertion(*, kid="k1", secret=SECRET, iss="c1", sub=None,
              aud=ENDPOINT, iat=1000, exp=1200, jti="jti-1"):
    header = {"alg": "HS256", "kid": kid}
    claims = {"iss": iss, "sub": iss if sub is None else sub, "aud": aud,
              "iat": iat, "exp": exp, "jti": jti}
    head = b64(json.dumps(header).encode("utf-8"))
    body = b64(json.dumps(claims).encode("utf-8"))
    sig = b64(hmac.new(secret, f"{head}.{body}".encode("ascii"),
                       hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def test_verify_valid_assertion_returns_claims():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    out = guard.verify_assertion(assertion(), now=NOW)
    assert out["client_id"] == "c1"
    assert out["kid"] == "k1"
    assert out["jti"] == "jti-1"
    assert out["iat"] == 1000
    assert out["exp"] == 1200
    assert guard.stats() == {"keys": 1, "retired": 0, "verified": 1,
                             "rejected": 0, "jti": 0, "purged": 0}


def test_verify_rejects_unknown_client():
    guard = make_guard()
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(iss="ghost"), now=NOW)
    assert exc.value.code == "ASSERTION_UNKNOWN_CLIENT"
    assert guard.stats()["rejected"] == 1
    assert guard.stats()["verified"] == 0


def test_verify_rejects_unknown_kid():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(kid="k9"), now=NOW)
    assert exc.value.code == "ASSERTION_UNKNOWN_KID"


def test_verify_rejects_retired_kid():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    guard.retire_key(client_id="c1", kid="k1")
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(), now=NOW)
    assert exc.value.code == "ASSERTION_KID_RETIRED"
    assert guard.stats()["retired"] == 1


def test_verify_rejects_tampered_or_malformed_assertion():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    head, body, sig = assertion().split(".")
    claims = json.loads(b64d(body).decode("utf-8"))
    claims["jti"] = "other-jti"
    tampered = f"{head}.{b64(json.dumps(claims).encode('utf-8'))}.{sig}"
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(tampered, now=NOW)
    assert exc.value.code == "ASSERTION_SIGNATURE_INVALID"

    for broken in ("only-one-part", "a.b", "a.b.c"):
        with pytest.raises(ca.ClientAssertionError) as exc:
            guard.verify_assertion(broken, now=NOW)
        assert exc.value.code == "ASSERTION_MALFORMED"


def test_verify_rejects_issuer_subject_mismatch():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(iss="c1", sub="c2"), now=NOW)
    assert exc.value.code == "ASSERTION_ISS_MISMATCH"


def test_verify_rejects_wrong_audience():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(aud="https://other.example.com/token"), now=NOW)
    assert exc.value.code == "ASSERTION_AUD_MISMATCH"


def test_verify_rejects_expired_and_not_yet_valid():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(iat=900, exp=1000), now=NOW)
    assert exc.value.code == "ASSERTION_EXPIRED"
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.verify_assertion(assertion(iat=2000, exp=2100), now=NOW)
    assert exc.value.code == "ASSERTION_NOT_YET_VALID"
    assert guard.stats()["rejected"] == 2
    assert guard.stats()["verified"] == 0


def test_consume_jti_rejects_replay_within_ttl():
    guard = make_guard(jti_ttl=600)
    guard.consume_jti(client_id="c1", jti="j-1", now=1000)
    assert guard.is_jti_consumed("c1", "j-1", now=1001) is True
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.consume_jti(client_id="c1", jti="j-1", now=1001)
    assert exc.value.code == "ASSERTION_JTI_REPLAY"
    assert guard.stats()["jti"] == 1
    assert guard.is_jti_consumed("c1", "j-2", now=1001) is False


def test_purge_jti_reclaims_after_ttl():
    guard = make_guard(jti_ttl=600)
    guard.consume_jti(client_id="c1", jti="j-1", now=1000)
    assert guard.purge_jti(now=1599) == 0
    assert guard.purge_jti(now=1600) == 1
    assert guard.purge_jti(now=1600) == 0
    assert guard.is_jti_consumed("c1", "j-1", now=1601) is False
    guard.consume_jti(client_id="c1", jti="j-1", now=1601)
    assert guard.stats()["jti"] == 2
    assert guard.stats()["purged"] == 1


def test_jti_ledger_capacity_is_capped():
    guard = make_guard(max_jti=2, jti_ttl=600)
    guard.consume_jti(client_id="c1", jti="j-1", now=1000)
    guard.consume_jti(client_id="c1", jti="j-2", now=1000)
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.consume_jti(client_id="c1", jti="j-3", now=1000)
    assert exc.value.code == "ASSERTION_JTI_LEDGER_FULL"
    assert guard.is_jti_consumed("c1", "j-3", now=1000) is False
    assert guard.stats()["jti"] == 2
    guard.purge_jti(now=1601)
    guard.consume_jti(client_id="c1", jti="j-3", now=1601)
    assert guard.stats()["jti"] == 3


def test_register_key_is_idempotent_and_conflict_aware():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    assert guard.stats()["keys"] == 1
    with pytest.raises(ca.ClientAssertionError) as exc:
        guard.register_key(client_id="c1", kid="k1", secret=b"other-secret")
    assert exc.value.code == "ASSERTION_KEY_CONFLICT"
    assert guard.stats()["keys"] == 1


def test_activate_key_switches_active_kid():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=b"secret-one")
    assert guard.active_kid("c1") == "k1"
    guard.register_key(client_id="c1", kid="k2", secret=b"secret-two", active=False)
    assert guard.active_kid("c1") == "k1"
    guard.activate_key(client_id="c1", kid="k2")
    assert guard.active_kid("c1") == "k2"
    out = guard.verify_assertion(assertion(kid="k2", secret=b"secret-two"), now=NOW)
    assert out["kid"] == "k2"


def test_snapshot_restore_keeps_keys_and_ledger():
    guard = make_guard()
    guard.register_key(client_id="c1", kid="k1", secret=SECRET)
    guard.consume_jti(client_id="c1", jti="j-1", now=1000)
    state = guard.snapshot()
    pristine = copy.deepcopy(state)

    state["stats"]["verified"] = 999
    assert guard.stats()["verified"] == 0

    restored = make_guard()
    restored.restore(pristine)
    assert restored.stats()["jti"] == 1
    assert restored.verify_assertion(assertion(), now=NOW)["client_id"] == "c1"
    assert restored.is_jti_consumed("c1", "j-1", now=1001) is True
    with pytest.raises(ca.ClientAssertionError) as exc:
        restored.consume_jti(client_id="c1", jti="j-1", now=1001)
    assert exc.value.code == "ASSERTION_JTI_REPLAY"

    for bad in (None, [], {}, {"version": 99}, "nope"):
        with pytest.raises(ca.ClientAssertionError) as exc:
            restored.restore(bad)
        assert exc.value.code == "ASSERTION_SNAPSHOT_INVALID"
    assert restored.is_jti_consumed("c1", "j-1", now=1001) is True


def test_concurrent_consume_same_jti_has_single_winner():
    guard = make_guard()
    barrier = threading.Barrier(16)
    errors = []
    winners = []

    def worker():
        try:
            barrier.wait(timeout=5)
            guard.consume_jti(client_id="c1", jti="j-race", now=1000)
            winners.append(1)
        except ca.ClientAssertionError as exc:
            errors.append(exc.code)
        except Exception as exc:  # pragma: no cover - unexpected failure
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert errors == ["ASSERTION_JTI_REPLAY"] * 15
    assert guard.stats()["jti"] == 1
    assert guard.is_jti_consumed("c1", "j-race", now=1000) is True
