import threading

import pytest

import restart_supervisor as rs


def make(**kwargs):
    return rs.RestartSupervisor(**kwargs)


def test_report_exit_backoff_doubles():
    sup = make()
    first = sup.report_exit("c1", 1, at=1000)
    assert first["container_id"] == "c1"
    assert first["code"] == 1
    assert first["round"] == 1
    assert first["delay"] == 1.0
    assert first["next_at"] == 1001.0
    assert first["state"] == "backoff"

    second = sup.report_exit("c1", 1, at=1001)
    assert second["round"] == 2
    assert second["delay"] == 2.0
    assert second["next_at"] == 1003.0

    third = sup.report_exit("c1", 1, at=1003)
    assert third["round"] == 3
    assert third["delay"] == 4.0
    assert third["next_at"] == 1007.0


def test_backoff_capped_at_max_delay():
    sup = make(max_delay=5.0)
    assert sup.report_exit("c1", 1, at=1000)["delay"] == 1.0
    assert sup.report_exit("c1", 1, at=1001)["delay"] == 2.0
    assert sup.report_exit("c1", 1, at=1003)["delay"] == 4.0
    assert sup.report_exit("c1", 1, at=1007)["delay"] == 5.0
    assert sup.report_exit("c1", 1, at=1012)["delay"] == 5.0


def test_oom_exit_uses_longer_delay():
    sup = make()
    oom = sup.report_exit("c1", 137, at=1000)
    assert oom["delay"] == 2.0
    assert oom["next_at"] == 1002.0

    plain = make()
    assert plain.report_exit("c2", 1, at=1000)["delay"] == 1.0
    assert plain.report_exit("c2", 1, at=1001)["delay"] == 2.0


def test_next_delay_clamped_on_clock_jump():
    sup = make()
    sup.report_exit("c1", 1, at=1000)

    assert sup.next_delay("c1", now=900)["delay"] == 0.0
    assert sup.next_delay("c1", now=1000)["delay"] == 1.0
    assert sup.next_delay("c1", now=1000.5)["delay"] == 0.5
    assert sup.next_delay("c1", now=1002)["delay"] == 0.0


def test_mark_stable_requires_full_uptime():
    sup = make()
    sup.report_exit("c1", 1, at=1000)
    sup.report_exit("c1", 1, at=1001)
    assert sup.get("c1")["round"] == 2

    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.mark_stable("c1", uptime=10)
    assert exc.value.code == "RESTART_UNSTABLE"
    assert sup.get("c1")["round"] == 2

    stable = sup.mark_stable("c1", uptime=120)
    assert stable["round"] == 0
    assert stable["state"] == "stable"
    assert stable["uptime"] == 120

    idle = sup.next_delay("c1", now=1010)
    assert idle["round"] == 0
    assert idle["delay"] == 0.0

    fresh = sup.report_exit("c1", 1, at=1100)
    assert fresh["round"] == 1
    assert fresh["delay"] == 1.0


def test_allow_restart_respects_delay():
    sup = make()
    sup.report_exit("c1", 1, at=1000)

    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.allow_restart("c1", now=1000)
    assert exc.value.code == "RESTART_TOO_EARLY"

    allowed = sup.allow_restart("c1", now=1001)
    assert allowed["allowed"] is True
    assert allowed["restarts_used"] == 1
    assert allowed["remaining"] == 4

    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.allow_restart("c1", now=1001)
    assert exc.value.code == "RESTART_IN_PROGRESS"


def test_allow_restart_requires_exit_record():
    sup = make()
    sup.configure("c1")
    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.allow_restart("c1", now=0)
    assert exc.value.code == "RESTART_NOT_ARMED"

    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.next_delay("ghost", now=0)
    assert exc.value.code == "RESTART_UNKNOWN_CONTAINER"


def test_budget_exhaustion_and_window_slide():
    sup = make(budget=2, window=600.0)
    sup.report_exit("c1", 1, at=1000)
    sup.allow_restart("c1", now=1001)
    sup.report_exit("c1", 1, at=1002)
    sup.allow_restart("c1", now=1003)
    sup.report_exit("c1", 1, at=1004)

    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.allow_restart("c1", now=1005)
    assert exc.value.code == "RESTART_BUDGET_EXHAUSTED"

    current = sup.budget("c1", now=1005)
    assert current["budget"] == 2
    assert current["used"] == 2
    assert current["remaining"] == 0

    slid = sup.budget("c1", now=1601)
    assert slid["used"] == 1
    assert slid["remaining"] == 1
    assert slid["window_start"] == 1003

    allowed = sup.allow_restart("c1", now=1601)
    assert allowed["allowed"] is True
    assert allowed["restarts_used"] == 2
    assert allowed["remaining"] == 0


def test_reset_restores_budget_and_round():
    sup = make(budget=1, window=600.0)
    sup.report_exit("c1", 1, at=1000)
    sup.allow_restart("c1", now=1001)
    sup.report_exit("c1", 1, at=1002)

    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.allow_restart("c1", now=1003)
    assert exc.value.code == "RESTART_BUDGET_EXHAUSTED"

    reset = sup.reset("c1")
    assert reset["state"] == "idle"
    assert reset["round"] == 0
    assert reset["remaining"] == 1
    assert sup.budget("c1", now=1004)["used"] == 0

    sup.report_exit("c1", 1, at=1010)
    assert sup.allow_restart("c1", now=1011)["allowed"] is True


def test_concurrent_report_exit_records_once():
    sup = make()
    reports = []
    guard = threading.Lock()

    def worker():
        try:
            report = sup.report_exit("c1", 1, at=1000)
            with guard:
                reports.append(report)
        except Exception as exc:  # noqa: BLE001 - surfaced as a failed report
            with guard:
                reports.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sup.get("c1")["round"] == 1
    assert sup.stats()["exits"] == 1
    assert len([r for r in reports if isinstance(r, dict) and not r["duplicate"]]) == 1


def test_concurrent_allow_restart_single_pass():
    sup = make()
    sup.report_exit("c1", 1, at=1000)
    allowed = []
    refused = []
    guard = threading.Lock()

    def worker():
        try:
            grant = sup.allow_restart("c1", now=1001)
            with guard:
                allowed.append(grant)
        except rs.RestartSupervisorError as exc:
            with guard:
                refused.append(exc.code)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed admission
            with guard:
                refused.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(allowed) == 1
    assert len(refused) == 7
    assert all(code == "RESTART_IN_PROGRESS" for code in refused)
    assert sup.stats()["restarts"] == 1


def test_snapshot_restore_continues_backoff():
    sup = make()
    sup.report_exit("c1", 1, at=1000)
    snap = sup.snapshot()
    assert snap["version"] == 1

    restored = make()
    restored.restore(snap)

    pending = restored.next_delay("c1", now=1000)
    assert pending["round"] == 1
    assert pending["delay"] == 1.0
    with pytest.raises(rs.RestartSupervisorError) as exc:
        restored.allow_restart("c1", now=1000)
    assert exc.value.code == "RESTART_TOO_EARLY"

    assert restored.allow_restart("c1", now=1001)["allowed"] is True


def test_restore_rejects_malformed_state():
    sup = make()
    sup.report_exit("c1", 1, at=1000)
    with pytest.raises(rs.RestartSupervisorError) as exc:
        sup.restore({"version": 99})
    assert exc.value.code == "RESTART_SNAPSHOT_INVALID"
    assert sup.get("c1")["round"] == 1


def test_stats_and_configure_merges_policy():
    sup = make()
    policy = sup.configure("c1", base_delay=2.0, budget=3)
    assert policy["container_id"] == "c1"
    assert policy["base_delay"] == 2.0
    assert policy["budget"] == 3
    assert policy["factor"] == 2.0
    assert policy["stable_period"] == 120.0

    assert sup.report_exit("c1", 1, at=1000)["delay"] == 2.0
    sup.allow_restart("c1", now=1002)
    assert sup.report_exit("c1", 1, at=1003)["delay"] == 4.0
    sup.mark_stable("c1", uptime=120)

    assert sup.stats() == {
        "containers": 1,
        "exits": 2,
        "restarts": 1,
        "stable_marks": 1,
        "resets": 0,
        "budget_exhausted": 0,
    }


def test_snapshot_detached_from_live_state():
    sup = make()
    sup.report_exit("c1", 1, at=1000)

    snap = sup.snapshot()
    snap["containers"]["c1"]["round"] = 9
    assert sup.get("c1")["round"] == 1
    assert sup.get("c1")["state"] == "backoff"
