import copy
import threading

import pytest

import authority_handover_guard as aho


def make(**kwargs):
    settings = {"receipt_timeout_ticks": 3, "max_buffered": 8}
    settings.update(kwargs)
    return aho.AuthorityHandoverGuard(**settings)


def test_register_entity_records_authority_and_rejects_duplicates():
    guard = make()
    assert guard.register_entity("hero", process="world-1", fields={"hp": 100}) == {
        "entity": "hero",
        "authority": "world-1",
        "children": [],
        "state": "visible",
        "fields": {"hp": 100},
    }
    guard.register_entity("pet", process="world-1")
    assert guard.query("pet") == {
        "entity": "pet",
        "state": "visible",
        "authority": "world-1",
    }
    assert guard.authority("hero") == "world-1"
    assert guard.invisible("hero") is False
    with pytest.raises(aho.HandoverError) as exc:
        guard.register_entity("hero", process="world-2")
    assert exc.value.code == "HANDOVER_DUPLICATE"
    with pytest.raises(aho.HandoverError) as exc:
        guard.register_entity("", process="world-1")
    assert exc.value.code == "HANDOVER_INVALID_ARGUMENT"
    with pytest.raises(aho.HandoverError) as exc:
        guard.query("ghost")
    assert exc.value.code == "HANDOVER_UNKNOWN_ENTITY"
    with pytest.raises(aho.HandoverError) as exc:
        guard.authority("ghost")
    assert exc.value.code == "HANDOVER_UNKNOWN_ENTITY"
    with pytest.raises(aho.HandoverError) as exc:
        guard.handover("ho-9")
    assert exc.value.code == "HANDOVER_UNKNOWN_TICKET"
    assert guard.destroy_entity("pet") == {"entity": "pet", "state": "absent"}
    assert guard.query("pet") == {
        "entity": "pet",
        "state": "absent",
        "authority": None,
    }
    with pytest.raises(aho.HandoverError) as exc:
        guard.destroy_entity("pet")
    assert exc.value.code == "HANDOVER_UNKNOWN_ENTITY"


def test_concurrent_begin_handover_yields_a_single_winner():
    guard = make()
    guard.register_entity("hero", process="world-1")
    winners = []
    failures = []
    start = threading.Barrier(8)

    def worker():
        start.wait()
        try:
            winners.append(guard.begin_handover(
                "hero", source="world-1", target="world-2", tick=0))
        except aho.HandoverError as exc:
            failures.append(exc.code)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(winners) == 1
    assert failures == ["HANDOVER_INVALID_STATE"] * 7
    assert guard.stats()["handovers"] == 1
    assert guard.handover(winners[0]["ticket"])["state"] == "migrating"


def test_begin_handover_hides_the_entity_and_sets_the_deadline():
    guard = make()
    guard.register_entity("hero", process="world-1")
    result = guard.begin_handover("hero", source="world-1", target="world-2",
                                  tick=10)
    assert result == {
        "entity": "hero",
        "ticket": "ho-1",
        "source": "world-1",
        "target": "world-2",
        "state": "migrating",
        "deadline": 13,
    }
    assert guard.invisible("hero") is True
    assert guard.authority("hero") == "world-1"
    assert guard.stats()["handovers"] == 1


def test_handover_guards_source_and_second_windows():
    guard = make()
    guard.register_entity("hero", process="world-1")
    with pytest.raises(aho.HandoverError) as exc:
        guard.begin_handover("hero", source="world-9", target="world-2")
    assert exc.value.code == "HANDOVER_WRONG_PROCESS"
    with pytest.raises(aho.HandoverError) as exc:
        guard.begin_handover("hero", source="world-1", target="world-1")
    assert exc.value.code == "HANDOVER_INVALID_ARGUMENT"
    first = guard.begin_handover("hero", source="world-1", target="world-2",
                                 tick=0)
    with pytest.raises(aho.HandoverError) as exc:
        guard.begin_handover("hero", source="world-1", target="world-3",
                             tick=0)
    assert exc.value.code == "HANDOVER_INVALID_STATE"
    assert guard.handover(first["ticket"])["target"] == "world-2"
    assert guard.stats()["handovers"] == 1


def test_export_state_is_self_consistent_and_isolated():
    guard = make()
    guard.register_entity("hero", process="world-1",
                          fields={"hp": 100, "buffs": [1]})
    guard.register_entity("npc", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=4)
    state = guard.export_state("hero")
    assert state["entity"] == "hero"
    assert state["ticket"] == "ho-1"
    assert state["authority"] == "world-1"
    assert state["children"] == []
    assert state["tick"] == 4
    state["fields"]["hp"] = 1
    state["fields"]["buffs"].append(2)
    fresh = guard.export_state("hero")
    assert fresh["fields"] == {"hp": 100, "buffs": [1]}
    with pytest.raises(aho.HandoverError) as exc:
        guard.export_state("npc")
    assert exc.value.code == "HANDOVER_INVALID_STATE"
    with pytest.raises(aho.HandoverError) as exc:
        guard.export_state("ghost")
    assert exc.value.code == "HANDOVER_UNKNOWN_ENTITY"


def test_import_into_the_wrong_target_is_rejected():
    guard = make()
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    state = guard.export_state("hero")
    assert guard.import_state("world-3", state) == {
        "accepted": False,
        "ticket": "ho-1",
        "reason": "wrong_target",
    }
    assert guard.invisible("hero") is True
    assert guard.authority("hero") == "world-1"
    assert guard.stats()["rejected"] == 1


def test_import_of_an_inconsistent_snapshot_is_rejected():
    guard = make()
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    state = guard.export_state("hero")
    mismatched = dict(state)
    mismatched["entity"] = "other"
    assert guard.import_state("world-2", mismatched) == {
        "accepted": False,
        "ticket": "ho-1",
        "reason": "state_mismatch",
    }
    incomplete = {"ticket": "ho-1", "entity": "hero"}
    assert guard.import_state("world-2", incomplete)["reason"] == "state_mismatch"
    bogus_tick = dict(state)
    bogus_tick["tick"] = "soon"
    assert guard.import_state("world-2", bogus_tick)["reason"] == "state_mismatch"
    assert guard.authority("hero") == "world-1"
    assert guard.invisible("hero") is True
    assert guard.stats()["rejected"] == 3


def test_receipt_commit_moves_authority_to_the_target():
    guard = make()
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    state = guard.export_state("hero")
    accepted = guard.import_state("world-2", state)
    assert accepted == {
        "accepted": True,
        "ticket": "ho-1",
        "entity": "hero",
        "target": "world-2",
        "receipt": "rc-ho-1",
    }
    done = guard.deliver_receipt("ho-1", "rc-ho-1")
    assert done == {
        "ticket": "ho-1",
        "entity": "hero",
        "outcome": "moved",
        "authority": "world-2",
        "invisible": False,
    }
    assert guard.invisible("hero") is False
    assert guard.authority("hero") == "world-2"
    assert guard.query("hero") == {
        "entity": "hero",
        "state": "visible",
        "authority": "world-2",
    }
    assert guard.handover("ho-1")["state"] == "moved"
    assert guard.stats()["moved"] == 1


def test_receipt_errors_are_explicit():
    guard = make()
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    with pytest.raises(aho.HandoverError) as exc:
        guard.deliver_receipt("ho-99", "rc-ho-99")
    assert exc.value.code == "HANDOVER_UNKNOWN_TICKET"
    with pytest.raises(aho.HandoverError) as exc:
        guard.deliver_receipt("ho-1", "rc-ho-1")
    assert exc.value.code == "HANDOVER_INVALID_STATE"
    state = guard.export_state("hero")
    guard.import_state("world-2", state)
    with pytest.raises(aho.HandoverError) as exc:
        guard.deliver_receipt("ho-1", "rc-bogus")
    assert exc.value.code == "HANDOVER_RECEIPT_INVALID"
    assert guard.authority("hero") == "world-1"
    assert guard.invisible("hero") is True


def test_timeout_rolls_back_only_past_the_deadline():
    guard = make(receipt_timeout_ticks=3)
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=10)
    assert guard.expire(13) == []
    assert guard.invisible("hero") is True
    assert guard.expire(14) == ["ho-1"]
    assert guard.authority("hero") == "world-1"
    assert guard.invisible("hero") is False
    assert guard.query("hero") == {
        "entity": "hero",
        "state": "visible",
        "authority": "world-1",
    }
    assert guard.handover("ho-1")["state"] == "rolled_back"
    assert guard.stats()["rolled_back"] == 1
    assert guard.expire(20) == []


def test_buffered_inputs_forward_in_sequence_and_apply_once():
    guard = make()
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    assert guard.buffer_input("hero", 2, {"dx": 1}) == {
        "entity": "hero",
        "seq": 2,
        "buffered": True,
        "duplicate": False,
    }
    assert guard.buffer_input("hero", 1)["buffered"] is True
    assert guard.buffer_input("hero", 1) == {
        "entity": "hero",
        "seq": 1,
        "buffered": False,
        "duplicate": True,
    }
    with pytest.raises(aho.HandoverError) as exc:
        guard.forward_inputs("ho-1")
    assert exc.value.code == "HANDOVER_INVALID_STATE"
    state = guard.export_state("hero")
    guard.import_state("world-2", state)
    guard.deliver_receipt("ho-1", "rc-ho-1")
    assert guard.forward_inputs("ho-1") == {
        "ticket": "ho-1",
        "entity": "hero",
        "forwarded": [1, 2],
        "count": 2,
    }
    assert guard.apply_input("world-2", "hero", 1) == {
        "entity": "hero",
        "seq": 1,
        "applied": True,
        "duplicate": False,
        "next_expected": 2,
    }
    assert guard.apply_input("world-2", "hero", 1) == {
        "entity": "hero",
        "seq": 1,
        "applied": False,
        "duplicate": True,
        "next_expected": 2,
    }
    assert guard.apply_input("world-2", "hero", 2) == {
        "entity": "hero",
        "seq": 2,
        "applied": True,
        "duplicate": False,
        "next_expected": 3,
    }
    assert guard.forward_inputs("ho-1") == {
        "ticket": "ho-1",
        "entity": "hero",
        "forwarded": [],
        "count": 0,
    }
    assert guard.stats()["inputs_forwarded"] == 2
    assert guard.stats()["inputs_applied"] == 2
    assert guard.stats()["inputs_duplicated"] == 1


def test_input_buffer_is_bounded():
    guard = make(max_buffered=2)
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    assert guard.buffer_input("hero", 1)["buffered"] is True
    assert guard.buffer_input("hero", 2)["buffered"] is True
    with pytest.raises(aho.HandoverError) as exc:
        guard.buffer_input("hero", 3)
    assert exc.value.code == "HANDOVER_INPUT_LIMIT"
    state = guard.export_state("hero")
    guard.import_state("world-2", state)
    guard.deliver_receipt("ho-1", "rc-ho-1")
    assert guard.forward_inputs("ho-1")["forwarded"] == [1, 2]
    assert guard.stats()["inputs_forwarded"] == 2


def test_apply_input_requires_the_owning_process():
    guard = make()
    guard.register_entity("hero", process="world-1")
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    state = guard.export_state("hero")
    guard.import_state("world-2", state)
    guard.deliver_receipt("ho-1", "rc-ho-1")
    with pytest.raises(aho.HandoverError) as exc:
        guard.apply_input("world-1", "hero", 1)
    assert exc.value.code == "HANDOVER_WRONG_PROCESS"
    with pytest.raises(aho.HandoverError) as exc:
        guard.apply_input("world-2", "ghost", 1)
    assert exc.value.code == "HANDOVER_UNKNOWN_ENTITY"
    with pytest.raises(aho.HandoverError) as exc:
        guard.apply_input("world-2", "hero", 0)
    assert exc.value.code == "HANDOVER_INVALID_ARGUMENT"
    assert guard.apply_input("world-2", "hero", 1)["applied"] is True


def test_stats_track_handover_outcomes():
    guard = make(receipt_timeout_ticks=2)
    for name in ("a", "b", "c"):
        guard.register_entity(name, process="world-1")
    ticket_a = guard.begin_handover("a", source="world-1", target="world-2",
                                    tick=0)["ticket"]
    guard.import_state("world-2", guard.export_state("a"))
    guard.deliver_receipt(ticket_a, f"rc-{ticket_a}")
    guard.begin_handover("b", source="world-1", target="world-2", tick=0)
    guard.expire(3)
    guard.begin_handover("c", source="world-1", target="world-2", tick=4)
    guard.import_state("world-3", guard.export_state("c"))
    assert guard.stats() == {
        "handovers": 3,
        "moved": 1,
        "rolled_back": 1,
        "rejected": 1,
        "inputs_forwarded": 0,
        "inputs_applied": 0,
        "inputs_duplicated": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "handovers": 0,
        "moved": 0,
        "rolled_back": 0,
        "rejected": 0,
        "inputs_forwarded": 0,
        "inputs_applied": 0,
        "inputs_duplicated": 0,
    }
    assert guard.authority("a") == "world-2"
    assert guard.authority("b") == "world-1"
    assert guard.invisible("c") is True


def test_snapshot_restore_preserves_an_open_window():
    guard = make()
    guard.register_entity("hero", process="world-1", fields={"hp": 100})
    guard.begin_handover("hero", source="world-1", target="world-2", tick=0)
    guard.buffer_input("hero", 1)
    state = guard.snapshot()
    assert state["version"] == 1
    frozen = copy.deepcopy(state)
    state["handovers"].clear()
    state["stats"]["handovers"] = 99
    assert guard.handover("ho-1")["state"] == "migrating"
    assert guard.stats()["handovers"] == 1
    twin = make()
    twin.restore(frozen)
    assert twin.invisible("hero") is True
    assert twin.authority("hero") == "world-1"
    assert twin.handover("ho-1")["deadline"] == 3
    exported = twin.export_state("hero")
    assert twin.import_state("world-2", exported)["accepted"] is True
    assert twin.deliver_receipt("ho-1", "rc-ho-1")["outcome"] == "moved"
    assert twin.authority("hero") == "world-2"
    assert twin.forward_inputs("ho-1")["forwarded"] == [1]
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(aho.HandoverError) as exc:
            twin.restore(bad)
        assert exc.value.code == "HANDOVER_SNAPSHOT_INVALID"
    assert twin.authority("hero") == "world-2"
