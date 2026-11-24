import threading

import pytest

import spectator_bench_orchestrator as sb


def make(**kwargs):
    return sb.SpectatorBenchOrchestrator(**kwargs)


def test_assign_active_lists_roster_and_seat_state():
    room = make(active_slots=3)
    out = room.assign_active(2, "alice")
    assert out == {"player": "alice", "seat": 2, "role": "active", "permission": True}
    room.assign_active(1, "bob")

    roster = room.roster()
    assert roster["active"] == [
        {"seat": 1, "player": "bob"},
        {"seat": 2, "player": "alice"},
    ]
    assert roster["bench"] == []
    assert roster["spectators"] == []
    assert roster["settled"] is False
    assert roster["counts"] == {"active": 2, "bench": 0, "spectators": 0}

    state = room.seat_state("alice")
    assert state == {
        "player": "alice",
        "role": "active",
        "seat": 2,
        "bench_index": None,
        "permission": True,
        "reason": None,
    }
    assert room.can_operate("alice") is True


def test_assign_active_rejects_bad_seat_occupied_seat_and_duplicate_player():
    room = make(active_slots=2)
    room.assign_active(1, "alice")

    with pytest.raises(sb.SpectatorError) as exc:
        room.assign_active(0, "bob")
    assert exc.value.code == "SPECTATOR_SEAT_INVALID"

    with pytest.raises(sb.SpectatorError) as exc:
        room.assign_active(3, "bob")
    assert exc.value.code == "SPECTATOR_SEAT_INVALID"

    with pytest.raises(sb.SpectatorError) as exc:
        room.assign_active(1, "bob")
    assert exc.value.code == "SPECTATOR_SEAT_OCCUPIED"

    with pytest.raises(sb.SpectatorError) as exc:
        room.assign_active(2, "alice")
    assert exc.value.code == "SPECTATOR_ALREADY_SEATED"

    assert room.stats()["rejected"] == 4
    assert room.roster()["counts"]["active"] == 1


def test_spectator_is_read_only_and_capacity_capped():
    room = make(spectator_limit=2)
    first = room.join_spectator("spec-a")
    second = room.join_spectator("spec-b")
    assert first == {"player": "spec-a", "role": "spectator", "seat": 1, "permission": False}
    assert second["seat"] == 2
    assert room.can_operate("spec-a") is False

    with pytest.raises(sb.SpectatorError) as exc:
        room.set_permission("spec-a", True)
    assert exc.value.code == "SPECTATOR_READ_ONLY"

    with pytest.raises(sb.SpectatorError) as exc:
        room.join_spectator("spec-c")
    assert exc.value.code == "SPECTATOR_ROOM_FULL"

    assert room.roster()["counts"]["spectators"] == 2
    assert room.stats()["spectators_joined"] == 2


def test_bench_join_keeps_fifo_order_and_caps_capacity():
    room = make(bench_limit=2)
    assert room.join_bench("ben-a")["bench_index"] == 1
    assert room.join_bench("ben-b")["bench_index"] == 2

    roster = room.roster()
    assert [entry["player"] for entry in roster["bench"]] == ["ben-a", "ben-b"]
    assert [entry["reason"] for entry in roster["bench"]] == [None, None]

    with pytest.raises(sb.SpectatorError) as exc:
        room.join_bench("ben-c")
    assert exc.value.code == "SPECTATOR_BENCH_FULL"

    state = room.seat_state("ben-b")
    assert state["role"] == "bench"
    assert state["bench_index"] == 2
    assert state["seat"] is None
    assert room.can_operate("ben-a") is False


def test_promote_moves_bench_player_and_returns_replaced_to_bench():
    room = make(active_slots=2, bench_limit=3)
    room.assign_active(1, "starter")
    room.join_bench("sub-one")

    out = room.promote("sub-one", 1, reason="starter_disconnect")
    assert out["status"] == "promoted"
    assert out["player"] == "sub-one"
    assert out["seat"] == 1
    assert out["replaced"] == "starter"
    assert out["reason"] == "starter_disconnect"

    assert room.seat_state("sub-one")["role"] == "active"
    assert room.can_operate("sub-one") is True
    replaced = room.seat_state("starter")
    assert replaced["role"] == "bench"
    assert replaced["reason"] == "starter_disconnect"
    assert replaced["bench_index"] == 1
    assert room.stats()["promotions"] == 1
    assert room.roster()["counts"] == {"active": 1, "bench": 1, "spectators": 0}


def test_promote_rejects_empty_or_invalid_seat():
    room = make(active_slots=2, bench_limit=2)
    room.join_bench("sub")

    with pytest.raises(sb.SpectatorError) as exc:
        room.promote("sub", 2)
    assert exc.value.code == "SPECTATOR_SEAT_EMPTY"

    with pytest.raises(sb.SpectatorError) as exc:
        room.promote("sub", 7)
    assert exc.value.code == "SPECTATOR_SEAT_INVALID"

    assert room.seat_state("sub")["role"] == "bench"
    assert room.stats()["promotions"] == 0


def test_promote_and_demote_reject_wrong_roles_and_unknown_players():
    room = make(active_slots=2)
    room.assign_active(1, "starter")
    room.join_spectator("watcher")
    room.join_bench("sub")

    with pytest.raises(sb.SpectatorError) as exc:
        room.promote("ghost", 1)
    assert exc.value.code == "SPECTATOR_UNKNOWN_PLAYER"

    with pytest.raises(sb.SpectatorError) as exc:
        room.promote("watcher", 1)
    assert exc.value.code == "SPECTATOR_BENCH_REQUIRED"

    with pytest.raises(sb.SpectatorError) as exc:
        room.demote("sub")
    assert exc.value.code == "SPECTATOR_NOT_ACTIVE"

    with pytest.raises(sb.SpectatorError) as exc:
        room.demote("watcher")
    assert exc.value.code == "SPECTATOR_NOT_ACTIVE"

    with pytest.raises(sb.SpectatorError) as exc:
        room.demote("ghost")
    assert exc.value.code == "SPECTATOR_UNKNOWN_PLAYER"

    assert room.stats()["rejected"] == 5
    assert room.stats()["promotions"] == 0
    assert room.stats()["demotions"] == 0


def test_demote_moves_active_player_to_bench_with_reason():
    room = make(active_slots=2, bench_limit=2)
    room.assign_active(1, "starter")

    out = room.demote("starter", reason="voluntary")
    assert out == {
        "player": "starter",
        "seat": 1,
        "role": "bench",
        "reason": "voluntary",
        "bench_index": 1,
    }
    state = room.seat_state("starter")
    assert state["role"] == "bench"
    assert state["reason"] == "voluntary"
    assert state["seat"] is None
    assert room.roster()["counts"] == {"active": 0, "bench": 1, "spectators": 0}

    room.assign_active(1, "next")
    assert room.seat_state("next")["seat"] == 1
    assert room.stats()["demotions"] == 1


def test_demote_is_refused_when_bench_is_full():
    room = make(active_slots=2, bench_limit=1)
    room.assign_active(1, "starter")
    room.join_bench("sub")

    with pytest.raises(sb.SpectatorError) as exc:
        room.demote("starter", reason="timeout")
    assert exc.value.code == "SPECTATOR_BENCH_FULL"

    assert room.seat_state("starter")["role"] == "active"
    assert room.roster()["counts"] == {"active": 1, "bench": 1, "spectators": 0}
    assert room.stats()["demotions"] == 0
    assert room.stats()["rejected"] == 1


def test_swap_seat_exchanges_two_active_seats_and_rejects_errors():
    room = make(active_slots=3)
    room.assign_active(1, "alice")
    room.assign_active(2, "bob")
    room.assign_active(3, "carol")
    room.join_bench("sub")

    out = room.swap_seat("alice", "bob")
    assert out == {"player_a": "alice", "seat_a": 2, "player_b": "bob", "seat_b": 1}
    assert room.seat_state("alice")["seat"] == 2
    assert room.seat_state("bob")["seat"] == 1
    assert room.seat_state("carol")["seat"] == 3

    with pytest.raises(sb.SpectatorError) as exc:
        room.swap_seat("alice", "alice")
    assert exc.value.code == "SPECTATOR_SAME_PLAYER"

    with pytest.raises(sb.SpectatorError) as exc:
        room.swap_seat("alice", "sub")
    assert exc.value.code == "SPECTATOR_NOT_ACTIVE"

    with pytest.raises(sb.SpectatorError) as exc:
        room.swap_seat("carol", "ghost")
    assert exc.value.code == "SPECTATOR_UNKNOWN_PLAYER"

    assert room.stats()["swaps"] == 1


def test_leave_frees_active_bench_and_spectator_slots():
    room = make(active_slots=2, spectator_limit=3, bench_limit=3)
    room.assign_active(1, "starter")
    room.join_bench("sub")
    room.join_spectator("watcher")

    assert room.leave("starter") == {
        "player": "starter", "role": "active", "seat": 1, "settled": False,
    }
    assert room.roster()["counts"]["active"] == 0

    assert room.leave("sub") == {
        "player": "sub", "role": "bench", "seat": None, "settled": False,
    }
    assert room.roster()["bench"] == []

    assert room.leave("watcher") == {
        "player": "watcher", "role": "spectator", "seat": 1, "settled": False,
    }
    assert room.roster()["spectators"] == []

    with pytest.raises(sb.SpectatorError) as exc:
        room.leave("watcher")
    assert exc.value.code == "SPECTATOR_UNKNOWN_PLAYER"
    assert room.stats()["left"] == 3


def test_permission_switch_applies_to_active_and_bench_only():
    room = make(active_slots=2)
    room.assign_active(1, "starter")
    room.join_bench("sub")

    assert room.can_operate("starter") is True
    out = room.set_permission("starter", False)
    assert out == {"player": "starter", "role": "active", "permission": False}
    assert room.can_operate("starter") is False
    assert room.seat_state("starter")["permission"] is False

    out = room.set_permission("sub", True)
    assert out == {"player": "sub", "role": "bench", "permission": True}
    assert room.seat_state("sub")["permission"] is True
    assert room.can_operate("sub") is False

    with pytest.raises(sb.SpectatorError) as exc:
        room.set_permission("ghost", True)
    assert exc.value.code == "SPECTATOR_UNKNOWN_PLAYER"


def test_settle_match_freezes_room_and_rejects_second_settle():
    room = make(active_slots=2)
    room.assign_active(1, "starter")
    room.join_bench("sub")

    out = room.settle_match("blue_win")
    assert out == {
        "settled": True,
        "result": "blue_win",
        "counts": {"active": 1, "bench": 1, "spectators": 0},
    }
    roster = room.roster()
    assert roster["settled"] is True
    assert roster["result"] == "blue_win"

    with pytest.raises(sb.SpectatorError) as exc:
        room.settle_match("red_win")
    assert exc.value.code == "SPECTATOR_MATCH_SETTLED"

    with pytest.raises(sb.SpectatorError) as exc:
        room.assign_active(2, "late")
    assert exc.value.code == "SPECTATOR_MATCH_SETTLED"

    with pytest.raises(sb.SpectatorError) as exc:
        room.join_spectator("watcher")
    assert exc.value.code == "SPECTATOR_MATCH_SETTLED"

    assert room.stats()["settled"] == 1
    assert room.roster()["counts"] == {"active": 1, "bench": 1, "spectators": 0}


def test_snapshot_restore_roundtrip_and_rejects_corrupt_state():
    room = make(active_slots=2, spectator_limit=4, bench_limit=4)
    room.assign_active(1, "starter")
    room.join_bench("sub")
    room.join_spectator("watcher")

    snap = room.snapshot()
    assert snap["version"] == 1
    snap["active"].clear()
    snap["bench"].clear()
    snap["spectators"].clear()
    assert room.roster()["counts"] == {"active": 1, "bench": 1, "spectators": 1}

    fresh = make(active_slots=2, spectator_limit=4, bench_limit=4)
    fresh.restore(room.snapshot())
    assert fresh.roster()["active"] == [{"seat": 1, "player": "starter"}]
    assert fresh.seat_state("sub")["role"] == "bench"
    assert fresh.seat_state("watcher")["seat"] == 1
    assert fresh.can_operate("starter") is True

    bad_states = [
        None,
        {},
        {"version": 2, "active": {}, "bench": [], "spectators": {},
         "permissions": {}, "settled": False, "result": None,
         "stats": {key: 0 for key in sb.STATS_KEYS}},
        {"version": 1, "active": "nope", "bench": [], "spectators": {},
         "permissions": {}, "settled": False, "result": None,
         "stats": {key: 0 for key in sb.STATS_KEYS}},
        {"version": 1, "active": {1: "starter"}, "bench": [{"player": "x"}],
         "spectators": {}, "permissions": {}, "settled": False, "result": None,
         "stats": {key: 0 for key in sb.STATS_KEYS}},
        {"version": 1, "active": {1: "starter"}, "bench": [], "spectators": {},
         "permissions": {}, "settled": False, "result": None, "stats": {"left": 0}},
    ]
    for state in bad_states:
        with pytest.raises(sb.SpectatorError) as exc:
            fresh.restore(state)
        assert exc.value.code == "SPECTATOR_SNAPSHOT_INVALID"

    assert fresh.roster()["counts"] == {"active": 1, "bench": 1, "spectators": 1}
    assert fresh.seat_state("starter")["role"] == "active"


def test_concurrent_promotions_to_distinct_seats_are_all_applied():
    room = make(active_slots=8, bench_limit=16)
    for index in range(8):
        room.assign_active(index + 1, f"starter-{index}")
    for index in range(8):
        room.join_bench(f"sub-{index}")

    errors = []

    def worker(index):
        try:
            room.promote(f"sub-{index}", index + 1, reason="rotation")
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    active = {row["player"]: row["seat"] for row in room.roster()["active"]}
    for index in range(8):
        assert active[f"sub-{index}"] == index + 1
    assert room.roster()["counts"] == {"active": 8, "bench": 8, "spectators": 0}
    assert room.stats()["promotions"] == 8
    assert room.stats()["rejected"] == 0
