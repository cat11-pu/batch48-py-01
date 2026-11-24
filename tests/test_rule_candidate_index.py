import threading

import pytest

import rule_candidate_index as rci


def make(**kwargs):
    return rci.RuleCandidateIndex(**kwargs)


def test_register_returns_normalised_entry():
    index = make()
    entry = index.register("R-001", ["PY", ".js", ".py"], ["Eval", "exec"], cost=3)
    assert entry["rule_id"] == "R-001"
    assert entry["file_kinds"] == [".js", ".py"]
    assert entry["keywords"] == ["Eval", "exec"]
    assert entry["cost"] == 3
    assert index.rules() == [entry]


def test_register_rejects_invalid_identifier_and_cost():
    index = make()
    with pytest.raises(rci.RuleIndexError) as empty:
        index.register("", [".py"], ["import"])
    assert empty.value.code == "RULE_ID_INVALID"

    with pytest.raises(rci.RuleIndexError) as zero:
        index.register("R-1", [".py"], ["import"], cost=0)
    assert zero.value.code == "RULE_COST_INVALID"

    with pytest.raises(rci.RuleIndexError) as kinds:
        index.register("R-1", [42], ["import"])
    assert kinds.value.code == "RULE_KINDS_INVALID"

    assert index.rules() == []

    stored = index.register("R-9", [".py"], ["eval"], cost=7)
    with pytest.raises(rci.RuleIndexError) as duplicate:
        index.register("R-9", [".js"], ["require"], cost=1)
    assert duplicate.value.code == "RULE_DUPLICATE"
    assert index.rules() == [stored]


def test_keyword_limit_rejects_without_writing_rule():
    index = make(max_keywords=4)
    words = ["kw-%d" % item for item in range(5)]
    with pytest.raises(rci.RuleIndexError) as exc:
        index.register("R-1", [".py"], words)
    assert exc.value.code == "RULE_KEYWORDS_LIMIT"
    assert index.stats()["rules"] == 0
    assert index.candidates("app.py", b"kw-1", whole_file=True) == []


def test_entry_capacity_is_a_hard_top():
    index = make(max_entries=2)
    index.register("R-1", [".py"], ["alpha"], cost=1)
    index.register("R-2", [".py"], ["beta"], cost=2)
    with pytest.raises(rci.RuleIndexError) as exc:
        index.register("R-3", [".py"], ["gamma"], cost=3)
    assert exc.value.code == "RULE_CAPACITY_EXCEEDED"
    assert [item["rule_id"] for item in index.rules()] == ["R-1", "R-2"]


def test_candidates_order_by_cost_then_identifier():
    index = make()
    index.register("R-b", [".py"], [], cost=5)
    index.register("R-c", [".py"], [], cost=1)
    index.register("R-a", [".py"], [], cost=1)
    assert index.candidates("pkg/mod.py", b"import os") == ["R-a", "R-c", "R-b"]


def test_file_kind_mismatch_prunes_candidate():
    index = make()
    index.register("R-py", [".py"], ["import"], cost=1)
    index.register("R-js", [".js"], ["import"], cost=2)
    assert index.candidates("app.js", b"import os") == ["R-js"]
    assert index.candidates("app.py", b"import os") == ["R-py"]


def test_keyword_miss_prunes_only_when_whole_file_is_sampled():
    index = make()
    index.register("R-1", [".py"], ["eval"], cost=1)
    assert index.candidates("app.py", b"print(1)", whole_file=True) == []
    assert index.candidates("app.py", b"x = eval(y)", whole_file=True) == ["R-1"]


def test_explain_matches_candidate_decision():
    index = make()
    index.register("R-1", [".py"], ["eval"], cost=2)
    kept = index.explain("R-1", "app.py", b"eval(x)")
    assert kept == {"rule_id": "R-1", "candidate": True, "basis": "keyword_matched"}
    pruned = index.explain("R-1", "app.js", b"eval(x)")
    assert pruned == {"rule_id": "R-1", "candidate": False, "basis": "kind_mismatch"}


def test_audit_counts_kept_and_pruned_lookups():
    index = make()
    index.register("R-1", [".py"], [], cost=1)
    index.candidates("a.py", b"")
    index.candidates("a.py", b"")
    index.candidates("a.js", b"")
    report = index.audit("R-1")
    assert report["rule_id"] == "R-1"
    assert report["hits"] == 2
    assert report["pruned"] == 1
    assert report["last_basis"] == "kind_mismatch"
    assert report["active"] is True


def test_remove_drops_rule_from_lookup_and_listing():
    index = make()
    index.register("R-1", [".py"], [], cost=1)
    assert index.remove("R-1") is True
    assert index.candidates("a.py", b"") == []
    assert index.rules() == []
    assert index.remove("R-1") is False


def test_snapshot_roundtrip_keeps_candidates_and_counters():
    index = make()
    index.register("R-2", [".py"], ["eval"], cost=4)
    index.register("R-1", [], ["token"], cost=3)
    index.candidates("a.py", b"eval(x)")
    state = index.snapshot()

    clone = make()
    clone.restore(state)
    assert clone.rules() == index.rules()
    assert clone.candidates("a.py", b"eval(x)") == ["R-1", "R-2"]
    assert clone.audit("R-1")["hits"] == 1
    state["rules"][0]["cost"] = 999
    assert clone.rules()[0]["cost"] != 999


def test_corrupt_snapshot_is_rejected_without_state_change():
    index = make()
    index.register("R-1", [".py"], ["eval"], cost=1)
    state = index.snapshot()
    state["version"] = 99
    other = make()
    with pytest.raises(rci.RuleIndexError) as exc:
        other.restore(state)
    assert exc.value.code == "SNAPSHOT_INVALID"
    assert other.rules() == []
    assert index.rules() != []


def test_stats_and_reset_only_clear_counters():
    index = make()
    index.register("R-1", [".py"], [], cost=1)
    index.register("R-2", [".js"], [], cost=2)
    index.candidates("a.py", b"")
    counters = index.stats()
    assert counters == {"rules": 2, "calls": 1, "served": 1, "pruned": 1}
    index.reset_stats()
    assert index.stats() == {"rules": 2, "calls": 0, "served": 0, "pruned": 0}


def test_concurrent_registration_is_not_lost():
    index = make()
    errors = []

    def worker(number):
        try:
            for offset in range(40):
                index.register("R-%d-%d" % (number, offset), [".py"], [], cost=1)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(item,)) for item in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert index.stats()["rules"] == 160
    found = index.candidates("main.py", b"")
    assert len(found) == 160
    assert len(set(found)) == 160


def test_repeated_lookup_returns_identical_independent_lists():
    index = make()
    index.register("R-1", [], ["token"], cost=2)
    index.register("R-2", [".py"], [], cost=1)
    first = index.candidates("a.py", b"token")
    second = index.candidates("a.py", b"token")
    assert first == second == ["R-2", "R-1"]
    first.append("R-999")
    assert index.candidates("a.py", b"token") == ["R-2", "R-1"]
