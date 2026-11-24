import threading

import pytest

import taint_summary_fixpoint as tsf


def make(**kwargs):
    return tsf.TaintSummary(**kwargs)


def sink(param, name):
    return {"param": param, "sink": name}


def ret(param):
    return {"param": param, "ret": True}


def call(target, param, arg=0, use_ret=False):
    return {"target": target, "param": param, "arg": arg, "ret": use_ret}


def test_index_returns_canonical_definition():
    flow = make()
    entry = flow.index_function(
        "handler",
        ["req", "ctx"],
        [call("clean", 0, 0, True), sink(1, "audit.log")],
    )
    assert entry == {
        "name": "handler",
        "params": ["req", "ctx"],
        "callees": ["clean"],
        "sinks": ["audit.log"],
    }
    assert flow.functions() == ["handler"]


def test_index_rejects_bad_name_and_parameter_list():
    flow = make()
    with pytest.raises(tsf.TaintError) as blank:
        flow.index_function("", ["src"], [])
    assert blank.value.code == "TAINT_NAME_INVALID"

    with pytest.raises(tsf.TaintError) as duplicate:
        flow.index_function("dup", ["src", "src"], [])
    assert duplicate.value.code == "TAINT_PARAMS_INVALID"

    with pytest.raises(tsf.TaintError) as wrong_type:
        flow.index_function("bad", ["src", 7], [])
    assert wrong_type.value.code == "TAINT_PARAMS_INVALID"

    assert flow.functions() == []


def test_callee_edge_limit_rejects_without_indexing():
    flow = make(max_callees=2)
    edges = [sink(0, "s%d" % item) for item in range(3)]
    with pytest.raises(tsf.TaintError) as exc:
        flow.index_function("wide", ["src"], edges)
    assert exc.value.code == "TAINT_CALLEES_LIMIT"
    assert flow.functions() == []


def test_function_limit_is_a_hard_top():
    flow = make(max_functions=2)
    flow.index_function("one", ["a"], [])
    flow.index_function("two", ["b"], [])
    with pytest.raises(tsf.TaintError) as exc:
        flow.index_function("three", ["c"], [])
    assert exc.value.code == "TAINT_FUNCTION_LIMIT"
    assert flow.functions() == ["one", "two"]


def test_edge_positions_are_validated():
    flow = make()
    with pytest.raises(tsf.TaintError) as param:
        flow.index_function("bad", ["a"], [sink(4, "exec")])
    assert param.value.code == "TAINT_EDGE_INVALID"

    flow.index_function("target", ["a", "b"], [])
    with pytest.raises(tsf.TaintError) as arg:
        flow.index_function("caller", ["x"], [call("target", 0, 9, True)])
    assert arg.value.code == "TAINT_EDGE_INVALID"

    with pytest.raises(tsf.TaintError) as negative:
        flow.index_function("negative", ["x"], [call("target", -1, 0, False)])
    assert negative.value.code == "TAINT_EDGE_INVALID"


def test_summarize_reports_direct_return_and_sink_flows():
    flow = make()
    flow.index_function("handler", ["req", "ctx", "raw"],
                        [sink(0, "sql"), ret(2)])
    summary = flow.summarize("handler")
    assert summary == {
        "name": "handler",
        "params": 3,
        "flows": ["0->sink:sql", "2->ret"],
        "unstable": False,
        "version": 1,
    }


def test_callee_flows_follow_exact_argument_position():
    flow = make()
    flow.index_function("inner", ["first", "second"], [sink(1, "exec")])
    flow.index_function("hit", ["x"], [call("inner", 0, 1, False)])
    flow.index_function("miss", ["x"], [call("inner", 0, 0, False)])
    assert flow.summarize("inner")["flows"] == ["1->sink:exec"]
    assert flow.summarize("hit")["flows"] == ["0->sink:exec"]
    assert flow.summarize("miss")["flows"] == []


def test_return_flow_requires_the_return_edge_to_be_used():
    flow = make()
    flow.index_function("inner", ["first", "second"], [ret(1), sink(1, "exec")])
    flow.index_function("kept", ["x"], [call("inner", 0, 1, True)])
    flow.index_function("dropped", ["x"], [call("inner", 0, 1, False)])
    assert flow.summarize("kept")["flows"] == ["0->ret", "0->sink:exec"]
    assert flow.summarize("dropped")["flows"] == ["0->sink:exec"]


def test_summarize_is_repeatable_and_returns_a_copy():
    flow = make()
    flow.index_function("handler", ["req"], [sink(0, "sql"), ret(0)])
    first = flow.summarize("handler")
    second = flow.summarize("handler")
    assert first == second
    assert first["version"] == second["version"] == 1
    first["flows"].append("9->ret")
    assert flow.summarize("handler")["flows"] == ["0->ret", "0->sink:sql"]


def test_unknown_functions_are_refused():
    flow = make()
    with pytest.raises(tsf.TaintError) as summary:
        flow.summarize("missing")
    assert summary.value.code == "TAINT_UNKNOWN_FUNCTION"

    with pytest.raises(tsf.TaintError) as invalidation:
        flow.invalidate(["missing"])
    assert invalidation.value.code == "TAINT_UNKNOWN_FUNCTION"


def test_analyze_reports_closure_and_prefixed_flows():
    flow = make()
    flow.index_function("leaf", ["v"], [sink(0, "db")])
    flow.index_function("mid", ["v"], [call("leaf", 0, 0, False)])
    flow.index_function("entry", ["v"], [call("mid", 0, 0, False), ret(0)])
    report = flow.analyze("entry")
    assert report["entry"] == "entry"
    assert report["functions"] == ["entry", "leaf", "mid"]
    assert report["flows"] == [
        "entry::0->ret",
        "entry::0->sink:db",
        "leaf::0->sink:db",
        "mid::0->sink:db",
    ]
    assert report["unstable"] == []


def test_invalidate_recomputes_dependents_after_redefinition():
    flow = make()
    flow.index_function("inner", ["v"], [sink(0, "sql")])
    flow.index_function("outer", ["v"], [call("inner", 0, 0, False)])
    assert flow.summarize("outer")["flows"] == ["0->sink:sql"]

    flow.index_function("inner", ["v"], [sink(0, "shell")])
    touched = flow.invalidate(["inner"])
    assert touched == ["inner", "outer"]
    assert flow.summarize("outer")["flows"] == ["0->sink:shell"]


def test_snapshot_restore_keeps_summaries_and_analysis():
    flow = make()
    flow.index_function("leaf", ["v"], [sink(0, "db")])
    flow.index_function("entry", ["v"], [call("leaf", 0, 0, True), ret(0)])
    before = flow.summarize("entry")
    report = flow.analyze("entry")
    state = flow.snapshot()

    clone = make()
    clone.restore(state)
    assert clone.summarize("entry") == before
    assert clone.analyze("entry") == report
    state["functions"][0]["name"] = "tampered"
    assert clone.functions() == ["entry", "leaf"]


def test_corrupt_snapshot_is_rejected_without_state_change():
    flow = make()
    flow.index_function("leaf", ["v"], [sink(0, "db")])
    state = flow.snapshot()
    state["version"] = 99

    other = make()
    with pytest.raises(tsf.TaintError) as exc:
        other.restore(state)
    assert exc.value.code == "TAINT_SNAPSHOT_INVALID"
    assert other.functions() == []
    assert flow.summarize("leaf")["flows"] == ["0->sink:db"]


def test_concurrent_analysis_sees_whole_summaries():
    flow = make()
    flow.index_function("leaf", ["v"], [sink(0, "db")])
    flow.index_function("entry", ["v"], [call("leaf", 0, 0, False)])
    expected = flow.analyze("entry")
    errors = []

    def reader():
        try:
            for _ in range(10):
                assert flow.analyze("entry") == expected
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def writer():
        try:
            for _ in range(25):
                flow.index_function("leaf", ["v"], [sink(0, "db")])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=reader) for _ in range(4)]
    threads.append(threading.Thread(target=writer))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert flow.analyze("entry") == expected
