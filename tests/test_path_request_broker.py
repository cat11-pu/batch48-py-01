import threading

import pytest

import path_request_broker as prb


def make(**kwargs):
    settings = {"max_open": 8, "cache_limit": 32}
    settings.update(kwargs)
    return prb.PathBroker(**settings)


def open_grid(broker, version=1, width=4, height=4, blocked=()):
    return broker.set_grid(version, width=width, height=height, blocked=blocked)


def test_set_grid_validates_and_conflicts_on_same_version():
    broker = make()
    assert open_grid(broker) == {
        "version": 1,
        "width": 4,
        "height": 4,
        "blocked": 0,
    }
    assert open_grid(broker) == {
        "version": 1,
        "width": 4,
        "height": 4,
        "blocked": 0,
    }
    with pytest.raises(prb.PathError) as exc:
        broker.set_grid(1, width=5, height=5)
    assert exc.value.code == "PB_GRID_CONFLICT"
    with pytest.raises(prb.PathError) as exc:
        broker.set_grid(2, width=0, height=4)
    assert exc.value.code == "PB_INVALID_GRID"
    with pytest.raises(prb.PathError) as exc:
        broker.set_grid(2, width=2, height=2, blocked=[(5, 5)])
    assert exc.value.code == "PB_INVALID_GRID"


def test_request_allocates_ticket_and_validates_cells():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (2, 0), 1)
    assert ticket["start"] == (0, 0)
    assert ticket["goal"] == (2, 0)
    assert ticket["grid_version"] == 1
    assert ticket["status"] == "pending"
    assert isinstance(ticket["ticket"], str)
    with pytest.raises(prb.PathError) as exc:
        broker.request((0, 0), (2, 0), 9)
    assert exc.value.code == "PB_UNKNOWN_GRID"
    with pytest.raises(prb.PathError) as exc:
        broker.request((0, 0), (9, 0), 1)
    assert exc.value.code == "PB_INVALID_CELL"


def test_step_completes_shortest_path_in_fixed_order():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (0, 2), 1)
    out = broker.step(ticket["ticket"], 64)
    assert out["status"] == "done"
    assert out["path"] == [(0, 0), (0, 1), (0, 2)]
    assert broker.result(ticket["ticket"])["path"] == [
        (0, 0),
        (0, 1),
        (0, 2),
    ]


def test_result_before_completion_is_not_ready():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (3, 3), 1)
    broker.step(ticket["ticket"], 1)
    with pytest.raises(prb.PathError) as exc:
        broker.result(ticket["ticket"])
    assert exc.value.code == "PB_NOT_READY"


def test_unreachable_goal_reports_distinct_status():
    broker = make()
    broker.set_grid(1, width=3, height=3,
                    blocked=[(1, 0), (1, 1), (1, 2)])
    ticket = broker.request((0, 0), (2, 0), 1)
    out = broker.step(ticket["ticket"], 100)
    assert out["status"] == "unreachable"
    with pytest.raises(prb.PathError) as exc:
        broker.result(ticket["ticket"])
    assert exc.value.code == "PB_UNREACHABLE"


def test_open_ticket_limit_is_enforced():
    broker = make(max_open=1)
    open_grid(broker)
    first = broker.request((0, 0), (3, 3), 1)
    with pytest.raises(prb.PathError) as exc:
        broker.request((0, 1), (3, 3), 1)
    assert exc.value.code == "PB_QUEUE_FULL"
    broker.step(first["ticket"], 100)
    second = broker.request((0, 1), (3, 3), 1)
    assert second["status"] == "pending"
    assert second["ticket"] != first["ticket"]


def test_same_start_different_goals_are_not_merged():
    broker = make()
    open_grid(broker)
    down = broker.request((0, 0), (0, 2), 1)
    right = broker.request((0, 0), (2, 0), 1)
    broker.step(down["ticket"], 64)
    broker.step(right["ticket"], 64)
    down_result = broker.result(down["ticket"])
    right_result = broker.result(right["ticket"])
    assert down["ticket"] != right["ticket"]
    assert down_result["path"] != right_result["path"]
    assert down_result["path"][-1] == (0, 2)
    assert right_result["path"][-1] == (2, 0)


def test_completed_request_is_served_from_cache():
    broker = make()
    open_grid(broker)
    first = broker.request((0, 0), (2, 2), 1)
    broker.step(first["ticket"], 64)
    hits_before = broker.stats()["cache_hits"]
    second = broker.request((0, 0), (2, 2), 1)
    assert second["status"] == "done"
    assert second["cache_hit"] is True
    cached_path = broker.result(second["ticket"])["path"]
    assert cached_path[0] == (0, 0)
    assert cached_path[-1] == (2, 2)
    assert len(cached_path) == 5
    assert broker.stats()["cache_hits"] == hits_before + 1
    assert broker.cache_info()["entries"] >= 1


def test_cache_key_includes_grid_version():
    broker = make()
    open_grid(broker)
    first = broker.request((0, 0), (0, 2), 1)
    broker.step(first["ticket"], 64)
    open_grid(broker, version=2)
    second = broker.request((0, 0), (0, 2), 2)
    assert second["status"] == "pending"
    assert second["cache_hit"] is False
    out = broker.step(second["ticket"], 64)
    assert out["status"] == "done"
    assert out["path"] == [(0, 0), (0, 1), (0, 2)]


def test_cancel_is_idempotent_and_blocks_later_calls():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (3, 3), 1)
    assert broker.pending() == [ticket["ticket"]]
    assert broker.cancel(ticket["ticket"]) == {
        "ticket": ticket["ticket"],
        "status": "cancelled",
    }
    assert broker.pending() == []
    assert broker.cancel(ticket["ticket"]) == {
        "ticket": ticket["ticket"],
        "status": "cancelled",
    }
    with pytest.raises(prb.PathError) as exc:
        broker.step(ticket["ticket"], 10)
    assert exc.value.code == "PB_CANCELLED"
    with pytest.raises(prb.PathError) as exc:
        broker.result(ticket["ticket"])
    assert exc.value.code == "PB_CANCELLED"


def test_step_advances_across_multiple_budgeted_calls():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (3, 3), 1)
    for _ in range(50):
        state = broker.ticket_state(ticket["ticket"])
        if state["status"] == "done":
            break
        broker.step(ticket["ticket"], 1)
    result = broker.result(ticket["ticket"])
    assert result["path"][0] == (0, 0)
    assert result["path"][-1] == (3, 3)
    assert broker.ticket_state(ticket["ticket"])["expanded"] >= 6


def test_invalidate_reports_zero_for_unused_region_and_rejects_bad_input():
    broker = make()
    open_grid(broker)
    assert broker.invalidate([(99, 99)]) == 0
    with pytest.raises(prb.PathError) as exc:
        broker.invalidate("not-a-region")
    assert exc.value.code == "PB_INVALID_REGION"


def test_snapshot_restore_resumes_pending_ticket():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (3, 3), 1)
    broker.step(ticket["ticket"], 2)
    state = broker.snapshot()
    broker.step(ticket["ticket"], 64)
    done_path = broker.result(ticket["ticket"])["path"]
    broker.restore(state)
    assert broker.ticket_state(ticket["ticket"])["status"] == "pending"
    assert broker.ticket_state(ticket["ticket"])["expanded"] == 2
    broker.step(ticket["ticket"], 64)
    assert broker.result(ticket["ticket"])["path"] == done_path


def test_invalid_snapshot_leaves_queue_untouched():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (3, 3), 1)
    with pytest.raises(prb.PathError) as exc:
        broker.restore({"tickets": 5})
    assert exc.value.code == "PB_SNAPSHOT_INVALID"
    assert broker.pending() == [ticket["ticket"]]


def test_concurrent_step_has_single_advancer():
    broker = make()
    open_grid(broker)
    ticket = broker.request((0, 0), (3, 3), 1)
    barrier = threading.Barrier(6)
    successes = []
    errors = []
    guard = threading.Lock()

    def advance():
        barrier.wait()
        try:
            out = broker.step(ticket["ticket"], 1)
        except Exception as exc:  # noqa: BLE001 - test records the domain code
            with guard:
                errors.append(getattr(exc, "code", type(exc).__name__))
        else:
            with guard:
                successes.append(out)

    threads = [threading.Thread(target=advance) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert sorted(errors) == ["PB_BUSY"] * 5
    assert broker.ticket_state(ticket["ticket"])["expanded"] == 1
