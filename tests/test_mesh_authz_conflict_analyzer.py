import copy
import threading

import pytest

import mesh_authz_conflict_analyzer as ma

PRINCIPAL = "spiffe://cluster.local/ns/payments/sa/checkout"


def policy(pid, effect="ALLOW", principals=("*",), methods=("GET",),
           paths=("/api/*",), ports=(443,)):
    return {
        "id": pid,
        "effect": effect,
        "principals": list(principals),
        "methods": list(methods),
        "paths": list(paths),
        "ports": list(ports),
    }


def request(**overrides):
    base = {
        "principal": PRINCIPAL,
        "method": "GET",
        "path": "/api/v1/items",
        "port": 443,
    }
    base.update(overrides)
    return base


def analyzer_with(policies, revision=1):
    analyzer = ma.MeshAuthzConflictAnalyzer()
    analyzer.load(revision, policies)
    return analyzer


def test_load_reports_counts_and_revisions():
    analyzer = analyzer_with([policy("a"), policy("d", effect="DENY")])
    assert analyzer.revisions() == [1]
    assert len(analyzer.policies()) == 2
    assert analyzer.load(2, []) == {"revision": 2, "policies": 0}
    assert analyzer.revisions() == [1, 2]


def test_revision_must_increase_strictly():
    analyzer = analyzer_with([policy("a")])
    with pytest.raises(ma.MeshAuthzError) as same:
        analyzer.load(1, [policy("b")])
    assert same.value.code == "AUTHZ_REVISION_STALE"
    with pytest.raises(ma.MeshAuthzError) as back:
        analyzer.load(0, [policy("b")])
    assert back.value.code == "AUTHZ_INVALID_REVISION"
    assert analyzer.revisions() == [1]


def test_invalid_revision_and_policy_shape_rejected():
    analyzer = ma.MeshAuthzConflictAnalyzer()
    with pytest.raises(ma.MeshAuthzError) as zero:
        analyzer.load(0, [])
    assert zero.value.code == "AUTHZ_INVALID_REVISION"
    with pytest.raises(ma.MeshAuthzError) as text:
        analyzer.load("one", [])
    assert text.value.code == "AUTHZ_INVALID_REVISION"

    bad_policies = [
        policy("a", effect="PERMIT"),
        policy("a", paths=("api",)),
        policy("a", ports=(70000,)),
        policy("a", principals=()),
    ]
    for bad in bad_policies:
        with pytest.raises(ma.MeshAuthzError) as exc:
            analyzer.load(1, [bad])
        assert exc.value.code == "AUTHZ_INVALID_POLICY"
    assert analyzer.revisions() == []
    analyzer.load(1, [policy("a")])
    assert analyzer.revisions() == [1]


def test_duplicate_policy_id_rejected():
    analyzer = ma.MeshAuthzConflictAnalyzer()
    with pytest.raises(ma.MeshAuthzError) as exc:
        analyzer.load(1, [policy("a"), policy("a", effect="DENY")])
    assert exc.value.code == "AUTHZ_INVALID_POLICY"
    assert analyzer.revisions() == []


def test_match_allow_deny_and_default():
    analyzer = analyzer_with([policy("a")])
    assert analyzer.match(request()) == "allow"
    assert analyzer.match(request(path="/other/place")) == "default_deny"
    analyzer.load(2, [policy("a"), policy("d", effect="DENY")])
    assert analyzer.match(request()) == "deny"
    assert analyzer.match(request(path="/other/place")) == "default_deny"


def test_method_and_port_wildcards():
    analyzer = analyzer_with([policy("a", methods=("*",), ports=("*",))])
    assert analyzer.match(request(method="POST", port=8443)) == "allow"
    strict = analyzer_with([policy("a", methods=("GET",), ports=(443,))], revision=1)
    assert strict.match(request(method="POST")) == "default_deny"
    assert strict.match(request(port=8443)) == "default_deny"


def test_path_matching_uses_segments():
    prefix = analyzer_with([policy("a", paths=("/api/*",))])
    assert prefix.match(request(path="/api")) == "allow"
    assert prefix.match(request(path="/api/v1/items")) == "allow"
    assert prefix.match(request(path="/apix/v1")) == "default_deny"
    exact = analyzer_with([policy("a", paths=("/api/v1",))])
    assert exact.match(request(path="/api/v1")) == "allow"
    assert exact.match(request(path="/api/v1/items")) == "default_deny"


def test_invalid_request_and_path_rejected():
    analyzer = analyzer_with([policy("a")])
    with pytest.raises(ma.MeshAuthzError) as missing:
        analyzer.match({})
    assert missing.value.code == "AUTHZ_REQUEST_INVALID"
    with pytest.raises(ma.MeshAuthzError) as bad_port:
        analyzer.match(request(port="443"))
    assert bad_port.value.code == "AUTHZ_REQUEST_INVALID"
    with pytest.raises(ma.MeshAuthzError) as bad_path:
        analyzer.match(request(path="api/v1"))
    assert bad_path.value.code == "AUTHZ_PATH_INVALID"
    assert analyzer.path_segments("/") == []
    assert analyzer.path_segments("/api/v1/items") == ["api", "v1", "items"]
    with pytest.raises(ma.MeshAuthzError) as bare:
        analyzer.path_segments("api")
    assert bare.value.code == "AUTHZ_PATH_INVALID"


def test_analyze_disjoint_rules_report_no_conflicts():
    analyzer = analyzer_with([
        policy("allow-https", ports=(443,)),
        policy("deny-admin", effect="DENY", ports=(8443,), paths=("/admin/*",)),
    ])
    expected = {
        "revision": 1,
        "allow_count": 1,
        "deny_count": 1,
        "fully_shadowed": [],
        "weak_shadowed": [],
        "conflicts": [],
    }
    assert analyzer.analyze() == expected
    assert analyzer.analyze(revision=1) == expected


def test_explain_returns_normalised_domain():
    analyzer = analyzer_with([policy("a", paths=("/api/*", "/admin/*"),
                                     methods=("POST", "GET"), ports=(8443, 443))])
    info = analyzer.explain("a")
    assert info["id"] == "a"
    assert info["effect"] == "ALLOW"
    assert info["methods"] == ["GET", "POST"]
    assert info["ports"] == [443, 8443]
    assert set(info) >= {"principals", "paths"}
    with pytest.raises(ma.MeshAuthzError) as unknown:
        analyzer.explain("missing")
    assert unknown.value.code == "AUTHZ_POLICY_UNKNOWN"


def test_diff_reports_added_removed_and_changed_ids():
    analyzer = analyzer_with([policy("a", ports=(443,)), policy("b", ports=(8443,))])
    analyzer.load(2, [policy("b", ports=(9443,)), policy("c")])
    result = analyzer.diff(1, 2)
    assert result["from"] == 1
    assert result["to"] == 2
    assert result["added"] == ["c"]
    assert result["removed"] == ["a"]
    assert result["changed"] == ["b"]


def test_diff_unknown_revision_and_identical_revisions():
    analyzer = analyzer_with([policy("a")])
    with pytest.raises(ma.MeshAuthzError) as unknown:
        analyzer.diff(1, 9)
    assert unknown.value.code == "AUTHZ_REVISION_UNKNOWN"
    same = analyzer.diff(1, 1)
    assert same["added"] == []
    assert same["removed"] == []
    assert same["changed"] == []


def test_snapshot_restore_keeps_revisions_and_decisions():
    analyzer = analyzer_with([policy("a")])
    state = analyzer.snapshot()
    assert state["version"] == 1
    assert set(state["revisions"]) == {"1"}
    clone = ma.MeshAuthzConflictAnalyzer()
    clone.restore(copy.deepcopy(state))
    assert clone.revisions() == [1]
    assert clone.match(request()) == "allow"
    state["revisions"].clear()
    assert analyzer.match(request()) == "allow"
    assert analyzer.revisions() == [1]


def test_restore_rejects_malformed_state():
    analyzer = analyzer_with([policy("a")])
    bad_states = [
        None,
        {},
        {"version": 2, "revisions": {}, "stats": {}},
        {"version": 1, "revisions": [], "stats": {}},
        {"version": 1, "revisions": {"1": "nope"}, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(ma.MeshAuthzError) as exc:
            analyzer.restore(bad)
        assert exc.value.code == "AUTHZ_SNAPSHOT_INVALID"
    assert analyzer.match(request()) == "allow"


def test_concurrent_match_and_load_are_serialised():
    analyzer = analyzer_with([policy("a")])
    errors = []
    results = []

    def worker():
        try:
            for _ in range(20):
                results.append(analyzer.match(request()))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    analyzer.load(2, [policy("a"), policy("audit", effect="DENY", paths=("/audit/*",))])
    for thread in threads:
        thread.join()

    assert errors == []
    assert results == ["allow"] * 120
    assert analyzer.revisions() == [1, 2]
    assert analyzer.stats()["matches"] == 120
