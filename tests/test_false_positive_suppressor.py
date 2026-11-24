import pytest

import false_positive_suppressor as fps


def finding(rule="R-1", path="src/app.py", line=10, hit="run(cmd)",
            pad=("a = 1", "b = 2", "c = 3", "d = 4")):
    return {
        "rule_id": rule,
        "path": path,
        "line": line,
        "context": [pad[0], pad[1], hit, pad[2], pad[3]],
    }


def make(**kwargs):
    return fps.FalsePositiveSuppressor(**kwargs)


def test_fingerprint_is_deterministic_and_context_sensitive():
    guard = make()
    base = finding()
    first = guard.fingerprint(base)
    second = guard.fingerprint(finding())
    assert isinstance(first, str) and len(first) == 64
    assert set(first) <= set("0123456789abcdef")
    assert first == second

    changed_line = finding(hit="run(other)")
    assert guard.fingerprint(changed_line) != first

    changed_rule = finding(rule="R-2")
    assert guard.fingerprint(changed_rule) != first

    padded = finding(hit="   run(cmd)   ")
    assert guard.fingerprint(padded) == first


def test_fingerprint_rejects_invalid_finding_shape():
    guard = make()

    with pytest.raises(fps.SuppressorError) as exc:
        guard.fingerprint({"rule_id": "R-1", "path": "a.py", "line": 1})
    assert exc.value.code == "SUPPRESS_FINDING_INVALID"

    with pytest.raises(fps.SuppressorError) as exc:
        guard.fingerprint(dict(finding(), context=["a", "b", "c", "d"]))
    assert exc.value.code == "SUPPRESS_FINDING_INVALID"

    with pytest.raises(fps.SuppressorError) as exc:
        guard.fingerprint(dict(finding(), context=["a", "b", 3, "d", "e"]))
    assert exc.value.code == "SUPPRESS_FINDING_INVALID"

    with pytest.raises(fps.SuppressorError) as exc:
        guard.fingerprint(dict(finding(), rule_id=""))
    assert exc.value.code == "SUPPRESS_FINDING_INVALID"

    with pytest.raises(fps.SuppressorError) as exc:
        guard.fingerprint(dict(finding(), line="10"))
    assert exc.value.code == "SUPPRESS_FINDING_INVALID"


def test_suppress_registers_entry_and_reports_fields():
    guard = make()
    fp = guard.fingerprint(finding())
    entry = guard.suppress(fp, "known false positive", 500.0)
    assert entry["fingerprint"] == fp
    assert entry["reason"] == "known false positive"
    assert entry["until"] == 500.0
    assert entry["scope"] == "file"
    assert entry["count"] == 1

    assert guard.is_suppressed(fp, 499.0) is True
    listed = guard.entries()
    assert len(listed) == 1
    assert listed[0]["fingerprint"] == fp
    assert guard.stats() == {
        "active_entries": 1,
        "suppressed_total": 1,
        "expired_total": 0,
        "filter_calls": 0,
    }


def test_suppress_duplicate_keeps_later_until_and_counts():
    guard = make()
    fp = guard.fingerprint(finding())
    guard.suppress(fp, "first", 100.0)
    second = guard.suppress(fp, "second", 50.0)
    assert second["until"] == 100.0
    assert second["count"] == 2
    third = guard.suppress(fp, "third", 150.0)
    assert third["until"] == 150.0
    assert third["count"] == 3
    assert len(guard.entries()) == 1
    assert guard.entries()[0]["reason"] == "third"
    assert guard.is_suppressed(fp, 149.0) is True
    assert guard.is_suppressed(fp, 150.0) is False


def test_suppress_reason_length_limit_keeps_old_entry():
    guard = make(max_reason_len=8)
    fp = guard.fingerprint(finding())
    guard.suppress(fp, "keep", 100.0)
    with pytest.raises(fps.SuppressorError) as exc:
        guard.suppress(fp, "0123456789", 900.0)
    assert exc.value.code == "SUPPRESS_REASON_TOO_LONG"

    listed = guard.entries()
    assert len(listed) == 1
    assert listed[0]["reason"] == "keep"
    assert listed[0]["until"] == 100.0
    assert listed[0]["count"] == 1
    assert guard.stats()["suppressed_total"] == 1


def test_capacity_limit_rejects_without_dropping_old_entries():
    guard = make(max_entries=1)
    first = guard.fingerprint(finding(rule="R-1"))
    second = guard.fingerprint(finding(rule="R-2"))
    guard.suppress(first, "one", 100.0)
    with pytest.raises(fps.SuppressorError) as exc:
        guard.suppress(second, "two", 100.0)
    assert exc.value.code == "SUPPRESS_CAPACITY_EXCEEDED"

    assert [item["fingerprint"] for item in guard.entries()] == [first]
    assert guard.is_suppressed(first, 50.0) is True
    assert guard.is_suppressed(second, 50.0) is False


def test_filter_splits_kept_and_suppressed_preserving_order():
    guard = make()
    findings = [finding(rule="R-1"), finding(rule="R-2"), finding(rule="R-3")]
    fp2 = guard.fingerprint(findings[1])
    guard.suppress(fp2, "noise", 1000.0)

    kept, suppressed = guard.filter(findings, 10.0)
    assert [item["rule_id"] for item in kept] == ["R-1", "R-3"]
    assert len(suppressed) == 1
    assert suppressed[0][0]["rule_id"] == "R-2"
    assert suppressed[0][1]["fingerprint"] == fp2
    assert guard.stats()["filter_calls"] == 1


def test_filter_boundary_at_exact_until_counts_as_open():
    guard = make()
    item = finding()
    fp = guard.fingerprint(item)
    guard.suppress(fp, "until boundary", 100.0)

    assert guard.is_suppressed(fp, 99.99) is True
    assert guard.is_suppressed(fp, 100.0) is False
    kept, suppressed = guard.filter([item], 100.0)
    assert kept == [item]
    assert suppressed == []


def test_filter_is_repeatable_and_side_effect_free():
    guard = make()
    findings = [finding(rule="R-1"), finding(rule="R-2")]
    guard.suppress(guard.fingerprint(findings[0]), "noise", 900.0)

    before_entries = guard.entries()
    first = guard.filter(findings, 5.0)
    second = guard.filter(findings, 5.0)
    assert [f["rule_id"] for f in first[0]] == [f["rule_id"] for f in second[0]]
    assert len(first[1]) == len(second[1]) == 1
    assert guard.entries() == before_entries
    assert guard.stats()["filter_calls"] == 2


def test_expire_removes_due_entries_and_returns_fingerprints():
    guard = make()
    fp1 = guard.fingerprint(finding(rule="R-1"))
    fp2 = guard.fingerprint(finding(rule="R-2"))
    guard.suppress(fp1, "due", 100.0)
    guard.suppress(fp2, "later", 200.0)

    assert guard.expire(150.0) == [fp1]
    assert guard.expire(150.0) == []
    assert guard.is_suppressed(fp1, 150.0) is False
    assert guard.is_suppressed(fp2, 150.0) is True
    assert len(guard.entries()) == 1
    assert guard.stats()["expired_total"] == 1


def test_diff_classifies_added_removed_and_changed():
    guard = make()
    base_one = finding(rule="R-1", path="a.py", line=3, hit="os.system(x)")
    base_two = finding(rule="R-2", path="b.py", line=7, hit="eval(y)")
    cur_first = finding(rule="R-2", path="b.py", line=7, hit="eval(z)")
    cur_second = finding(rule="R-3", path="c.py", line=9)

    result = guard.diff([base_one, base_two], [cur_first, cur_second], 0.0)
    assert [item["rule_id"] for item in result["added"]] == ["R-3"]
    assert [item["rule_id"] for item in result["removed"]] == ["R-1"]
    assert len(result["changed"]) == 1
    change = result["changed"][0]
    assert change["path"] == "b.py"
    assert change["line"] == 7
    assert change["from"] == guard.fingerprint(base_two)
    assert change["to"] == guard.fingerprint(cur_first)


def test_diff_excludes_suppressed_findings_from_added():
    guard = make()
    base = finding(rule="R-1", path="a.py", line=3)
    fresh = finding(rule="R-9", path="z.py", line=21)
    guard.suppress(guard.fingerprint(fresh), "accepted risk", 1000.0)

    result = guard.diff([base], [fresh], 10.0)
    assert result["added"] == []
    assert [item["rule_id"] for item in result["removed"]] == ["R-1"]
    assert result["changed"] == []


def test_history_tracks_count_and_last_until():
    guard = make()
    fp = guard.fingerprint(finding())
    assert guard.history(fp) == {"count": 0, "last_until": None, "scopes": []}
    guard.suppress(fp, "first", 100.0)
    guard.suppress(fp, "second", 150.0)
    info = guard.history(fp)
    assert info["count"] == 2
    assert info["last_until"] == 150.0
    assert info["scopes"] == ["file"]


def test_snapshot_restore_keeps_until_count_and_counters():
    guard = make()
    fp = guard.fingerprint(finding())
    guard.suppress(fp, "copied", 300.0)
    guard.filter([finding()], 1.0)

    state = guard.snapshot()
    assert state["version"] == 1
    clone = make()
    clone.restore(state)
    assert clone.is_suppressed(fp, 299.0) is True
    assert clone.history(fp)["count"] == 1
    assert clone.stats() == guard.stats()

    state["entries"].clear()
    assert clone.entries() != []
    assert clone.is_suppressed(fp, 1.0) is True

    for bad in (
        None,
        {},
        {"version": 2, "entries": [], "history": {}, "stats": {}},
        {"version": 1, "entries": "nope", "history": {}, "stats": {}},
        {"version": 1, "entries": [], "history": [], "stats": {}},
    ):
        with pytest.raises(fps.SuppressorError) as exc:
            clone.restore(bad)
        assert exc.value.code == "SUPPRESS_SNAPSHOT_INVALID"
    assert clone.is_suppressed(fp, 1.0) is True


def test_concurrent_filter_and_suppress_keeps_counters_exact():
    guard = make(max_entries=200)
    shared = [finding(rule="S-1"), finding(rule="S-2")]
    errors = []

    def worker(index):
        try:
            fp = guard.fingerprint(finding(rule="W-%d" % index))
            guard.suppress(fp, "worker", 1000.0 + index)
            for _ in range(5):
                kept, suppressed = guard.filter(shared, 1.0)
                if len(kept) != 2 or suppressed != []:
                    raise AssertionError("unexpected filter result")
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    import threading

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    counters = guard.stats()
    assert counters["active_entries"] == 8
    assert counters["suppressed_total"] == 8
    assert counters["filter_calls"] == 40
    assert len(guard.entries()) == 8
