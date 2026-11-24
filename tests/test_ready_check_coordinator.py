import copy
import threading

import pytest

import ready_check_coordinator as rc


def make(**kwargs):
    return rc.ReadyCheckCoordinator(**kwargs)


def players(*names, team="blue"):
    return [{"player": name, "team": team} for name in names]


def test_open_check_issues_tickets_with_mode_window():
    coord = make(mode_windows={"ranked": 4, "casual": 9}, default_window=6)
    out = coord.open_check("c1", mode="ranked", players=[
        {"player": "p1", "team": "blue"},
        {"player": "p2", "team": "blue"},
        {"player": "p3", "team": "red"},
    ])
    assert out == {
        "check_id": "c1",
        "mode": "ranked",
        "window": 4,
        "state": "open",
        "tickets": [
            {"player": "p1", "team": "blue"},
            {"player": "p2", "team": "blue"},
            {"player": "p3", "team": "red"},
        ],
    }
    assert coord.window_for("ranked") == 4
    assert coord.window_for("casual") == 9
    assert coord.window_for("brawl") == 6

    status = coord.status("c1")
    assert status["pending"] == 3
    assert status["accepted"] == 0
    assert status["declined"] == 0
    assert status["verdict"] is None
    assert [row["player"] for row in status["tickets"]] == ["p1", "p2", "p3"]
    assert coord.stats()["checks_opened"] == 1
    assert coord.stats()["tickets_issued"] == 3


def test_open_check_rejects_capacity_before_index_and_after_close():
    coord = make(ticket_limit=3, player_limit=4)
    coord.open_check("c1", mode="ranked", players=players("p1", "p2", "p3"))

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.open_check("c2", mode="ranked", players=players("q1", "q2", "q3", "q4"))
    assert exc.value.code == "READY_TICKET_CAPACITY"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.open_check("c3", mode="ranked", players=players("q1", "q2"))
    assert exc.value.code == "READY_PLAYER_CAPACITY"

    coord.close_check("c1")
    out = coord.open_check("c3", mode="ranked", players=players("q1", "q2"))
    assert out["state"] == "open"
    assert coord.status("c3")["pending"] == 2
    assert coord.stats()["checks_opened"] == 2


def test_open_check_rejects_duplicate_check_and_duplicate_players():
    coord = make()
    coord.open_check("c1", mode="ranked", players=players("p1", "p2"))

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.open_check("c1", mode="ranked", players=players("p9"))
    assert exc.value.code == "READY_CHECK_EXISTS"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.open_check("c2", mode="ranked", players=players("p1", "p1"))
    assert exc.value.code == "READY_DUPLICATE_PLAYER"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.open_check("c3", mode="ranked", players=[{"player": "", "team": "blue"}])
    assert exc.value.code == "READY_INVALID_PLAYER"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.open_check("c4", mode="ranked", players=[{"player": "px"}])
    assert exc.value.code == "READY_INVALID_PLAYER"

    assert coord.status("c1")["pending"] == 2
    assert coord.stats()["rejected"] == 4


def test_submit_ready_records_answers_and_status_counts():
    coord = make()
    coord.open_check("c1", mode="ranked", players=players("p1", "p2", "p3"))

    assert coord.submit_ready("c1", "p1", accept=True, tick=1) == {
        "status": "recorded",
        "player": "p1",
        "answer": "accept",
        "team": "blue",
        "tick": 1,
    }
    out = coord.submit_ready("c1", "p2", accept=False, tick=2)
    assert out["answer"] == "decline"

    status = coord.status("c1")
    assert status["accepted"] == 1
    assert status["declined"] == 1
    assert status["pending"] == 1
    assert status["elapsed"] == 2
    assert status["state"] == "open"
    assert status["verdict"] is None
    p1 = status["tickets"][0]
    assert p1["answer"] == "accept"
    assert p1["reason"] == "player"
    assert p1["at"] == 1
    assert coord.ticket_for("p1")["answer"] == "accept"
    assert coord.stats()["accepts"] == 1
    assert coord.stats()["declines"] == 1


def test_duplicate_submit_keeps_first_answer():
    coord = make()
    coord.open_check("c1", mode="ranked", players=players("p1", "p2"))
    coord.submit_ready("c1", "p1", accept=True, tick=1)

    dup = coord.submit_ready("c1", "p1", accept=False, tick=2)
    assert dup == {
        "status": "duplicate",
        "player": "p1",
        "answer": "accept",
        "accept": True,
        "reason": "player",
    }
    assert coord.stats()["accepts"] == 1
    assert coord.stats()["declines"] == 0
    assert coord.status("c1")["tickets"][0]["at"] == 1

    coord.submit_ready("c1", "p2", accept=True, tick=2)
    status = coord.status("c1")
    assert status["state"] == "resolved"
    assert status["verdict"] == "ready"
    assert status["elapsed"] == 2


def test_submit_ready_rejects_unknown_and_late_answers():
    coord = make(mode_windows={"ranked": 3})
    coord.open_check("c1", mode="ranked", players=players("p1", "p2"))

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.submit_ready("nope", "p1", accept=True, tick=0)
    assert exc.value.code == "READY_UNKNOWN_CHECK"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.submit_ready("c1", "ghost", accept=True, tick=0)
    assert exc.value.code == "READY_UNKNOWN_TICKET"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.submit_ready("c1", "p1", accept=True, tick=4)
    assert exc.value.code == "READY_TOO_LATE"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.submit_ready("c1", "p1", accept=True, tick=-1)
    assert exc.value.code == "READY_INVALID_TICK"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.submit_ready("c1", "p1", accept="yes", tick=0)
    assert exc.value.code == "READY_INVALID_ANSWER"

    status = coord.status("c1")
    assert status["pending"] == 2
    assert status["elapsed"] == 0
    assert coord.stats()["rejected"] == 5


def test_tick_marks_timeouts_at_window_boundary():
    coord = make(mode_windows={"ranked": 3})
    coord.open_check("c1", mode="ranked", players=players("p1", "p2", "p3"))
    coord.submit_ready("c1", "p1", accept=True, tick=0)

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.tick("c1", ticks=0)
    assert exc.value.code == "READY_INVALID_TICK"
    assert coord.status("c1")["elapsed"] == 0

    out = coord.tick("c1", ticks=2)
    assert out == {
        "check_id": "c1",
        "elapsed": 2,
        "state": "open",
        "verdict": None,
        "pending": 2,
    }

    out = coord.tick("c1", ticks=1)
    assert out["elapsed"] == 3
    assert out["state"] == "resolved"
    assert out["verdict"] == "partial"
    assert out["pending"] == 0
    assert coord.stats()["timeouts"] == 2

    status = coord.status("c1")
    assert status["tickets"][1]["answer"] == "decline"
    assert status["tickets"][1]["reason"] == "timeout"
    assert status["tickets"][1]["at"] == 3

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.tick("c1", ticks=1)
    assert exc.value.code == "READY_CHECK_CLOSED"


def test_all_accept_sets_ready_and_declines_make_partial():
    coord = make()
    coord.open_check("c1", mode="ranked", players=players("a1", "a2"))
    coord.submit_ready("c1", "a1", accept=True, tick=0)
    coord.submit_ready("c1", "a2", accept=True, tick=1)
    status = coord.status("c1")
    assert status["state"] == "resolved"
    assert status["verdict"] == "ready"
    assert status["accepted"] == 2
    assert status["declined"] == 0

    coord.open_check("c2", mode="ranked", players=players("b1", "b2", "b3"))
    coord.submit_ready("c2", "b1", accept=True, tick=0)
    coord.submit_ready("c2", "b2", accept=False, tick=0)
    assert coord.status("c2")["verdict"] is None
    coord.submit_ready("c2", "b3", accept=True, tick=1)
    status = coord.status("c2")
    assert status["state"] == "resolved"
    assert status["verdict"] == "partial"
    assert status["accepted"] == 2
    assert status["declined"] == 1


def test_close_check_returns_priority_and_penalty_lists():
    coord = make(mode_windows={"ranked": 5})
    coord.open_check("c1", mode="ranked", players=[
        {"player": "p1", "team": "blue"},
        {"player": "p2", "team": "blue"},
        {"player": "p3", "team": "red"},
        {"player": "p4", "team": "red"},
    ])
    coord.submit_ready("c1", "p1", accept=True, tick=1)
    coord.submit_ready("c1", "p2", accept=True, tick=1)
    coord.submit_ready("c1", "p3", accept=False, tick=2)

    summary = coord.close_check("c1")
    assert summary["state"] == "closed"
    assert summary["verdict"] == "partial"
    assert summary["accepted"] == ["p1", "p2"]
    assert summary["declined"] == ["p3", "p4"]
    assert summary["priority_requeue"] == ["p1", "p2"]
    assert [row["player"] for row in summary["penalized"]] == ["p3", "p4"]
    assert [row["cooldown"] for row in summary["penalized"]] == [3, 3]
    assert coord.stats()["timeouts"] == 1
    assert coord.stats()["closed"] == 1
    assert coord.requeue_penalty("p3")["cooldown"] == 3
    assert coord.requeue_penalty("p4")["declines"] == 1

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.close_check("c1")
    assert exc.value.code == "READY_CHECK_CLOSED"


def test_late_accept_after_close_cannot_revive_check():
    coord = make(mode_windows={"blitz": 2})
    coord.open_check("c1", mode="blitz", players=players("p1", "p2"))
    coord.submit_ready("c1", "p1", accept=True, tick=0)
    summary = coord.close_check("c1")
    assert summary["verdict"] == "partial"
    assert summary["declined"] == ["p2"]

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.submit_ready("c1", "p2", accept=True, tick=0)
    assert exc.value.code == "READY_CHECK_CLOSED"

    status = coord.status("c1")
    assert status["state"] == "closed"
    assert status["verdict"] == "partial"
    assert status["tickets"][1]["answer"] == "decline"
    assert status["tickets"][1]["reason"] == "timeout"
    assert coord.stats()["closed"] == 1


def test_requeue_penalty_accumulates_and_is_zero_for_clean_players():
    coord = make(penalty_ticks=2)
    coord.open_check("c1", mode="ranked", players=players("p1", "p2"))
    coord.submit_ready("c1", "p1", accept=False, tick=0)
    coord.submit_ready("c1", "p2", accept=True, tick=0)
    coord.close_check("c1")
    assert coord.requeue_penalty("p1") == {
        "player": "p1", "declines": 1, "cooldown": 2, "penalty_ticks": 2,
    }

    coord.open_check("c2", mode="ranked", players=players("p1", "p3"))
    coord.submit_ready("c2", "p1", accept=False, tick=0)
    coord.submit_ready("c2", "p3", accept=True, tick=0)
    coord.close_check("c2")
    assert coord.requeue_penalty("p1")["declines"] == 2
    assert coord.requeue_penalty("p1")["cooldown"] == 4

    assert coord.requeue_penalty("p2") == {
        "player": "p2", "declines": 0, "cooldown": 0, "penalty_ticks": 2,
    }
    assert coord.requeue_penalty("ghost")["cooldown"] == 0


def test_window_for_validates_mode_names():
    coord = make(mode_windows={"ranked": 4}, default_window=7)
    assert coord.window_for("ranked") == 4
    assert coord.window_for("anything") == 7

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.window_for("")
    assert exc.value.code == "READY_INVALID_MODE"

    with pytest.raises(rc.ReadyCheckError) as exc:
        coord.window_for(None)
    assert exc.value.code == "READY_INVALID_MODE"
    assert coord.stats()["rejected"] == 2


def test_stats_counters_across_flow_and_reset():
    coord = make()
    coord.open_check("c1", mode="ranked", players=players("p1", "p2", "p3"))
    coord.submit_ready("c1", "p1", accept=True, tick=1)
    coord.submit_ready("c1", "p2", accept=False, tick=1)
    coord.close_check("c1")

    assert coord.stats() == {
        "checks_opened": 1,
        "tickets_issued": 3,
        "accepts": 1,
        "declines": 1,
        "timeouts": 1,
        "closed": 1,
        "rejected": 0,
    }
    coord.reset_stats()
    assert coord.stats() == {
        "checks_opened": 0,
        "tickets_issued": 0,
        "accepts": 0,
        "declines": 0,
        "timeouts": 0,
        "closed": 0,
        "rejected": 0,
    }


def test_snapshot_restore_roundtrip_and_rejects_corrupt_state():
    coord = make(penalty_ticks=2)
    coord.open_check("c1", mode="ranked", players=players("p1", "p2", "p3"))
    coord.submit_ready("c1", "p1", accept=True, tick=0)
    coord.submit_ready("c1", "p2", accept=False, tick=0)
    coord.submit_ready("c1", "p3", accept=True, tick=1)
    coord.close_check("c1")
    coord.open_check("c2", mode="ranked", players=players("p4", "p5"))
    coord.submit_ready("c2", "p4", accept=True, tick=0)

    snap = coord.snapshot()
    assert snap["version"] == 1
    corrupted = copy.deepcopy(snap)
    corrupted["checks"]["c1"]["tickets"]["p1"]["answer"] = "decline"
    assert coord.ticket_for("p1")["answer"] == "accept"

    fresh = make(penalty_ticks=2)
    fresh.restore(copy.deepcopy(snap))
    assert fresh.status("c1")["state"] == "closed"
    assert fresh.status("c1")["verdict"] == "partial"
    assert fresh.status("c2")["state"] == "open"
    assert fresh.status("c2")["pending"] == 1
    assert fresh.ticket_for("p4")["check_id"] == "c2"
    assert fresh.requeue_penalty("p2")["cooldown"] == 2
    assert fresh.stats()["checks_opened"] == 2

    bad_states = [
        None,
        {},
        {**copy.deepcopy(snap), "version": 2},
        {**copy.deepcopy(snap), "checks": "nope"},
        {**copy.deepcopy(snap), "check_order": ["ghost"]},
        {**copy.deepcopy(snap), "stats": {"checks_opened": 1}},
    ]
    broken_check = copy.deepcopy(snap)
    broken_check["checks"]["c1"]["state"] = "bogus"
    bad_states.append(broken_check)
    broken_ticket = copy.deepcopy(snap)
    broken_ticket["checks"]["c2"]["tickets"]["p4"].pop("at")
    bad_states.append(broken_ticket)

    for state in bad_states:
        with pytest.raises(rc.ReadyCheckError) as exc:
            fresh.restore(state)
        assert exc.value.code == "READY_SNAPSHOT_INVALID"

    assert fresh.status("c1")["state"] == "closed"
    assert fresh.status("c2")["pending"] == 1
    assert fresh.requeue_penalty("p2")["declines"] == 1


def test_concurrent_accepts_are_counted_exactly():
    names = [f"p{index}" for index in range(12)]
    coord = make(ticket_limit=32, player_limit=64)
    coord.open_check("c1", mode="ranked", players=players(*names))

    errors = []

    def worker(name):
        try:
            coord.submit_ready("c1", name, accept=True, tick=0)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((name, repr(exc)))

    threads = [threading.Thread(target=worker, args=(name,)) for name in names]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    status = coord.status("c1")
    assert status["accepted"] == 12
    assert status["pending"] == 0
    assert status["state"] == "resolved"
    assert status["verdict"] == "ready"
    assert coord.stats()["accepts"] == 12
    assert coord.stats()["rejected"] == 0
