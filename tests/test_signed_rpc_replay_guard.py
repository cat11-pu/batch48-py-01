import hashlib
import threading

import pytest

import signed_rpc_replay_guard as rg

T0 = 1_700_000_000
BODY = b'{"amount":100}'


def make(**kwargs):
    guard = rg.SignedRpcReplayGuard(clock_skew=30, **kwargs)
    guard.add_client("pay-1", b"secret-1")
    return guard


def signed(guard, *, client_id="pay-1", nonce="n-1", timestamp=T0,
           body=BODY, method="POST", path="/v1/charge"):
    return guard.sign_request(client_id=client_id, method=method, path=path,
                              body=body, timestamp=timestamp, nonce=nonce)


def test_sign_and_verify_round_trip():
    guard = make()
    request = signed(guard)
    assert request["client_id"] == "pay-1"
    assert request["body_sha256"] == hashlib.sha256(BODY).hexdigest()
    assert request["nonce"] == "n-1"
    assert request["timestamp"] == T0
    assert len(request["signature"]) == 64

    out = guard.verify_request(request, now=T0)
    assert out["status"] == "ok"
    assert out["client_id"] == "pay-1"
    assert out["nonce"] == "n-1"
    assert out["replayed"] is False
    assert guard.stats()["verified"] == 1


def test_every_envelope_field_is_signed():
    guard = make()
    base = signed(guard)
    variants = []
    for field, value in [
        ("method", "GET"),
        ("path", "/v1/refund"),
        ("timestamp", T0 + 1),
        ("nonce", "n-other"),
        ("body_sha256", hashlib.sha256(b"other").hexdigest()),
    ]:
        item = dict(base)
        item[field] = value
        variants.append(item)

    for item in variants:
        with pytest.raises(rg.RpcGuardError) as exc:
            guard.verify_request(item, now=T0)
        assert exc.value.code == "BAD_SIGNATURE"
    assert guard.stats()["rejected"] == 5
    assert guard.stats()["verified"] == 0


def test_unknown_client_is_refused():
    guard = make()
    forged = {
        "client_id": "ghost",
        "method": "POST",
        "path": "/v1/charge",
        "body_sha256": hashlib.sha256(b"x").hexdigest(),
        "timestamp": T0,
        "nonce": "n-ghost",
        "signature": "a" * 64,
    }
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.verify_request(forged, now=T0)
    assert exc.value.code == "UNKNOWN_CLIENT"

    with pytest.raises(rg.RpcGuardError) as exc:
        guard.sign_request(client_id="ghost", method="POST", path="/v1/charge",
                           body=b"{}", timestamp=T0, nonce="n-ghost-2")
    assert exc.value.code == "UNKNOWN_CLIENT"


def test_revoked_client_cannot_sign_or_verify():
    guard = make()
    request = signed(guard)
    guard.revoke_client("pay-1")

    with pytest.raises(rg.RpcGuardError) as exc:
        guard.verify_request(request, now=T0)
    assert exc.value.code == "CLIENT_REVOKED"

    with pytest.raises(rg.RpcGuardError) as exc:
        guard.sign_request(client_id="pay-1", method="POST", path="/v1/charge",
                           body=b"{}", timestamp=T0, nonce="n-3")
    assert exc.value.code == "CLIENT_REVOKED"


def test_timestamp_skew_boundary():
    guard = make()
    edge = signed(guard, nonce="n-edge")
    assert guard.verify_request(edge, now=T0 + 30)["status"] == "ok"

    later = signed(guard, nonce="n-later", timestamp=T0)
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.verify_request(later, now=T0 + 31)
    assert exc.value.code == "STALE_TIMESTAMP"

    past = signed(guard, nonce="n-past", timestamp=T0)
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.verify_request(past, now=T0 - 31)
    assert exc.value.code == "STALE_TIMESTAMP"


def test_nonce_reuse_with_different_body_conflicts():
    guard = make()
    first = signed(guard, nonce="n-dup", body=b'{"amount":100}')
    assert guard.verify_request(first, now=T0)["status"] == "ok"

    second = signed(guard, nonce="n-dup", body=b'{"amount":900}')
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.verify_request(second, now=T0)
    assert exc.value.code == "NONCE_CONFLICT"
    assert guard.stats()["conflicts"] == 1


def test_execute_records_handler_output():
    guard = make()
    request = signed(guard, nonce="n-exec")
    out = guard.execute_once(request, lambda item: {"charged": 100}, now=T0)
    assert out["status"] == "ok"
    assert out["response"] == {"charged": 100}
    assert out["nonce"] == "n-exec"

    recorded = guard.record_response("pay-1", "n-exec", {"charged": 100})
    assert recorded["nonce"] == "n-exec"
    assert guard.stats() == {
        "verified": 1, "rejected": 0, "replayed": 0,
        "conflicts": 0, "executed": 1,
    }


def test_nonce_windows_are_per_client():
    guard = make()
    guard.add_client("pay-2", b"secret-2")
    first = signed(guard, client_id="pay-1", nonce="shared-nonce")
    second = signed(guard, client_id="pay-2", nonce="shared-nonce")
    assert guard.verify_request(first, now=T0)["status"] == "ok"
    assert guard.verify_request(second, now=T0)["status"] == "ok"
    assert guard.nonce_window("pay-1", now=T0) == ["shared-nonce"]
    assert guard.nonce_window("pay-2", now=T0) == ["shared-nonce"]

    colliding = signed(guard, client_id="pay-1", nonce="shared-nonce", body=b"{}")
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.verify_request(colliding, now=T0)
    assert exc.value.code == "NONCE_CONFLICT"


def test_purge_drops_only_expired_nonces():
    guard = make()
    guard.verify_request(signed(guard, nonce="n-old", timestamp=T0), now=T0)
    assert guard.purge(now=T0 + 300) == 0
    assert guard.purge(now=T0 + 301) == 1
    assert guard.nonce_window("pay-1", now=T0 + 301) == []


def test_snapshot_restore_keeps_clients_and_window():
    guard = make()
    guard.verify_request(signed(guard, nonce="n-keep"), now=T0)
    state = guard.snapshot()

    revived = rg.SignedRpcReplayGuard(clock_skew=30)
    revived.restore(state)
    assert revived.nonce_window("pay-1", now=T0) == ["n-keep"]

    fresh = revived.sign_request(client_id="pay-1", method="POST",
                                 path="/v1/charge", body=b'{"amount":1}',
                                 timestamp=T0, nonce="n-new")
    assert revived.verify_request(fresh, now=T0)["status"] == "ok"


def test_malformed_snapshot_leaves_state_intact():
    guard = make()
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.restore({"clients": 7})
    assert exc.value.code == "STATE_INVALID"
    assert guard.verify_request(signed(guard, nonce="n-after"), now=T0)["status"] == "ok"


def test_client_table_capacity():
    guard = rg.SignedRpcReplayGuard(max_clients=2)
    guard.add_client("a", b"sa")
    guard.add_client("b", b"sb")
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.add_client("c", b"sc")
    assert exc.value.code == "CLIENT_FULL"


def test_client_registration_is_idempotent():
    guard = make()
    same = guard.add_client("pay-1", b"secret-1")
    assert same["client_id"] == "pay-1"
    with pytest.raises(rg.RpcGuardError) as exc:
        guard.add_client("pay-1", b"other-secret")
    assert exc.value.code == "CLIENT_CONFLICT"


def test_concurrent_execute_runs_handler_once():
    guard = make()
    request = signed(guard, nonce="n-race")
    calls = []
    results = []
    start = threading.Barrier(8)

    def handler(item):
        calls.append(item["nonce"])
        return {"charged": 100}

    def run():
        start.wait()
        results.append(guard.execute_once(request, handler, now=T0))

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert calls == ["n-race"]
    assert len(results) == 8
    assert all(isinstance(item, dict) for item in results)
    assert guard.stats()["executed"] == 1


def test_reset_stats_zeroes_counters():
    guard = make()
    guard.verify_request(signed(guard, nonce="n-one"), now=T0)
    guard.reset_stats()
    assert guard.stats() == {
        "verified": 0, "rejected": 0, "replayed": 0,
        "conflicts": 0, "executed": 0,
    }
