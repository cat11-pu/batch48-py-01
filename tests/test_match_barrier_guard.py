import copy
import threading

import pytest

import match_barrier_guard as mbg


def make(**kwargs):
    settings = {
        "threshold": "baseline",
        "timeout_ticks": 10,
        "grace_ticks": 5,
        "max_players": 8,
    }
    settings.update(kwargs)
    return mbg.MatchBarrierGuard(**settings)


def test_join_assigns_slots_and_rejects_duplicates():
    guard = make(max_players=2)
    assert guard.join("a") == {
        "player": "a",
        "slot": 0,
        "stage": None,
        "ready": False,
        "state": "waiting",
    }
    assert guard.join("b", tick=1)["slot"] == 1
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.join("a")
    assert exc.value.code == "MATCH_DUPLICATE_PLAYER"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.join("c")
    assert exc.value.code == "MATCH_FULL"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.join("")
    assert exc.value.code == "MATCH_INVALID_ARGUMENT"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.report_ready("ghost", "assets")
    assert exc.value.code == "MATCH_UNKNOWN_PLAYER"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.drop("ghost", tick=0)
    assert exc.value.code == "MATCH_UNKNOWN_PLAYER"
    assert guard.deadline() == 10
    assert guard.stats()["joins"] == 2


def test_report_ready_advances_monotonically_to_the_threshold():
    guard = make()
    guard.join("a")
    assert guard.report_ready("a", "assets", tick=1) == {
        "player": "a",
        "stage": "assets",
        "ready": False,
        "tick": 1,
    }
    assert guard.report_ready("a", "scene", tick=2)["ready"] is False
    assert guard.report_ready("a", "baseline", tick=3) == {
        "player": "a",
        "stage": "baseline",
        "ready": True,
        "tick": 3,
    }
    assert guard.report_ready("a", "baseline", tick=3)["ready"] is True
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.report_ready("a", "scene", tick=4)
    assert exc.value.code == "MATCH_STAGE_REGRESSION"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.report_ready("a", "loading", tick=4)
    assert exc.value.code == "MATCH_INVALID_STAGE"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        mbg.MatchBarrierGuard(threshold="loading")
    assert exc.value.code == "MATCH_INVALID_ARGUMENT"
    assert guard.players()["a"] == {
        "slot": 0,
        "stage": "baseline",
        "ready": True,
        "state": "waiting",
    }
    assert guard.stats()["reports"] == 3


def test_barrier_holds_until_every_player_is_ready():
    guard = make()
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline", tick=1)
    assert guard.barrier(3) == {
        "tick": 3,
        "released": False,
        "waiting": ["b"],
        "hosted": [],
        "released_at": None,
    }
    assert guard.state() == "waiting"
    assert guard.released_at() is None
    assert guard.stats()["barriers"] == 1


def test_barrier_releases_when_all_players_are_ready():
    guard = make()
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline")
    guard.report_ready("b", "baseline")
    assert guard.barrier(4) == {
        "tick": 4,
        "released": True,
        "waiting": [],
        "hosted": [],
        "released_at": 4,
    }
    assert guard.state() == "released"
    assert guard.released_at() == 4
    assert guard.players()["a"]["state"] == "released"
    assert guard.stats()["released"] == 1


def test_release_is_one_way_and_rejects_late_ready_reports():
    guard = make()
    guard.join("a")
    guard.report_ready("a", "baseline")
    guard.barrier(2)
    assert guard.barrier(9) == {
        "tick": 9,
        "released": True,
        "waiting": [],
        "hosted": [],
        "released_at": 2,
    }
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.report_ready("a", "scene", tick=9)
    assert exc.value.code == "MATCH_ALREADY_RELEASED"
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.join("late", tick=9)
    assert exc.value.code == "MATCH_ALREADY_RELEASED"
    assert guard.state() == "released"
    assert guard.released_at() == 2


def test_timeout_hosts_unready_players():
    guard = make(timeout_ticks=5)
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline")
    assert guard.barrier(4)["released"] is False
    assert guard.barrier(5) == {
        "tick": 5,
        "released": True,
        "waiting": [],
        "hosted": ["b"],
        "released_at": 5,
    }
    assert guard.players()["b"]["state"] == "hosted"
    assert guard.players()["a"]["state"] == "released"
    assert guard.stats()["hosted"] == 1
    assert guard.state() == "released"


def test_drop_before_release_removes_the_blocker():
    guard = make()
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline")
    assert guard.drop("b", tick=2) == {
        "player": "b",
        "state": "left",
        "deadline": None,
    }
    assert guard.players()["b"]["state"] == "left"
    out = guard.barrier(3)
    assert out["released"] is True
    assert out["waiting"] == []
    assert out["released_at"] == 3
    assert guard.stats()["dropped"] == 1
    assert guard.state() == "released"


def test_drop_after_release_opens_a_grace_window():
    guard = make()
    guard.join("a")
    guard.report_ready("a", "baseline")
    guard.barrier(2)
    assert guard.drop("a", tick=3) == {
        "player": "a",
        "state": "grace",
        "deadline": 8,
    }
    assert guard.state() == "released"
    assert guard.released_at() == 2
    assert guard.short_handed() == []
    assert guard.players()["a"]["state"] == "grace"
    assert guard.stats()["dropped"] == 1


def test_grace_expiry_marks_the_match_short_handed():
    guard = make()
    guard.join("a")
    guard.report_ready("a", "baseline")
    guard.barrier(0)
    guard.drop("a", tick=1)
    assert guard.advance(6) == {"tick": 6, "expired": [], "short_handed": []}
    out = guard.advance(7)
    assert out["expired"] == ["a"]
    assert out["short_handed"] == ["a"]
    assert guard.short_handed() == ["a"]
    assert guard.players()["a"]["state"] == "absent"
    assert guard.state() == "released"
    assert guard.released_at() == 0
    assert guard.stats()["short_handed"] == 1


def test_substitute_takes_over_the_grace_slot():
    guard = make()
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline")
    guard.report_ready("b", "baseline")
    guard.barrier(2)
    guard.drop("b", tick=3)
    out = guard.substitute("c", tick=4)
    assert out["player"] == "c"
    assert out["slot"] == 1
    assert out["state"] == "substituted"
    assert guard.players()["c"]["slot"] == 1
    assert guard.players()["c"]["state"] == "substituted"
    assert guard.players()["b"]["state"] == "replaced"
    assert guard.short_handed() == []
    assert guard.stats()["substituted"] == 1


def test_substitute_after_grace_expiry_is_refused():
    guard = make()
    guard.join("a")
    guard.report_ready("a", "baseline")
    guard.barrier(2)
    guard.drop("a", tick=3)
    guard.advance(9)
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.substitute("d", tick=10)
    assert exc.value.code == "MATCH_GRACE_EXPIRED"
    assert guard.stats()["substituted"] == 0


def test_substitute_outside_a_grace_window_is_refused():
    guard = make()
    guard.join("a")
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.substitute("c", tick=1)
    assert exc.value.code == "MATCH_INVALID_STATE"
    guard.report_ready("a", "baseline")
    guard.barrier(2)
    with pytest.raises(mbg.MatchBarrierError) as exc:
        guard.substitute("c", tick=3)
    assert exc.value.code == "MATCH_INVALID_STATE"
    assert guard.stats()["substituted"] == 0


def test_stats_and_reset():
    guard = make()
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline")
    guard.report_ready("b", "baseline")
    guard.barrier(3)
    guard.drop("a", tick=4)
    guard.advance(9)
    guard.advance(10)
    assert guard.stats() == {
        "joins": 2,
        "reports": 2,
        "barriers": 1,
        "released": 1,
        "hosted": 0,
        "dropped": 1,
        "substituted": 0,
        "short_handed": 1,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "joins": 0,
        "reports": 0,
        "barriers": 0,
        "released": 0,
        "hosted": 0,
        "dropped": 0,
        "substituted": 0,
        "short_handed": 0,
    }
    assert guard.state() == "released"
    assert guard.released_at() == 3


def test_snapshot_restore_keeps_the_open_window():
    guard = make()
    guard.join("a")
    guard.join("b")
    guard.report_ready("a", "baseline", tick=1)
    state = guard.snapshot()
    assert state["version"] == 1
    frozen = copy.deepcopy(state)
    state["players"].clear()
    state["stats"]["joins"] = 99
    assert guard.players()["a"]["stage"] == "baseline"
    assert guard.stats()["joins"] == 2
    twin = make()
    twin.restore(frozen)
    assert twin.state() == "waiting"
    assert twin.deadline() == 10
    assert twin.players()["a"]["stage"] == "baseline"
    assert twin.players()["b"]["stage"] is None
    twin.report_ready("b", "baseline", tick=2)
    out = twin.barrier(3)
    assert out["released"] is True
    assert out["released_at"] == 3
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(mbg.MatchBarrierError) as exc:
            twin.restore(bad)
        assert exc.value.code == "MATCH_SNAPSHOT_INVALID"
    assert twin.state() == "released"
    assert twin.released_at() == 3


def test_concurrent_ready_reports_release_exactly_once():
    guard = make(max_players=8)
    for index in range(8):
        guard.join(f"p{index}")
    errors = []

    def reporter(index):
        try:
            guard.report_ready(f"p{index}", "baseline", tick=1)
        except Exception as exc:  # pragma: no cover - collected below
            errors.append(repr(exc))

    threads = [threading.Thread(target=reporter, args=(index,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []

    results = []

    def gate():
        try:
            results.append(guard.barrier(2))
        except Exception as exc:  # pragma: no cover - collected below
            errors.append(repr(exc))

    gates = [threading.Thread(target=gate) for _ in range(8)]
    for thread in gates:
        thread.start()
    for thread in gates:
        thread.join()

    assert errors == []
    assert len(results) == 8
    assert all(item["released"] is True for item in results)
    assert {item["released_at"] for item in results} == {2}
    assert guard.state() == "released"
    assert guard.stats()["released"] == 1
    assert guard.stats()["barriers"] == 8
