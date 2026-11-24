import re
import threading

import pytest

import download_grant_guard as dg


def make(resource_id="rid", subject="alice", ttl=60, now=100, **kwargs):
    guard = dg.DownloadGrantGuard(**kwargs)
    return guard, guard.issue(resource_id, subject, ttl, now)


def test_issue_returns_scoped_grant_with_ttl():
    guard, grant = make()
    assert re.fullmatch(r"[0-9a-f]{32}", grant["grant_id"])
    assert grant["resource_id"] == "rid"
    assert grant["subject"] == "alice"
    assert grant["root"] == "/data/rid"
    assert grant["issued_at"] == 100
    assert grant["expires_at"] == 160
    assert grant["consumed"] is False
    other = guard.issue("rid", "alice", 60, 100)
    assert other["grant_id"] != grant["grant_id"]
    assert len(guard.grants()) == 2


def test_issue_validates_arguments_and_capacity():
    guard = dg.DownloadGrantGuard(max_grants=1)
    with pytest.raises(dg.DownloadError) as exc:
        guard.issue("Bad/Id", "alice", 60, 100)
    assert exc.value.code == "DOWNLOAD_INVALID_RESOURCE"
    with pytest.raises(dg.DownloadError) as exc:
        guard.issue("rid", "", 60, 100)
    assert exc.value.code == "DOWNLOAD_INVALID_SUBJECT"
    for ttl in (0, -5, 1.5):
        with pytest.raises(dg.DownloadError) as exc:
            guard.issue("rid", "alice", ttl, 100)
        assert exc.value.code == "DOWNLOAD_INVALID_TTL"
    guard.issue("rid", "alice", 60, 100)
    with pytest.raises(dg.DownloadError) as exc:
        guard.issue("rid", "bob", 60, 100)
    assert exc.value.code == "DOWNLOAD_GRANT_LIMIT"


def test_authorize_allows_get_and_head_under_root():
    guard, grant = make()
    decision = guard.authorize(grant, "/data/rid/reports/day.csv", "GET", 110)
    assert decision == dg.GrantDecision(True, "ok", 160)
    head = guard.authorize(grant, "/data/rid/reports/day.csv", "HEAD", 110)
    assert head.allowed is True
    assert head.expires_at == 160
    assert guard.authorize(grant, "/data/rid", "GET", 110).allowed is True


def test_authorize_denies_paths_outside_resource_root():
    guard, grant = make()
    for path in (
        "/data/other/x",
        "/data/rid2/x",
        "/data/rid/../../other/x",
    ):
        decision = guard.authorize(grant, path, "GET", 110)
        assert decision.reason == "path-outside-resource"
        assert decision.allowed is False


def test_authorize_denies_percent_encoded_traversal():
    guard, grant = make()
    decision = guard.authorize(grant, "/data/rid/%2e%2e/other/x", "GET", 110)
    assert decision.allowed is False
    assert decision.reason == "path-outside-resource"


def test_authorize_rejects_other_methods():
    guard, grant = make()
    for method in ("POST", "PUT", "DELETE", "get"):
        decision = guard.authorize(grant, "/data/rid/x", method, 110)
        assert decision.reason == "method-not-allowed"
        assert decision.allowed is False


def test_authorize_rejects_aliasing_path_shapes():
    guard, grant = make()
    assert guard.authorize(grant, "/data/RID/x", "GET", 110).reason == (
        "path-outside-resource"
    )
    for path in ("/data/rid/notes.txt.", "/data/rid/notes.txt ", "/data/rid/\x00x"):
        decision = guard.authorize(grant, path, "GET", 110)
        assert decision.reason == "path-invalid"
        assert decision.allowed is False


def test_authorize_expiry_boundary():
    guard, grant = make(ttl=60, now=100)
    assert guard.authorize(grant, "/data/rid/x", "GET", 159).allowed is True
    decision = guard.authorize(grant, "/data/rid/x", "GET", 160)
    assert decision.allowed is False
    assert decision.reason == "expired"
    assert decision.expires_at == 160


def test_revoke_invalidates_in_flight_grants_once():
    guard = dg.DownloadGrantGuard()
    first = guard.issue("rid", "alice", 60, 100)
    second = guard.issue("rid", "bob", 60, 100)
    assert guard.revoke("rid", 100) == 2
    assert guard.revoke("rid", 100) == 0
    for grant in (first, second):
        decision = guard.authorize(grant, "/data/rid/x", "GET", 110)
        assert decision.allowed is False
        assert decision.reason == "revoked"
    later = guard.issue("rid", "carol", 60, 200)
    assert guard.authorize(later, "/data/rid/x", "GET", 210).allowed is True


def test_consume_is_single_use_and_ends_authorization():
    guard, grant = make()
    assert guard.consume(grant["grant_id"], 110) is True
    with pytest.raises(dg.DownloadError) as exc:
        guard.consume(grant["grant_id"], 111)
    assert exc.value.code == "DOWNLOAD_TOKEN_REPLAYED"
    decision = guard.authorize(grant, "/data/rid/x", "GET", 112)
    assert decision.allowed is False
    assert decision.reason == "consumed"
    with pytest.raises(dg.DownloadError) as exc:
        guard.consume("0" * 32, 112)
    assert exc.value.code == "DOWNLOAD_UNKNOWN_GRANT"


def test_consume_refuses_expired_grant():
    guard, grant = make(ttl=10, now=100)
    with pytest.raises(dg.DownloadError) as exc:
        guard.consume(grant["grant_id"], 110)
    assert exc.value.code == "DOWNLOAD_GRANT_EXPIRED"


def test_purge_drops_only_expired_grants():
    guard = dg.DownloadGrantGuard()
    short = guard.issue("rid", "alice", 10, 100)
    long = guard.issue("rid", "bob", 100, 100)
    assert guard.purge(109) == 0
    assert guard.purge(110) == 1
    with pytest.raises(dg.DownloadError) as exc:
        guard.inspect(short["grant_id"])
    assert exc.value.code == "DOWNLOAD_UNKNOWN_GRANT"
    assert guard.authorize(long, "/data/rid/x", "GET", 110).allowed is True


def test_snapshot_restore_keeps_consumed_and_revocations():
    guard = dg.DownloadGrantGuard()
    used = guard.issue("rid-a", "alice", 60, 100)
    revoked = guard.issue("rid-b", "bob", 60, 100)
    assert guard.consume(used["grant_id"], 105) is True
    assert guard.revoke("rid-b", 106) == 1
    state = guard.snapshot()
    state["grants"][used["grant_id"]]["consumed"] = False
    fresh = dg.DownloadGrantGuard()
    fresh.restore(guard.snapshot())
    with pytest.raises(dg.DownloadError) as exc:
        fresh.consume(used["grant_id"], 110)
    assert exc.value.code == "DOWNLOAD_TOKEN_REPLAYED"
    decision = fresh.authorize(revoked, "/data/rid-b/x", "GET", 110)
    assert decision.reason == "revoked"
    with pytest.raises(dg.DownloadError) as exc:
        fresh.restore({"grants": 3})
    assert exc.value.code == "DOWNLOAD_SNAPSHOT_INVALID"
    assert isinstance(fresh.inspect(used["grant_id"]), dict)


def test_check_range_validates_single_byte_range():
    guard, grant = make(ttl=60, now=100)
    assert guard.check_range(grant, "bytes=0-99", 100, 110) == {
        "allowed": True, "reason": "ok", "ranges": [(0, 99)],
    }
    assert guard.check_range(grant, "bytes=50-", 100, 110)["ranges"] == [(50, 99)]
    assert guard.check_range(grant, "bytes=-10", 100, 110)["ranges"] == [(90, 99)]
    assert guard.check_range(grant, "bytes=90-200", 100, 110)["ranges"] == [(90, 99)]
    for header in ("bytes=200-300", "bytes=50-10"):
        out = guard.check_range(grant, header, 100, 110)
        assert out["allowed"] is False
        assert out["reason"] == "range-unsatisfiable"
    for header in ("chars=0-1", "bytes=", "bytes=a-b"):
        out = guard.check_range(grant, header, 100, 110)
        assert out["allowed"] is False
        assert out["reason"] == "range-invalid"
    assert guard.check_range(grant, "bytes=0-9", 100, 190)["reason"] == "expired"


def test_concurrent_consume_has_exactly_one_winner():
    guard, grant = make()
    barrier = threading.Barrier(12)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            value = guard.consume(grant["grant_id"], 110)
        except dg.DownloadError as exc:
            value = exc.code
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures
            value = type(exc).__name__
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 1
    assert results.count("DOWNLOAD_TOKEN_REPLAYED") == 11
