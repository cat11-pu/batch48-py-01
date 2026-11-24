import threading

import pytest

import detection_rule_engine as dr

FIELDS = {"path", "size", "owner"}


def fresh(**kwargs):
    return dr.DetectionRuleEngine(fields=FIELDS, **kwargs)


def ids(engine, record):
    return [hit["rule_id"] for hit in engine.evaluate(record)]


def test_add_rule_reports_compiled_shape():
    engine = fresh()
    assert engine.version() == 0
    assert engine.known_fields() == ("owner", "path", "size")
    out = engine.add_rule('RULE r1 PRIORITY 10 WHEN size >= 4096')
    assert out["accepted"] is True
    assert out["rule_id"] == "r1"
    assert out["priority"] == 10
    assert out["depth"] == 1
    assert engine.version() == 1
    assert engine.rule_ids() == ["r1"]

    info = engine.explain("r1")
    assert info["rule_id"] == "r1"
    assert info["priority"] == 10
    assert info["depth"] == 1
    assert info["tree"] == {"op": "cmp", "field": "size", "cmp": ">=", "value": 4096}


def test_hits_are_ordered_by_priority_then_rule_id():
    engine = fresh()
    engine.add_rule('RULE r_c PRIORITY 1 WHEN size > 0')
    engine.add_rule('RULE r_b PRIORITY 5 WHEN path ~ "*.tmp"')
    engine.add_rule('RULE r_a PRIORITY 5 WHEN size == 10')
    record = {"record_id": "rec-1", "path": "note.tmp", "size": 10}
    hits = engine.evaluate(record)

    assert [hit["rule_id"] for hit in hits] == ["r_c", "r_a", "r_b"]
    assert [hit["priority"] for hit in hits] == [1, 5, 5]
    assert {hit["version"] for hit in hits} == {3}
    assert [hit["dedup_key"] for hit in hits] == [
        "rec-1:r_c@3", "rec-1:r_a@3", "rec-1:r_b@3",
    ]
    assert record == {"record_id": "rec-1", "path": "note.tmp", "size": 10}


def test_explain_renders_nested_predicate_tree():
    engine = fresh()
    engine.add_rule(
        'RULE deep PRIORITY 7 WHEN AND(size >= 10, '
        'OR(path ~ "*.log", NOT(owner == "root")))'
    )
    info = engine.explain("deep")
    assert info["rule_id"] == "deep"
    assert info["priority"] == 7
    assert info["depth"] == 4
    assert info["tree"] == {
        "op": "and",
        "children": [
            {"op": "cmp", "field": "size", "cmp": ">=", "value": 10},
            {
                "op": "or",
                "children": [
                    {"op": "cmp", "field": "path", "cmp": "~", "value": "*.log"},
                    {
                        "op": "not",
                        "children": [
                            {"op": "cmp", "field": "owner", "cmp": "==", "value": "root"},
                        ],
                    },
                ],
            },
        ],
    }

    with pytest.raises(dr.DetectionError) as exc:
        engine.explain("missing")
    assert exc.value.code == "rule.unknown"


def test_glob_operator_is_case_sensitive():
    engine = fresh()
    engine.add_rule('RULE g1 PRIORITY 1 WHEN path ~ "src/*.py"')
    engine.add_rule('RULE g2 PRIORITY 2 WHEN path ~ "a?.txt"')

    assert ids(engine, {"record_id": "p1", "path": "src/app.py"}) == ["g1"]
    assert ids(engine, {"record_id": "p2", "path": "src/app.pyc"}) == []
    assert ids(engine, {"record_id": "p3", "path": "a1.txt"}) == ["g2"]
    assert ids(engine, {"record_id": "p4", "path": "abc.txt"}) == []
    assert ids(engine, {"record_id": "p5", "path": "A1.TXT"}) == []


def test_comparisons_only_apply_to_numbers():
    engine = fresh()
    engine.add_rule('RULE n1 PRIORITY 1 WHEN size >= 100')
    engine.add_rule('RULE n2 PRIORITY 2 WHEN size != 100')

    assert ids(engine, {"record_id": "r1", "size": 150}) == ["n1", "n2"]
    assert ids(engine, {"record_id": "r2", "size": "large"}) == ["n2"]
    assert ids(engine, {"record_id": "r3"}) == ["n2"]
    assert ids(engine, {"record_id": "r4", "size": 100}) == ["n1"]


def test_unknown_field_is_recorded_without_replacing_rules():
    engine = fresh()
    out = engine.add_rule('RULE u1 PRIORITY 1 WHEN tag == "x"')
    assert out["accepted"] is False
    assert out["code"] == "rule.unknown_field"
    assert "tag" in out["detail"]

    errors = engine.compile_errors()
    assert len(errors) == 1
    assert errors[0]["index"] == 0
    assert errors[0]["code"] == "rule.unknown_field"
    assert engine.version() == 0
    assert engine.rule_ids() == []

    engine.register_field("tag")
    assert engine.known_fields() == ("owner", "path", "size", "tag")
    assert engine.add_rule('RULE u1 PRIORITY 1 WHEN tag == "x"')["accepted"] is True
    assert engine.compile_errors() == []


def test_reload_keeps_valid_rules_when_one_fails():
    engine = fresh()
    engine.add_rule('RULE r0 PRIORITY 9 WHEN size > 0')

    result = engine.reload([
        'RULE r1 PRIORITY 1 WHEN size > 0',
        'RULE broken PRIORITY 2 WHEN AND(size > 1)',
        'RULE r2 PRIORITY 3 WHEN path ~ "*.tmp"',
    ])
    assert result["previous_version"] == 1
    assert result["version"] == 2
    assert result["accepted"] == 2
    assert result["rejected"] == 1

    errors = engine.compile_errors()
    assert len(errors) == 1
    assert errors[0]["index"] == 1
    assert errors[0]["code"] == "rule.syntax"
    assert engine.rule_ids() == ["r1", "r2"]

    hits = engine.evaluate({"record_id": "x", "size": 5, "path": "a.tmp"})
    assert [hit["rule_id"] for hit in hits] == ["r1", "r2"]


def test_duplicate_rule_id_is_refused():
    engine = fresh()
    engine.add_rule('RULE dup PRIORITY 4 WHEN size > 0')
    out = engine.add_rule('RULE dup PRIORITY 1 WHEN size > 10')

    assert out["accepted"] is False
    assert out["code"] == "rule.duplicate_id"
    assert engine.explain("dup")["priority"] == 4
    assert engine.version() == 1


def test_rule_cap_rejects_new_rule_without_mutation():
    engine = fresh(max_rules=2)
    assert engine.add_rule('RULE a PRIORITY 1 WHEN size > 0')["accepted"] is True
    assert engine.add_rule('RULE b PRIORITY 2 WHEN size > 1')["accepted"] is True

    out = engine.add_rule('RULE c PRIORITY 3 WHEN size > 2')
    assert out["accepted"] is False
    assert out["code"] == "rule.limit"
    assert engine.rule_ids() == ["a", "b"]
    assert engine.version() == 2


def test_repeated_evaluation_of_same_record_counts_once():
    engine = fresh()
    engine.add_rule('RULE r1 PRIORITY 1 WHEN size > 0')
    record = {"record_id": "a", "size": 5}

    for _ in range(3):
        assert len(engine.evaluate(record)) == 1
    assert engine.hit_counts() == {"r1@1": 1}

    assert len(engine.evaluate({"record_id": "b", "size": 5})) == 1
    assert engine.hit_counts() == {"r1@1": 2}
    assert engine.stats()["evaluations"] == 4
    assert engine.stats()["hits"] == 4


def test_record_without_identifier_is_refused():
    engine = fresh()
    engine.add_rule('RULE r1 PRIORITY 1 WHEN size > 0')

    for bad in ({"size": 3}, {"record_id": "", "size": 3}, {"record_id": 7, "size": 3}):
        with pytest.raises(dr.DetectionError) as exc:
            engine.evaluate(bad)
        assert exc.value.code == "record.invalid"

    assert engine.hit_counts() == {}
    assert engine.stats()["evaluations"] == 0


def test_hit_counts_are_keyed_by_rule_version():
    engine = fresh()
    engine.add_rule('RULE r1 PRIORITY 1 WHEN size > 0')
    record = {"record_id": "a", "size": 5}
    assert ids(engine, record) == ["r1"]

    engine.reload([
        'RULE r1 PRIORITY 1 WHEN size > 0',
        'RULE r2 PRIORITY 2 WHEN size > 0',
    ])
    hits = engine.evaluate(record)
    assert [hit["rule_id"] for hit in hits] == ["r1", "r2"]
    assert {hit["version"] for hit in hits} == {2}
    assert engine.hit_counts() == {"r1@1": 1, "r1@2": 1, "r2@2": 1}


def test_remove_rule_updates_version_once():
    engine = fresh()
    engine.add_rule('RULE r1 PRIORITY 1 WHEN size > 0')
    engine.add_rule('RULE r2 PRIORITY 2 WHEN size > 0')

    assert engine.remove_rule("r1") is True
    assert engine.version() == 3
    assert engine.rule_ids() == ["r2"]
    assert engine.remove_rule("r1") is False
    assert engine.version() == 3
    assert engine.rule_ids() == ["r2"]


def test_stats_reports_engine_totals():
    engine = fresh()
    engine.add_rule('RULE r1 PRIORITY 1 WHEN size > 0')
    engine.add_rule('RULE r2 PRIORITY 2 WHEN path ~ "*.tmp"')
    engine.add_rule('RULE bad PRIORITY 3 WHEN AND(size > 1)')

    engine.evaluate({"record_id": "a", "size": 4, "path": "x.tmp"})
    engine.evaluate({"record_id": "b", "size": 4, "path": "y.log"})

    assert engine.stats() == {"version": 2, "rules": 2, "evaluations": 2, "hits": 3}


def test_concurrent_evaluate_sees_whole_rule_sets():
    engine = fresh()
    set_a = [
        'RULE a1 PRIORITY 1 WHEN size > 0',
        'RULE a2 PRIORITY 2 WHEN size >= 0',
    ]
    set_b = [
        'RULE b1 PRIORITY 1 WHEN size > 0',
        'RULE b2 PRIORITY 2 WHEN size >= 0',
        'RULE b3 PRIORITY 3 WHEN size >= 0',
    ]
    engine.reload(set_a)

    errors = []
    observed = []
    guard = threading.Lock()

    def worker(index):
        try:
            for round_no in range(25):
                record = {"record_id": f"t{index}-{round_no}", "size": 5}
                hits = engine.evaluate(record)
                with guard:
                    observed.append(tuple(hit["rule_id"] for hit in hits))
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for _ in range(20):
        engine.reload(set_b)
        engine.reload(set_a)
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(observed) == 200
    assert set(observed) <= {("a1", "a2"), ("b1", "b2", "b3")}
    assert engine.rule_ids() == ["a1", "a2"]
