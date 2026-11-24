import copy
import threading

import pytest

import mptcp_scheduler as mp


def make_guard(**kwargs):
    return mp.MptcpScheduler(**kwargs)


def test_add_subflow_assigns_roles_and_rejects_duplicates():
    guard = make_guard()
    assert guard.add_subflow("s1", path_id=1) == {
        "sid": "s1", "path_id": 1, "role": "primary", "state": "active",
    }
    assert guard.add_subflow("s2") == {
        "sid": "s2", "path_id": None, "role": "backup", "state": "standby",
    }

    with pytest.raises(mp.MptcpError) as exc:
        guard.add_subflow("s1", path_id=9)
    assert exc.value.code == "MPTCP_DUPLICATE_SUBFLOW"

    with pytest.raises(mp.MptcpError) as exc:
        guard.add_subflow("")
    assert exc.value.code == "MPTCP_INVALID_ARGUMENT"


def test_subflow_count_is_capped():
    guard = make_guard(max_subflows=2)
    guard.add_subflow("s1")
    guard.add_subflow("s2")
    with pytest.raises(mp.MptcpError) as exc:
        guard.add_subflow("s3")
    assert exc.value.code == "MPTCP_SUBFLOW_LIMIT"
    assert [item["sid"] for item in guard.list_subflows()] == ["s1", "s2"]


def test_list_subflows_is_sorted_with_all_fields():
    guard = make_guard()
    guard.add_subflow("s2", path_id=2)
    guard.add_subflow("s1", path_id=1)
    guard.report_path("s2", ok=True, rtt_ms=55, loss=0.01)
    guard.set_cwnd("s2", 900)
    guard.schedule(100)

    assert guard.list_subflows() == [
        {"sid": "s1", "path_id": 1, "role": "backup", "state": "standby",
         "cwnd": 1000, "rtt_ms": 100, "inflight": 0},
        {"sid": "s2", "path_id": 2, "role": "primary", "state": "active",
         "cwnd": 900, "rtt_ms": 55, "inflight": 100},
    ]


def test_schedule_prefers_lowest_rtt_and_breaks_ties_by_sid():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")
    guard.add_subflow("s3")
    for _ in range(3):
        guard.report_path("s1", ok=False)
    guard.enable_backup("s2")
    guard.enable_backup("s3")

    guard.report_path("s2", ok=True, rtt_ms=80)
    guard.report_path("s3", ok=True, rtt_ms=40)
    assert guard.schedule(100) == {"size": 100, "sid": "s3", "inflight": 100}

    guard.report_path("s2", ok=True, rtt_ms=60)
    guard.report_path("s3", ok=True, rtt_ms=60)
    assert guard.schedule(50) == {"size": 50, "sid": "s2", "inflight": 50}


def test_failed_and_standby_subflows_are_not_schedulable():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")

    assert guard.report_path("s1", ok=False, rtt_ms=5) == {
        "sid": "s1", "rtt_ms": 5, "loss": 0.0, "fails": 1, "state": "active",
    }
    guard.report_path("s1", ok=False, rtt_ms=6)
    failed = guard.report_path("s1", ok=False, rtt_ms=7)
    assert failed["fails"] == 3
    assert failed["state"] == "failed"

    with pytest.raises(mp.MptcpError) as exc:
        guard.schedule(10)
    assert exc.value.code == "MPTCP_NO_SUBFLOW"

    recovered = guard.report_path("s1", ok=True, rtt_ms=9)
    assert recovered == {"sid": "s1", "rtt_ms": 9, "loss": 0.0,
                         "fails": 0, "state": "active"}
    assert guard.schedule(10)["sid"] == "s1"

    with pytest.raises(mp.MptcpError) as exc:
        guard.report_path("missing", ok=True)
    assert exc.value.code == "MPTCP_UNKNOWN_SUBFLOW"


def test_cwnd_exhaustion_and_ack_release_window():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")
    assert guard.set_cwnd("s1", 300) == {"sid": "s1", "cwnd": 300}

    assert guard.schedule(200) == {"size": 200, "sid": "s1", "inflight": 200}
    with pytest.raises(mp.MptcpError) as exc:
        guard.schedule(200)
    assert exc.value.code == "MPTCP_CWND_EXHAUSTED"

    assert guard.ack("s1", 200) == {"subflow": "s1", "inflight": 0, "acked": 200}
    assert guard.schedule(200)["inflight"] == 200

    with pytest.raises(mp.MptcpError) as exc:
        guard.set_cwnd("s1", 0)
    assert exc.value.code == "MPTCP_INVALID_ARGUMENT"
    with pytest.raises(mp.MptcpError) as exc:
        guard.schedule(0)
    assert exc.value.code == "MPTCP_INVALID_ARGUMENT"


def test_enable_backup_requires_failed_primary_and_standby_backup():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")

    with pytest.raises(mp.MptcpError) as exc:
        guard.enable_backup("s2")
    assert exc.value.code == "MPTCP_BACKUP_NOT_ELIGIBLE"

    with pytest.raises(mp.MptcpError) as exc:
        guard.enable_backup("s1")
    assert exc.value.code == "MPTCP_INVALID_ROLE"

    for _ in range(3):
        guard.report_path("s1", ok=False)
    assert guard.enable_backup("s2", now_ms=500) == {
        "sid": "s2", "state": "backup_active",
    }
    assert guard.schedule(30) == {"size": 30, "sid": "s2", "inflight": 30}

    with pytest.raises(mp.MptcpError) as exc:
        guard.enable_backup("s2", now_ms=600)
    assert exc.value.code == "MPTCP_BACKUP_NOT_ELIGIBLE"


def test_dss_map_reports_delivered_ranges_across_subflows():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")

    assert guard.dss_map(0, 100, "s1") == {
        "seq": 0, "length": 100, "subflow": "s1",
        "delivered": 100, "ranges": [[0, 100]],
    }
    assert guard.dss_map(100, 50, "s2") == {
        "seq": 100, "length": 50, "subflow": "s2",
        "delivered": 50, "ranges": [[0, 150]],
    }
    assert guard.dss_map(300, 40, "s2") == {
        "seq": 300, "length": 40, "subflow": "s2",
        "delivered": 40, "ranges": [[0, 150], [300, 340]],
    }
    stats = guard.stats()
    assert stats["delivered_bytes"] == 190
    assert stats["mappings"] == 3


def test_dss_map_rejects_bad_length_and_unknown_subflow():
    guard = make_guard()
    guard.add_subflow("s1")

    with pytest.raises(mp.MptcpError) as exc:
        guard.dss_map(0, 0, "s1")
    assert exc.value.code == "MPTCP_INVALID_MAPPING"

    with pytest.raises(mp.MptcpError) as exc:
        guard.dss_map(0, 10, "missing")
    assert exc.value.code == "MPTCP_UNKNOWN_SUBFLOW"
    assert guard.stats()["mappings"] == 0


def test_mapping_table_is_capped():
    guard = make_guard(max_mappings=3)
    guard.add_subflow("s1")
    for index in range(3):
        guard.dss_map(index * 10, 10, "s1")

    with pytest.raises(mp.MptcpError) as exc:
        guard.dss_map(100, 10, "s1")
    assert exc.value.code == "MPTCP_MAPPING_LIMIT"
    assert guard.stats()["mappings"] == 3


def test_remove_subflow_reports_released_window():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")

    assert guard.remove_subflow("s2") == {
        "sid": "s2", "inflight_released": 0,
    }
    assert [item["sid"] for item in guard.list_subflows()] == ["s1"]

    with pytest.raises(mp.MptcpError) as exc:
        guard.remove_subflow("s2")
    assert exc.value.code == "MPTCP_UNKNOWN_SUBFLOW"
    assert guard.stats()["removed"] == 1


def test_snapshot_is_deep_copy_and_rejects_malformed_restore():
    guard = make_guard()
    guard.add_subflow("s1", path_id=1)
    guard.add_subflow("s2")
    guard.set_cwnd("s1", 500)

    snap = guard.snapshot()
    assert set(snap) == {"version", "subflows", "mappings", "ranges", "stats"}
    assert snap["version"] == 1

    snap["subflows"]["s1"]["cwnd"] = 1
    snap["stats"]["scheduled"] = 99
    snap["ranges"].append([0, 999])
    assert guard.list_subflows()[0]["cwnd"] == 500
    assert guard.stats()["scheduled"] == 0
    assert guard.dss_map(0, 10, "s1")["ranges"] == [[0, 10]]

    bad_states = [
        {"version": 2},
        {"version": 1},
        {"version": 1, "subflows": [], "mappings": [],
         "ranges": [], "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(mp.MptcpError) as exc:
            guard.restore(bad)
        assert exc.value.code == "MPTCP_SNAPSHOT_INVALID"
    assert guard.list_subflows()[0]["cwnd"] == 500


def test_restore_preserves_subflow_state_and_delivery():
    guard = make_guard()
    guard.add_subflow("s1", path_id=1)
    guard.add_subflow("s2")
    guard.report_path("s1", ok=True, rtt_ms=30)
    guard.set_cwnd("s1", 700)
    guard.schedule(100)
    guard.dss_map(0, 80, "s1")
    frozen = guard.stats()
    snap = copy.deepcopy(guard.snapshot())

    revived = make_guard()
    revived.restore(snap)
    assert revived.stats() == frozen
    assert revived.list_subflows() == guard.list_subflows()

    assert revived.dss_map(80, 20, "s1") == {
        "seq": 80, "length": 20, "subflow": "s1",
        "delivered": 20, "ranges": [[0, 100]],
    }
    assert revived.schedule(200) == {"size": 200, "sid": "s1", "inflight": 300}


def test_stats_and_reset():
    guard = make_guard()
    guard.add_subflow("s1")
    guard.add_subflow("s2")
    for _ in range(3):
        guard.report_path("s1", ok=False)
    guard.enable_backup("s2")
    guard.schedule(100)
    guard.ack("s2", 40)
    guard.dss_map(0, 100, "s2")

    assert guard.stats() == {
        "subflows": 2, "scheduled": 1, "acked_bytes": 40,
        "delivered_bytes": 100, "mappings": 1, "removed": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "subflows": 2, "scheduled": 0, "acked_bytes": 0,
        "delivered_bytes": 0, "mappings": 0, "removed": 0,
    }


def test_concurrent_disjoint_mappings_are_merged_once():
    guard = make_guard()
    guard.add_subflow("s1")
    errors = []
    barrier = threading.Barrier(8)

    def worker(index):
        try:
            barrier.wait(timeout=10)
            for round_no in range(20):
                guard.dss_map(index * 200 + round_no * 10, 10, "s1")
        except Exception as exc:  # noqa: BLE001 - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    stats = guard.stats()
    assert stats["mappings"] == 160
    assert stats["delivered_bytes"] == 1600
    assert guard.dss_map(5000, 1, "s1")["ranges"] == [[0, 1600], [5000, 5001]]
