import copy
import threading

import pytest

import input_budget_arbiter as ib


def make(**kwargs):
    kwargs.setdefault("budget", 4)
    return ib.InputBudgetArbiter(**kwargs)


def submit(arb, player, seq, entity, command="move", **kwargs):
    kwargs.setdefault("frame", seq)
    kwargs.setdefault("now", 0)
    kwargs.setdefault("ttl", 10000)
    return arb.submit(player, seq=seq, entity=entity, command=command, **kwargs)


def selected_seqs(out):
    return [entry["seq"] for entry in out["selected"]]


def selected_players(out):
    return [entry["player"] for entry in out["selected"]]


def test_set_budget_returns_previous_and_validates():
    arb = make(budget=8)
    assert arb.set_budget(3) == {"budget": 3, "previous": 8, "frame": None,
                                 "status": "updated"}
    assert arb.budget_of() == 3
    assert arb.set_budget(0, frame=7)["previous"] == 3
    for bad in (-1, True, "3"):
        with pytest.raises(ib.InputError) as exc:
            arb.set_budget(bad)
        assert exc.value.code == "INPUT_INVALID_BUDGET"
    assert arb.budget_of() == 0


def test_submit_accepts_and_reports_ticket():
    arb = make()
    out = submit(arb, "p1", 1, "e1", frame=1, now=0, ttl=1000)
    assert out == {"player": "p1", "seq": 1, "entity": "e1",
                   "priority": "normal", "cost": 1, "enqueued_frame": 1,
                   "expires_at": 1000, "pending": 1, "status": "accepted",
                   "superseded_seq": None}
    assert arb.pending()["count"] == 1
    assert arb.stats()["accepted"] == 1
    assert arb.stats()["submitted"] == 1


def test_submit_deduplicates_and_rejects_conflicting_payload():
    arb = make()
    submit(arb, "p1", 7, "e1", command="move")
    duplicate = submit(arb, "p1", 7, "e1", command="move")
    assert duplicate["status"] == "duplicate"
    assert duplicate["pending"] == 1
    with pytest.raises(ib.InputError) as exc:
        submit(arb, "p1", 7, "e2", command="move")
    assert exc.value.code == "INPUT_SEQ_CONFLICT"
    assert arb.pending()["count"] == 1
    assert arb.stats()["duplicates"] == 1
    assert arb.stats()["conflicts"] == 1


def test_submit_validation():
    arb = make()
    with pytest.raises(ib.InputError) as exc:
        arb.submit("", seq=1, entity="e1", command="move")
    assert exc.value.code == "INPUT_INVALID_SUBMISSION"
    bad = [
        dict(player="p1", seq=0, entity="e1", command="move"),
        dict(player="p1", seq=True, entity="e1", command="move"),
        dict(player="p1", seq=1, entity="", command="move"),
        dict(player="p1", seq=1, entity="e1", command=""),
        dict(player="p1", seq=1, entity="e1", command="move", priority="high"),
        dict(player="p1", seq=1, entity="e1", command="move", cost=0),
        dict(player="p1", seq=1, entity="e1", command="move", cost=5),
        dict(player="p1", seq=1, entity="e1", command="move", cost=True),
        dict(player="p1", seq=1, entity="e1", command="move", ttl=0),
        dict(player="p1", seq=1, entity="e1", command="move", ttl=-5),
    ]
    for kwargs in bad:
        with pytest.raises(ib.InputError) as exc:
            arb.submit(**kwargs)
        assert exc.value.code == "INPUT_INVALID_SUBMISSION"
    assert arb.stats()["rejected"] == len(bad) + 1
    assert arb.pending()["count"] == 0


def test_per_player_backlog_and_global_queue_limits():
    arb = make(budget=4, player_limit=2)
    submit(arb, "p1", 1, "e1", frame=1)
    submit(arb, "p1", 2, "e2", frame=2)
    with pytest.raises(ib.InputError) as exc:
        submit(arb, "p1", 3, "e3", frame=3)
    assert exc.value.code == "INPUT_BACKLOG_FULL"
    assert exc.value.detail == {"player": "p1", "limit": 2}
    crowded = make(budget=4, queue_limit=3)
    submit(crowded, "p1", 1, "e1", frame=1)
    submit(crowded, "p2", 1, "e2", frame=1)
    submit(crowded, "p3", 1, "e3", frame=1)
    with pytest.raises(ib.InputError) as exc:
        submit(crowded, "p4", 1, "e4", frame=1)
    assert exc.value.code == "INPUT_QUEUE_FULL"
    assert crowded.pending()["count"] == 3


def test_arbitrate_respects_budget_and_fifo_order():
    arb = make(budget=3)
    submit(arb, "p1", 1, "e1", frame=1)
    submit(arb, "p1", 2, "e2", frame=2)
    submit(arb, "p1", 3, "e3", frame=3)
    out = arb.arbitrate(frame=10, now=10)
    assert selected_seqs(out) == [1, 2, 3]
    assert out["used"] == 3
    assert out["remaining"] == 0
    assert out["deferred"] == 0
    assert out["pending"] == 0
    assert out["budget"] == 3
    assert arb.pending()["count"] == 0
    assert arb.stats()["selected"] == 3


def test_arbitrate_defers_overflow_to_next_frame():
    arb = make(budget=2)
    submit(arb, "p1", 1, "e1", frame=1)
    submit(arb, "p1", 2, "e2", frame=2)
    submit(arb, "p1", 3, "e3", frame=3)
    first = arb.arbitrate(frame=10, now=10)
    assert selected_seqs(first) == [1, 2]
    assert first["used"] == 2
    assert first["deferred"] == 1
    second = arb.arbitrate(frame=11, now=11)
    assert selected_seqs(second) == [3]
    assert second["deferred"] == 0


def test_urgent_preempts_normal_and_normal_stays_pending():
    arb = make(budget=1)
    submit(arb, "p1", 1, "e1", frame=1, priority="normal")
    submit(arb, "p2", 1, "e2", frame=2, priority="urgent")
    out = arb.arbitrate(frame=10, now=10)
    assert selected_seqs(out) == [1]
    assert selected_players(out) == ["p2"]
    assert out["used"] == 1
    assert out["deferred"] == 1
    assert arb.pending(player="p1")["count"] == 1
    follow = arb.arbitrate(frame=11, now=11)
    assert selected_players(follow) == ["p1"]


def test_fair_rotation_serves_starved_player_next_frame():
    arb = make(budget=1)
    submit(arb, "p1", 1, "e1", frame=1)
    submit(arb, "p1", 2, "e2", frame=2)
    submit(arb, "p1", 3, "e3", frame=3)
    submit(arb, "p2", 1, "x1", frame=2)
    first = arb.arbitrate(frame=10, now=10)
    second = arb.arbitrate(frame=11, now=11)
    third = arb.arbitrate(frame=12, now=12)
    assert selected_players(first) == ["p1"]
    assert selected_players(second) == ["p2"]
    assert selected_players(third) == ["p1"]
    assert selected_seqs(third) == [2]


def test_expired_inputs_are_dropped_before_arbitration():
    arb = make(budget=3)
    submit(arb, "p1", 1, "e1", frame=1, now=0, ttl=100)
    submit(arb, "p1", 2, "e2", frame=2, now=0, ttl=1000)
    out = arb.arbitrate(frame=5, now=100)
    assert out["expired"] == 1
    assert selected_seqs(out) == [2]
    assert arb.stats()["expired"] == 1
    submit(arb, "p9", 1, "e9", frame=6, now=1000, ttl=50)
    assert arb.expire(now=1049) == 0
    assert arb.expire(now=1050) == 1
    assert arb.pending()["count"] == 0


def test_cancel_removes_pending_and_unknown_cancel_errors():
    arb = make(budget=2)
    submit(arb, "p1", 1, "e1", frame=1)
    out = arb.cancel("p1", seq=1)
    assert out == {"player": "p1", "seq": 1, "status": "cancelled",
                   "pending": 0}
    with pytest.raises(ib.InputError) as exc:
        arb.cancel("p1", seq=1)
    assert exc.value.code == "INPUT_NOT_FOUND"
    assert arb.arbitrate(frame=10, now=10)["selected"] == []
    assert arb.stats()["cancelled"] == 1


def test_entity_queue_keeps_earlier_pending_input_first():
    arb = make(budget=1)
    submit(arb, "p1", 1, "e1", frame=1, cost=2)
    submit(arb, "p2", 1, "e1", frame=2, cost=1)
    out = arb.arbitrate(frame=10, now=10)
    assert out["selected"] == []
    assert out["used"] == 0
    assert out["deferred"] == 2
    assert arb.pending()["count"] == 2


def test_pending_and_item_of_report_queue_state():
    arb = make()
    submit(arb, "p1", 1, "e1", command="dash", frame=1)
    submit(arb, "p2", 1, "e2", command="guard", frame=2)
    queue = arb.pending()
    assert queue["count"] == 2
    assert queue["players"] == {"p1": 1, "p2": 1}
    only = arb.pending(player="p1")
    assert only["count"] == 1
    assert only["items"][0]["seq"] == 1
    item = arb.item_of("p1", seq=1)
    assert item["command"] == "dash"
    assert item["entity"] == "e1"
    assert item["expires_at"] == 10000
    assert arb.item_of("p1", seq=9) is None


def test_snapshot_restore_preserves_queue_and_budget():
    arb = make(budget=4)
    arb.set_budget(2)
    submit(arb, "p1", 1, "e1", frame=1)
    submit(arb, "p1", 2, "e2", frame=2)
    submit(arb, "p1", 3, "e3", frame=3)
    arb.arbitrate(frame=10, now=10)
    assert arb.pending()["count"] == 1
    snap = arb.snapshot()
    assert snap["version"] == 1
    assert snap["budget"] == 2
    pristine = copy.deepcopy(snap)
    snap["inputs"].clear()
    snap["last_served"].clear()
    assert arb.pending()["count"] == 1

    restored = ib.InputBudgetArbiter()
    restored.restore(pristine)
    assert restored.budget_of() == 2
    assert restored.pending()["count"] == 1
    assert restored.stats() == pristine["stats"]
    out = restored.arbitrate(frame=11, now=11)
    assert selected_seqs(out) == [3]
    assert out["budget"] == 2

    broken = [None, {}, {"version": 2},
              {"version": 1, "budget": -1, "inputs": {}, "order": 0,
               "last_served": {}, "stats": {}},
              {"version": 1, "budget": 2, "inputs": [], "order": 0,
               "last_served": {}, "stats": {}}]
    for bad in broken:
        with pytest.raises(ib.InputError) as exc:
            restored.restore(bad)
        assert exc.value.code == "INPUT_SNAPSHOT_INVALID"
    assert restored.budget_of() == 2
    assert restored.pending()["count"] == 0


def test_concurrent_duplicate_submissions_are_accepted_once():
    arb = make()
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            tag = submit(arb, "p1", 5, "e5", frame=5)["status"]
        except Exception as exc:  # pragma: no cover - surfaced as a failure
            tag = repr(exc)
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("accepted") == 1
    assert outcomes.count("duplicate") == 7
    assert arb.pending()["count"] == 1
    assert arb.stats()["accepted"] == 1
    assert arb.stats()["duplicates"] == 7
