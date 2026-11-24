import copy
import threading

import pytest

import snapshot_delta_encoder as sde

FIELDS = {"x": 0, "y": 0, "hp": 100}


def make(**kwargs):
    return sde.SnapshotDeltaEncoder(**kwargs)


def prime(guard, tick=1, budget=100):
    guard.register_entity("e1", dict(FIELDS), local=True)
    guard.register_entity("e2", {"x": 5, "y": 5, "hp": 50})
    out = guard.encode(tick=tick, budget=budget)
    guard.ack(tick=tick)
    return out


def test_first_encode_is_a_keyframe_with_create_events():
    guard = make()
    guard.register_entity("e1", {"x": 0, "y": 0, "hp": 100})
    out = guard.encode(tick=1, budget=100)
    assert out["tick"] == 1
    assert out["keyframe"] is True
    assert out["events"] == [{"type": "create", "entity": "e1"}]
    assert out["entities"] == [{"entity": "e1", "fields": {"x": 0, "y": 0, "hp": 100}}]
    assert out["used"] == 4
    assert out["deferred"] == 0
    assert guard.stats()["fields"] == 3


def test_duplicate_entity_registration_is_refused():
    guard = make()
    guard.register_entity("e1", {"x": 1})
    with pytest.raises(sde.DeltaError) as exc:
        guard.register_entity("e1", {"x": 2})
    assert exc.value.code == "DELTA_DUPLICATE_ENTITY"
    assert guard.baseline_of("e1") is None
    assert guard.dirty_of("e1") == ["x"]
    assert guard.entities()["e1"]["fields"] == {"x": 1}


def test_unknown_entity_operations_are_refused():
    guard = make()
    with pytest.raises(sde.DeltaError) as exc:
        guard.update_field("ghost", "hp", 1)
    assert exc.value.code == "DELTA_UNKNOWN_ENTITY"
    with pytest.raises(sde.DeltaError) as exc:
        guard.set_distance("ghost", 10.0)
    assert exc.value.code == "DELTA_UNKNOWN_ENTITY"
    with pytest.raises(sde.DeltaError) as exc:
        guard.remove_entity("ghost")
    assert exc.value.code == "DELTA_UNKNOWN_ENTITY"


def test_delta_encode_sends_only_changed_fields():
    guard = make()
    prime(guard)
    guard.update_field("e1", "hp", 90)
    assert guard.dirty_of("e1") == ["hp"]
    out = guard.encode(tick=2, budget=100)
    assert out["keyframe"] is False
    assert out["entities"] == [{"entity": "e1", "fields": {"hp": 90}}]
    assert out["used"] == 1


def test_unchanged_entities_are_not_resent():
    guard = make()
    prime(guard)
    out = guard.encode(tick=2, budget=100)
    assert out == {
        "tick": 2,
        "keyframe": False,
        "entities": [],
        "events": [],
        "used": 0,
        "deferred": 0,
    }


def test_priority_orders_local_then_near_then_far():
    guard = make()
    guard.register_entity("far", {"hp": 1})
    guard.register_entity("near", {"hp": 2})
    guard.register_entity("local", {"hp": 3}, local=True)
    guard.set_distance("far", 1000.0)
    guard.set_distance("near", 200.0)
    guard.set_distance("local", 500.0)
    guard.encode(tick=1, budget=100)
    guard.ack(tick=1)
    for name in ("far", "near", "local"):
        guard.update_field(name, "hp", 10)
    out = guard.encode(tick=2, budget=100)
    assert [item["entity"] for item in out["entities"]] == ["local", "near", "far"]
    assert out["used"] == 3


def test_budget_defers_a_whole_entity_and_keeps_dirty_flags():
    guard = make()
    guard.register_entity("hot", {"hp": 1}, local=True)
    guard.register_entity("cold", {"hp": 2})
    guard.set_distance("cold", 900.0)
    guard.encode(tick=1, budget=100)
    guard.ack(tick=1)
    guard.update_field("hot", "hp", 10)
    guard.update_field("cold", "hp", 20)
    out = guard.encode(tick=2, budget=1)
    assert out["entities"] == [{"entity": "hot", "fields": {"hp": 10}}]
    assert out["deferred"] == 1
    assert out["used"] == 1
    assert guard.dirty_of("cold") == ["hp"]
    assert guard.baseline_of("cold") == {"hp": 2}


def test_deferred_entity_is_shipped_on_the_next_tick():
    guard = make()
    guard.register_entity("hot", {"hp": 1}, local=True)
    guard.register_entity("cold", {"hp": 2})
    guard.set_distance("cold", 900.0)
    guard.encode(tick=1, budget=100)
    guard.ack(tick=1)
    guard.update_field("hot", "hp", 10)
    guard.update_field("cold", "hp", 20)
    guard.encode(tick=2, budget=1)
    out = guard.encode(tick=3, budget=10)
    assert [item["entity"] for item in out["entities"]] == ["hot", "cold"]
    assert out["used"] == 2
    assert out["deferred"] == 0


def test_keyframe_interval_forces_full_state():
    guard = make(keyframe_interval=3)
    guard.register_entity("e1", {"x": 1, "y": 2})
    guard.encode(tick=1, budget=50)
    guard.ack(tick=1)
    guard.update_field("e1", "x", 9)
    guard.encode(tick=2, budget=50)
    guard.ack(tick=2)
    out = guard.encode(tick=3, budget=50)
    assert out["keyframe"] is True
    assert out["entities"] == [{"entity": "e1", "fields": {"x": 9, "y": 2}}]
    assert out["used"] == 2


def test_remove_entity_emits_explicit_event_and_drops_fields():
    guard = make()
    guard.register_entity("e1", {"hp": 1})
    guard.register_entity("e2", {"hp": 2})
    guard.encode(tick=1, budget=50)
    guard.ack(tick=1)
    guard.remove_entity("e2")
    out = guard.encode(tick=2, budget=50)
    assert out["events"] == [{"type": "remove", "entity": "e2"}]
    assert out["entities"] == []
    assert guard.baseline_of("e2") is None
    with pytest.raises(sde.DeltaError) as exc:
        guard.update_field("e2", "hp", 3)
    assert exc.value.code == "DELTA_UNKNOWN_ENTITY"


def test_ack_advances_the_baseline_and_clears_dirty_flags():
    guard = make()
    guard.register_entity("e1", {"hp": 100})
    guard.encode(tick=1, budget=50)
    assert guard.dirty_of("e1") == ["hp"]
    assert guard.ack(tick=1) == {"tick": 1, "applied": ["e1"]}
    assert guard.dirty_of("e1") == []
    guard.update_field("e1", "hp", 90)
    assert guard.dirty_of("e1") == ["hp"]
    guard.encode(tick=2, budget=50)
    assert guard.dirty_of("e1") == ["hp"]
    assert guard.ack(tick=2) == {"tick": 2, "applied": ["e1"]}
    assert guard.dirty_of("e1") == []
    assert guard.baseline_of("e1") == {"hp": 90}
    assert guard.encode(tick=3, budget=50)["entities"] == []


def test_acknowledging_a_tick_that_was_never_encoded_is_refused():
    guard = make()
    guard.register_entity("e1", {"hp": 1})
    guard.encode(tick=1, budget=50)
    with pytest.raises(sde.DeltaError) as exc:
        guard.ack(tick=9)
    assert exc.value.code == "DELTA_UNKNOWN_TICK"
    assert guard.stats()["acks"] == 0


def test_stats_count_fields_events_and_deferrals():
    guard = make()
    guard.register_entity("e1", {"x": 0, "y": 0, "hp": 100})
    guard.encode(tick=1, budget=100)
    guard.ack(tick=1)
    guard.update_field("e1", "hp", 90)
    guard.encode(tick=2, budget=1)
    guard.ack(tick=2)
    assert guard.stats() == {
        "encoded": 2,
        "keyframes": 1,
        "fields": 4,
        "events": 1,
        "deferred": 0,
        "acks": 2,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "encoded": 0,
        "keyframes": 0,
        "fields": 0,
        "events": 0,
        "deferred": 0,
        "acks": 0,
    }


def test_snapshot_restore_keeps_baselines_and_dirty_flags():
    guard = make()
    guard.register_entity("e1", {"hp": 100})
    guard.encode(tick=1, budget=50)
    guard.ack(tick=1)
    guard.update_field("e1", "hp", 70)
    state = guard.snapshot()
    locked = copy.deepcopy(state)
    assert state["version"] == 1
    guard.update_field("e1", "hp", 10)
    assert state == locked
    twin = make()
    twin.restore(state)
    assert twin.dirty_of("e1") == ["hp"]
    assert twin.baseline_of("e1") == {"hp": 100}
    assert twin.encode(tick=2, budget=10)["entities"] == [
        {"entity": "e1", "fields": {"hp": 70}}
    ]
    for bad in (None, [], {"version": 2}):
        with pytest.raises(sde.DeltaError) as exc:
            twin.restore(bad)
        assert exc.value.code == "DELTA_SNAPSHOT_INVALID"
    assert twin.dirty_of("e1") == ["hp"]


def test_concurrent_field_updates_are_all_recorded():
    guard = make()
    names = [f"e{index}" for index in range(8)]
    for name in names:
        guard.register_entity(name, {"hp": 100})
    guard.encode(tick=1, budget=100)
    guard.ack(tick=1)
    errors = []

    def worker(name):
        try:
            for value in range(1, 5):
                guard.update_field(name, "hp", value)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert all(guard.dirty_of(name) == ["hp"] for name in names)
    out = guard.encode(tick=2, budget=100)
    assert len(out["entities"]) == 8
    assert guard.stats()["fields"] == 16
