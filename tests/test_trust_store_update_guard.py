import copy
import hashlib
import hmac
import json
import threading

import pytest

import trust_store_update_guard as ts

SECRET = b"issuer-a-secret"
NOW = "2026-11-24T09:00:00+00:00"
NB = "2026-01-01T00:00:00+00:00"
NA = "2027-01-01T00:00:00+00:00"


def make(**kwargs):
    guard = ts.TrustStoreUpdateGuard(**kwargs)
    guard.register_issuer("issuer-a", SECRET)
    return guard


def root(subject, char, *, ca=True, not_before=NB, not_after=NA):
    return {
        "subject": subject,
        "fingerprint": char * 64,
        "not_before": not_before,
        "not_after": not_after,
        "ca": ca,
    }


def digest_of(roots):
    blob = json.dumps(roots, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def bundle(version, roots, *, issuer="issuer-a", secret=SECRET,
           digest=None, signature=None):
    body_digest = digest_of(roots) if digest is None else digest
    if signature is None:
        message = "%s:%d:%s" % (issuer, version, body_digest)
        signature = hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()
    return {
        "version": version,
        "issuer": issuer,
        "roots": copy.deepcopy(roots),
        "digest": body_digest,
        "signature": signature,
    }


def test_install_activates_bundle_and_keeps_root_order():
    guard = make()
    out = guard.install(
        bundle(1, [root("Root Alpha", "a"), root("Root Beta", "b")]), now=NOW
    )
    assert out == {"status": "installed", "version": 1, "active": 2, "legacy": 0}
    assert guard.current_version() == 1
    subjects = [item["subject"] for item in guard.active_roots(now=NOW)]
    assert subjects == ["Root Alpha", "Root Beta"]
    assert guard.legacy_roots(now=NOW) == []
    assert guard.stats()["install_ok"] == 1


def test_second_install_replaces_whole_active_set():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    guard.install(
        bundle(2, [root("Root Beta", "b"), root("Root Gamma", "c")]), now=NOW
    )
    assert guard.current_version() == 2
    assert [r["subject"] for r in guard.active_roots(now=NOW)] == [
        "Root Beta",
        "Root Gamma",
    ]
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.lookup("Root Alpha", now=NOW)
    assert exc.value.code == "TRUST_ROOT_NOT_FOUND"


def test_malformed_bundle_keeps_previous_set_and_logs_rejection():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    bad = bundle(2, [root("Root Beta", "b")])
    bad.pop("signature")
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(bad, now=NOW)
    assert exc.value.code == "TRUST_BUNDLE_MALFORMED"
    assert guard.current_version() == 1
    assert [r["fingerprint"] for r in guard.active_roots(now=NOW)] == ["a" * 64]
    assert guard.rejection_log()[-1]["code"] == "TRUST_BUNDLE_MALFORMED"


def test_unknown_and_revoked_issuer_are_rejected():
    guard = make()
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(bundle(1, [root("Root Alpha", "a")], issuer="issuer-z"), now=NOW)
    assert exc.value.code == "TRUST_ISSUER_UNKNOWN"

    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    guard.revoke_issuer("issuer-a")
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(bundle(2, [root("Root Beta", "b")]), now=NOW)
    assert exc.value.code == "TRUST_ISSUER_UNKNOWN"
    assert guard.current_version() == 1


def test_bad_signature_is_rejected():
    guard = make()
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(
            bundle(1, [root("Root Alpha", "a")], signature="0" * 64), now=NOW
        )
    assert exc.value.code == "TRUST_SIGNATURE_INVALID"
    assert guard.current_version() == 0
    assert guard.stats()["install_rejected"] == 1


def test_digest_mismatch_is_rejected():
    guard = make()
    tampered = bundle(1, [root("Root Alpha", "a")])
    tampered["roots"][0]["subject"] = "Evil Root"
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(tampered, now=NOW)
    assert exc.value.code == "TRUST_DIGEST_MISMATCH"
    assert guard.active_roots(now=NOW) == []


def test_duplicate_fingerprint_in_bundle_is_rejected():
    guard = make()
    dup = [root("Root Alpha", "a"), root("Root Beta", "a")]
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(bundle(1, dup), now=NOW)
    assert exc.value.code == "TRUST_DUPLICATE_FINGERPRINT"
    assert guard.current_version() == 0


def test_invalid_root_constraints_are_rejected():
    guard = make()
    for bad_root in (
        root("Root X", "d", ca=False),
        root("Root X", "d", not_before=NA, not_after=NB),
    ):
        with pytest.raises(ts.TrustStoreError) as exc:
            guard.install(bundle(1, [bad_root]), now=NOW)
        assert exc.value.code == "TRUST_ROOT_INVALID"
    assert guard.current_version() == 0


def test_capacity_overflow_keeps_previous_roots():
    guard = make(max_roots=2)
    guard.install(bundle(1, [root("Root A", "a"), root("Root B", "b")]), now=NOW)
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.install(
            bundle(
                2,
                [root("Root C", "c"), root("Root D", "d"), root("Root E", "e")],
            ),
            now=NOW,
        )
    assert exc.value.code == "TRUST_CAPACITY_EXCEEDED"
    assert [r["subject"] for r in guard.active_roots(now=NOW)] == [
        "Root A",
        "Root B",
    ]


def test_lookup_returns_root_metadata_and_counts_hits():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    info = guard.lookup("Root Alpha", now=NOW)
    assert info["subject"] == "Root Alpha"
    assert info["fingerprint"] == "a" * 64
    assert info["ca"] is True
    assert info["not_after"] == NA
    assert guard.stats()["lookups"] == 1


def test_lookup_miss_counts_separately():
    guard = make()
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.lookup("Root Missing", now=NOW)
    assert exc.value.code == "TRUST_ROOT_NOT_FOUND"
    counters = guard.stats()
    assert counters["lookups"] == 0
    assert counters["lookups_missed"] == 1


def test_snapshot_is_deep_copy_and_restore_resumes_versioning():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    snap = guard.snapshot()
    assert snap["version"] == 1

    local = copy.deepcopy(snap)
    snap["roots"]["active"][0]["subject"] = "Mutated"
    assert guard.active_roots(now=NOW)[0]["subject"] == "Root Alpha"

    other = make()
    other.restore(local)
    assert other.current_version() == 1
    assert [r["subject"] for r in other.active_roots(now=NOW)] == ["Root Alpha"]
    other.install(bundle(2, [root("Root Beta", "b")]), now=NOW)
    assert other.current_version() == 2


def test_restore_rejects_malformed_state_without_touching_current_set():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    with pytest.raises(ts.TrustStoreError) as exc:
        guard.restore({"version": 1})
    assert exc.value.code == "TRUST_SNAPSHOT_INVALID"
    assert guard.current_version() == 1
    assert [r["subject"] for r in guard.active_roots(now=NOW)] == ["Root Alpha"]


def test_stats_shape_and_reset():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    guard.install(
        bundle(2, [root("Root Beta", "b")], signature="1" * 64), now=NOW
    )
    guard.lookup("Root Alpha", now=NOW)
    with pytest.raises(ts.TrustStoreError):
        guard.lookup("Root Missing", now=NOW)
    assert guard.stats() == {
        "install_ok": 1,
        "install_rejected": 1,
        "lookups": 1,
        "lookups_missed": 1,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "install_ok": 0,
        "install_rejected": 0,
        "lookups": 0,
        "lookups_missed": 0,
    }


def test_concurrent_lookups_are_serialized_and_counted():
    guard = make()
    guard.install(bundle(1, [root("Root Alpha", "a")]), now=NOW)
    errors = []

    def worker():
        try:
            for _ in range(40):
                guard.lookup("Root Alpha", now=NOW)
        except Exception as exc:  # pragma: no cover - only on broken locking
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["lookups"] == 400
