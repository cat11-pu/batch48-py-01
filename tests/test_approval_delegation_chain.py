import copy
import threading

import pytest

import approval_delegation_chain as dc

NOW = 1_000


def chain(**kwargs):
    return dc.ApprovalDelegationChain(**kwargs)


def delegate_link(chain_obj, delegator, delegate, scopes, *, now=0, ttl=600):
    record = chain_obj.delegate_approval(delegator, delegate, scopes,
                                         now=now, ttl=ttl)
    return chain_obj.accept_delegation(record["id"], now=now)


def test_create_request_validates_parties_and_quorum():
    links = chain()
    out = links.create_request("dave", "expense", ["bob", "alice"])
    assert out == {"id": "r1", "requester": "dave", "category": "expense",
                   "required": ["alice", "bob"], "quorum": 2,
                   "status": "pending", "votes": 0, "voters": []}

    with pytest.raises(dc.DelegationError) as exc:
        links.create_request("alice", "expense", ["alice", "bob"])
    assert exc.value.code == "DELEGATION_SELF_APPROVAL"
    with pytest.raises(dc.DelegationError) as exc:
        links.create_request("dave", "expense", [])
    assert exc.value.code == "DELEGATION_INVALID_REQUEST"
    with pytest.raises(dc.DelegationError) as exc:
        links.create_request("dave", "expense", ["alice"], quorum=3)
    assert exc.value.code == "DELEGATION_INVALID_REQUEST"
    with pytest.raises(dc.DelegationError) as exc:
        links.request_state("r9")
    assert exc.value.code == "DELEGATION_UNKNOWN"
    assert links.stats()["rejected"] == 4


def test_delegation_only_applies_after_acceptance():
    links = chain()
    record = links.delegate_approval("alice", "carol", ["expense"],
                                     now=NOW, ttl=600)
    assert record["accepted"] is False
    assert record["depth"] == 1
    assert record["parent"] is None
    assert record["expires_at"] == NOW + 600
    assert links.effective_approvers("alice", now=NOW) == ["alice"]

    accepted = links.accept_delegation(record["id"], now=NOW)
    assert accepted["accepted"] is True
    assert links.effective_approvers("alice", now=NOW) == ["alice", "carol"]
    repeated = links.accept_delegation(record["id"], now=NOW + 5)
    assert repeated == accepted
    assert links.stats()["accepted"] == 1

    with pytest.raises(dc.DelegationError) as exc:
        links.accept_delegation("d99")
    assert exc.value.code == "DELEGATION_UNKNOWN"
    with pytest.raises(dc.DelegationError) as exc:
        links.revoke_delegation("d99")
    assert exc.value.code == "DELEGATION_UNKNOWN"


def test_delegation_duration_cap_is_enforced():
    links = chain(max_duration=600)
    with pytest.raises(dc.DelegationError) as exc:
        links.delegate_approval("alice", "carol", ["expense"], ttl=601)
    assert exc.value.code == "DELEGATION_DURATION_EXCEEDED"
    assert links.list_delegations("alice") == []

    ok = links.delegate_approval("alice", "carol", ["expense"], now=10, ttl=600)
    assert ok["expires_at"] == 610
    with pytest.raises(dc.DelegationError) as exc:
        links.delegate_approval("alice", "bob", ["expense"], ttl=0)
    assert exc.value.code == "DELEGATION_INVALID_REQUEST"
    with pytest.raises(dc.DelegationError) as exc:
        links.delegate_approval("alice", "alice", ["expense"])
    assert exc.value.code == "DELEGATION_INVALID_REQUEST"
    assert links.stats()["delegated"] == 1
    assert links.stats()["rejected"] == 3


def test_delegation_depth_cap_blocks_deeper_chains():
    links = chain(max_depth=2)
    delegate_link(links, "alice", "bob", ["expense"], ttl=900)
    second = delegate_link(links, "bob", "carol", ["expense"], ttl=900)
    assert second["depth"] == 2
    assert links.effective_approvers("alice") == ["alice", "bob", "carol"]

    with pytest.raises(dc.DelegationError) as exc:
        links.delegate_approval("carol", "dave", ["expense"], ttl=900)
    assert exc.value.code == "DELEGATION_DEPTH_EXCEEDED"
    assert len(links.list_delegations("carol")) == 0


def test_redelegation_cannot_widen_the_scope():
    links = chain()
    first = delegate_link(links, "alice", "bob", ["expense", "travel"])
    with pytest.raises(dc.DelegationError) as exc:
        links.delegate_approval("bob", "carol", ["expense", "travel", "payroll"])
    assert exc.value.code == "DELEGATION_SCOPE_EXCEEDED"
    assert links.list_delegations("bob") == []

    second = delegate_link(links, "bob", "carol", ["expense"])
    assert len(links.list_delegations("bob")) == 1
    assert second["scopes"] == ["expense"]
    assert second["parent"] == first["id"]
    assert second["depth"] == 2
    assert links.effective_approvers("alice", scopes=["expense"]) == [
        "alice", "bob", "carol",
    ]
    assert links.effective_approvers("alice", scopes=["travel"]) == [
        "alice", "bob",
    ]


def test_base_approvers_reach_quorum_in_any_order():
    links = chain()
    request = links.create_request("dave", "expense", ["alice", "bob"])
    first = links.decide(request["id"], "alice", "approve", now=NOW)
    assert first == {"id": "r1", "requester": "dave", "category": "expense",
                     "required": ["alice", "bob"], "quorum": 2,
                     "status": "pending", "votes": 1, "voters": ["alice"]}
    second = links.decide(request["id"], "bob", "approve", now=NOW)
    assert second["status"] == "approved"
    assert second["votes"] == 2
    assert second["voters"] == ["alice", "bob"]
    assert links.stats()["decided"] == 2


def test_requester_cannot_vote_through_own_delegation():
    links = chain()
    delegate_link(links, "alice", "dave", ["expense"], now=NOW)
    request = links.create_request("dave", "expense", ["alice"], quorum=1)
    with pytest.raises(dc.DelegationError) as exc:
        links.decide(request["id"], "dave", "approve", now=NOW)
    assert exc.value.code == "DELEGATION_SELF_APPROVAL"
    assert links.request_state(request["id"])["votes"] == 0
    assert links.stats()["decided"] == 0


def test_duplicate_vote_from_same_approver_counts_once():
    links = chain()
    request = links.create_request("dave", "expense", ["alice", "bob"])
    links.decide(request["id"], "alice", "approve", now=NOW)
    again = links.decide(request["id"], "alice", "approve", now=NOW + 1)
    assert again["votes"] == 1
    assert again["status"] == "pending"
    assert links.stats()["decided"] == 1

    done = links.decide(request["id"], "bob", "approve", now=NOW + 2)
    assert done["status"] == "approved"
    assert done["votes"] == 2
    assert links.request_state(request["id"])["voters"] == ["alice", "bob"]


def test_delegate_vote_satisfies_the_granting_approver():
    links = chain()
    delegate_link(links, "alice", "carol", ["expense"], now=NOW)
    request = links.create_request("dave", "expense", ["alice"], quorum=1)
    out = links.decide(request["id"], "carol", "approve", now=NOW)
    assert out["status"] == "approved"
    assert out["votes"] == 1
    assert out["voters"] == ["carol"]


def test_vote_outside_delegated_scope_is_refused():
    links = chain()
    delegate_link(links, "alice", "carol", ["expense"], now=NOW)
    request = links.create_request("dave", "payroll", ["alice"], quorum=1)
    with pytest.raises(dc.DelegationError) as exc:
        links.decide(request["id"], "carol", "approve", now=NOW)
    assert exc.value.code == "DELEGATION_UNAUTHORIZED"
    assert links.request_state(request["id"])["votes"] == 0
    assert links.decide(request["id"], "alice", "approve",
                        now=NOW)["status"] == "approved"


def test_reject_decision_ends_the_request():
    links = chain()
    request = links.create_request("dave", "expense", ["alice", "bob"])
    out = links.decide(request["id"], "bob", "reject", now=NOW)
    assert out["status"] == "rejected"
    assert out["votes"] == 1
    after = links.decide(request["id"], "alice", "approve", now=NOW + 1)
    assert after["status"] == "rejected"
    assert after["votes"] == 1


def test_revoking_a_delegation_removes_the_stand_in():
    links = chain()
    record = delegate_link(links, "alice", "carol", ["expense"], now=NOW)
    request = links.create_request("dave", "expense", ["alice"], quorum=1)
    out = links.revoke_delegation(record["id"], now=NOW)
    assert out == {"id": "d1", "revoked": ["d1"]}
    assert links.effective_approvers("alice", now=NOW) == ["alice"]
    with pytest.raises(dc.DelegationError) as exc:
        links.decide(request["id"], "carol", "approve", now=NOW)
    assert exc.value.code == "DELEGATION_UNAUTHORIZED"
    assert links.revoke_delegation(record["id"], now=NOW) == {
        "id": "d1", "revoked": [],
    }
    assert links.stats()["revoked"] == 1


def test_expiry_returns_pending_votes_to_the_original_approver():
    links = chain()
    delegate_link(links, "alice", "carol", ["expense"], now=0, ttl=100)
    request = links.create_request("dave", "expense", ["alice", "bob"])
    links.decide(request["id"], "carol", "approve", now=50)
    assert links.request_state(request["id"])["votes"] == 1

    out = links.expire_due(now=100)
    assert out == {"delegations": ["d1"], "requests": ["r1"]}
    state = links.request_state(request["id"])
    assert state["votes"] == 0
    assert state["status"] == "pending"
    assert links.effective_approvers("alice", now=100) == ["alice"]
    assert links.expire_due(now=100) == {"delegations": [], "requests": []}

    resumed = links.decide(request["id"], "alice", "approve", now=100)
    assert resumed["votes"] == 1
    assert resumed["voters"] == ["alice"]


def test_snapshot_restore_keeps_delegations_and_vote_state():
    links = chain()
    record = delegate_link(links, "alice", "carol", ["expense"], now=NOW)
    request = links.create_request("dave", "expense", ["alice", "bob"])
    links.decide(request["id"], "carol", "approve", now=NOW)

    snap = links.snapshot()
    pristine = copy.deepcopy(snap)
    clone = chain()
    clone.restore(pristine)
    assert clone.request_state("r1")["votes"] == 1
    assert clone.effective_approvers("alice", now=NOW) == ["alice", "carol"]
    assert clone.list_delegations("alice")[0]["id"] == record["id"]
    assert clone.stats() == links.stats()

    snap["requests"]["r1"]["votes"].clear()
    snap["delegations"]["d1"]["active"] = False
    assert links.request_state("r1")["votes"] == 1
    assert links.effective_approvers("alice", now=NOW) == ["alice", "carol"]

    empty_stats = {"delegated": 0, "accepted": 0, "revoked": 0,
                   "decided": 0, "expired": 0, "rejected": 0}
    bad_states = [
        None,
        {},
        {"version": 2, "max_depth": 3, "max_duration": 600,
         "delegations": {}, "requests": {}, "stats": empty_stats},
        {"version": 1, "max_depth": 3, "max_duration": 600,
         "delegations": {}, "requests": {}, "stats": {"decided": 0}},
        {"version": 1, "max_depth": 3, "max_duration": 600,
         "delegations": {"d1": {"id": "d9", "delegator": "a", "delegate": "b",
                                "scopes": ["x"], "expires_at": 1, "depth": 1,
                                "parent": None, "active": True,
                                "accepted_at": 0}},
         "requests": {}, "stats": empty_stats},
        {"version": 1, "max_depth": 3, "max_duration": 600,
         "delegations": {},
         "requests": {"r1": {"id": "r1", "requester": "d", "category": "c",
                             "required": ["a"], "quorum": 0,
                             "status": "pending", "votes": {}}},
         "stats": empty_stats},
    ]
    for bad in bad_states:
        with pytest.raises(dc.DelegationError) as exc:
            links.restore(bad)
        assert exc.value.code == "DELEGATION_SNAPSHOT_INVALID"
    assert links.request_state("r1")["status"] == "pending"


def test_concurrent_votes_are_counted_exactly_once_per_approver():
    links = chain()
    required = ["alice", "bob", "carol", "dave"]
    request = links.create_request("erin", "expense", required, quorum=4)
    errors = []

    def worker(name):
        try:
            for offset in range(2):
                links.decide(request["id"], name, "approve", now=NOW + offset)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(name,))
               for name in required for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    state = links.request_state(request["id"])
    assert state["votes"] == 4
    assert state["voters"] == required
    assert state["status"] == "approved"
