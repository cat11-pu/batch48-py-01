import copy
import threading

import pytest

import group_mapping_registry as gm

NOW = 1_000_000


def registry(**kwargs):
    return gm.GroupMappingRegistry(**kwargs)


def mapping(**overrides):
    base = {
        "id": "m1",
        "tenant": "t1",
        "pattern": "eng-oncall",
        "roles": ["oncall", "read"],
        "priority": 0,
    }
    base.update(overrides)
    return base


def test_add_mapping_records_normalized_view_and_rejects_bad_input():
    reg = registry()
    out = reg.add_mapping(mapping(roles=["read", "oncall", "read"]))
    assert out["id"] == "m1"
    assert out["tenant"] == "t1"
    assert out["pattern"] == "eng-oncall"
    assert out["roles"] == ["oncall", "read"]
    assert out["priority"] == 0
    assert out["enabled"] is True
    assert out["wildcard"] is False
    assert reg.stats() == {"grants": 0, "revocations": 0, "syncs": 0,
                           "rejected": 0}

    bad_inputs = [
        None,
        {},
        {"tenant": "t1", "pattern": "  ", "roles": ["read"]},
        {"tenant": "t1", "pattern": "p", "roles": []},
        {"tenant": "t1", "pattern": "p", "roles": ["read", ""]},
        {"tenant": "t1", "pattern": "p", "roles": ["read"], "priority": "high"},
    ]
    for bad in bad_inputs:
        with pytest.raises(gm.GroupMappingError) as exc:
            reg.add_mapping(bad)
        assert exc.value.code == "GROUP_MAPPING_INVALID"

    with pytest.raises(gm.GroupMappingError) as exc:
        reg.add_mapping(mapping())
    assert exc.value.code == "GROUP_MAPPING_CONFLICT"
    assert reg.stats()["rejected"] == 7
    assert len(reg.list_mappings("t1")) == 1


def test_mapping_capacity_is_enforced_per_tenant():
    reg = registry(max_mappings_per_tenant=2)
    reg.add_mapping(mapping(id="m1"))
    reg.add_mapping(mapping(id="m2", pattern="ops-core"))
    with pytest.raises(gm.GroupMappingError) as exc:
        reg.add_mapping(mapping(id="m3", pattern="fin-core"))
    assert exc.value.code == "GROUP_MAPPING_CAPACITY_EXCEEDED"
    assert [item["id"] for item in reg.list_mappings("t1")] == ["m1", "m2"]

    reg.add_mapping(mapping(id="m1", tenant="t2"))
    assert [item["id"] for item in reg.list_mappings("t2")] == ["m1"]
    assert reg.stats()["rejected"] == 1


def test_priority_and_specificity_pick_the_winning_mapping():
    reg = registry()
    reg.add_mapping(mapping(id="broad", pattern="*@corp.example",
                            roles=["staff"], priority=5))
    reg.add_mapping(mapping(id="eng", pattern="eng-*@corp.example",
                            roles=["engineer"], priority=7))
    out = reg.resolve_roles("t1", ["eng-platform@corp.example"])
    assert out["roles"] == ["engineer"]
    assert out["matched"] == {"eng-platform@corp.example": "eng"}

    reg.add_mapping(mapping(id="fin", pattern="fin-*", roles=["finance"],
                            priority=7))
    reg.add_mapping(mapping(id="fin-eu", pattern="fin-eu-*",
                            roles=["finance_eu"], priority=7))
    out = reg.resolve_roles("t1", ["fin-eu-01"])
    assert out["roles"] == ["finance_eu"]
    assert out["matched"] == {"fin-eu-01": "fin-eu"}


def test_resolve_roles_unions_roles_across_matched_groups():
    reg = registry()
    reg.add_mapping(mapping(id="ops", pattern="ops-*", roles=["ops"]))
    reg.add_mapping(mapping(id="fin", pattern="fin-*", roles=["finance"]))
    out = reg.resolve_roles("t1", ["ops-a", "fin-b", "unmapped"])
    assert out["roles"] == ["finance", "ops"]
    assert out["matched"] == {"ops-a": "ops", "fin-b": "fin",
                              "unmapped": None}


def test_disabled_mapping_stops_matching():
    reg = registry()
    reg.add_mapping(mapping())
    assert reg.resolve_roles("t1", ["eng-oncall"])["roles"] == ["oncall", "read"]

    off = reg.disable_mapping("t1", "m1")
    assert off == {"id": "m1", "enabled": False, "revoked_roles": 0}
    assert reg.resolve_roles("t1", ["eng-oncall"]) == {
        "roles": [], "matched": {"eng-oncall": None},
    }

    on = reg.enable_mapping("t1", "m1")
    assert on == {"id": "m1", "enabled": True, "granted": 0}
    assert reg.resolve_roles("t1", ["eng-oncall"])["roles"] == ["oncall", "read"]


def test_sync_membership_derives_roles_for_every_member():
    reg = registry()
    reg.add_mapping(mapping())
    out = reg.sync_membership("t1", "eng-oncall", ["u1", "u2"])
    assert out["group"] == "eng-oncall"
    assert out["members"] == 2
    assert out["added"] == {"u1": ["oncall", "read"], "u2": ["oncall", "read"]}
    assert out["removed"] == {}
    assert reg.roles_of("t1", "u1") == ["oncall", "read"]
    assert reg.roles_of("t1", "u2") == ["oncall", "read"]
    assert reg.roles_of("t1", "u3") == []
    assert reg.stats()["grants"] == 4
    assert reg.stats()["syncs"] == 1


def test_repeated_sync_of_same_member_list_is_idempotent():
    reg = registry()
    reg.add_mapping(mapping(roles=["oncall"]))
    reg.sync_membership("t1", "eng-oncall", ["u1", "u2"])
    before = reg.stats()

    again = reg.sync_membership("t1", "eng-oncall", ["u2", "u1"])
    assert again["added"] == {}
    assert again["removed"] == {}
    assert again["members"] == 2
    assert reg.stats()["grants"] == before["grants"] == 2
    assert reg.stats()["revocations"] == 0
    assert reg.stats()["syncs"] == 2
    assert reg.roles_of("t1", "u1") == ["oncall"]


def test_removed_member_loses_derived_roles():
    reg = registry()
    reg.add_mapping(mapping(roles=["oncall"]))
    reg.sync_membership("t1", "eng-oncall", ["u1", "u2"])

    out = reg.sync_membership("t1", "eng-oncall", ["u2"])
    assert out["removed"] == {"u1": ["oncall"]}
    assert out["added"] == {}
    assert reg.roles_of("t1", "u1") == []
    assert reg.roles_of("t1", "u2") == ["oncall"]
    assert reg.stats()["revocations"] == 1


def test_manual_grants_are_tracked_outside_the_derived_table():
    reg = registry()
    reg.add_mapping(mapping(roles=["oncall"]))
    first = reg.grant_manual("t1", "u1", "auditor")
    assert first["manual"] == ["auditor"]
    second = reg.grant_manual("t1", "u1", "auditor")
    assert second["manual"] == ["auditor"]

    reg.sync_membership("t1", "eng-oncall", ["u1"])
    assert reg.roles_of("t1", "u1") == ["auditor", "oncall"]
    assert reg.stats()["grants"] == 1

    removed = reg.revoke_manual("t1", "u1", "auditor")
    assert removed == {"removed": True, "manual": []}
    assert reg.roles_of("t1", "u1") == ["oncall"]
    again = reg.revoke_manual("t1", "u1", "auditor")
    assert again == {"removed": False, "manual": []}


def test_remove_mapping_revokes_only_roles_it_derived():
    reg = registry()
    reg.add_mapping(mapping(id="m1", pattern="eng-*", roles=["oncall"]))
    reg.add_mapping(mapping(id="m2", pattern="ops-*", roles=["ops"]))
    reg.sync_membership("t1", "eng-oncall", ["u1"])
    reg.sync_membership("t1", "ops-core", ["u1", "u2"])
    assert reg.roles_of("t1", "u1") == ["oncall", "ops"]

    out = reg.remove_mapping("t1", "m1")
    assert out == {"removed": "m1", "revoked_roles": 1}
    assert reg.roles_of("t1", "u1") == ["ops"]
    assert reg.roles_of("t1", "u2") == ["ops"]
    assert [item["id"] for item in reg.list_mappings("t1")] == ["m2"]

    with pytest.raises(gm.GroupMappingError) as exc:
        reg.remove_mapping("t1", "m1")
    assert exc.value.code == "GROUP_MAPPING_UNKNOWN"


def test_disable_then_enable_reapplies_current_members():
    reg = registry()
    reg.add_mapping(mapping(roles=["oncall"]))
    reg.sync_membership("t1", "eng-oncall", ["u1", "u2"])

    off = reg.disable_mapping("t1", "m1", now=NOW)
    assert off["revoked_roles"] == 2
    assert reg.roles_of("t1", "u1") == []
    assert reg.roles_of("t1", "u2") == []

    on = reg.enable_mapping("t1", "m1", now=NOW)
    assert on == {"id": "m1", "enabled": True, "granted": 2}
    assert reg.roles_of("t1", "u1") == ["oncall"]
    assert reg.roles_of("t1", "u2") == ["oncall"]


def test_drift_report_flags_missing_roles_and_reconcile_fixes_them():
    reg = registry()
    reg.sync_membership("t1", "eng-oncall", ["u1"])
    reg.add_mapping(mapping(roles=["oncall"]))

    report = reg.drift_report("t1")
    assert report == {"checked": 1,
                      "drifted": {"u1": {"missing": ["oncall"], "extra": []}}}

    fixed = reg.reconcile("t1")
    assert fixed == {"granted": 1, "revoked": 0}
    assert reg.drift_report("t1") == {"checked": 1, "drifted": {}}
    assert reg.roles_of("t1", "u1") == ["oncall"]


def test_snapshot_restore_keeps_mappings_members_and_manual_grants():
    reg = registry()
    reg.add_mapping(mapping())
    reg.sync_membership("t1", "eng-oncall", ["u1"])
    reg.grant_manual("t1", "u1", "auditor")

    snap = reg.snapshot()
    pristine = copy.deepcopy(snap)
    clone = registry()
    clone.restore(pristine)
    assert clone.roles_of("t1", "u1") == ["auditor", "oncall", "read"]
    assert clone.resolve_roles("t1", ["eng-oncall"])["roles"] == ["oncall", "read"]
    assert clone.stats() == reg.stats()

    snap["mappings"].clear()
    snap["stats"]["grants"] = 99
    assert reg.stats()["grants"] == 2
    assert reg.list_mappings("t1")[0]["id"] == "m1"
    assert reg.resolve_roles("t1", ["eng-oncall"])["roles"] == ["oncall", "read"]


def test_restore_refuses_malformed_state_and_keeps_live_data():
    reg = registry()
    reg.add_mapping(mapping(roles=["oncall"]))
    reg.sync_membership("t1", "eng-oncall", ["u1"])

    empty_stats = {"grants": 0, "revocations": 0, "syncs": 0, "rejected": 0}
    bad_states = [
        None,
        {},
        {"version": 2, "mappings": {}, "members": {}, "derived": {},
         "manual": {}, "stats": empty_stats},
        {"version": 1, "mappings": [], "members": {}, "derived": {},
         "manual": {}, "stats": empty_stats},
        {"version": 1, "mappings": {}, "members": {}, "derived": {},
         "manual": {}, "stats": {"grants": 1}},
        {"version": 1,
         "mappings": {"t1": {"m1": {"id": "m1", "tenant": "t1",
                                    "pattern": "", "roles": ["r"],
                                    "priority": 0, "enabled": True}}},
         "members": {}, "derived": {}, "manual": {}, "stats": empty_stats},
        {"version": 1, "mappings": {}, "members": {"t1": {"g": "u1"}},
         "derived": {}, "manual": {}, "stats": empty_stats},
    ]
    for bad in bad_states:
        with pytest.raises(gm.GroupMappingError) as exc:
            reg.restore(bad)
        assert exc.value.code == "GROUP_MAPPING_SNAPSHOT_INVALID"

    assert reg.roles_of("t1", "u1") == ["oncall"]
    assert reg.resolve_roles("t1", ["eng-oncall"])["roles"] == ["oncall"]


def test_concurrent_sync_of_same_group_applies_once():
    reg = registry()
    reg.add_mapping(mapping(roles=["oncall"]))
    errors = []

    def worker():
        try:
            reg.sync_membership("t1", "eng-oncall", ["u1", "u2", "u3"])
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert reg.stats()["grants"] == 3
    assert reg.stats()["syncs"] == 8
    assert [reg.roles_of("t1", user) for user in ("u1", "u2", "u3")] == [
        ["oncall"], ["oncall"], ["oncall"],
    ]
