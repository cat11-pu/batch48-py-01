import threading

import pytest

import ct_log_matcher as ctm


def fp(value):
    return "%064x" % (0xAB0000 + value)


def entry(log, fingerprint, serial="01", timestamp=10.0,
          not_before=0.0, not_after=100.0):
    return {
        "log": log,
        "fingerprint": fingerprint,
        "serial": serial,
        "timestamp": timestamp,
        "not_before": not_before,
        "not_after": not_after,
    }


def make(**kwargs):
    return ctm.CtLogMatcher(**kwargs)


def test_ingest_counts_new_unique_fingerprints():
    guard = make()
    added = guard.ingest([
        entry("log-a", fp(1), serial="aa", timestamp=10.0),
        entry("log-a", fp(2), serial="bb", timestamp=20.0),
        entry("log-b", fp(3), serial="cc", timestamp=30.0),
    ])
    assert added == 3
    assert guard.processed() == 3
    assert len(guard.entries()) == 3
    assert guard.stats() == {
        "ingested": 3,
        "duplicates": 0,
        "revoked": 0,
        "ignored": 0,
        "cursor": 30,
    }


def test_ingest_and_dedup_merge_duplicate_fingerprints():
    guard = make()
    assert guard.ingest([entry("log-a", fp(1), timestamp=10.0)]) == 1
    assert guard.ingest([entry("log-b", fp(1), timestamp=10.0)]) == 0
    assert guard.processed() == 1
    assert guard.stats()["duplicates"] == 1

    assert guard.dedup() == 1
    assert len(guard.entries()) == 1
    assert guard.dedup() == 0
    info = guard.lookup(fp(1))
    assert info["sources"] == ["log-a", "log-b"]
    assert guard.processed() == 1


def test_fingerprint_case_is_normalized():
    guard = make()
    guard.ingest([entry("log-a", fp(7).upper(), timestamp=5.0)])
    assert guard.processed() == 1
    assert guard.lookup(fp(7))["fingerprint"] == fp(7)
    assert guard.lookup(fp(7).upper())["fingerprint"] == fp(7)


def test_ingest_rejects_malformed_entries():
    guard = make()
    bad_batches = [
        "not-a-list",
        [{"log": "log-a", "fingerprint": fp(1)}],
        [entry("log-a", "not-hex", timestamp=1.0)],
        [entry("log-a", fp(1), timestamp="10")],
        [entry("", fp(1), timestamp=1.0)],
        [entry("log-a", fp(1), not_before=50.0, not_after=10.0)],
    ]
    for batch in bad_batches:
        with pytest.raises(ctm.CtError) as exc:
            guard.ingest(batch)
        assert exc.value.code == "CT_INVALID_ENTRY"
    assert guard.processed() == 0
    assert guard.stats()["ingested"] == 0


def test_ingest_accepts_out_of_order_timestamps():
    guard = make()
    guard.ingest([entry("log-a", fp(1), timestamp=100.0)])
    guard.ingest([entry("log-a", fp(2), timestamp=50.0)])
    assert guard.processed() == 2
    assert guard.cursor() == 100
    assert [item["fingerprint"] for item in guard.entries()] == [fp(2), fp(1)]


def test_advance_cursor_is_monotonic_and_rejects_regression():
    guard = make()
    guard.ingest([entry("log-a", fp(1), timestamp=100.0)])
    assert guard.cursor() == 100
    assert guard.advance_cursor(200) == 200
    with pytest.raises(ctm.CtError) as exc:
        guard.advance_cursor(150)
    assert exc.value.code == "CT_CURSOR_REGRESSION"
    with pytest.raises(ctm.CtError) as exc:
        guard.advance_cursor(200)
    assert exc.value.code == "CT_CURSOR_REGRESSION"
    with pytest.raises(ctm.CtError) as exc:
        guard.advance_cursor("250")
    assert exc.value.code == "CT_INVALID_CURSOR"
    assert guard.cursor() == 200


def test_match_revocation_uses_fingerprint_not_serial():
    guard = make()
    guard.ingest([
        entry("log-a", fp(1), serial="S1", timestamp=1.0),
        entry("log-a", fp(2), serial="S2", timestamp=2.0),
    ])
    out = guard.match_revocation(
        {"fingerprint": fp(1), "serial": "S2"}, "keyCompromise", 50.0)
    assert out["status"] == "revoked"
    assert out["fingerprint"] == fp(1)
    assert out["reason"] == "keyCompromise"
    assert out["at"] == 50.0
    assert guard.stats()["revoked"] == 1

    unknown = guard.match_revocation(
        {"fingerprint": fp(99), "serial": "S1"}, "keyCompromise", 50.0)
    assert unknown["status"] == "unknown"


def test_match_revocation_ignores_after_validity_window():
    guard = make()
    guard.ingest([entry("log-a", fp(1), serial="S1",
                        not_before=0.0, not_after=100.0)])
    assert guard.match_revocation({"fingerprint": fp(1), "serial": "S1"},
                                  "cessation", 100.0)["status"] == "revoked"
    late = guard.match_revocation({"fingerprint": fp(1), "serial": "S1"},
                                  "cessation", 100.5)
    assert late["status"] == "expired"
    assert guard.stats()["revoked"] == 1
    assert guard.stats()["ignored"] == 1


def test_match_revocation_rejects_invalid_arguments():
    guard = make()
    guard.ingest([entry("log-a", fp(1), serial="S1", timestamp=1.0)])
    bad_calls = [
        ("not-a-dict", "keyCompromise", 50.0),
        ({"fingerprint": fp(1), "serial": "S1"}, "", 50.0),
        ({"fingerprint": fp(1), "serial": "S1"}, "keyCompromise", "now"),
        ({"fingerprint": "short", "serial": "S1"}, "keyCompromise", 50.0),
    ]
    for cert, reason, at in bad_calls:
        with pytest.raises(ctm.CtError) as exc:
            guard.match_revocation(cert, reason, at)
        assert exc.value.code == "CT_INVALID_REVOCATION"
    assert guard.stats()["revoked"] == 0


def test_capacity_limit_rejects_new_fingerprints():
    guard = make(max_entries=2)
    assert guard.ingest([
        entry("log-a", fp(1), timestamp=1.0),
        entry("log-a", fp(2), timestamp=2.0),
    ]) == 2
    with pytest.raises(ctm.CtError) as exc:
        guard.ingest([entry("log-a", fp(3), timestamp=3.0)])
    assert exc.value.code == "CT_CAPACITY_EXCEEDED"
    assert guard.processed() == 2


def test_lookup_unknown_fingerprint_and_returns_copy():
    guard = make()
    guard.ingest([entry("log-a", fp(1), serial="S1", timestamp=10.0)])
    info = guard.lookup(fp(1))
    assert info["fingerprint"] == fp(1)
    assert info["serial"] == "S1"
    assert info["sources"] == ["log-a"]
    info["sources"].append("edited")
    assert guard.lookup(fp(1))["sources"] == ["log-a"]

    with pytest.raises(ctm.CtError) as exc:
        guard.lookup(fp(404))
    assert exc.value.code == "CT_UNKNOWN_CERT"


def test_entries_are_sorted_by_timestamp():
    guard = make()
    guard.ingest([
        entry("log-a", fp(3), timestamp=30.0),
        entry("log-a", fp(1), timestamp=10.0),
        entry("log-a", fp(2), timestamp=20.0),
    ])
    assert [item["fingerprint"] for item in guard.entries()] == [fp(1), fp(2), fp(3)]


def test_snapshot_restore_keeps_unique_set_and_cursor():
    guard = make()
    guard.ingest([
        entry("log-a", fp(1), serial="S1", timestamp=10.0),
        entry("log-b", fp(2), serial="S2", timestamp=20.0),
    ])
    guard.match_revocation({"fingerprint": fp(1), "serial": "S1"}, "keyCompromise", 15.0)
    state = guard.snapshot()
    assert state["version"] == 1

    clone = make()
    clone.restore(state)
    assert clone.processed() == 2
    assert clone.cursor() == 20
    assert clone.lookup(fp(1))["sources"] == ["log-a"]
    assert clone.stats() == guard.stats()
    assert clone.match_revocation({"fingerprint": fp(2), "serial": "S2"},
                                  "superseded", 15.0)["status"] == "revoked"

    state["records"].clear()
    state["unique"].clear()
    assert clone.processed() == 2


def test_corrupt_snapshot_is_rejected_without_state_change():
    guard = make()
    guard.ingest([entry("log-a", fp(1), timestamp=10.0)])
    before = guard.stats()

    for bad in (
        None,
        {},
        {"version": 2, "records": [], "unique": {}, "stats": {}},
        {"version": 1, "records": "nope", "unique": {}, "stats": {}},
        {"version": 1, "records": [], "unique": [], "stats": {}},
        {"version": 1, "records": [], "unique": {}, "stats": []},
    ):
        with pytest.raises(ctm.CtError) as exc:
            guard.restore(bad)
        assert exc.value.code == "CT_SNAPSHOT_INVALID"
    assert guard.stats() == before
    assert guard.processed() == 1


def test_concurrent_ingest_does_not_lose_entries():
    guard = make(max_entries=1000)
    errors = []

    def worker(index):
        try:
            for offset in range(25):
                guard.ingest([entry(
                    "log-%d" % index,
                    fp(index * 100 + offset),
                    serial="%02x" % offset,
                    timestamp=float(index * 1000 + offset),
                )])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    counters = guard.stats()
    assert counters["ingested"] == 200
    assert counters["duplicates"] == 0
    assert counters["cursor"] == 7024
    assert guard.processed() == 200
    assert len(guard.entries()) == 200
