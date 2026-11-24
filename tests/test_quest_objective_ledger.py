import pytest

import quest_objective_ledger as ql


def ledger():
    return ql.ObjectiveLedger()


def solo(event_id):
    return {"kind": "solo_kill", "event_id": event_id}


def test_define_and_accept_returns_zero_progress():
    l = ledger()
    definition = l.DefineQuest(
        "q1",
        {"wolf": {"required": 5}, "herb": {"required": 2}},
        turn_in={"coin": 3},
        reward={"xp": 100},
    )
    assert definition["quest"] == "q1"
    assert definition["objectives"] == {"wolf": 5, "herb": 2}
    assert definition["turn_in"] == {"coin": 3}
    assert definition["reward"] == {"xp": 100}
    out = l.Accept("p1", "q1")
    assert out == {
        "player": "p1",
        "quest": "q1",
        "state": "active",
        "objectives": {
            "wolf": {"count": 0, "required": 5},
            "herb": {"count": 0, "required": 2},
        },
    }
    assert l.Stats() == {
        "accepted": 1,
        "recorded": 0,
        "delivered": 0,
        "abandoned": 0,
        "rewards": 0,
    }


def test_define_rejects_duplicate_and_invalid_definitions():
    l = ledger()
    l.DefineQuest("q1", {"a": {"required": 1}})
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.DefineQuest("q1", {"a": {"required": 2}})
    assert exc.value.code == "QUEST_ALREADY_DEFINED"
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.DefineQuest("q2", {"a": {"required": 0}})
    assert exc.value.code == "QUEST_DEFINITION_INVALID"
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.DefineQuest("q3", {})
    assert exc.value.code == "QUEST_DEFINITION_INVALID"


def test_accept_twice_keeps_existing_progress():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 2, solo("e1"))
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.Accept("p1", "q1")
    assert exc.value.code == "QUEST_ALREADY_ACCEPTED"
    assert l.Progress("p1", "q1")["objectives"]["wolf"]["count"] == 2
    assert l.Stats()["accepted"] == 1


def test_record_caps_at_required_count():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    l.Accept("p1", "q1")
    first = l.Record("p1", "q1", "wolf", 4, solo("e1"))
    assert first["count"] == 4
    assert first["required"] == 5
    assert first["counted"] is True
    assert first["capped"] is False
    second = l.Record("p1", "q1", "wolf", 4, solo("e2"))
    assert second["count"] == 5
    assert second["capped"] is True
    assert l.Progress("p1", "q1")["objectives"]["wolf"]["count"] == 5
    assert l.Stats()["recorded"] == 2


def test_record_rejects_bad_delta_and_source():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    l.Accept("p1", "q1")
    for delta in (0, -3):
        with pytest.raises(ql.QuestLedgerError) as exc:
            l.Record("p1", "q1", "wolf", delta, solo("e1"))
        assert exc.value.code == "QUEST_DELTA_INVALID"
    for source in ({}, {"kind": "kill"}, {"kind": "", "event_id": "e1"}, None):
        with pytest.raises(ql.QuestLedgerError) as exc:
            l.Record("p1", "q1", "wolf", 1, source)
        assert exc.value.code == "QUEST_SOURCE_INVALID"
    assert l.Progress("p1", "q1")["objectives"]["wolf"]["count"] == 0


def test_record_same_event_twice_counts_once():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 10}})
    l.Accept("p1", "q1")
    source = {"kind": "party_kill", "event_id": "e1"}
    first = l.Record("p1", "q1", "wolf", 3, source)
    second = l.Record("p1", "q1", "wolf", 3, source)
    assert first["count"] == 3
    assert first["counted"] is True
    assert second["count"] == 3
    assert second["counted"] is False
    assert second["capped"] is False
    assert l.Sources("p1", "q1", "wolf") == [source]
    assert l.Stats()["recorded"] == 1


def test_sources_keep_insertion_order_and_are_copies():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 10}})
    l.Accept("p1", "q1")
    first = solo("e1")
    second = {"kind": "pickup", "event_id": "e2"}
    l.Record("p1", "q1", "wolf", 1, first)
    l.Record("p1", "q1", "wolf", 1, second)
    sources = l.Sources("p1", "q1", "wolf")
    assert sources == [first, second]
    sources[0]["kind"] = "hacked"
    assert l.Sources("p1", "q1", "wolf") == [first, second]


def test_record_rejects_unknown_objective_and_unaccepted_task():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    l.Accept("p1", "q1")
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.Record("p1", "q1", "bear", 1, solo("e1"))
    assert exc.value.code == "QUEST_OBJECTIVE_UNKNOWN"
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.Record("p2", "q1", "wolf", 1, solo("e1"))
    assert exc.value.code == "QUEST_NOT_ACCEPTED"


def test_progress_rejects_unknown_and_unaccepted():
    l = ledger()
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.Progress("p1", "q9")
    assert exc.value.code == "QUEST_UNKNOWN"
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.Progress("p1", "q1")
    assert exc.value.code == "QUEST_NOT_ACCEPTED"


def test_turnin_requires_complete_objectives():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 2}}, turn_in={"coin": 1}, reward={"xp": 10})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 1, solo("e1"))
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.TurnIn("p1", "q1", {"coin": 1})
    assert exc.value.code == "QUEST_OBJECTIVES_INCOMPLETE"
    progress = l.Progress("p1", "q1")
    assert progress["state"] == "active"
    assert progress["objectives"]["wolf"]["count"] == 1
    assert l.Stats()["rewards"] == 0


def test_turnin_success_marks_delivered_and_returns_reward():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 2}}, turn_in={"coin": 3}, reward={"xp": 100})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 2, solo("e1"))
    out = l.TurnIn("p1", "q1", {"coin": 3})
    assert out["player"] == "p1"
    assert out["quest"] == "q1"
    assert out["state"] == "delivered"
    assert out["reward"] == {"xp": 100}
    assert l.Progress("p1", "q1")["state"] == "delivered"
    assert l.Stats()["delivered"] == 1
    assert l.Stats()["rewards"] == 1


def test_duplicate_turnin_rejected_and_reward_once():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 1}}, turn_in={"coin": 1}, reward={"xp": 5})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 1, solo("e1"))
    l.TurnIn("p1", "q1", {"coin": 1})
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.TurnIn("p1", "q1", {"coin": 1})
    assert exc.value.code == "QUEST_ALREADY_DELIVERED"
    assert l.Stats()["rewards"] == 1
    assert l.Stats()["delivered"] == 1


def test_abandon_is_idempotent_and_clears_counts():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 2, solo("e1"))
    first = l.Abandon("p1", "q1")
    assert first == {"player": "p1", "quest": "q1", "state": "abandoned"}
    assert l.Progress("p1", "q1")["objectives"]["wolf"]["count"] == 0
    assert l.Stats()["abandoned"] == 1
    second = l.Abandon("p1", "q1")
    assert second == first
    assert l.Stats()["abandoned"] == 1
    again = l.Accept("p1", "q1")
    assert again["state"] == "active"
    assert again["objectives"]["wolf"]["count"] == 0
    assert l.Stats()["accepted"] == 2


def test_snapshot_restore_preserves_progress_and_delivery():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 2}}, turn_in={"coin": 1}, reward={"xp": 7})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 2, solo("e1"))
    l.TurnIn("p1", "q1", {"coin": 1})
    l.DefineQuest("q2", {"herb": {"required": 4}})
    l.Accept("p2", "q2")
    l.Record("p2", "q2", "herb", 1, {"kind": "pickup", "event_id": "h1"})
    snap = l.Snapshot()
    stats_before = l.Stats()
    l.Record("p2", "q2", "herb", 2, {"kind": "pickup", "event_id": "h2"})
    assert l.Progress("p2", "q2")["objectives"]["herb"]["count"] == 3
    fresh = ledger()
    fresh.Restore(snap)
    assert fresh.Progress("p1", "q1")["state"] == "delivered"
    assert fresh.Progress("p2", "q2")["objectives"]["herb"]["count"] == 1
    assert fresh.Stats() == stats_before
    with pytest.raises(ql.QuestLedgerError) as exc:
        fresh.TurnIn("p1", "q1", {"coin": 1})
    assert exc.value.code == "QUEST_ALREADY_DELIVERED"


def test_restore_rejects_malformed_snapshot():
    l = ledger()
    l.DefineQuest("q1", {"wolf": {"required": 5}})
    l.Accept("p1", "q1")
    l.Record("p1", "q1", "wolf", 2, solo("e1"))
    with pytest.raises(ql.QuestLedgerError) as exc:
        l.Restore({"players": []})
    assert exc.value.code == "QUEST_SNAPSHOT_INVALID"
    assert l.Progress("p1", "q1")["objectives"]["wolf"]["count"] == 2
    assert l.Stats()["accepted"] == 1
