import copy
import threading

import pytest

import resource_node_registry as nr


def test_register_node_exposes_storage_fields():
    registry = nr.NodeRegistry(claim_lease=30, default_respawn=120)
    out = registry.register_node("iron-1", capacity=40, now=0)
    assert out["node_id"] == "iron-1"
    assert out["capacity"] == 40
    assert out["remaining"] == 40
    assert out["respawn_seconds"] == 120
    assert out["respawn_at"] is None
    assert out["depleted"] is False
    assert out["occupier"] is None
    assert registry.node("iron-1")["remaining"] == 40
    with pytest.raises(nr.NodeError) as exc:
        registry.register_node("iron-1", capacity=10, now=0)
    assert exc.value.code == "NODE_DUPLICATE"
    with pytest.raises(nr.NodeError) as exc:
        registry.register_node("iron-2", capacity=0, now=0)
    assert exc.value.code == "NODE_INVALID"
    with pytest.raises(nr.NodeError) as exc:
        registry.register_node("iron-3", capacity=10, respawn_seconds=-5,
                               now=0)
    assert exc.value.code == "NODE_INVALID"


def test_claim_reserves_a_node_for_one_player():
    registry = nr.NodeRegistry(claim_lease=30)
    registry.register_node("iron-1", capacity=10, now=0)
    claim = registry.claim("iron-1", "ann", now=0)
    assert claim["claim_id"] == "clm-1"
    assert claim["node_id"] == "iron-1"
    assert claim["player"] == "ann"
    assert claim["remaining"] == 10
    assert claim["expires_at"] == 30
    assert claim["produced"] == 0
    with pytest.raises(nr.NodeError) as exc:
        registry.claim("iron-1", "bob", now=1)
    assert exc.value.code == "NODE_OCCUPIED"
    assert len(registry.claims()) == 1
    assert registry.claims("ann")[0]["player"] == "ann"
    assert registry.claims("bob") == []


def test_harvest_draws_down_node_and_records_output():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    first = registry.harvest("clm-1", 4, now=5)
    assert first["harvested"] == 4
    assert first["remaining"] == 6
    assert first["depleted"] is False
    assert registry.inventory("ann") == 4
    assert registry.node("iron-1")["remaining"] == 6
    second = registry.harvest("clm-1", 3, now=6)
    assert second["harvested"] == 3
    assert second["remaining"] == 3
    assert registry.inventory("ann") == 7


def test_harvest_cannot_exceed_node_storage():
    registry = nr.NodeRegistry(default_respawn=120)
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    out = registry.harvest("clm-1", 25, now=10)
    assert out["harvested"] == 10
    assert out["remaining"] == 0
    assert out["depleted"] is True
    assert registry.inventory("ann") == 10
    node = registry.node("iron-1")
    assert node["depleted"] is True
    assert node["remaining"] == 0
    assert node["respawn_at"] == 130


def test_depleted_node_cannot_be_claimed_before_respawn():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=5, now=0)
    registry.claim("iron-1", "ann", now=0)
    registry.harvest("clm-1", 10, now=0)
    registry.release("clm-1")
    with pytest.raises(nr.NodeError) as exc:
        registry.claim("iron-1", "bob", now=20)
    assert exc.value.code == "NODE_DEPLETED"
    assert registry.available("iron-1", now=20) is False


def test_respawn_window_starts_at_depletion():
    registry = nr.NodeRegistry(default_respawn=60)
    registry.register_node("iron-1", capacity=5, now=0)
    registry.claim("iron-1", "ann", now=0)
    registry.harvest("clm-1", 5, now=10)
    assert registry.node("iron-1")["respawn_at"] == 70
    assert registry.available("iron-1", now=69) is False
    assert registry.node("iron-1")["remaining"] == 0
    assert registry.available("iron-1", now=70) is True
    assert registry.node("iron-1")["remaining"] == 5
    assert registry.node("iron-1")["respawn_at"] is None
    claim = registry.claim("iron-1", "bob", now=70)
    assert claim["player"] == "bob"


def test_harvest_after_depletion_is_refused():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=5, now=0)
    registry.claim("iron-1", "ann", now=0)
    registry.harvest("clm-1", 5, now=0)
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-1", 1, now=1)
    assert exc.value.code == "CLAIM_EMPTY"
    assert registry.inventory("ann") == 5


def test_release_hands_the_node_to_the_next_player():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    out = registry.release("clm-1")
    assert out["claim_id"] == "clm-1"
    assert out["released"] is True
    assert registry.node("iron-1")["occupier"] is None
    second = registry.claim("iron-1", "bob", now=2)
    assert second["claim_id"] == "clm-2"
    assert second["player"] == "bob"
    with pytest.raises(nr.NodeError) as exc:
        registry.release("clm-1")
    assert exc.value.code == "CLAIM_INVALID"
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-1", 1, now=3)
    assert exc.value.code == "CLAIM_INVALID"
    assert [claim["player"] for claim in registry.claims()] == ["bob"]


def test_expired_claim_is_reclaimed_on_next_access():
    registry = nr.NodeRegistry(claim_lease=30)
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-1", 1, now=31)
    assert exc.value.code == "CLAIM_EXPIRED"
    second = registry.claim("iron-1", "bob", now=31)
    assert second["player"] == "bob"
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-1", 1, now=32)
    assert exc.value.code == "CLAIM_EXPIRED"
    assert registry.inventory("ann") == 0


def test_removed_node_invalidates_open_claims():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    out = registry.remove_node("iron-1")
    assert out["node_id"] == "iron-1"
    assert out["removed"] is True
    with pytest.raises(nr.NodeError) as exc:
        registry.node("iron-1")
    assert exc.value.code == "NODE_UNKNOWN"
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-1", 1, now=1)
    assert exc.value.code == "CLAIM_INVALID"
    with pytest.raises(nr.NodeError) as exc:
        registry.claim("iron-1", "bob", now=1)
    assert exc.value.code == "NODE_UNKNOWN"
    with pytest.raises(nr.NodeError) as exc:
        registry.available("iron-1", now=1)
    assert exc.value.code == "NODE_UNKNOWN"


def test_unknown_node_and_claim_are_refused():
    registry = nr.NodeRegistry()
    with pytest.raises(nr.NodeError) as exc:
        registry.claim("ghost", "ann", now=0)
    assert exc.value.code == "NODE_UNKNOWN"
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-9", 1, now=0)
    assert exc.value.code == "CLAIM_UNKNOWN"
    with pytest.raises(nr.NodeError) as exc:
        registry.release("clm-9")
    assert exc.value.code == "CLAIM_UNKNOWN"
    with pytest.raises(nr.NodeError) as exc:
        registry.respawn("ghost", now=0)
    assert exc.value.code == "NODE_UNKNOWN"


def test_non_positive_harvest_amount_is_refused():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    for amount in (0, -3):
        with pytest.raises(nr.NodeError) as exc:
            registry.harvest("clm-1", amount, now=1)
        assert exc.value.code == "CLAIM_INVALID_AMOUNT"
    assert registry.node("iron-1")["remaining"] == 10
    assert registry.inventory("ann") == 0


def test_snapshot_restore_keeps_storage_claims_and_bag():
    registry = nr.NodeRegistry(claim_lease=30, default_respawn=60)
    registry.register_node("iron-1", capacity=20, now=0)
    registry.claim("iron-1", "ann", now=0)
    registry.harvest("clm-1", 3, now=5)
    state = registry.snapshot()
    pristine = copy.deepcopy(state)
    state["nodes"].clear()
    assert registry.node("iron-1")["remaining"] == 17
    restored = nr.NodeRegistry(claim_lease=30, default_respawn=60)
    restored.restore(pristine)
    assert restored.node("iron-1")["remaining"] == 17
    assert restored.inventory("ann") == 3
    open_claims = restored.claims()
    assert len(open_claims) == 1
    assert open_claims[0]["claim_id"] == "clm-1"
    assert open_claims[0]["expires_at"] == 30
    follow_up = restored.harvest("clm-1", 2, now=10)
    assert follow_up["remaining"] == 15
    assert restored.inventory("ann") == 5


def test_invalid_snapshot_is_rejected_without_changing_state():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=10, now=0)
    registry.claim("iron-1", "ann", now=0)
    bad_states = [
        None,
        {},
        {"version": 2, "nodes": {}, "claims": {}, "inventories": {}},
        {"version": 1, "nodes": "nope", "claims": {}, "inventories": {}},
        {"version": 1, "nodes": {"iron-1": "bad"}, "claims": {},
         "inventories": {}},
    ]
    for bad in bad_states:
        with pytest.raises(nr.NodeError) as exc:
            registry.restore(bad)
        assert exc.value.code == "NODE_SNAPSHOT_INVALID"
    assert registry.node("iron-1")["remaining"] == 10
    out = registry.harvest("clm-1", 1, now=1)
    assert out["remaining"] == 9


def test_concurrent_harvest_never_overdraws():
    registry = nr.NodeRegistry()
    registry.register_node("iron-1", capacity=50, now=0)
    registry.claim("iron-1", "ann", now=0)
    barrier = threading.Barrier(8)
    results = []
    errors = []

    def worker():
        barrier.wait()
        try:
            results.append(registry.harvest("clm-1", 10, now=5))
        except nr.NodeError as exc:
            errors.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) + len(errors) == 8
    assert set(errors) <= {"CLAIM_EMPTY"}
    assert sum(result["harvested"] for result in results) == 50
    assert all(result["harvested"] >= 0 for result in results)
    assert registry.inventory("ann") == 50
    assert registry.node("iron-1")["remaining"] == 0
    with pytest.raises(nr.NodeError) as exc:
        registry.harvest("clm-1", 1, now=6)
    assert exc.value.code == "CLAIM_EMPTY"
