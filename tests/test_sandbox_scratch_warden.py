import threading

import pytest

import sandbox_scratch_warden as sw


ROOT = "/srv/scratch"


def test_allocate_reports_root_quota_and_empty_usage():
    warden = sw.ScratchWarden()
    info = warden.allocate("job-a")
    assert info["job_id"] == "job-a"
    assert info["root"] == ROOT + "/job-a"
    assert info["quota_bytes"] == 16 * 1024 * 1024
    assert info["used_bytes"] == 0
    assert info["files"] == 0
    assert info["created_at"] == 0
    usage = warden.usage("job-a")
    assert usage["used_bytes"] == 0
    assert usage["remaining_bytes"] == usage["quota_bytes"] == 16 * 1024 * 1024


def test_allocate_rejects_duplicate_and_bad_arguments():
    warden = sw.ScratchWarden()
    warden.allocate("job-a")
    with pytest.raises(sw.ScratchError) as exc:
        warden.allocate("job-a")
    assert exc.value.code == "SCRATCH_DUPLICATE_JOB"
    for quota in (0, -3, 2.5, True):
        with pytest.raises(sw.ScratchError) as exc:
            warden.allocate("quota-job", quota)
        assert exc.value.code == "SCRATCH_INVALID_QUOTA"
    for job_id in ("", "Job/A", "-lead"):
        with pytest.raises(sw.ScratchError) as exc:
            warden.allocate(job_id)
        assert exc.value.code == "SCRATCH_INVALID_JOB"
    with pytest.raises(sw.ScratchError) as exc:
        warden.usage("absent")
    assert exc.value.code == "SCRATCH_UNKNOWN_JOB"


def test_resolve_collapses_dot_segments_and_empty_parts():
    warden = sw.ScratchWarden()
    warden.allocate("job-a")
    assert warden.resolve("job-a", "a//b/./c") == ROOT + "/job-a/a/b/c"
    assert warden.resolve("job-a", "") == ROOT + "/job-a"
    assert warden.resolve("job-a", ".") == ROOT + "/job-a"
    assert warden.resolve("job-a", "a/../b") == ROOT + "/job-a/b"
    assert warden.resolve("job-a", "a/b/../../c") == ROOT + "/job-a/c"


def test_resolve_refuses_absolute_and_escaping_paths():
    warden = sw.ScratchWarden()
    warden.allocate("job-a")
    for bad in ("/etc/passwd", "../escape", "a/../../b"):
        with pytest.raises(sw.ScratchError) as exc:
            warden.resolve("job-a", bad)
        assert exc.value.code == "SCRATCH_PATH_ESCAPE"
    for bad in ("a\x00b", 123):
        with pytest.raises(sw.ScratchError) as exc:
            warden.resolve("job-a", bad)
        assert exc.value.code == "SCRATCH_INVALID_PATH"


def test_released_job_refuses_every_operation():
    warden = sw.ScratchWarden()
    warden.allocate("job-b")
    warden.charge("job-b", 10)
    warden.release("job-b", 50)
    for call in (
        lambda: warden.resolve("job-b", "a"),
        lambda: warden.charge("job-b", 1),
        lambda: warden.usage("job-b"),
    ):
        with pytest.raises(sw.ScratchError) as exc:
            call()
        assert exc.value.code == "SCRATCH_JOB_RELEASED"
    with pytest.raises(sw.ScratchError) as exc:
        warden.resolve("absent", "a")
    assert exc.value.code == "SCRATCH_UNKNOWN_JOB"


def test_charge_accumulates_bytes_and_remaining():
    warden = sw.ScratchWarden()
    warden.allocate("job-a", quota_mb=1)
    assert warden.charge("job-a", 100)["used_bytes"] == 100
    out = warden.charge("job-a", 250)
    assert out["used_bytes"] == 350
    assert out["remaining_bytes"] == 1024 * 1024 - 350
    for bad in (-1, 1.5, "12"):
        with pytest.raises(sw.ScratchError) as exc:
            warden.charge("job-a", bad)
        assert exc.value.code == "SCRATCH_INVALID_CHARGE"
    assert warden.usage("job-a")["used_bytes"] == 350


def test_charge_respects_quota_including_exact_fit():
    warden = sw.ScratchWarden()
    warden.allocate("job-a", quota_mb=1)
    warden.charge("job-a", 1048000)
    with pytest.raises(sw.ScratchError) as exc:
        warden.charge("job-a", 1000)
    assert exc.value.code == "SCRATCH_QUOTA_EXCEEDED"
    assert warden.usage("job-a")["used_bytes"] == 1048000
    assert warden.charge("job-a", 576)["used_bytes"] == 1024 * 1024
    with pytest.raises(sw.ScratchError) as exc:
        warden.charge("job-a", 1)
    assert exc.value.code == "SCRATCH_QUOTA_EXCEEDED"


def test_charge_with_path_enforces_file_cap_atomically():
    warden = sw.ScratchWarden()
    warden.allocate("job-a", max_files=2)
    warden.charge("job-a", 10, path="a.txt")
    warden.charge("job-a", 10, path="b.txt")
    with pytest.raises(sw.ScratchError) as exc:
        warden.charge("job-a", 5, path="c.txt")
    assert exc.value.code == "SCRATCH_FILE_LIMIT"
    usage = warden.usage("job-a")
    assert usage["used_bytes"] == 20
    assert usage["files"] == 2
    out = warden.charge("job-a", 5, path="a.txt")
    assert out["files"] == 2
    assert out["used_bytes"] == 25


def test_release_reports_reclaimed_usage_once():
    warden = sw.ScratchWarden()
    warden.allocate("job-a")
    warden.charge("job-a", 100, path="x")
    warden.charge("job-a", 50, path="y")
    out = warden.release("job-a", 500)
    assert out == {
        "job_id": "job-a",
        "reclaimed_bytes": 150,
        "reclaimed_files": 2,
        "released_at": 500,
    }
    with pytest.raises(sw.ScratchError) as exc:
        warden.release("job-a", 501)
    assert exc.value.code == "SCRATCH_UNKNOWN_JOB"


def test_jobs_have_isolated_quotas_and_roots():
    warden = sw.ScratchWarden()
    one = warden.allocate("job-a", quota_mb=1)
    two = warden.allocate("job-b", quota_mb=2)
    assert one["root"] != two["root"]
    warden.charge("job-a", 1024 * 1024)
    with pytest.raises(sw.ScratchError) as exc:
        warden.charge("job-a", 1)
    assert exc.value.code == "SCRATCH_QUOTA_EXCEEDED"
    assert warden.charge("job-b", 1024 * 1024)["used_bytes"] == 1024 * 1024
    assert warden.usage("job-a")["quota_bytes"] == 1024 * 1024


def test_sweep_reclaims_only_released_jobs_past_retention():
    warden = sw.ScratchWarden(retention=5)
    warden.allocate("job-a")
    warden.allocate("job-b")
    warden.charge("job-a", 10)
    warden.release("job-a", 100)
    assert warden.sweep(104) == []
    assert warden.orphans() == ["job-a"]
    assert warden.sweep(106) == ["job-a"]
    assert warden.orphans() == []
    assert [job["job_id"] for job in warden.list_jobs()] == ["job-b"]


def test_snapshot_restore_keeps_usage_and_tombstones():
    warden = sw.ScratchWarden()
    warden.allocate("job-a", quota_mb=1)
    warden.charge("job-a", 300, path="p")
    warden.allocate("job-b")
    warden.release("job-b", 10)
    fresh = sw.ScratchWarden()
    fresh.restore(warden.snapshot())
    assert fresh.usage("job-a")["used_bytes"] == 300
    assert fresh.resolve("job-a", "x") == ROOT + "/job-a/x"
    assert fresh.orphans() == ["job-b"]
    with pytest.raises(sw.ScratchError) as exc:
        fresh.charge("job-b", 1)
    assert exc.value.code == "SCRATCH_JOB_RELEASED"
    with pytest.raises(sw.ScratchError) as exc:
        fresh.restore({"jobs": 1})
    assert exc.value.code == "SCRATCH_SNAPSHOT_INVALID"
    assert fresh.usage("job-a")["used_bytes"] == 300


def test_snapshot_is_a_deep_copy():
    warden = sw.ScratchWarden()
    warden.allocate("job-a")
    warden.charge("job-a", 77)
    state = warden.snapshot()
    state["jobs"]["job-a"]["used_bytes"] = 999999
    state["jobs"]["job-a"]["files"] = 42
    assert warden.usage("job-a")["used_bytes"] == 77
    assert warden.usage("job-a")["files"] == 0


def test_concurrent_charges_never_oversell_quota():
    warden = sw.ScratchWarden()
    warden.allocate("job-a", quota_mb=1)
    barrier = threading.Barrier(10)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        try:
            warden.charge("job-a", 131072)
            value = "ok"
        except sw.ScratchError as exc:
            value = exc.code
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures
            value = type(exc).__name__
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 8
    assert results.count("SCRATCH_QUOTA_EXCEEDED") == 2
    assert warden.usage("job-a")["used_bytes"] == 1024 * 1024


def test_list_jobs_is_sorted_and_reports_state():
    warden = sw.ScratchWarden()
    warden.allocate("job-b")
    warden.allocate("job-a")
    warden.charge("job-a", 7)
    assert warden.list_jobs() == [
        {"job_id": "job-a", "state": "ACTIVE", "used_bytes": 7, "files": 0},
        {"job_id": "job-b", "state": "ACTIVE", "used_bytes": 0, "files": 0},
    ]
    warden.release("job-b", 9)
    assert warden.list_jobs()[1] == {
        "job_id": "job-b", "state": "RELEASED", "used_bytes": 0, "files": 0,
    }
