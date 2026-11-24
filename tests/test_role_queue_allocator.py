import copy
import threading

import pytest

import role_queue_allocator as rq


def make(**kwargs):
    return rq.RoleQueueAllocator(**kwargs)


def seed_five(alloc):
    for name, role in (("a1", "tank"), ("a2", "healer"), ("a3", "dps"),
                       ("a4", "dps"), ("a5", "dps")):
        alloc.enqueue(name, primary=role)


def test_enqueue_keeps_positions_and_wait_times():
    alloc = make()
    out = alloc.enqueue("p1", primary="tank", at=5)
    assert out == {
        "player": "p1", "primary": "tank", "secondary": None,
        "at": 5, "seq": 1, "position": 1,
    }
    alloc.enqueue("p2", primary="healer", secondary="dps", at=9)

    rows = alloc.queue_state(now=20)
    assert rows == [
        {"player": "p1", "primary": "tank", "secondary": None,
         "at": 5, "seq": 1, "position": 1, "wait": 15},
        {"player": "p2", "primary": "healer", "secondary": "dps",
         "at": 9, "seq": 2, "position": 2, "wait": 11},
    ]
    assert alloc.stats()["queued"] == 2


def test_enqueue_rejects_duplicate_unknown_role_and_full_queue():
    alloc = make(queue_limit=2)
    alloc.enqueue("p1", primary="tank")
    alloc.enqueue("p2", primary="dps")

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("p3", primary="dps")
    assert exc.value.code == "ROLE_QUEUE_FULL"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("p1", primary="dps")
    assert exc.value.code == "ROLE_ALREADY_QUEUED"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("p4", primary="wizard")
    assert exc.value.code == "ROLE_INVALID_ROLE"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("", primary="tank")
    assert exc.value.code == "ROLE_INVALID_PLAYER"

    assert [row["player"] for row in alloc.queue_state()] == ["p1", "p2"]
    assert alloc.stats()["rejected"] == 4


def test_enqueue_rejects_bad_secondary_and_bad_time():
    alloc = make()

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("p1", primary="tank", secondary="tank")
    assert exc.value.code == "ROLE_PREFERENCE_INVALID"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("p1", primary="tank", secondary="wizard")
    assert exc.value.code == "ROLE_INVALID_ROLE"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.enqueue("p1", primary="tank", at=-1)
    assert exc.value.code == "ROLE_INVALID_TIME"

    assert alloc.queue_state() == []
    assert alloc.stats()["rejected"] == 3


def test_form_team_fills_primary_slots_exactly():
    alloc = make()
    seed_five(alloc)

    team = alloc.form_team("t1")
    assert team == {
        "team_id": "t1",
        "slots": {"tank": ["a1"], "healer": ["a2"], "dps": ["a3", "a4", "a5"]},
        "assignment": {
            "a1": "primary", "a2": "primary", "a3": "primary",
            "a4": "primary", "a5": "primary",
        },
        "autofilled": [],
        "quotas": {"tank": 1, "healer": 1, "dps": 3},
        "locked": False,
        "size": 5,
    }
    assert alloc.queue_state() == []
    assert alloc.stats()["teams_formed"] == 1
    assert alloc.stats()["autofilled"] == 0


def test_form_team_uses_secondary_then_autofill_and_leaves_rest_queued():
    alloc = make()
    alloc.enqueue("b1", primary="tank")
    alloc.enqueue("b2", primary="tank", secondary="healer")
    alloc.enqueue("b3", primary="dps")
    alloc.enqueue("b4", primary="dps")
    alloc.enqueue("b5", primary="tank")
    alloc.enqueue("b6", primary="tank")

    team = alloc.form_team("t1")
    assert team["slots"] == {
        "tank": ["b1"], "healer": ["b2"], "dps": ["b3", "b4", "b5"],
    }
    assert team["assignment"] == {
        "b1": "primary", "b2": "secondary", "b3": "primary",
        "b4": "primary", "b5": "autofill",
    }
    assert team["autofilled"] == ["b5"]
    assert team["size"] == 5

    rows = alloc.queue_state(now=0)
    assert [row["player"] for row in rows] == ["b6"]
    assert rows[0]["position"] == 1
    assert alloc.stats()["autofilled"] == 1


def test_form_team_rejects_bad_quotas_missing_players_and_duplicate_team():
    alloc = make()
    alloc.enqueue("c1", primary="tank")
    alloc.enqueue("c2", primary="dps")

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.form_team("t1")
    assert exc.value.code == "ROLE_NOT_ENOUGH_PLAYERS"

    for name in ("c3", "c4", "c5"):
        alloc.enqueue(name, primary="dps")

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.form_team("t2", quotas={"tank": 1, "healer": 1, "dps": 2})
    assert exc.value.code == "ROLE_INVALID_QUOTA"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.form_team("t2", quotas={"tank": 1, "healer": 4})
    assert exc.value.code == "ROLE_INVALID_QUOTA"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.form_team("t2", quotas={"tank": 0, "healer": 1, "dps": 4})
    assert exc.value.code == "ROLE_INVALID_QUOTA"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.form_team("")
    assert exc.value.code == "ROLE_INVALID_TEAM"

    team = alloc.form_team("t2")
    assert team["size"] == 5

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.form_team("t2")
    assert exc.value.code == "ROLE_TEAM_EXISTS"
    assert alloc.stats()["rejected"] == 6


def test_swap_role_exchanges_two_members_and_updates_slots():
    alloc = make()
    seed_five(alloc)
    alloc.form_team("t1")

    out = alloc.swap_role("t1", "a1", "a3")
    assert out == {
        "team_id": "t1", "player_a": "a1", "role_a": "dps",
        "player_b": "a3", "role_b": "tank",
    }
    state = alloc.team_state("t1")
    assert state["slots"] == {
        "tank": ["a3"], "healer": ["a2"], "dps": ["a1", "a4", "a5"],
    }
    assert state["size"] == 5
    assert alloc.stats()["swapped"] == 1


def test_swap_role_rejects_unknown_team_member_same_player_and_same_role():
    alloc = make()
    seed_five(alloc)
    alloc.form_team("t1")

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.swap_role("nope", "a1", "a2")
    assert exc.value.code == "ROLE_UNKNOWN_TEAM"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.swap_role("t1", "a1", "ghost")
    assert exc.value.code == "ROLE_UNKNOWN_MEMBER"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.swap_role("t1", "a1", "a1")
    assert exc.value.code == "ROLE_SAME_PLAYER"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.swap_role("t1", "a3", "a4")
    assert exc.value.code == "ROLE_SAME_ROLE"

    assert alloc.team_state("t1")["slots"]["tank"] == ["a1"]
    assert alloc.stats()["swapped"] == 0


def test_leave_team_then_change_role_into_freed_slot():
    alloc = make()
    seed_five(alloc)
    alloc.form_team("t1")

    out = alloc.leave_team("t1", "a4")
    assert out == {"team_id": "t1", "player": "a4", "role": "dps"}
    state = alloc.team_state("t1")
    assert state["slots"] == {
        "tank": ["a1"], "healer": ["a2"], "dps": ["a3", "a5"],
    }
    assert state["size"] == 4
    assert state["assignment"].get("a4") is None

    out = alloc.change_role("t1", "a1", "dps")
    assert out == {"team_id": "t1", "player": "a1", "role": "dps", "from": "tank"}
    state = alloc.team_state("t1")
    assert state["slots"] == {
        "tank": [], "healer": ["a2"], "dps": ["a3", "a5", "a1"],
    }
    assert state["assignment"]["a1"] == "changed"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.leave_team("t1", "ghost")
    assert exc.value.code == "ROLE_UNKNOWN_MEMBER"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.change_role("t1", "ghost", "tank")
    assert exc.value.code == "ROLE_UNKNOWN_MEMBER"
    assert alloc.stats()["departed"] == 1
    assert alloc.stats()["changed"] == 1


def test_lock_team_freezes_role_edits():
    alloc = make()
    seed_five(alloc)
    alloc.form_team("t1")

    out = alloc.lock_team("t1")
    assert out == {"team_id": "t1", "locked": True, "size": 5}
    assert alloc.team_state("t1")["locked"] is True

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.swap_role("t1", "a1", "a3")
    assert exc.value.code == "ROLE_TEAM_LOCKED"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.change_role("t1", "a1", "dps")
    assert exc.value.code == "ROLE_TEAM_LOCKED"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.leave_team("t1", "a1")
    assert exc.value.code == "ROLE_TEAM_LOCKED"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.lock_team("t1")
    assert exc.value.code == "ROLE_ALREADY_LOCKED"
    assert alloc.stats()["locked"] == 1
    assert alloc.stats()["rejected"] == 4


def test_cancel_removes_ticket_and_second_cancel_is_rejected():
    alloc = make()
    alloc.enqueue("x1", primary="tank", at=3)
    alloc.enqueue("x2", primary="dps")

    out = alloc.cancel("x1")
    assert out == {
        "player": "x1", "primary": "tank", "secondary": None, "at": 3, "seq": 1,
    }
    rows = alloc.queue_state(now=10)
    assert [row["player"] for row in rows] == ["x2"]
    assert rows[0]["position"] == 1
    assert rows[0]["wait"] == 10

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.cancel("x1")
    assert exc.value.code == "ROLE_NOT_QUEUED"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.cancel("ghost")
    assert exc.value.code == "ROLE_NOT_QUEUED"

    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.cancel("")
    assert exc.value.code == "ROLE_INVALID_PLAYER"
    assert alloc.stats()["cancelled"] == 1
    assert alloc.stats()["rejected"] == 3


def test_autofill_protection_defaults_for_unseen_players():
    alloc = make()
    assert alloc.autofill_protection("someone") == {
        "player": "someone", "autofills": 0, "protection": 0,
    }
    with pytest.raises(rq.RoleQueueError) as exc:
        alloc.autofill_protection("")
    assert exc.value.code == "ROLE_INVALID_PLAYER"
    assert alloc.stats()["rejected"] == 1


def test_stats_counters_and_reset():
    alloc = make()
    for name, role in (("a1", "tank"), ("a2", "healer"), ("a3", "dps"),
                       ("a4", "dps"), ("a5", "dps"), ("a6", "dps")):
        alloc.enqueue(name, primary=role)
    alloc.form_team("t1")
    alloc.leave_team("t1", "a3")
    alloc.change_role("t1", "a1", "dps")
    alloc.swap_role("t1", "a2", "a4")
    alloc.cancel("a6")
    alloc.lock_team("t1")
    with pytest.raises(rq.RoleQueueError):
        alloc.cancel("ghost")

    assert alloc.stats() == {
        "queued": 6,
        "cancelled": 1,
        "teams_formed": 1,
        "autofilled": 0,
        "swapped": 1,
        "changed": 1,
        "locked": 1,
        "departed": 1,
        "rejected": 1,
    }
    alloc.reset_stats()
    assert alloc.stats() == {key: 0 for key in rq.STATS_KEYS}


def test_snapshot_restore_roundtrip_and_rejects_corrupt_state():
    alloc = make()
    alloc.enqueue("b1", primary="tank")
    alloc.enqueue("b2", primary="tank", secondary="healer")
    alloc.enqueue("b3", primary="dps")
    alloc.enqueue("b4", primary="dps")
    alloc.enqueue("b5", primary="tank")
    alloc.enqueue("b6", primary="tank")
    alloc.form_team("t1")

    snap = alloc.snapshot()
    assert snap["version"] == 1
    corrupted = copy.deepcopy(snap)
    corrupted["queue"][0]["primary"] = "wizard"
    assert alloc.queue_state()[0]["primary"] == "tank"

    fresh = make()
    fresh.restore(copy.deepcopy(snap))
    assert fresh.queue_state(now=4)[0]["player"] == "b6"
    assert fresh.queue_state(now=4)[0]["wait"] == 4
    assert fresh.team_state("t1") == alloc.team_state("t1")
    assert fresh.stats() == alloc.stats()

    bad_states = [
        None,
        {},
        {**copy.deepcopy(snap), "version": 2},
        {**copy.deepcopy(snap), "queue": "nope"},
        {**copy.deepcopy(snap), "seq": 0},
        {**copy.deepcopy(snap), "protection": {"b5": {"autofills": 1, "protection": -1}}},
        {**copy.deepcopy(snap), "stats": {"queued": 1}},
    ]
    broken_team = copy.deepcopy(snap)
    broken_team["teams"]["t1"]["slots"] = {"tank": ["b1"]}
    bad_states.append(broken_team)

    for state in bad_states:
        with pytest.raises(rq.RoleQueueError) as exc:
            fresh.restore(state)
        assert exc.value.code == "ROLE_SNAPSHOT_INVALID"

    assert fresh.queue_state()[0]["player"] == "b6"
    assert fresh.team_state("t1")["slots"]["dps"] == ["b3", "b4", "b5"]
    assert fresh.stats()["queued"] == 6


def test_concurrent_enqueue_allows_single_ticket_per_player():
    alloc = make()
    outcomes = []

    def worker():
        try:
            alloc.enqueue("racer", primary="tank")
            outcomes.append("ok")
        except rq.RoleQueueError as exc:
            outcomes.append(exc.code)
        except Exception as exc:  # pragma: no cover - surfaced through outcomes
            outcomes.append(f"unexpected:{type(exc).__name__}")

    threads = [threading.Thread(target=worker) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("ROLE_ALREADY_QUEUED") == 11
    assert len(alloc.queue_state()) == 1
    assert alloc.queue_state()[0]["player"] == "racer"
    assert alloc.stats()["queued"] == 1
    assert alloc.stats()["rejected"] == 11
