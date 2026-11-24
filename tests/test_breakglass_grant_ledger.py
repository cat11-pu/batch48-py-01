import threading

import pytest

import breakglass_grant_ledger as bl

SCOPE = "cluster:prod/ns:payments"


def make(**kwargs):
    return bl.BreakglassLedger(**kwargs)


def open_grant(ledger, operator="ops", incident="INC-1", scope=SCOPE,
               ttl=300, now=1000):
    return ledger.activate(operator, scope, incident, ttl=ttl, now=now)


def test_activate_validates_incident_operator_and_scope():
    ledger = make()
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("", SCOPE, "INC-1", now=1000)
    assert exc.value.code == "BREAKGLASS_OPERATOR_INVALID"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("ops", SCOPE, "   ", now=1000)
    assert exc.value.code == "BREAKGLASS_INCIDENT_REQUIRED"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("ops", "cluster", "INC-1", now=1000)
    assert exc.value.code == "BREAKGLASS_SCOPE_INVALID"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("ops", "cluster:prod/", "INC-1", now=1000)
    assert exc.value.code == "BREAKGLASS_SCOPE_INVALID"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("ops", SCOPE, "INC-1", ttl=0, now=1000)
    assert exc.value.code == "BREAKGLASS_TTL_INVALID"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("ops", SCOPE, "INC-1", ttl=99999, now=1000)
    assert exc.value.code == "BREAKGLASS_TTL_INVALID"


def test_activate_returns_grant_identity_and_expiry():
    ledger = make()
    grant = ledger.activate("ops", SCOPE, "INC-7", ttl=300, now=1000)
    assert grant["grant_id"] == "bg-1"
    assert grant["operator"] == "ops"
    assert grant["scope"] == SCOPE
    assert grant["incident_id"] == "INC-7"
    assert grant["activated_at"] == 1000
    assert grant["expires_at"] == 1300
    assert grant["ttl"] == 300
    assert grant["acknowledged"] is False
    assert grant["reviewer"] is None
    assert grant["state"] == "active"

    defaulted = ledger.activate("ops", "cluster:prod", "INC-8", now=1000)
    assert defaulted["grant_id"] == "bg-2"
    assert defaulted["ttl"] == 600
    assert defaulted["expires_at"] == 1600


def test_reviewer_must_differ_from_operator():
    ledger = make()
    open_grant(ledger)
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.acknowledge("bg-1", "   ", now=1010)
    assert exc.value.code == "BREAKGLASS_REVIEWER_REQUIRED"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.acknowledge("bg-1", "ops", now=1010)
    assert exc.value.code == "BREAKGLASS_SELF_REVIEW"

    ack = ledger.acknowledge("bg-1", "sec-1", now=1010)
    assert ack["acknowledged"] is True
    assert ack["reviewer"] == "sec-1"
    assert ack["acknowledged_at"] == 1010

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.acknowledge("bg-1", "sec-2", now=1011)
    assert exc.value.code == "BREAKGLASS_ALREADY_REVIEWED"


def test_acknowledge_requires_live_grant():
    ledger = make()
    open_grant(ledger, incident="INC-1", ttl=100, now=1000)
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.acknowledge("bg-1", "sec-1", now=1100)
    assert exc.value.code == "BREAKGLASS_GRANT_EXPIRED"

    open_grant(ledger, incident="INC-2", ttl=300, now=1000)
    ledger.close("bg-2", "收工", now=1010)
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.acknowledge("bg-2", "sec-1", now=1011)
    assert exc.value.code == "BREAKGLASS_GRANT_CLOSED"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.acknowledge("bg-99", "sec-1", now=1011)
    assert exc.value.code == "BREAKGLASS_GRANT_UNKNOWN"


def test_use_records_ordered_audit_entries():
    ledger = make()
    open_grant(ledger)
    ledger.acknowledge("bg-1", "sec-1", now=1010)

    first = ledger.use("bg-1", "pod/delete", now=1020)
    assert first["seq"] == 1
    assert first["scope"] == SCOPE
    assert first["reviewed"] is True
    assert first["audit_size"] == 1

    deeper = SCOPE + "/db"
    second = ledger.use("bg-1", "pod/exec", scope=deeper, now=1021)
    assert second["seq"] == 2
    assert second["scope"] == deeper
    assert second["audit_size"] == 2

    trail = ledger.audit_trail("bg-1")
    assert [entry["action"] for entry in trail["entries"]] == [
        "pod/delete", "pod/exec",
    ]
    assert [entry["seq"] for entry in trail["entries"]] == [1, 2]
    assert trail["entries"][0]["at"] == 1020
    assert trail["entries"][1]["scope"] == deeper


def test_use_rejected_at_expiry_boundary():
    ledger = make()
    open_grant(ledger, ttl=100, now=1000)
    assert ledger.use("bg-1", "pod/delete", now=1099)["seq"] == 1
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.use("bg-1", "pod/delete", now=1100)
    assert exc.value.code == "BREAKGLASS_GRANT_EXPIRED"


def test_close_requires_note_and_blocks_use():
    ledger = make()
    open_grant(ledger)
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.close("bg-1", "   ", now=1010)
    assert exc.value.code == "BREAKGLASS_NOTE_INVALID"

    closed = ledger.close("bg-1", "演练结束", now=1010)
    assert closed["state"] == "closed"
    assert closed["closed_at"] == 1010
    assert closed["note"] == "演练结束"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.use("bg-1", "pod/delete", now=1011)
    assert exc.value.code == "BREAKGLASS_GRANT_CLOSED"

    again = ledger.close("bg-1", "再关一次", now=1020)
    assert again["state"] == "closed"
    assert again["closed_at"] == 1010


def test_action_scope_must_stay_within_grant():
    ledger = make()
    open_grant(ledger)
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.use("bg-1", "pod/delete", scope="cluster:prod/ns:other", now=1010)
    assert exc.value.code == "BREAKGLASS_SCOPE_DENIED"

    with pytest.raises(bl.BreakglassError) as exc:
        ledger.use("bg-1", "pod/delete", scope="cluster:prod", now=1011)
    assert exc.value.code == "BREAKGLASS_SCOPE_DENIED"

    assert ledger.use("bg-1", "pod/exec", scope=SCOPE + "/db",
                      now=1012)["scope"] == SCOPE + "/db"
    assert ledger.use("bg-1", "pod/log", now=1013)["scope"] == SCOPE


def test_duplicate_incident_from_other_operator_busy():
    ledger = make()
    open_grant(ledger, operator="ops", incident="INC-9")
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.activate("ops2", SCOPE, "INC-9", now=1001)
    assert exc.value.code == "BREAKGLASS_INCIDENT_BUSY"
    assert len(ledger.active()) == 1


def test_concurrent_activate_keeps_single_grant():
    ledger = make()
    grants = []
    failures = []
    guard = threading.Lock()

    def worker(index):
        try:
            grant = ledger.activate(f"ops-{index}", SCOPE, "INC-C", ttl=300,
                                    now=1000)
            with guard:
                grants.append(grant)
        except bl.BreakglassError as exc:
            with guard:
                failures.append(exc.code)
        except Exception as exc:  # noqa: BLE001 - surfaced as an activation error
            with guard:
                failures.append(type(exc).__name__)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(grants) == 1
    assert len(failures) == 5
    assert all(code == "BREAKGLASS_INCIDENT_BUSY" for code in failures)
    assert len(ledger.active()) == 1
    assert ledger.stats()["activations"] == 1


def test_sweep_expires_and_purges_closed_records():
    ledger = make()
    open_grant(ledger, incident="INC-A", scope="cluster:prod", ttl=100, now=1000)
    open_grant(ledger, incident="INC-B", scope="cluster:prod", ttl=1000, now=1000)

    assert ledger.sweep(1100) == 1
    assert ledger.get("bg-1")["state"] == "expired"
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.use("bg-1", "pod/delete", now=1101)
    assert exc.value.code == "BREAKGLASS_GRANT_EXPIRED"
    assert ledger.get("bg-2")["state"] == "active"

    ledger.close("bg-2", "演练结束", now=1101)
    assert ledger.sweep(87501) == 1
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.get("bg-2")
    assert exc.value.code == "BREAKGLASS_GRANT_UNKNOWN"
    assert ledger.get("bg-1")["state"] == "expired"


def test_audit_trail_survives_close():
    ledger = make()
    open_grant(ledger)
    ledger.use("bg-1", "pod/delete", now=1005)
    ledger.acknowledge("bg-1", "sec-1", now=1010)
    ledger.close("bg-1", "演练结束", now=1020)

    trail = ledger.audit_trail("bg-1")
    assert trail["incident_id"] == "INC-1"
    assert trail["operator"] == "ops"
    assert trail["state"] == "closed"
    assert trail["note"] == "演练结束"
    assert trail["acknowledged"] is True
    assert trail["reviewer"] == "sec-1"
    assert [entry["action"] for entry in trail["entries"]] == ["pod/delete"]


def test_restore_keeps_trail_and_expiry():
    ledger = make()
    open_grant(ledger, ttl=300, now=1000)
    ledger.use("bg-1", "pod/delete", now=1010)
    snap = ledger.snapshot()

    restored = make()
    restored.restore(snap)
    trail = restored.audit_trail("bg-1")
    assert len(trail["entries"]) == 1
    assert restored.get("bg-1")["expires_at"] == 1300

    second = restored.use("bg-1", "pod/exec", now=1200)
    assert second["seq"] == 2
    with pytest.raises(bl.BreakglassError) as exc:
        restored.use("bg-1", "pod/kill", now=1300)
    assert exc.value.code == "BREAKGLASS_GRANT_EXPIRED"


def test_restore_rejects_bad_state_and_snapshot_is_detached():
    ledger = make()
    open_grant(ledger)
    with pytest.raises(bl.BreakglassError) as exc:
        ledger.restore({"version": 99})
    assert exc.value.code == "BREAKGLASS_SNAPSHOT_INVALID"
    assert ledger.get("bg-1")["state"] == "active"

    snap = ledger.snapshot()
    snap["grants"]["bg-1"]["state"] = "closed"
    snap["grants"]["bg-1"]["note"] = "tampered"
    assert ledger.get("bg-1")["state"] == "active"
    assert ledger.get("bg-1")["note"] is None


def test_stats_counters_and_reset():
    ledger = make()
    open_grant(ledger, incident="INC-1", ttl=600, now=1000)
    ledger.acknowledge("bg-1", "sec-1", now=1010)
    ledger.use("bg-1", "pod/delete", now=1011)

    open_grant(ledger, operator="ops2", incident="INC-2", scope="cluster:dev",
               ttl=100, now=1000)
    ledger.use("bg-2", "pod/delete", now=1001)
    ledger.close("bg-2", "演练结束", now=1002)

    assert ledger.sweep(1200) == 0
    assert ledger.stats() == {
        "activations": 2,
        "acknowledgements": 1,
        "uses": 2,
        "closes": 1,
        "swept": 0,
        "unreviewed_uses": 1,
    }

    ledger.reset_stats()
    assert ledger.stats() == {
        "activations": 0,
        "acknowledgements": 0,
        "uses": 0,
        "closes": 0,
        "swept": 0,
        "unreviewed_uses": 0,
    }
