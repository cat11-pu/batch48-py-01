import copy
import threading

import pytest

import sim_lod_guard as slg


def make(**kwargs):
    return slg.SimLodGuard(**kwargs)


def register(guard, name, distance, region="A"):
    return guard.register_entity(name, distance=distance, region=region)


def test_registration_assigns_tiers_by_distance():
    guard = make()
    assert register(guard, "a", 10.0) == {"entity": "a", "tier": "each_tick", "region": "A"}
    assert register(guard, "b", 150.0)["tier"] == "every_2"
    assert register(guard, "c", 400.0)["tier"] == "every_8"
    assert register(guard, "d", 900.0)["tier"] == "sleep"
    assert guard.tier_of("c") == "every_8"
    assert guard.entities()["d"] == {
        "tier": "sleep",
        "distance": 900.0,
        "region": "A",
        "last_updated": None,
    }
    assert guard.last_updated("a") is None
    assert guard.budget() == 8


def test_duplicate_registration_is_refused():
    guard = make()
    register(guard, "a", 10.0)
    with pytest.raises(slg.SimError) as exc:
        register(guard, "a", 900.0)
    assert exc.value.code == "SIM_DUPLICATE_ENTITY"
    assert guard.tier_of("a") == "each_tick"
    assert guard.entities()["a"]["distance"] == 10.0


def test_unknown_entities_are_refused():
    guard = make()
    calls = [
        lambda: guard.update_distance("ghost", 10.0),
        lambda: guard.set_tier("ghost", "sleep"),
        lambda: guard.tier_of("ghost"),
        lambda: guard.last_updated("ghost"),
    ]
    for call in calls:
        with pytest.raises(slg.SimError) as exc:
            call()
        assert exc.value.code == "SIM_UNKNOWN_ENTITY"


def test_explicit_tier_change_and_invalid_names():
    guard = make()
    register(guard, "a", 10.0)
    assert guard.set_tier("a", "every_8") == {"entity": "a", "tier": "every_8"}
    assert guard.tier_of("a") == "every_8"
    assert guard.stats()["tier_changes"] == 1
    with pytest.raises(slg.SimError) as exc:
        guard.set_tier("a", "turbo")
    assert exc.value.code == "SIM_TIER_INVALID"
    assert guard.tier_of("a") == "every_8"
    assert guard.stats()["tier_changes"] == 1


def test_hysteresis_prevents_tier_flapping():
    guard = make(hysteresis=20.0)
    register(guard, "a", 90.0)
    assert guard.update_distance("a", 110.0) == {
        "entity": "a",
        "tier": "each_tick",
        "changed": False,
    }
    assert guard.update_distance("a", 125.0) == {
        "entity": "a",
        "tier": "every_2",
        "changed": True,
    }
    assert guard.update_distance("a", 95.0) == {
        "entity": "a",
        "tier": "every_2",
        "changed": False,
    }
    assert guard.update_distance("a", 75.0)["tier"] == "each_tick"
    assert guard.update_distance("a", 900.0) == {
        "entity": "a",
        "tier": "sleep",
        "changed": True,
    }
    assert guard.entities()["a"]["distance"] == 900.0
    assert guard.stats()["tier_changes"] == 3


def test_region_change_recomputes_tier_without_hysteresis():
    guard = make()
    register(guard, "a", 90.0)
    guard.update_distance("a", 110.0)
    assert guard.tier_of("a") == "each_tick"
    assert guard.move_region("a", "B") == {"entity": "a", "region": "B", "tier": "every_2"}
    assert guard.stats()["tier_changes"] == 1


def test_each_tick_tier_is_updated_every_frame():
    guard = make()
    register(guard, "a", 10.0)
    register(guard, "b", 400.0)
    assert guard.tick() == {"tick": 1, "updated": ["a"], "deferred": []}
    assert guard.tick() == {"tick": 2, "updated": ["a"], "deferred": []}
    assert guard.last_updated("a") == 2
    assert guard.last_updated("b") is None


def test_every_two_and_every_eight_cadence():
    guard = make()
    register(guard, "a", 10.0)
    register(guard, "b", 150.0)
    register(guard, "c", 400.0)
    assert guard.tick()["updated"] == ["a"]
    assert guard.tick()["updated"] == ["a", "b"]
    assert guard.tick()["updated"] == ["a"]
    assert guard.tick()["updated"] == ["a", "b"]
    for _ in range(3):
        guard.tick()
    assert guard.tick() == {"tick": 8, "updated": ["a", "b", "c"], "deferred": []}


def test_budget_limits_updates_and_reports_deferred():
    guard = make(budget=2)
    for name in ("a", "b", "c"):
        register(guard, name, 10.0)
    assert guard.tick() == {"tick": 1, "updated": ["a", "b"], "deferred": ["c"]}
    assert guard.last_updated("c") is None
    assert guard.stats()["updates"] == 2
    assert guard.stats()["deferred"] == 1


def test_budget_prefers_the_higher_tier():
    guard = make(budget=1)
    register(guard, "slow", 150.0)
    register(guard, "fast", 10.0)
    assert guard.tick() == {"tick": 1, "updated": ["fast"], "deferred": []}
    assert guard.tick() == {"tick": 2, "updated": ["fast"], "deferred": ["slow"]}


def test_sleeping_entities_never_update():
    guard = make()
    register(guard, "campfire", 900.0)
    for _ in range(16):
        guard.tick()
    assert guard.last_updated("campfire") is None
    assert guard.stats()["updates"] == 0


def test_equal_priority_breaks_ties_by_entity_name():
    guard = make(budget=1)
    register(guard, "b", 10.0)
    register(guard, "a", 10.0)
    assert guard.tick()["updated"] == ["a"]


def test_stats_and_reset():
    guard = make()
    register(guard, "a", 10.0)
    register(guard, "b", 150.0)
    register(guard, "c", 400.0)
    register(guard, "d", 900.0)
    guard.tick()
    guard.tick()
    assert guard.stats() == {
        "ticks": 2,
        "updates": 3,
        "deferred": 0,
        "tier_changes": 0,
    }
    guard.set_tier("d", "every_8")
    assert guard.stats()["tier_changes"] == 1
    guard.reset_stats()
    assert guard.stats() == {
        "ticks": 0,
        "updates": 0,
        "deferred": 0,
        "tier_changes": 0,
    }


def test_snapshot_restore_replays_identical_ticks():
    guard = make()
    register(guard, "a", 10.0)
    register(guard, "b", 150.0)
    guard.tick()
    guard.set_tier("b", "every_8")
    state = guard.snapshot()
    locked = copy.deepcopy(state)
    assert state["version"] == 1
    second = guard.tick()
    assert second == {"tick": 2, "updated": ["a"], "deferred": []}
    assert state == locked
    twin = make()
    twin.restore(state)
    assert twin.stats()["ticks"] == 1
    assert twin.tick() == second
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(slg.SimError) as exc:
            twin.restore(bad)
        assert exc.value.code == "SIM_SNAPSHOT_INVALID"
    assert twin.last_updated("a") == 2


def test_concurrent_tier_switches_count_exactly():
    guard = make()
    names = [f"e{index}" for index in range(8)]
    for name in names:
        register(guard, name, 10.0)
    errors = []

    def worker(name):
        try:
            guard.update_distance(name, 900.0)
            guard.set_tier(name, "every_8")
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert all(guard.tier_of(name) == "every_8" for name in names)
    assert guard.stats()["tier_changes"] == 16
