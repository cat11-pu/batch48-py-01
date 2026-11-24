import threading

import pytest

import sbom_drift_detector as sdd


def make(**kwargs):
    return sdd.SbomDriftDetector(**kwargs)


def comp(name, version="1.0.0", scope="runtime", qualifiers=None):
    purl = "pkg:npm/" + name
    if qualifiers:
        purl = purl + "?" + qualifiers
    return {"purl": purl, "version": version, "scope": scope}


def edge(source, target):
    return {"from": "pkg:npm/" + source, "to": "pkg:npm/" + target}


def empty_diff():
    return {
        "added": [],
        "removed": [],
        "upgraded": [],
        "downgraded": [],
        "scope_changed": [],
        "unknown": [],
    }


def test_load_reports_counts_and_normalised_components():
    detector = make()
    report = detector.load(
        "base",
        [comp("lodash", qualifiers="B=2&a=1"), comp("app")],
        [edge("app", "lodash")],
    )
    assert report == {"side": "base", "components": 2, "edges": 1}
    assert detector.components("base") == [
        {"purl": "pkg:npm/app", "version": "1.0.0", "scope": "runtime"},
        {"purl": "pkg:npm/lodash?a=1&b=2", "version": "1.0.0", "scope": "runtime"},
    ]


def test_case_and_qualifier_order_do_not_create_drift():
    detector = make()
    detector.load("base", [comp("Lodash", qualifiers="B=2&a=1")], [])
    detector.load("current", [comp("lodash", qualifiers="a=1&b=2")], [])
    assert detector.diff() == empty_diff()


def test_load_rejects_invalid_side_and_component_fields():
    detector = make()
    with pytest.raises(sdd.SbomDriftError) as side:
        detector.load("other", [], [])
    assert side.value.code == "INVALID_SIDE"

    with pytest.raises(sdd.SbomDriftError) as purl:
        detector.load("base", [{"purl": "https://example.com/a",
                                "version": "1.0.0", "scope": "runtime"}], [])
    assert purl.value.code == "INVALID_PURL"

    with pytest.raises(sdd.SbomDriftError) as scope:
        detector.load("base", [comp("a", scope="dev")], [])
    assert scope.value.code == "INVALID_SCOPE"

    with pytest.raises(sdd.SbomDriftError) as version:
        detector.load("base", [comp("a", version="")], [])
    assert version.value.code == "INVALID_COMPONENT"

    assert detector.components("base") == []


def test_duplicate_normalised_component_is_refused():
    detector = make()
    with pytest.raises(sdd.SbomDriftError) as exc:
        detector.load("base", [comp("demo"), comp("DEMO")], [])
    assert exc.value.code == "DUPLICATE_COMPONENT"
    assert detector.components("base") == []


def test_component_capacity_keeps_previous_side():
    detector = make(max_components=2)
    detector.load("base", [comp("a"), comp("b")], [])
    with pytest.raises(sdd.SbomDriftError) as exc:
        detector.load("base", [comp("a"), comp("b"), comp("c")], [])
    assert exc.value.code == "SBOM_CAPACITY_EXCEEDED"
    assert [item["purl"] for item in detector.components("base")] == [
        "pkg:npm/a",
        "pkg:npm/b",
    ]


def test_edges_must_reference_loaded_components():
    detector = make()
    with pytest.raises(sdd.SbomDriftError) as exc:
        detector.load("base", [comp("a")], [edge("a", "missing")])
    assert exc.value.code == "UNKNOWN_COMPONENT"
    assert detector.components("base") == []


def test_diff_requires_both_sides():
    detector = make()
    with pytest.raises(sdd.SbomDriftError) as first:
        detector.diff()
    assert first.value.code == "SBOM_INCOMPLETE"

    detector.load("base", [comp("a")], [])
    with pytest.raises(sdd.SbomDriftError) as second:
        detector.diff()
    assert second.value.code == "SBOM_INCOMPLETE"


def test_diff_reports_added_and_removed_sorted():
    detector = make()
    detector.load("base", [comp("b")], [])
    detector.load("current", [comp("a")], [])
    assert detector.diff() == {
        "added": [{"purl": "pkg:npm/a", "version": "1.0.0", "scope": "runtime"}],
        "removed": [{"purl": "pkg:npm/b", "version": "1.0.0", "scope": "runtime"}],
        "upgraded": [],
        "downgraded": [],
        "scope_changed": [],
        "unknown": [],
    }


def test_diff_reports_upgrades_and_downgrades_numerically():
    detector = make()
    detector.load("base", [comp("a", "1.2.3"), comp("b", "2.0.0")], [])
    detector.load("current", [comp("a", "1.3.0"), comp("b", "1.9.9")], [])
    report = detector.diff()
    assert report["upgraded"] == [
        {"purl": "pkg:npm/a", "from": "1.2.3", "to": "1.3.0"},
    ]
    assert report["downgraded"] == [
        {"purl": "pkg:npm/b", "from": "2.0.0", "to": "1.9.9"},
    ]
    assert report["added"] == []
    assert report["removed"] == []


def test_diff_reports_runtime_to_development_scope_change():
    detector = make()
    detector.load("base", [comp("a", scope="runtime")], [])
    detector.load("current", [comp("a", scope="development")], [])
    report = detector.diff()
    assert report["scope_changed"] == [
        {"purl": "pkg:npm/a", "from": "runtime", "to": "development"},
    ]
    assert report["upgraded"] == []
    assert report["downgraded"] == []


def test_accept_and_revoke_manage_the_whitelist():
    detector = make()
    detector.load("base", [comp("a")], [])
    detector.load("current", [comp("a")], [])

    entry = detector.accept("pkg:NPM/A", until="2026-12-31", note="vendor ok")
    assert entry == {"purl": "pkg:npm/a", "until": "2026-12-31", "note": "vendor ok"}
    assert detector.whitelist() == [entry]

    updated = detector.accept("pkg:npm/a", until="2027-01-31")
    assert updated["until"] == "2027-01-31"
    assert len(detector.whitelist()) == 1

    assert detector.revoke("pkg:npm/a") is True
    assert detector.whitelist() == []
    assert detector.revoke("pkg:npm/a") is False

    with pytest.raises(sdd.SbomDriftError) as exc:
        detector.accept("pkg:npm/ghost")
    assert exc.value.code == "UNKNOWN_COMPONENT"


def test_explain_reports_category_and_version_bounds():
    detector = make()
    detector.load("base", [comp("a", "1.2.3"), comp("b", "1.0.0")], [])
    detector.load("current", [comp("a", "1.3.0"), comp("b", "1.0.0")], [])

    upgraded = detector.explain("pkg:npm/a")
    assert upgraded["category"] == "upgraded"
    assert upgraded["from"] == "1.2.3"
    assert upgraded["to"] == "1.3.0"
    assert upgraded["scope_from"] == "runtime"
    assert upgraded["scope_to"] == "runtime"

    assert detector.explain("pkg:npm/b")["category"] == "unchanged"

    with pytest.raises(sdd.SbomDriftError) as exc:
        detector.explain("pkg:npm/ghost")
    assert exc.value.code == "UNKNOWN_COMPONENT"


def test_snapshot_restore_keeps_graph_and_rejects_corrupt_state():
    detector = make()
    detector.load("base", [comp("a", "1.2.3")], [])
    detector.load("current", [comp("a", "1.3.0")], [])
    detector.accept("pkg:npm/a", until="2026-12-31")
    expected = detector.diff()
    state = detector.snapshot()

    clone = make()
    clone.restore(state)
    assert clone.diff() == expected
    assert clone.whitelist() == detector.whitelist()
    assert clone.stats() == detector.stats()

    broken = detector.snapshot()
    broken["version"] = 99
    other = make()
    with pytest.raises(sdd.SbomDriftError) as exc:
        other.restore(broken)
    assert exc.value.code == "SBOM_SNAPSHOT_INVALID"
    assert other.whitelist() == []
    assert other.components("base") == []
    assert detector.diff() == expected


def test_concurrent_diff_and_accept_stay_consistent():
    detector = make()
    base = [comp("pkg%d" % item) for item in range(20)]
    detector.load("base", base, [])
    detector.load("current", [comp("pkg%d" % item) for item in range(20)], [])
    errors = []

    def writer(number):
        try:
            for offset in range(5):
                index = number * 5 + offset
                detector.accept("pkg:npm/pkg%d" % index, until="2026-12-31")
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(10):
                assert detector.diff() == empty_diff()
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(item,)) for item in range(4)]
    threads.extend(threading.Thread(target=reader) for _ in range(3))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(detector.whitelist()) == 20
    assert detector.stats()["whitelisted"] == 20


def test_stats_reports_loaded_sizes_and_reset_clears_diff_counter():
    detector = make()
    detector.load("base", [comp("a"), comp("b")], [edge("a", "b")])
    detector.load("current", [comp("a"), comp("b")], [edge("a", "b")])
    detector.diff()
    detector.accept("pkg:npm/a")
    assert detector.stats() == {
        "base_components": 2,
        "current_components": 2,
        "edges": 1,
        "whitelisted": 1,
        "diffs": 1,
    }
    detector.reset_stats()
    assert detector.stats() == {
        "base_components": 2,
        "current_components": 2,
        "edges": 1,
        "whitelisted": 1,
        "diffs": 0,
    }
