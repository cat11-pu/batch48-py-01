import copy
import threading

import pytest

import aoi_subscription_guard as aoi


def make(**kwargs):
    settings = {"radius": 1, "leave_lag": 1, "budget": 4, "cell_size": 10.0}
    settings.update(kwargs)
    return aoi.AoiSubscriptionGuard(**settings)


def test_cell_of_uses_floor_division():
    guard = make(cell_size=10.0)
    assert guard.cell_of(0, 0) == (0, 0)
    assert guard.cell_of(9.9, 10.0) == (0, 1)
    assert guard.cell_of(-0.1, -0.1) == (-1, -1)
    assert guard.cell_of(25.0, -15.5) == (2, -2)


def test_add_observer_reports_cell_and_rejects_duplicates():
    guard = make()
    assert guard.add_observer("a", x=5, y=5) == {
        "observer": "a",
        "cell": (0, 0),
        "radius": 1,
        "budget": 4,
    }
    assert guard.add_observer("b", x=105, y=5, budget=2)["budget"] == 2
    with pytest.raises(aoi.AoiError) as exc:
        guard.add_observer("a", x=0, y=0)
    assert exc.value.code == "AOI_DUPLICATE"
    with pytest.raises(aoi.AoiError) as exc:
        guard.add_observer("", x=0, y=0)
    assert exc.value.code == "AOI_INVALID_ARGUMENT"
    with pytest.raises(aoi.AoiError) as exc:
        guard.visible("ghost")
    assert exc.value.code == "AOI_UNKNOWN_OBSERVER"
    with pytest.raises(aoi.AoiError) as exc:
        guard.subscribers("ghost")
    assert exc.value.code == "AOI_UNKNOWN_ENTITY"


def test_add_entity_subscribes_nearby_observers_only():
    guard = make()
    guard.add_observer("near", x=5, y=5)
    guard.add_observer("edge", x=15, y=5)
    guard.add_observer("far", x=205, y=5)
    assert guard.add_entity("mob", x=5, y=5) == {
        "entity": "mob",
        "cell": (0, 0),
        "kind": "npc",
        "priority": 0,
    }
    assert guard.visible("near") == ["mob"]
    assert guard.visible("edge") == ["mob"]
    assert guard.visible("far") == []
    assert guard.subscribers("mob") == ["edge", "near"]


def test_end_tick_emits_enter_events_and_advances_the_stream():
    guard = make()
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("mob", x=5, y=5)
    assert guard.next_seq() == 1
    assert guard.end_tick() == {
        "tick": 1,
        "events": [{"seq": 1, "kind": "enter", "observer": "a", "entity": "mob"}],
    }
    assert guard.next_seq() == 2
    assert guard.end_tick() == {"tick": 2, "events": []}
    assert guard.next_seq() == 2


def test_radius_boundary_counts_chebyshev_neighbour_cells():
    guard = make(radius=1, leave_lag=1)
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("diag", x=15, y=15)
    guard.add_entity("far", x=25, y=5)
    assert guard.visible("a") == ["diag"]
    assert guard.subscribers("diag") == ["a"]
    assert guard.subscribers("far") == []


def test_budget_trims_by_priority_then_distance():
    guard = make(budget=2)
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("weak", x=5, y=5, priority=0)
    guard.add_entity("boss", x=15, y=5, priority=5)
    guard.add_entity("drone", x=15, y=15, priority=0)
    assert guard.visible("a") == ["boss", "weak"]
    assert guard.subscribers("drone") == []
    assert guard.budget("a") == 2


def test_budget_release_promotes_trimmed_entities():
    guard = make(budget=1)
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("alpha", x=5, y=5)
    guard.add_entity("beta", x=5, y=5)
    assert guard.visible("a") == ["alpha"]
    assert guard.set_budget("a", 2) == {"observer": "a", "budget": 2}
    assert guard.visible("a") == ["alpha", "beta"]
    assert guard.subscribers("beta") == ["a"]
    assert guard.budget("a") == 2


def test_leave_uses_the_lag_radius_beyond_the_visibility_radius():
    guard = make(radius=1, leave_lag=1)
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("mob", x=15, y=5)
    assert guard.subscribers("mob") == ["a"]
    guard.move_entity("mob", x=25, y=5)
    assert guard.subscribers("mob") == ["a"]
    guard.move_entity("mob", x=35, y=5)
    assert guard.subscribers("mob") == []
    assert guard.visible("a") == []


def test_move_entity_reports_cells_and_subscription_counts():
    guard = make()
    guard.add_observer("a", x=5, y=5)
    guard.add_observer("b", x=1005, y=5)
    guard.add_entity("mob", x=5, y=5)
    assert guard.move_entity("mob", x=1005, y=5) == {
        "entity": "mob",
        "from": (0, 0),
        "to": (100, 0),
        "entered": 1,
        "left": 1,
    }
    assert guard.visible("a") == []
    assert guard.visible("b") == ["mob"]


def test_remove_entity_unsubscribes_every_observer():
    guard = make()
    guard.add_observer("a", x=5, y=5)
    guard.add_observer("b", x=15, y=5)
    guard.add_entity("mob", x=5, y=5)
    guard.end_tick()
    assert guard.remove_entity("mob") == {"entity": "mob", "state": "removed"}
    assert guard.visible("a") == []
    assert guard.visible("b") == []
    events = guard.end_tick()["events"]
    assert [event["kind"] for event in events] == ["leave", "leave"]
    assert [event["seq"] for event in events] == [1, 2]
    with pytest.raises(aoi.AoiError) as exc:
        guard.remove_entity("mob")
    assert exc.value.code == "AOI_UNKNOWN_ENTITY"


def test_observer_move_recomputes_visible_set():
    guard = make(radius=1, leave_lag=1)
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("mob", x=5, y=5)
    guard.end_tick()
    assert guard.move_observer("a", x=505, y=5) == {
        "observer": "a",
        "from": (0, 0),
        "to": (50, 0),
        "visible": 0,
    }
    assert guard.visible("a") == []
    assert guard.subscribers("mob") == []
    assert guard.move_observer("a", x=5, y=5) == {
        "observer": "a",
        "from": (50, 0),
        "to": (0, 0),
        "visible": 1,
    }
    assert guard.visible("a") == ["mob"]
    assert guard.subscribers("mob") == ["a"]


def test_budget_validation_and_unknown_identifiers():
    guard = make()
    guard.add_observer("a", x=5, y=5)
    with pytest.raises(aoi.AoiError) as exc:
        guard.set_budget("a", 0)
    assert exc.value.code == "AOI_BUDGET_INVALID"
    with pytest.raises(aoi.AoiError) as exc:
        guard.set_budget("ghost", 2)
    assert exc.value.code == "AOI_UNKNOWN_OBSERVER"
    with pytest.raises(aoi.AoiError) as exc:
        guard.move_observer("ghost", x=0, y=0)
    assert exc.value.code == "AOI_UNKNOWN_OBSERVER"
    with pytest.raises(aoi.AoiError) as exc:
        guard.move_entity("ghost", x=0, y=0)
    assert exc.value.code == "AOI_UNKNOWN_ENTITY"
    assert guard.budget("a") == 4


def test_stats_and_reset():
    guard = make(budget=2)
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("e1", x=5, y=5)
    guard.end_tick()
    guard.add_entity("e2", x=15, y=15)
    guard.end_tick()
    guard.move_entity("e2", x=2005, y=5)
    guard.end_tick()
    assert guard.stats() == {
        "ticks": 3,
        "enters": 2,
        "leaves": 1,
        "trimmed": 0,
        "events": 3,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "ticks": 0,
        "enters": 0,
        "leaves": 0,
        "trimmed": 0,
        "events": 0,
    }
    assert guard.visible("a") == ["e1"]
    assert guard.next_seq() == 4


def test_snapshot_restore_keeps_the_event_stream_continuous():
    guard = make()
    guard.add_observer("a", x=5, y=5)
    guard.add_entity("mob", x=5, y=5)
    guard.end_tick()
    state = guard.snapshot()
    assert state["version"] == 1
    frozen = copy.deepcopy(state)
    state["observers"].clear()
    assert guard.visible("a") == ["mob"]
    twin = make()
    twin.restore(frozen)
    assert twin.visible("a") == ["mob"]
    assert twin.subscribers("mob") == ["a"]
    assert twin.next_seq() == 2
    twin.add_entity("pet", x=5, y=5)
    assert twin.end_tick()["events"] == [
        {"seq": 2, "kind": "enter", "observer": "a", "entity": "pet"}
    ]
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(aoi.AoiError) as exc:
            twin.restore(bad)
        assert exc.value.code == "AOI_SNAPSHOT_INVALID"
    assert twin.visible("a") == ["mob", "pet"]


def test_concurrent_moves_keep_subscription_tables_consistent():
    guard = make(radius=1, leave_lag=1, budget=4)
    players = 8
    for index in range(players):
        base = index * 1000.0 + 5.0
        guard.add_observer(f"obs{index}", x=base, y=5)
        guard.add_entity(f"ent{index}", x=base, y=5)
    errors = []

    def worker(index):
        try:
            base = index * 1000.0 + 5.0
            for step in range(6):
                guard.move_entity(f"ent{index}", x=base + (step % 3) * 10.0, y=5)
                guard.move_observer(f"obs{index}", x=base + ((step + 1) % 3) * 10.0, y=5)
                guard.visible(f"obs{index}")
        except Exception as exc:  # pragma: no cover - collected below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(players)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    for index in range(players):
        assert guard.visible(f"obs{index}") == [f"ent{index}"]
        assert guard.subscribers(f"ent{index}") == [f"obs{index}"]
