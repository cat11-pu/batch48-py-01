import copy
import threading

import pytest

import org_policy_inheritance as op

NOW = 1_000_000


def tree(**kwargs):
    return op.OrgPolicyInheritance(**kwargs)


def allow(policy_id, **values):
    return {"id": policy_id, "effect": "allow", "values": values}


def deny(policy_id, **values):
    return {"id": policy_id, "effect": "deny", "values": values}


def test_add_org_unit_builds_tree_and_reports_path():
    units = tree()
    assert units.path_of("org") == ["org"]
    assert units.add_org_unit("sales", "org") == {
        "ou": "sales", "parent": "org", "depth": 1,
    }
    assert units.add_org_unit("sales-eu", "sales") == {
        "ou": "sales-eu", "parent": "sales", "depth": 2,
    }
    assert units.path_of("sales-eu") == ["org", "sales", "sales-eu"]

    with pytest.raises(op.OrgPolicyError) as exc:
        units.add_org_unit("spare", "missing")
    assert exc.value.code == "ORG_POLICY_UNKNOWN_NODE"
    with pytest.raises(op.OrgPolicyError) as exc:
        units.add_org_unit("sales", "org")
    assert exc.value.code == "ORG_POLICY_INVALID"
    with pytest.raises(op.OrgPolicyError) as exc:
        units.remove_org_unit("sales")
    assert exc.value.code == "ORG_POLICY_HAS_CHILDREN"
    assert units.remove_org_unit("sales-eu") == {"removed": "sales-eu"}
    assert units.stats()["rejected"] == 3


def test_depth_cap_refuses_units_past_the_limit():
    units = tree(max_depth=2)
    units.add_org_unit("a", "org")
    units.add_org_unit("b", "a")
    with pytest.raises(op.OrgPolicyError) as exc:
        units.add_org_unit("c", "b")
    assert exc.value.code == "ORG_POLICY_DEPTH_EXCEEDED"
    assert units.stats()["rejected"] == 1
    with pytest.raises(op.OrgPolicyError) as exc:
        units.path_of("c")
    assert exc.value.code == "ORG_POLICY_UNKNOWN_NODE"


def test_attach_policy_returns_view_and_validates_payload():
    units = tree()
    out = units.attach_policy("org", allow("base", audit=True, max_rows=100))
    assert out == {"ou": "org", "policy": "base", "effect": "allow",
                   "values": {"audit": True, "max_rows": 100}}
    assert units.stats()["attached"] == 1
    assert units.effective_policy("org")["values"] == {
        "audit": True, "max_rows": 100,
    }

    bad_payloads = [
        None,
        {"id": "", "effect": "allow", "values": {"a": 1}},
        {"id": "p", "effect": "maybe", "values": {"a": 1}},
        {"id": "p", "effect": "allow", "values": {}},
        {"id": "p", "effect": "allow", "values": {"a": [1, 2]}},
    ]
    for bad in bad_payloads:
        with pytest.raises(op.OrgPolicyError) as exc:
            units.attach_policy("org", bad)
        assert exc.value.code == "ORG_POLICY_INVALID"
    with pytest.raises(op.OrgPolicyError) as exc:
        units.attach_policy("missing", allow("p", a=1))
    assert exc.value.code == "ORG_POLICY_UNKNOWN_NODE"
    assert units.stats()["attached"] == 1
    assert units.stats()["rejected"] == 6


def test_per_node_policy_cap_is_enforced():
    units = tree(max_policies_per_node=1)
    units.attach_policy("org", allow("base", audit=True))
    with pytest.raises(op.OrgPolicyError) as exc:
        units.attach_policy("org", allow("second", region="eu"))
    assert exc.value.code == "ORG_POLICY_CAPACITY"
    assert units.stats()["attached"] == 1
    assert units.stats()["rejected"] == 1
    assert units.effective_policy("org")["values"] == {"audit": True}


def test_effective_policy_inherits_from_all_ancestors():
    units = tree()
    units.add_org_unit("sales", "org")
    units.add_org_unit("sales-eu", "sales")
    units.attach_policy("org", allow("base", audit=True))
    units.attach_policy("sales", allow("region", region="eu"))

    effective = units.effective_policy("sales-eu")
    assert effective["path"] == ["org", "sales", "sales-eu"]
    assert effective["values"] == {"audit": True, "region": "eu"}
    assert effective["sources"] == {"audit": ["base"], "region": ["region"]}
    assert units.effective_policy("org")["values"] == {"audit": True}


def test_child_policy_overrides_inheritable_value():
    units = tree()
    units.add_org_unit("sales", "org")
    units.attach_policy("org", allow("base", max_rows=100))
    units.attach_policy("sales", allow("tight", max_rows=10))

    assert units.effective_policy("sales")["values"] == {"max_rows": 10}
    assert units.effective_policy("sales")["sources"] == {
        "max_rows": ["base", "tight"],
    }
    assert units.effective_policy("org")["values"] == {"max_rows": 100}


def test_explicit_deny_beats_allow_on_the_same_node():
    units = tree()
    units.add_org_unit("sales", "org")
    units.attach_policy("sales", allow("grant", feature=True))
    units.attach_policy("sales", deny("block", feature=False))

    effective = units.effective_policy("sales")
    assert effective["values"] == {"feature": False}
    assert effective["locked"] == ["feature"]
    assert units.effective_policy("org")["values"] == {}


def test_ancestor_hard_deny_is_reported_as_locked():
    units = tree()
    units.add_org_unit("sales", "org")
    units.add_org_unit("sales-eu", "sales")
    units.attach_policy("org", deny("no-export", export=False))
    units.attach_policy("org", allow("base", audit=True))

    effective = units.effective_policy("sales-eu")
    assert effective["values"] == {"audit": True, "export": False}
    assert effective["locked"] == ["export"]
    assert effective["sources"]["export"] == ["no-export"]


def test_detach_policy_restores_inherited_value():
    units = tree()
    units.add_org_unit("sales", "org")
    units.attach_policy("org", allow("base", max_rows=100))
    units.attach_policy("sales", allow("tight", max_rows=10))
    assert units.effective_policy("sales")["values"] == {"max_rows": 10}

    out = units.detach_policy("sales", "tight")
    assert out == {"ou": "sales", "policy": "tight", "detached": True}
    assert units.effective_policy("sales")["values"] == {"max_rows": 100}
    assert units.effective_policy("sales")["sources"] == {"max_rows": ["base"]}

    again = units.detach_policy("sales", "tight")
    assert again == {"ou": "sales", "policy": "tight", "detached": False}
    assert units.stats()["detached"] == 1


def test_explain_walks_the_chain_root_to_leaf():
    units = tree()
    units.add_org_unit("sales", "org")
    units.add_org_unit("sales-eu", "sales")
    units.attach_policy("org", allow("base", audit=True))
    units.attach_policy("sales", allow("quiet", audit=False))

    out = units.explain("sales-eu", "audit")
    assert out["value"] is False
    assert out["locked"] is False
    assert out["chain"] == [
        {"ou": "org", "policy": "base", "effect": "allow",
         "applied": True, "value": True},
        {"ou": "sales", "policy": "quiet", "effect": "allow",
         "applied": True, "value": False},
    ]

    missing = units.explain("sales-eu", "absent")
    assert missing == {"ou": "sales-eu", "field": "absent", "value": None,
                       "locked": False, "chain": []}

    with pytest.raises(op.OrgPolicyError) as exc:
        units.explain("missing", "audit")
    assert exc.value.code == "ORG_POLICY_UNKNOWN_NODE"


def test_move_subtree_reparents_every_descendant():
    units = tree()
    units.add_org_unit("a", "org")
    units.add_org_unit("a1", "a")
    units.add_org_unit("a1x", "a1")
    units.add_org_unit("b", "org")

    out = units.move_subtree("a", "b")
    assert out == {"moved": "a", "from": "org", "to": "b",
                   "affected": ["a", "a1", "a1x"]}
    assert units.path_of("a1x") == ["org", "b", "a", "a1", "a1x"]
    assert units.stats()["moved"] == 1


def test_move_subtree_refuses_to_create_a_cycle():
    units = tree()
    units.add_org_unit("a", "org")
    units.add_org_unit("a1", "a")
    units.add_org_unit("a1x", "a1")

    with pytest.raises(op.OrgPolicyError) as exc:
        units.move_subtree("a", "a1x")
    assert exc.value.code == "ORG_POLICY_CYCLE"
    assert units.path_of("a1x") == ["org", "a", "a1", "a1x"]
    with pytest.raises(op.OrgPolicyError) as exc:
        units.move_subtree("a", "a")
    assert exc.value.code == "ORG_POLICY_CYCLE"
    assert units.stats()["moved"] == 0


def test_move_subtree_refuses_to_break_the_depth_cap():
    units = tree(max_depth=2)
    units.add_org_unit("a", "org")
    units.add_org_unit("b", "a")
    units.add_org_unit("x", "org")

    with pytest.raises(op.OrgPolicyError) as exc:
        units.move_subtree("a", "x")
    assert exc.value.code == "ORG_POLICY_DEPTH_EXCEEDED"
    assert units.path_of("b") == ["org", "a", "b"]
    assert units.stats()["moved"] == 0


def test_snapshot_restore_keeps_tree_policies_and_denies():
    units = tree()
    units.add_org_unit("sales", "org")
    units.attach_policy("org", deny("no-export", export=False))
    units.attach_policy("sales", allow("quiet", audit=False))

    snap = units.snapshot()
    pristine = copy.deepcopy(snap)
    clone = tree()
    clone.restore(pristine)
    assert clone.path_of("sales") == ["org", "sales"]
    assert clone.effective_policy("sales")["values"] == {
        "audit": False, "export": False,
    }
    assert clone.effective_policy("sales")["locked"] == ["export"]
    assert clone.stats() == units.stats()

    snap["parent"]["sales"] = None
    snap["policies"]["org"]["no-export"]["values"]["export"] = True
    assert units.effective_policy("sales")["values"]["export"] is False

    empty_stats = {"attached": 0, "detached": 0, "moved": 0, "rejected": 0}
    bad_states = [
        None,
        {},
        {"version": 2, "root": "org", "parent": {"org": None},
         "policies": {}, "stats": empty_stats},
        {"version": 1, "root": "org", "parent": {},
         "policies": {}, "stats": empty_stats},
        {"version": 1, "root": "org", "parent": {"org": None, "a": "ghost"},
         "policies": {}, "stats": empty_stats},
        {"version": 1, "root": "org", "parent": {"org": None},
         "policies": {"org": {"p": {"id": "p", "effect": "nope",
                                    "values": {"a": 1}}}},
         "stats": empty_stats},
    ]
    for bad in bad_states:
        with pytest.raises(op.OrgPolicyError) as exc:
            units.restore(bad)
        assert exc.value.code == "ORG_POLICY_SNAPSHOT_INVALID"
    assert units.effective_policy("sales")["values"]["audit"] is False


def test_concurrent_attach_and_evaluation_never_see_partial_state():
    units = tree()
    units.add_org_unit("base", "org")
    units.attach_policy("org", allow("audit-on", audit=True))
    errors = []

    def writer(index):
        try:
            ou = "team-%d" % index
            units.add_org_unit(ou, "base")
            units.attach_policy(ou, allow("p%d" % index, **{"flag%d" % index: index}))
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(40):
                effective = units.effective_policy("base")
                assert effective["values"]["audit"] is True
                assert effective["sources"]["audit"] == ["audit-on"]
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert units.stats()["attached"] == 9
    for index in range(8):
        effective = units.effective_policy("team-%d" % index)
        assert effective["values"] == {"audit": True, "flag%d" % index: index}
