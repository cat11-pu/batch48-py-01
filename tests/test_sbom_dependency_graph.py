import threading

import pytest

import sbom_dependency_graph as sdg


def make(**kwargs):
    return sdg.SbomDependencyGraph(**kwargs)


def purl(name, version="1.0.0", type_="npm", namespace=None):
    if namespace:
        return "pkg:%s/%s/%s@%s" % (type_, namespace, name, version)
    return "pkg:%s/%s@%s" % (type_, name, version)


def test_normalize_purl_folds_case_namespace_and_qualifiers():
    graph = make()
    raw = "pkg:PyPI/Demo//Lib@1.02.0?B=2&a=1"
    assert graph.normalize_purl(raw) == "pkg:pypi/demo/lib@1.2.0?a=1&b=2"
    assert graph.normalize_purl("pkg:npm/Lodash@4.17.21") == "pkg:npm/lodash@4.17.21"
    assert graph.normalize_purl("pkg:npm/lodash@4.17.21#lib/index.js") == \
        "pkg:npm/lodash@4.17.21"
    assert graph.normalize_purl("pkg:npm/lodash@4.17.21?x=1") == \
        "pkg:npm/lodash@4.17.21?x=1"


def test_normalize_purl_rejects_malformed_input():
    graph = make()
    bad = [
        "https://example.com/lodash",
        "pkg:pypi",
        "pkg:/lib@1.0.0",
        "pkg:pypi/lib",
        "pkg:pypi/lib@",
        "pkg:pypi/lib@1.0.0?=x",
        "pkg:pypi/lib@1.0.0?x",
    ]
    for value in bad:
        with pytest.raises(sdg.SbomError) as exc:
            graph.normalize_purl(value)
        assert exc.value.code == "SBOM_INVALID_PURL"


def test_add_component_folds_originals_and_explains_them():
    graph = make()
    first = graph.add_component("pkg:PyPI/Demo/Lib@1.02.0")
    second = graph.add_component("pkg:pypi/demo/lib@1.2.0", scope="dev")
    assert first == second == "pkg:pypi/demo/lib@1.2.0"
    assert graph.explain(first) == [
        "pkg:PyPI/Demo/Lib@1.02.0",
        "pkg:pypi/demo/lib@1.2.0",
    ]
    assert graph.components() == [first]
    counters = graph.stats()
    assert counters["components"] == 1
    assert counters["originals"] == 2
    assert counters["edges"] == 0


def test_add_edge_is_idempotent_per_scope():
    graph = make()
    parent = graph.normalize_purl(purl("a"))
    child = graph.normalize_purl(purl("b"))
    other = graph.normalize_purl(purl("c"))
    assert graph.add_edge(parent, child) == (parent, child)
    graph.add_edge(parent, child, scope="runtime")
    assert graph.edges() == [(parent, child, "runtime")]

    graph.add_edge(parent, other, scope="dev")
    assert graph.edges() == [
        (parent, child, "runtime"),
        (parent, other, "dev"),
    ]
    assert graph.stats()["edges"] == 2
    assert graph.components() == [parent, child, other]


def test_node_and_edge_limits_reject_without_mutating_graph():
    graph = make(max_nodes=2, max_edges=5)
    first = graph.add_component(purl("a"))
    second = graph.add_component(purl("b"))
    with pytest.raises(sdg.SbomError) as exc:
        graph.add_component(purl("c"))
    assert exc.value.code == "SBOM_NODE_LIMIT"
    assert graph.components() == [first, second]

    loose = make(max_nodes=10)
    with pytest.raises(sdg.SbomError) as exc:
        loose.add_component(purl("d"), scope="build")
    assert exc.value.code == "SBOM_INVALID_SCOPE"
    with pytest.raises(sdg.SbomError) as exc:
        loose.add_edge(purl("a"), purl("b"), scope="build")
    assert exc.value.code == "SBOM_INVALID_SCOPE"

    narrow = make(max_nodes=10, max_edges=1)
    parent = narrow.normalize_purl(purl("a"))
    kept = narrow.add_edge(parent, purl("b"))
    with pytest.raises(sdg.SbomError) as exc:
        narrow.add_edge(parent, purl("c"))
    assert exc.value.code == "SBOM_EDGE_LIMIT"
    assert narrow.edges() == [(kept[0], kept[1], "runtime")]


def test_add_edge_rejects_cycle_and_reports_path():
    graph = make()
    a = graph.add_component(purl("a"))
    b = graph.add_component(purl("b"))
    c = graph.add_component(purl("c"))
    graph.add_edge(a, b)
    graph.add_edge(b, c)
    with pytest.raises(sdg.SbomError) as exc:
        graph.add_edge(c, a)
    assert exc.value.code == "SBOM_CYCLE_DETECTED"
    assert isinstance(exc.value.detail, list)
    assert sorted(exc.value.detail) == sorted([a, b, c])
    assert len(graph.edges()) == 2
    assert graph.stats()["edges"] == 2


def test_parse_version_strips_leading_zeros_and_rejects_bad_input():
    graph = make()
    assert graph.parse_version("1.02.3") == (1, 2, 3, ())
    assert graph.parse_version("10.0.12") == (10, 0, 12, ())
    for value in ("1.2", "", "a.b.c", "1.2.3.4", "1.2.-3", 17):
        with pytest.raises(sdg.SbomError) as exc:
            graph.parse_version(value)
        assert exc.value.code == "SBOM_INVALID_VERSION"


def test_version_in_range_handles_bounds_wildcard_and_exact():
    graph = make()
    assert graph.version_in_range("1.2.0", ">=1.2.0,<1.5.0") is True
    assert graph.version_in_range("1.4.9", ">=1.2.0,<1.5.0") is True
    assert graph.version_in_range("1.5.0", ">=1.2.0,<1.5.0") is False
    assert graph.version_in_range("1.1.9", ">=1.2.0,<1.5.0") is False
    assert graph.version_in_range("2.0.0", ">1.2.0,<=2.0.0") is True
    assert graph.version_in_range("2.0.1", ">1.2.0,<=2.0.0") is False
    assert graph.version_in_range("1.2.3", "=1.2.3") is True
    assert graph.version_in_range("1.2.4", "=1.2.3") is False
    assert graph.version_in_range("3.1.4", "3.1.*") is True
    assert graph.version_in_range("3.2.0", "3.1.*") is False
    assert graph.version_in_range("0.0.1", "*") is True


def test_version_in_range_rejects_malformed_constraint():
    graph = make()
    for constraint in ("=>1.2.0", "1.2.3.4", ">=1.2.0,,<2.0.0", "<2"):
        with pytest.raises(sdg.SbomError) as exc:
            graph.version_in_range("1.2.3", constraint)
        assert exc.value.code == "SBOM_INVALID_CONSTRAINT"


def test_affected_reports_shortest_path_and_sorts_by_purl():
    graph = make()
    a = graph.add_component(purl("a", "1.0.0"))
    b_old = graph.add_component(purl("b", "1.2.0"))
    b_new = graph.add_component(purl("b", "1.9.0"))
    c = graph.add_component(purl("c", "2.0.0"))
    d = graph.add_component(purl("d", "1.3.0"))
    graph.add_edge(a, b_old)
    graph.add_edge(a, b_new)
    graph.add_edge(b_old, c)
    graph.add_edge(b_new, c)

    result = graph.affected({"id": "CVE-1", "constraint": ">=1.2.0,<1.6.0"})
    assert [item["purl"] for item in result] == [b_old, d]
    assert result[0]["version"] == "1.2.0"
    assert result[0]["path"] == [a, b_old]
    assert result[1]["path"] == [d]
    assert result[0]["advisory"] == "CVE-1"


def test_affected_skips_dev_only_subtree_but_keeps_edges():
    graph = make()
    a = graph.add_component(purl("a", "1.0.0"))
    b = graph.add_component(purl("b", "1.0.0"))
    c = graph.add_component(purl("c", "2.0.0"), scope="dev")
    graph.add_edge(a, b, scope="runtime")
    graph.add_edge(b, c, scope="dev")

    result = graph.affected({"id": "CVE-2", "constraint": ">=2.0.0"})
    assert result == []
    assert (b, c, "dev") in graph.edges()
    assert graph.stats()["components"] == 3
    assert graph.stats()["edges"] == 2


def test_paths_to_root_lists_distinct_paths_and_roots_sorted():
    graph = make()
    a = graph.add_component(purl("a"))
    b = graph.add_component(purl("b"))
    c = graph.add_component(purl("c"))
    d = graph.add_component(purl("d"))
    graph.add_edge(a, b)
    graph.add_edge(a, c)
    graph.add_edge(b, d)
    graph.add_edge(c, d)

    assert graph.paths_to_root(d) == [[a, b, d], [a, c, d]]
    assert graph.paths_to_root(a) == [[a]]
    assert graph.roots() == [a]
    assert graph.paths_to_root(purl("zzz", "9.9.9")) == []


def test_snapshot_restore_keeps_graph_and_originals():
    graph = make()
    a = graph.add_component("pkg:PyPI/Demo/Lib@1.02.0")
    b = graph.add_component(purl("b"))
    graph.add_edge(a, b)
    graph.add_component("pkg:pypi/demo/lib@1.2.0")

    state = graph.snapshot()
    assert state["version"] == 1
    clone = make()
    clone.restore(state)
    assert clone.stats() == graph.stats()
    assert clone.edges() == graph.edges()
    assert clone.explain(a) == graph.explain(a)
    assert [item["purl"] for item in clone.affected(
        {"id": "CVE-3", "constraint": ">=1.0.0"})] == [a, b]

    state["edges"].clear()
    state["components"].clear()
    assert len(clone.edges()) == 1
    assert clone.components() == [a, b]


def test_corrupt_snapshot_is_rejected_without_state_change():
    graph = make()
    a = graph.add_component(purl("a"))
    b = graph.add_component(purl("b"))
    graph.add_edge(a, b)
    before = graph.stats()

    for bad in (
        None,
        {},
        {"version": 2, "components": {}, "edges": [], "originals": {}},
        {"version": 1, "components": [], "edges": [], "originals": {}},
        {"version": 1, "components": {}, "edges": "nope", "originals": {}},
        {"version": 1, "components": {}, "edges": [], "originals": []},
    ):
        with pytest.raises(sdg.SbomError) as exc:
            graph.restore(bad)
        assert exc.value.code == "SBOM_SNAPSHOT_INVALID"
    assert graph.stats() == before
    assert graph.edges() == [(a, b, "runtime")]


def test_concurrent_edge_adds_and_reads_stay_consistent():
    graph = make()
    errors = []

    def writer(index):
        try:
            parent = graph.add_component(purl("root-%d" % index))
            for offset in range(4):
                child = purl("leaf-%d-%d" % (index, offset))
                graph.add_edge(parent, child)
                graph.roots()
                graph.affected({"id": "CVE-9", "constraint": ">=9.0.0"})
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    counters = graph.stats()
    assert counters["components"] == 30
    assert counters["edges"] == 24
    assert len(graph.edges()) == 24
    assert len(graph.roots()) == 6
