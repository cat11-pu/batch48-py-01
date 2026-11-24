import threading

import pytest

import incremental_scan_planner as isp


def make(**kwargs):
    return isp.IncrementalScanPlanner(**kwargs)


def chain(planner, *paths):
    for index in range(len(paths) - 1):
        planner.add_dependency(paths[index], paths[index + 1])


def test_update_reports_real_digest_changes():
    planner = make()
    assert planner.update("a.py", "h1") is True
    assert planner.update("a.py", "h1") is False
    assert planner.update("a.py", "h2") is True
    assert planner.files() == [{"path": "a.py", "digest": "h2", "deleted": False}]
    assert planner.stats()["dirty"] == 1


def test_update_validates_path_and_digest():
    planner = make()
    with pytest.raises(isp.PlannerError) as path:
        planner.update("", "h1")
    assert path.value.code == "PLAN_INVALID_PATH"

    with pytest.raises(isp.PlannerError) as digest:
        planner.update("a.py", "")
    assert digest.value.code == "PLAN_INVALID_DIGEST"

    assert planner.files() == []
    assert planner.stats()["files"] == 0


def test_add_dependency_is_idempotent_and_directional():
    planner = make()
    assert planner.add_dependency("app.py", "lib.py") == ("app.py", "lib.py")
    planner.add_dependency("app.py", "lib.py")
    assert planner.stats()["edges"] == 1
    assert planner.dependencies("app.py") == ["lib.py"]
    assert planner.dependencies("lib.py") == []
    assert planner.dependents("lib.py") == ["app.py"]
    assert planner.dependents("app.py") == []


def test_edge_capacity_is_a_hard_top():
    planner = make(max_edges=1)
    planner.add_dependency("a.py", "b.py")
    with pytest.raises(isp.PlannerError) as exc:
        planner.add_dependency("c.py", "d.py")
    assert exc.value.code == "PLAN_EDGE_CAPACITY_EXCEEDED"
    assert planner.stats()["edges"] == 1
    assert planner.dependents("d.py") == []


def test_plan_without_changes_is_refused():
    planner = make()
    with pytest.raises(isp.PlannerError) as exc:
        planner.plan(10)
    assert exc.value.code == "PLAN_NOTHING_TO_SCAN"


def test_plan_lists_changed_files_in_path_order():
    planner = make()
    planner.update("b.py", "hb")
    planner.update("a.py", "ha")
    planner.update("c.py", "hc")
    result = planner.plan(10)
    assert result == {
        "plan_id": 1,
        "files": ["a.py", "b.py", "c.py"],
        "total": 3,
        "truncated": False,
    }


def test_downstream_referrers_join_the_plan():
    planner = make()
    chain(planner, "cli.py", "app.py", "lib.py")
    planner.update("lib.py", "h1")
    result = planner.plan(10)
    assert result["files"] == ["app.py", "cli.py", "lib.py"]
    assert result["total"] == 3


def test_cycle_members_join_the_plan_once():
    planner = make()
    planner.add_dependency("a.py", "b.py")
    planner.add_dependency("b.py", "a.py")
    planner.update("a.py", "h1")
    assert planner.pending() == ["a.py", "b.py"]


def test_budget_truncates_but_reports_full_total():
    planner = make()
    for name in ("a.py", "b.py", "c.py", "d.py", "e.py"):
        planner.update(name, "h-" + name)
    result = planner.plan(2)
    assert result["files"] == ["a.py", "b.py"]
    assert result["total"] == 5
    assert result["truncated"] is True


def test_budget_limits_are_enforced():
    planner = make()
    for value in (0, -3, "ten"):
        with pytest.raises(isp.PlannerError) as exc:
            planner.plan(value)
        assert exc.value.code == "PLAN_BUDGET_INVALID"

    narrow = make(max_batch=4)
    with pytest.raises(isp.PlannerError) as exc:
        narrow.plan(5)
    assert exc.value.code == "PLAN_BUDGET_EXCEEDED"


def test_commit_completes_plan_and_rejects_repeats():
    planner = make()
    planner.update("a.py", "ha")
    planner.update("b.py", "hb")
    planned = planner.plan(10)
    assert planner.commit(planned["plan_id"]) == {
        "plan_id": planned["plan_id"],
        "committed": 2,
        "remaining": 0,
    }
    assert planner.pending() == []

    with pytest.raises(isp.PlannerError) as repeat:
        planner.commit(planned["plan_id"])
    assert repeat.value.code == "PLAN_ALREADY_COMMITTED"

    with pytest.raises(isp.PlannerError) as unknown:
        planner.commit(99)
    assert unknown.value.code == "PLAN_UNKNOWN"

    planner.update("c.py", "hc")
    assert planner.plan(10)["plan_id"] == 2


def test_remove_marks_files_deleted_without_duplicate_removal():
    planner = make()
    planner.update("a.py", "h1")
    assert planner.remove("a.py") is True
    assert planner.remove("a.py") is False
    assert planner.files() == [{"path": "a.py", "digest": "h1", "deleted": True}]
    assert planner.stats()["files"] == 1


def test_explain_reports_reason_and_roots():
    planner = make()
    chain(planner, "cli.py", "app.py", "lib.py")
    planner.update("lib.py", "h1")
    assert planner.explain("lib.py") == {
        "path": "lib.py",
        "pending": True,
        "reason": "changed",
        "roots": ["lib.py"],
    }
    assert planner.explain("app.py") == {
        "path": "app.py",
        "pending": True,
        "reason": "dependent",
        "roots": ["lib.py"],
    }
    assert planner.explain("cli.py")["roots"] == ["lib.py"]

    planner.update("solo.py", "s1")
    planner.commit(planner.plan(10)["plan_id"])
    assert planner.explain("solo.py") == {
        "path": "solo.py",
        "pending": False,
        "reason": "clean",
        "roots": [],
    }


def test_snapshot_restore_keeps_pending_and_corrupt_state_is_refused():
    planner = make()
    chain(planner, "cli.py", "app.py", "lib.py")
    planner.update("lib.py", "h1")
    state = planner.snapshot()

    clone = make()
    clone.restore(state)
    assert clone.pending() == ["app.py", "cli.py", "lib.py"]
    assert clone.plan(10)["files"] == ["app.py", "cli.py", "lib.py"]

    broken = planner.snapshot()
    broken["version"] = 99
    other = make()
    with pytest.raises(isp.PlannerError) as exc:
        other.restore(broken)
    assert exc.value.code == "PLAN_SNAPSHOT_INVALID"
    assert other.pending() == []
    assert planner.pending() == ["app.py", "cli.py", "lib.py"]


def test_concurrent_update_and_plan_stay_consistent():
    planner = make()
    errors = []

    def writer(number):
        try:
            for offset in range(20):
                planner.update("w%d/p%d.py" % (number, offset), "d%d" % offset)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(8):
                try:
                    report = planner.plan(1000)
                except isp.PlannerError as exc:
                    assert exc.code == "PLAN_NOTHING_TO_SCAN"
                    continue
                assert report["files"] == sorted(set(report["files"]))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(item,)) for item in range(4)]
    threads.extend(threading.Thread(target=reader) for _ in range(2))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert planner.stats()["files"] == 80
    assert len(planner.pending()) == 80
