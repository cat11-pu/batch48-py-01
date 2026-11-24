import threading

import pytest

import finding_clusterer as fc


def make(**kwargs):
    return fc.FindingClusterer(**kwargs)


def finding(path="a.py", line=1, rule="R-1", sink="sql.query", source="http",
            fid=None, **extra):
    item = {
        "rule_id": rule,
        "path": path,
        "line": line,
        "sink": sink,
        "source": source,
    }
    item.update(extra)
    if fid is not None:
        item["id"] = fid
    return item


def test_add_assigns_finding_identifiers():
    clusterer = make()
    assert clusterer.add(finding()) == "f-000001"
    assert clusterer.add(finding(path="b.py", line=7)) == "f-000002"
    listed = clusterer.findings()
    assert [item["id"] for item in listed] == ["f-000001", "f-000002"]
    assert listed[1]["path"] == "b.py"
    assert listed[1]["line"] == 7


def test_add_rejects_malformed_findings():
    clusterer = make()
    broken = dict(finding())
    del broken["line"]
    with pytest.raises(fc.ClusterError) as missing:
        clusterer.add(broken)
    assert missing.value.code == "FINDING_INVALID"

    with pytest.raises(fc.ClusterError) as line:
        clusterer.add(finding(line=0))
    assert line.value.code == "FINDING_INVALID"

    with pytest.raises(fc.ClusterError) as sink:
        clusterer.add(finding(sink=""))
    assert sink.value.code == "FINDING_INVALID"

    assert clusterer.findings() == []


def test_duplicate_finding_identifier_is_refused():
    clusterer = make()
    clusterer.add(finding(fid="dup-1"))
    with pytest.raises(fc.ClusterError) as exc:
        clusterer.add(finding(path="other.py", fid="dup-1"))
    assert exc.value.code == "FINDING_DUPLICATE"
    assert len(clusterer.findings()) == 1


def test_finding_capacity_is_a_hard_top():
    clusterer = make(max_findings=2)
    clusterer.add(finding())
    clusterer.add(finding(path="b.py"))
    with pytest.raises(fc.ClusterError) as exc:
        clusterer.add(finding(path="c.py"))
    assert exc.value.code == "FINDING_CAPACITY_EXCEEDED"
    assert clusterer.stats()["findings"] == 2


def test_cluster_groups_by_root_key_and_orders_members():
    clusterer = make()
    clusterer.add(finding(path="b.py", line=5, fid="f-c"))
    clusterer.add(finding(path="a.py", line=9, fid="f-a", entry="route_x"))
    clusterer.add(finding(path="a.py", line=2, fid="f-b", entry="route_y"))
    clusters = clusterer.cluster()
    assert len(clusters) == 1
    assert clusters[0]["roots"] == ["R-1|sql.query|http"]
    assert clusters[0]["members"] == ["f-b", "f-a", "f-c"]
    assert clusters[0]["overflow"] is False
    assert clusterer.members(clusters[0]["id"]) == ["f-b", "f-a", "f-c"]


def test_cluster_separates_sink_and_source_and_sorts_roots():
    clusterer = make()
    clusterer.add(finding(path="a.py", sink="sql.query", source="http"))
    clusterer.add(finding(path="b.py", sink="shell", source="http"))
    clusterer.add(finding(path="c.py", sink="sql.query", source="socket"))
    clusters = clusterer.cluster()
    assert [item["roots"] for item in clusters] == [
        ["R-1|shell|http"],
        ["R-1|sql.query|http"],
        ["R-1|sql.query|socket"],
    ]
    assert len({item["id"] for item in clusters}) == 3


def test_merge_combines_members_and_retires_second_identifier():
    clusterer = make()
    clusterer.add(finding(path="c.py", sink="sql.query", fid="f-3"))
    clusterer.add(finding(path="a.py", sink="shell", fid="f-1"))
    clusterer.add(finding(path="b.py", sink="sql.query", fid="f-2"))
    clusters = clusterer.cluster()
    first, second = clusters[0]["id"], clusters[1]["id"]

    merged = clusterer.merge(first, second)
    assert merged == first
    assert clusterer.members(first) == ["f-1", "f-2", "f-3"]
    assert clusterer.explain(first)["origin"] == "merge"
    assert clusterer.stats()["merges"] == 1
    with pytest.raises(fc.ClusterError) as exc:
        clusterer.members(second)
    assert exc.value.code == "CLUSTER_UNKNOWN"


def test_merge_rejects_self_and_unknown_clusters():
    clusterer = make()
    clusterer.add(finding())
    cluster_id = clusterer.cluster()[0]["id"]
    with pytest.raises(fc.ClusterError) as same:
        clusterer.merge(cluster_id, cluster_id)
    assert same.value.code == "MERGE_INVALID"
    with pytest.raises(fc.ClusterError) as unknown:
        clusterer.merge("rk-missing", cluster_id)
    assert unknown.value.code == "CLUSTER_UNKNOWN"
    assert clusterer.members(cluster_id) == ["f-000001"]


def test_split_moves_members_into_a_new_cluster():
    clusterer = make()
    for index in range(4):
        clusterer.add(finding(path="p%d.py" % index, fid="f-%d" % (index + 1)))
    cluster_id = clusterer.cluster()[0]["id"]

    split_id = clusterer.split(cluster_id, ["f-2", "f-4"])
    assert split_id != cluster_id
    assert clusterer.members(cluster_id) == ["f-1", "f-3"]
    assert clusterer.members(split_id) == ["f-2", "f-4"]
    assert clusterer.explain(split_id)["origin"] == "split"
    assert clusterer.stats()["splits"] == 1


def test_split_rejects_foreign_or_empty_member_lists():
    clusterer = make()
    clusterer.add(finding(path="a.py", sink="sql.query"))
    clusterer.add(finding(path="b.py", sink="shell"))
    first, second = [item["id"] for item in clusterer.cluster()]

    with pytest.raises(fc.ClusterError) as empty:
        clusterer.split(first, [])
    assert empty.value.code == "SPLIT_INVALID"

    with pytest.raises(fc.ClusterError) as foreign:
        clusterer.split(first, ["f-000001"])
    assert foreign.value.code == "SPLIT_INVALID"
    assert len(clusterer.members(first)) == 1
    assert len(clusterer.members(second)) == 1

    with pytest.raises(fc.ClusterError) as unknown:
        clusterer.split("rk-missing", ["f-000001"])
    assert unknown.value.code == "CLUSTER_UNKNOWN"


def test_oversized_cluster_is_capped_and_flagged():
    clusterer = make(max_members=3)
    for index in range(5):
        clusterer.add(finding(path="p%d.py" % index, fid="f-%d" % (index + 1)))
    clusters = clusterer.cluster()
    assert len(clusters) == 1
    assert clusters[0]["overflow"] is True
    assert len(clusters[0]["members"]) == 3
    assert clusterer.stats()["findings"] == 5


def test_stats_reports_rule_counts_and_reset_keeps_clusters():
    clusterer = make()
    clusterer.add(finding(rule="R-1"))
    clusterer.add(finding(rule="R-1", path="b.py", line=2))
    clusterer.add(finding(rule="R-2", path="c.py", sink="shell"))
    clusterer.cluster()
    counters = clusterer.stats()
    assert counters == {
        "findings": 3,
        "clusters": 2,
        "by_rule": {"R-1": 1, "R-2": 1},
        "merges": 0,
        "splits": 0,
    }
    clusterer.reset_stats()
    assert clusterer.stats() == {
        "findings": 3,
        "clusters": 2,
        "by_rule": {"R-1": 1, "R-2": 1},
        "merges": 0,
        "splits": 0,
    }


def test_snapshot_restore_keeps_clusters_and_merge_trace():
    clusterer = make()
    clusterer.add(finding(path="a.py", sink="shell", fid="f-1"))
    clusterer.add(finding(path="b.py", sink="sql.query", fid="f-2"))
    clusterer.add(finding(path="c.py", sink="sql.query", fid="f-3"))
    clusters = clusterer.cluster()
    merged = clusterer.merge(clusters[0]["id"], clusters[1]["id"])
    state = clusterer.snapshot()

    clone = make()
    clone.restore(state)
    assert clone.cluster() == clusterer.cluster()
    assert clone.members(merged) == ["f-1", "f-2", "f-3"]
    assert clone.stats() == clusterer.stats()
    state["findings"][0]["path"] = "tampered.py"
    assert clone.findings()[0]["path"] == "a.py"


def test_corrupt_snapshot_is_rejected_without_state_change():
    clusterer = make()
    clusterer.add(finding())
    state = clusterer.snapshot()
    state["version"] = 99

    other = make()
    with pytest.raises(fc.ClusterError) as exc:
        other.restore(state)
    assert exc.value.code == "SNAPSHOT_INVALID"
    assert other.findings() == []
    assert len(clusterer.cluster()) == 1


def test_concurrent_add_and_cluster_never_duplicates_members():
    clusterer = make()
    errors = []

    def writer(number):
        try:
            for offset in range(30):
                clusterer.add(finding(
                    path="w%d/p%d.py" % (number, offset),
                    line=offset + 1,
                ))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(15):
                for item in clusterer.cluster():
                    members = item["members"]
                    assert len(members) == len(set(members))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(item,)) for item in range(4)]
    threads.extend(threading.Thread(target=reader) for _ in range(2))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    clusters = clusterer.cluster()
    assert len(clusters) == 1
    assert len(clusters[0]["members"]) == 120
    assert len(set(clusters[0]["members"])) == 120
