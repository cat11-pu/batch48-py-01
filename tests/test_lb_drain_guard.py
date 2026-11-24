import copy
import threading

import pytest

import lb_drain_guard as lb


def make_guard(**kwargs):
    return lb.LbDrainGuard(**kwargs)


def keys(count=240):
    return ["session-%04d" % i for i in range(count)]


def mapping(guard, count=240):
    return {key: guard.route(key) for key in keys(count)}


def test_add_backend_returns_vnode_count_and_rejects_duplicates():
    guard = make_guard(virtual_nodes=8)
    assert guard.add_backend("b1", weight=2) == {
        "name": "b1", "weight": 2, "vnodes": 16, "state": "active",
    }
    assert guard.add_backend("b2") == {
        "name": "b2", "weight": 1, "vnodes": 8, "state": "active",
    }
    with pytest.raises(lb.LbError) as exc:
        guard.add_backend("b1")
    assert exc.value.code == "LB_BACKEND_EXISTS"
    with pytest.raises(lb.LbError) as exc:
        guard.add_backend("b3", weight=-1)
    assert exc.value.code == "LB_INVALID_WEIGHT"
    stats = guard.stats()
    assert stats["backends"] == 2
    assert stats["active"] == 2


def test_route_is_stable_and_empty_ring_is_rejected():
    guard = make_guard(virtual_nodes=8)
    with pytest.raises(lb.LbError) as exc:
        guard.route("session-0001")
    assert exc.value.code == "LB_NO_BACKEND"

    guard.add_backend("a")
    guard.add_backend("b")
    first = mapping(guard, 120)
    assert set(first.values()) == {"a", "b"}
    assert mapping(guard, 120) == first


def test_adding_backend_only_moves_keys_owned_by_new_nodes():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a")
    guard.add_backend("b")
    before = mapping(guard)
    guard.add_backend("c")
    after = mapping(guard)

    moved = [key for key in before if before[key] != after[key]]
    assert 0 < len(moved) < len(before)
    for key in moved:
        assert before[key] in ("a", "b")
        assert after[key] == "c"
    for key in before:
        if key not in moved:
            assert after[key] == before[key]


def test_removing_backend_only_migrates_its_own_keys():
    guard = make_guard(virtual_nodes=8)
    for name in ("a", "b", "c"):
        guard.add_backend(name)
    before = mapping(guard)

    result = guard.remove_backend("b")
    assert result == {"name": "b", "vnodes_removed": 8}

    after = mapping(guard)
    moved = [key for key in before if before[key] != after[key]]
    assert moved
    for key in before:
        if before[key] == "b":
            assert after[key] in ("a", "c")
        else:
            assert after[key] == before[key]
    assert [item["name"] for item in guard.list_backends()] == ["a", "c"]

    with pytest.raises(lb.LbError) as exc:
        guard.remove_backend("missing")
    assert exc.value.code == "LB_UNKNOWN_BACKEND"


def test_zero_weight_disables_backend_and_positive_weight_restores():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a")
    guard.add_backend("b")

    assert guard.set_weight("a", 0) == {
        "name": "a", "weight": 0, "vnodes": 0, "state": "disabled",
    }
    assert "a" not in set(mapping(guard, 200).values())

    assert guard.set_weight("a", 3) == {
        "name": "a", "weight": 3, "vnodes": 24, "state": "active",
    }
    assert "a" in set(mapping(guard, 300).values())

    stable = mapping(guard, 200)
    assert guard.set_weight("a", 3) == {
        "name": "a", "weight": 3, "vnodes": 24, "state": "active",
    }
    assert mapping(guard, 200) == stable

    with pytest.raises(lb.LbError) as exc:
        guard.set_weight("a", -2)
    assert exc.value.code == "LB_INVALID_WEIGHT"
    with pytest.raises(lb.LbError) as exc:
        guard.set_weight("missing", 1)
    assert exc.value.code == "LB_UNKNOWN_BACKEND"


def test_health_thresholds_flip_state():
    guard = make_guard(virtual_nodes=8, failure_threshold=2, success_threshold=2)
    guard.add_backend("a")
    guard.add_backend("b")

    first = guard.report_health("a", False)
    assert first == {"name": "a", "state": "active", "failures": 1,
                     "successes": 0, "changed": False}
    down = guard.report_health("a", False)
    assert down == {"name": "a", "state": "down", "failures": 0,
                    "successes": 0, "changed": True}
    assert "a" not in set(mapping(guard, 200).values())
    assert guard.list_backends()[0]["state"] == "down"

    still_down = guard.report_health("a", True)
    assert still_down == {"name": "a", "state": "down", "failures": 0,
                          "successes": 1, "changed": False}
    back = guard.report_health("a", True)
    assert back == {"name": "a", "state": "active", "failures": 0,
                    "successes": 0, "changed": True}
    assert "a" in set(mapping(guard, 300).values())

    with pytest.raises(lb.LbError) as exc:
        guard.report_health("missing", True)
    assert exc.value.code == "LB_UNKNOWN_BACKEND"


def test_drain_stops_routing_and_new_connections():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a")
    guard.add_backend("b")

    opened = guard.accept_connection("a", "conn-1", now_ms=1000)
    assert opened == {"conn_id": "conn-1", "backend": "a", "opened_ms": 1000}

    drained = guard.drain("a", deadline_ms=5000)
    assert drained == {"name": "a", "state": "draining", "deadline_ms": 5000}
    assert "a" not in set(mapping(guard, 200).values())

    with pytest.raises(lb.LbError) as exc:
        guard.accept_connection("a", "conn-2", now_ms=1100)
    assert exc.value.code == "LB_BACKEND_DRAINING"

    stats = guard.stats()
    assert stats["active"] == 1
    assert stats["draining"] == 1
    assert stats["connections"] == 1

    with pytest.raises(lb.LbError) as exc:
        guard.drain("missing", deadline_ms=5000)
    assert exc.value.code == "LB_UNKNOWN_BACKEND"


def test_graceful_close_before_deadline_is_not_forced():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a")
    guard.accept_connection("a", "conn-1", now_ms=1000)
    guard.drain("a", deadline_ms=5000)

    closed = guard.close_connection("conn-1", now_ms=4000)
    assert closed == {"conn_id": "conn-1", "closed_ms": 4000, "forced": False}
    stats = guard.stats()
    assert stats["connections"] == 0
    assert stats["closed"] == 1
    assert stats["forced"] == 0

    with pytest.raises(lb.LbError) as exc:
        guard.close_connection("conn-1", now_ms=4100)
    assert exc.value.code == "LB_UNKNOWN_CONNECTION"
    with pytest.raises(lb.LbError) as exc:
        guard.accept_connection("missing", "conn-2", now_ms=4100)
    assert exc.value.code == "LB_UNKNOWN_BACKEND"


def test_connection_limit_is_enforced():
    guard = make_guard(virtual_nodes=8, max_connections=2)
    guard.add_backend("a")

    guard.accept_connection("a", "conn-1", now_ms=1)
    guard.accept_connection("a", "conn-2", now_ms=2)
    with pytest.raises(lb.LbError) as exc:
        guard.accept_connection("a", "conn-3", now_ms=3)
    assert exc.value.code == "LB_CONNECTION_LIMIT"

    guard.close_connection("conn-1", now_ms=4)
    assert guard.accept_connection("a", "conn-3", now_ms=5)["conn_id"] == "conn-3"
    stats = guard.stats()
    assert stats["accepted"] == 3
    assert stats["closed"] == 1
    assert stats["connections"] == 2


def test_snapshot_is_deep_copy():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a", weight=2)
    guard.add_backend("b")
    baseline = mapping(guard, 160)

    snap = guard.snapshot()
    assert snap["version"] == 1
    snap["backends"]["a"]["weight"] = 99
    snap["stats"]["accepted"] = 999
    snap["ring"].clear()

    assert guard.list_backends()[0]["weight"] == 2
    assert guard.stats()["accepted"] == 0
    assert mapping(guard, 160) == baseline


def test_restore_preserves_ring_health_and_drain():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a")
    guard.add_backend("b")
    guard.add_backend("c")
    for _ in range(3):
        guard.report_health("a", False)
    guard.drain("b", deadline_ms=9000)
    guard.accept_connection("c", "conn-1", now_ms=100)
    baseline = mapping(guard, 200)

    snap = guard.snapshot()
    revived = make_guard(virtual_nodes=8)
    revived.restore(copy.deepcopy(snap))

    assert mapping(revived, 200) == baseline
    assert revived.list_backends() == guard.list_backends()
    assert revived.stats()["connections"] == 1
    with pytest.raises(lb.LbError) as exc:
        revived.accept_connection("b", "conn-2", now_ms=200)
    assert exc.value.code == "LB_BACKEND_DRAINING"
    assert "b" not in set(mapping(revived, 200).values())


def test_restore_rejects_malformed_snapshots():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("a")
    guard.add_backend("b")
    baseline = mapping(guard, 120)

    bad_states = [
        {"version": 2},
        {"version": 1},
        {"version": 1, "backends": [], "ring": [],
         "connections": {}, "stats": {}},
        {"version": 1, "backends": {}, "ring": "broken",
         "connections": {}, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(lb.LbError) as exc:
            guard.restore(bad)
        assert exc.value.code == "LB_SNAPSHOT_INVALID"

    assert mapping(guard, 120) == baseline
    assert [item["name"] for item in guard.list_backends()] == ["a", "b"]


def test_stats_gauges_and_reset():
    guard = make_guard(virtual_nodes=8)
    assert guard.stats() == {
        "backends": 0, "active": 0, "draining": 0, "down": 0,
        "disabled": 0, "connections": 0, "accepted": 0, "closed": 0,
        "forced": 0,
    }
    guard.add_backend("a")
    guard.add_backend("b", weight=2)
    guard.accept_connection("a", "conn-1", now_ms=10)
    guard.close_connection("conn-1", now_ms=20)
    guard.drain("b", deadline_ms=100)

    assert guard.stats() == {
        "backends": 2, "active": 1, "draining": 1, "down": 0,
        "disabled": 0, "connections": 0, "accepted": 1, "closed": 1,
        "forced": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "backends": 2, "active": 1, "draining": 1, "down": 0,
        "disabled": 0, "connections": 0, "accepted": 0, "closed": 0,
        "forced": 0,
    }


def test_concurrent_accepts_and_closes_are_counted_once():
    guard = make_guard(virtual_nodes=8, max_connections=500)
    guard.add_backend("a")
    guard.add_backend("b")
    errors = []
    barrier = threading.Barrier(8)

    def worker(index):
        try:
            barrier.wait(timeout=10)
            for round_no in range(20):
                conn_id = "conn-%d-%d" % (index, round_no)
                backend = "a" if round_no % 2 == 0 else "b"
                guard.accept_connection(backend, conn_id, now_ms=round_no)
                guard.route("session-%d" % (index * 20 + round_no))
                guard.close_connection(conn_id, now_ms=round_no + 100)
        except Exception as exc:  # noqa: BLE001 - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    stats = guard.stats()
    assert stats["accepted"] == 160
    assert stats["closed"] == 160
    assert stats["connections"] == 0


def test_list_backends_is_sorted_with_state_fields():
    guard = make_guard(virtual_nodes=8)
    guard.add_backend("c", weight=3)
    guard.add_backend("a")
    guard.add_backend("b", weight=2)

    assert guard.list_backends() == [
        {"name": "a", "weight": 1, "vnodes": 8, "state": "active"},
        {"name": "b", "weight": 2, "vnodes": 16, "state": "active"},
        {"name": "c", "weight": 3, "vnodes": 24, "state": "active"},
    ]
    guard.drain("b", deadline_ms=700)
    assert guard.list_backends() == [
        {"name": "a", "weight": 1, "vnodes": 8, "state": "active"},
        {"name": "b", "weight": 2, "vnodes": 16, "state": "draining"},
        {"name": "c", "weight": 3, "vnodes": 24, "state": "active"},
    ]
