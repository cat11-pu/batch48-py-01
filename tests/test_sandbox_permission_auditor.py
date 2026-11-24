import threading

import pytest

import sandbox_permission_auditor as pa


def make(**kwargs):
    return pa.PermissionAuditor(**kwargs)


def test_declare_normalizes_case_and_dedupes():
    audit = make()
    info = audit.declare("j1", ["FS.READ", "Net.Connect", "net.connect"], now=1000)
    assert info["job_id"] == "j1"
    assert info["declared"] == ["fs.read", "net.connect"]
    assert info["version"] == 1
    assert info["declared_at"] == 1000
    assert info["ttl"] is None
    assert info["expires_at"] is None


def test_declare_validates_names_ttl_and_limits():
    audit = make()
    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.declare("  ", ["fs.read"])
    assert exc.value.code == "PERMISSION_JOB_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.declare("j1", [])
    assert exc.value.code == "PERMISSION_DECLARE_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.declare("j1", ["nodot"])
    assert exc.value.code == "PERMISSION_NAME_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.declare("j1", ["Bad Name"])
    assert exc.value.code == "PERMISSION_NAME_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.declare("j1", ["fs.read"], ttl=0)
    assert exc.value.code == "PERMISSION_TTL_INVALID"

    audit.declare("j1", ["fs.read"])
    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.declare("j1", ["fs.write"])
    assert exc.value.code == "PERMISSION_JOB_DUPLICATE"

    tight = make(max_permissions=1)
    with pytest.raises(pa.PermissionAuditorError) as exc:
        tight.declare("j1", ["fs.read", "fs.write"])
    assert exc.value.code == "PERMISSION_DECLARE_LIMIT"

    capped = make(max_jobs=1)
    capped.declare("j1", ["fs.read"])
    with pytest.raises(pa.PermissionAuditorError) as exc:
        capped.declare("j2", ["fs.read"])
    assert exc.value.code == "PERMISSION_JOB_LIMIT"


def test_observe_undeclared_use_is_recorded_but_flagged():
    audit = make()
    audit.declare("j1", ["fs.read"], now=1000)

    first = audit.observe("j1", "proc.exec", now=1001)
    assert first["permission"] == "proc.exec"
    assert first["declared"] is False
    assert first["count"] == 1
    assert first["total"] == 1
    assert first["first_seen_at"] == 1001

    second = audit.observe("j1", "proc.exec", count=2, now=1002)
    assert second["total"] == 3
    assert second["first_seen_at"] == 1001


def test_observe_rejects_unknown_job_and_bad_counts():
    audit = make()
    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.observe("ghost", "fs.read", now=1000)
    assert exc.value.code == "PERMISSION_UNKNOWN_JOB"

    audit.declare("j1", ["fs.read"], now=1000)
    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.observe("j1", "fs.read", count=0, now=1001)
    assert exc.value.code == "PERMISSION_COUNT_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.observe("j1", "fs.read", count=-3, now=1001)
    assert exc.value.code == "PERMISSION_COUNT_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.observe("j1", "fs.read", count=1.5, now=1001)
    assert exc.value.code == "PERMISSION_COUNT_INVALID"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.observe("j1", "Bad Name", now=1001)
    assert exc.value.code == "PERMISSION_NAME_INVALID"


def test_report_flags_undeclared_usage():
    audit = make()
    audit.declare("j1", ["fs.read", "net.connect"], now=1000)
    audit.observe("j1", "fs.read", count=2, now=1001)
    audit.observe("j1", "proc.exec", now=1002)

    report = audit.report("j1")
    assert report["declared"] == ["fs.read", "net.connect"]
    assert report["observed"]["fs.read"] == 2
    assert report["observed"]["proc.exec"] == 1
    assert report["undeclared"] == ["proc.exec"]
    assert [item["permission"] for item in report["violations"]] == ["proc.exec"]
    assert report["violations"][0]["count"] == 1
    assert report["total_observations"] == 3
    assert report["compliant"] is False


def test_unused_declaration_is_not_a_violation():
    audit = make()
    audit.declare("j1", ["fs.read", "net.connect"], now=1000)
    audit.observe("j1", "fs.read", now=1001)

    report = audit.report("j1")
    assert report["violations"] == []
    assert report["undeclared"] == []
    assert report["compliant"] is True
    assert report["total_observations"] == 1
    assert report["declared"] == ["fs.read", "net.connect"]


def test_first_seen_tracks_earliest_observation():
    audit = make()
    audit.declare("j1", ["fs.read"], now=1000)
    audit.observe("j1", "fs.read", count=2, now=1005)
    audit.observe("j1", "fs.read", now=1001)
    audit.observe("j1", "fs.read", now=1003)

    report = audit.report("j1")
    assert report["first_seen"]["fs.read"] == 1001
    assert report["observed"]["fs.read"] == 4


def test_baseline_is_fixed_at_declaration():
    audit = make()
    audit.declare("j1", ["fs.read", "net.connect"], now=1000)

    base = audit.baseline("j1")
    assert base["counts"] == {"fs.read": 0, "net.connect": 0}
    assert base["version"] == 1
    assert base["captured_at"] == 1000

    audit.observe("j1", "fs.read", count=5, now=1001)
    base["counts"]["fs.read"] = 99
    again = audit.baseline("j1")
    assert again["counts"]["fs.read"] == 0

    report = audit.report("j1")
    assert report["baseline_version"] == base["version"]


def test_expire_blocks_observe_report_and_baseline():
    audit = make()
    audit.declare("j1", ["fs.read"], ttl=100, now=1000)
    assert audit.expire(1099) == 0
    assert audit.expire(1100) == 1

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.observe("j1", "fs.read", now=1100)
    assert exc.value.code == "PERMISSION_DECLARATION_EXPIRED"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.report("j1")
    assert exc.value.code == "PERMISSION_DECLARATION_EXPIRED"

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.baseline("j1")
    assert exc.value.code == "PERMISSION_DECLARATION_EXPIRED"


def test_case_variants_count_as_one_permission():
    audit = make()
    audit.declare("j1", ["FS.READ"], now=1000)
    audit.observe("j1", "fs.read", now=1001)
    audit.observe("j1", "FS.READ", now=1002)

    report = audit.report("j1")
    assert report["observed"] == {"fs.read": 2}
    assert report["undeclared"] == []
    assert report["violations"] == []


def test_concurrent_observe_keeps_exact_total():
    audit = make()
    audit.declare("j1", ["fs.read"], now=1000)
    guard = threading.Lock()
    errors = []

    def worker():
        try:
            for _ in range(200):
                audit.observe("j1", "fs.read", now=1000)
        except Exception as exc:  # noqa: BLE001 - recorded as a failed observation
            with guard:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    report = audit.report("j1")
    assert report["observed"]["fs.read"] == 1600
    assert report["total_observations"] == 1600
    assert report["first_seen"]["fs.read"] == 1000
    assert errors == []


def test_snapshot_restore_keeps_report_identical():
    audit = make()
    audit.declare("j1", ["fs.read", "net.connect"], ttl=600, now=1000)
    audit.observe("j1", "fs.read", count=3, now=1001)
    audit.observe("j1", "proc.exec", now=1002)
    before = audit.report("j1")

    snap = audit.snapshot()
    assert snap["version"] == 1
    restored = make()
    restored.restore(snap)
    after = restored.report("j1")

    for key in ("declared", "observed", "undeclared", "unused", "violations",
                "first_seen", "total_observations", "compliant", "version",
                "baseline_version"):
        assert after[key] == before[key]


def test_snapshot_is_detached_from_live_state():
    audit = make()
    audit.declare("j1", ["fs.read"], now=1000)
    audit.observe("j1", "fs.read", now=1001)

    snap = audit.snapshot()
    snap["jobs"]["j1"]["declared"].append("net.write")
    snap["jobs"]["j1"]["observations"]["fs.read"] = 99

    report = audit.report("j1")
    assert report["declared"] == ["fs.read"]
    assert report["observed"]["fs.read"] == 1


def test_restore_rejects_malformed_state():
    audit = make()
    audit.declare("j1", ["fs.read"], now=1000)

    with pytest.raises(pa.PermissionAuditorError) as exc:
        audit.restore({"version": 99})
    assert exc.value.code == "PERMISSION_SNAPSHOT_INVALID"
    assert audit.get("j1")["declared"] == ["fs.read"]
    assert audit.jobs() == ["j1"]


def test_stats_and_reset_stats():
    audit = make()
    audit.declare("j1", ["fs.read"], ttl=100, now=1000)
    audit.observe("j1", "fs.read", now=1001)
    audit.observe("j1", "proc.exec", now=1002)
    audit.report("j1")
    audit.declare("j2", ["net.connect"], ttl=100, now=1000)

    assert audit.expire(1100) == 2
    assert audit.stats() == {
        "jobs": 2,
        "observations": 2,
        "undeclared_hits": 1,
        "reports": 1,
        "expired_jobs": 2,
    }

    audit.reset_stats()
    assert audit.stats() == {
        "jobs": 2,
        "observations": 0,
        "undeclared_hits": 0,
        "reports": 0,
        "expired_jobs": 0,
    }
