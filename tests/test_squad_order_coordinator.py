import copy
import threading

import pytest

import squad_order_coordinator as sq


def test_form_assigns_slots_in_formation_order():
    squad = sq.SquadCoordinator()
    out = squad.form("alpha", ["u1", "u2", "u3"], "line")
    assert out["squad"] == "alpha"
    assert out["formation"] == "line"
    assert out["slots"] == {"front": "u1", "middle": "u2", "back": "u3"}
    assert squad.slots("alpha") == {"front": "u1", "middle": "u2",
                                    "back": "u3"}
    assert squad.roster("alpha") == {
        "u1": {"slot": "front", "alive": True, "busy": False},
        "u2": {"slot": "middle", "alive": True, "busy": False},
        "u3": {"slot": "back", "alive": True, "busy": False},
    }
    assert squad.idle_units("alpha") == ["u1", "u2", "u3"]


def test_form_rejects_duplicates_overflow_and_unknown_formation():
    squad = sq.SquadCoordinator()
    with pytest.raises(sq.SquadError) as exc:
        squad.form("a", ["u1", "u1"], "line")
    assert exc.value.code == "SQUAD_DUPLICATE_MEMBER"
    with pytest.raises(sq.SquadError) as exc:
        squad.form("b", ["u1", "u2", "u3", "u4"], "line")
    assert exc.value.code == "SQUAD_FORMATION_FULL"
    with pytest.raises(sq.SquadError) as exc:
        squad.form("c", ["u1"], "phalanx")
    assert exc.value.code == "SQUAD_UNKNOWN_FORMATION"
    with pytest.raises(sq.SquadError) as exc:
        squad.form("d", [], "line")
    assert exc.value.code == "SQUAD_INVALID_MEMBERS"
    with pytest.raises(sq.SquadError) as exc:
        squad.slots("a")
    assert exc.value.code == "SQUAD_UNKNOWN_SQUAD"


def test_form_twice_is_rejected():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    with pytest.raises(sq.SquadError) as exc:
        squad.form("alpha", ["u2"], "line")
    assert exc.value.code == "SQUAD_ALREADY_FORMED"
    assert squad.slots("alpha") == {"front": "u1", "middle": None,
                                    "back": None}


def test_issue_appends_instead_of_replacing():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1", "u2"], "line")
    first = squad.issue("alpha", "move", ["u1"])
    second = squad.issue("alpha", "hold")
    assert first["order_id"] == "ord-1"
    assert second["order_id"] == "ord-2"
    queued = squad.queue("alpha")
    assert [order["kind"] for order in queued] == ["move", "hold"]
    assert [order["order_id"] for order in queued] == ["ord-1", "ord-2"]
    assert queued[0]["targets"] == ["u1"]
    assert queued[0]["status"] == "pending"


def test_queue_sorts_by_priority_then_arrival():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1", "u2"], "line")
    squad.issue("alpha", "move", ["u1"], priority=1)
    squad.issue("alpha", "hold", priority=5)
    squad.issue("alpha", "move", ["u2"], priority=1)
    queued = squad.queue("alpha")
    assert [order["order_id"] for order in queued] == ["ord-2", "ord-1",
                                                       "ord-3"]


def test_next_order_hands_out_distinct_units():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1", "u2"], "line")
    squad.issue("alpha", "hold")
    squad.issue("alpha", "hold")
    first = squad.next_order("alpha")
    second = squad.next_order("alpha")
    assert first["order_id"] == "ord-1"
    assert first["unit"] == "u1"
    assert second["order_id"] == "ord-2"
    assert second["unit"] == "u2"
    assert squad.idle_units("alpha") == []
    assert squad.next_order("alpha") is None


def test_next_order_on_empty_queue_returns_none():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    assert squad.next_order("alpha") is None
    assert squad.queue("alpha") == []


def test_report_done_frees_unit_for_pending_order():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    squad.issue("alpha", "hold")
    squad.issue("alpha", "hold")
    first = squad.next_order("alpha")
    assert first["unit"] == "u1"
    assert squad.next_order("alpha") is None
    squad.report("alpha", "u1", "done")
    assert squad.roster("alpha")["u1"]["busy"] is False
    second = squad.next_order("alpha")
    assert second["order_id"] == "ord-2"
    assert second["unit"] == "u1"
    assert squad.roster("alpha")["u1"]["busy"] is True


def test_report_dead_removes_unit_from_idle_pool():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1", "u2"], "line")
    squad.issue("alpha", "hold")
    squad.issue("alpha", "hold")
    squad.next_order("alpha")
    squad.report("alpha", "u1", "dead")
    assert squad.roster("alpha")["u1"]["alive"] is False
    assert squad.roster("alpha")["u1"]["busy"] is False
    assert squad.idle_units("alpha") == ["u2"]
    second = squad.next_order("alpha")
    assert second["unit"] == "u2"
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "move", ["u1"])
    assert exc.value.code == "SQUAD_UNKNOWN_UNIT"


def test_morale_clamps_and_collapse_blocks_attack():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    assert squad.morale("alpha", -40) == {"squad": "alpha", "morale": 60,
                                          "collapsed": False}
    assert squad.morale("alpha", -30)["morale"] == 30
    collapsed = squad.morale("alpha", -100)
    assert collapsed["morale"] == 0
    assert collapsed["collapsed"] is True
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "attack", ["u1"])
    assert exc.value.code == "SQUAD_MORALE_BROKEN"
    squad.issue("alpha", "hold")
    restored = squad.morale("alpha", 500)
    assert restored["morale"] == 100
    assert restored["collapsed"] is False


def test_duplicate_move_orders_for_one_unit_are_refused():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1", "u2"], "line")
    squad.issue("alpha", "move", ["u1"])
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "move", ["u1"], priority=9)
    assert exc.value.code == "SQUAD_DUPLICATE_ORDER"
    squad.issue("alpha", "attack", ["u1"])
    assert len(squad.queue("alpha")) == 2
    for queued in squad.queue("alpha"):
        assert queued["targets"] == ["u1"]


def test_issue_validates_kind_and_targets():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "charge", ["u1"])
    assert exc.value.code == "SQUAD_INVALID_ORDER"
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "move", [])
    assert exc.value.code == "SQUAD_INVALID_ORDER"
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "move", ["ghost"])
    assert exc.value.code == "SQUAD_UNKNOWN_UNIT"
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("beta", "hold")
    assert exc.value.code == "SQUAD_UNKNOWN_SQUAD"
    assert squad.queue("alpha") == []


def test_disband_removes_squad_and_queued_orders():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    squad.issue("alpha", "hold")
    out = squad.disband("alpha")
    assert out["squad"] == "alpha"
    assert out["status"] == "disbanded"
    with pytest.raises(sq.SquadError) as exc:
        squad.next_order("alpha")
    assert exc.value.code == "SQUAD_UNKNOWN_SQUAD"
    with pytest.raises(sq.SquadError) as exc:
        squad.issue("alpha", "hold")
    assert exc.value.code == "SQUAD_UNKNOWN_SQUAD"
    with pytest.raises(sq.SquadError) as exc:
        squad.slots("alpha")
    assert exc.value.code == "SQUAD_UNKNOWN_SQUAD"
    with pytest.raises(sq.SquadError) as exc:
        squad.disband("alpha")
    assert exc.value.code == "SQUAD_UNKNOWN_SQUAD"


def test_snapshot_restore_keeps_queue_slots_and_morale():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1"], "line")
    squad.issue("alpha", "move", ["u1"], priority=2)
    squad.issue("alpha", "hold", priority=7)
    taken = squad.next_order("alpha")
    assert taken["order_id"] == "ord-2"
    squad.morale("alpha", -30)
    state = squad.snapshot()
    pristine = copy.deepcopy(state)
    state["squads"].clear()
    assert squad.slots("alpha") == {"front": "u1", "middle": None,
                                    "back": None}
    restored = sq.SquadCoordinator()
    restored.restore(pristine)
    assert restored.slots("alpha") == squad.slots("alpha")
    assert restored.roster("alpha")["u1"]["busy"] is True
    assert [order["order_id"] for order in restored.queue("alpha")] == ["ord-1"]
    assert restored.morale("alpha", 0)["morale"] == 70
    follow_up = restored.issue("alpha", "hold")
    assert follow_up["order_id"] == "ord-3"
    with pytest.raises(sq.SquadError) as exc:
        restored.restore(None)
    assert exc.value.code == "SQUAD_SNAPSHOT_INVALID"


def test_concurrent_issue_and_next_claim_each_order_once():
    squad = sq.SquadCoordinator()
    squad.form("alpha", ["u1", "u2", "u3", "u4"], "wedge")
    for _ in range(4):
        squad.issue("alpha", "hold")
    barrier = threading.Barrier(4)
    claims = []
    errors = []

    def worker():
        barrier.wait()
        try:
            claims.append(squad.next_order("alpha"))
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert all(claim is not None for claim in claims)
    order_ids = [claim["order_id"] for claim in claims]
    units = [claim["unit"] for claim in claims]
    assert sorted(order_ids) == ["ord-1", "ord-2", "ord-3", "ord-4"]
    assert sorted(units) == ["u1", "u2", "u3", "u4"]
    assert squad.queue("alpha") == []
