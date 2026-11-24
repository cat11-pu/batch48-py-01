import copy
import threading

import pytest

import match_result_ledger as ml

BASE = 1000


def make(**kwargs):
    kwargs.setdefault("season_starts", {"s1": 0, "s2": 1000, "s3": 2000})
    return ml.MatchResultLedger(**kwargs)


def result(delta, win=True):
    return {"delta": delta, "win": win}


def test_submit_result_records_entries_and_returns_ratings():
    led = make()
    out = led.submit_result(
        "m1", ended_at=100,
        results={"p1": result(25), "p2": result(-10, False)},
    )
    assert out == {
        "match_id": "m1",
        "season": "s1",
        "status": "accepted",
        "players": ["p1", "p2"],
        "ratings": {"p1": 1025, "p2": 990},
        "entry_count": 2,
        "revision": 1,
    }
    assert led.stats()["accepted"] == 1
    assert led.stats()["entries"] == 2


def test_duplicate_submission_returns_first_result_without_entries():
    led = make()
    first = led.submit_result("m1", ended_at=100,
                              results={"p1": result(25), "p2": result(-10, False)})
    again = led.submit_result("m1", ended_at=100,
                             results={"p2": result(-10, False), "p1": result(25)})
    assert again["status"] == "duplicate"
    assert again["ratings"] == first["ratings"] == {"p1": 1025, "p2": 990}
    assert again["entry_count"] == 2
    assert len(led.history_of("p1", season="s1")) == 1
    assert led.stats()["accepted"] == 1
    assert led.stats()["duplicates"] == 1
    assert led.stats()["entries"] == 2


def test_late_result_uses_ended_at_for_season_attribution():
    led = make()
    fresh = led.submit_result("late-2", ended_at=1500, results={"p1": result(50)})
    late = led.submit_result("late-1", ended_at=500, results={"p1": result(10)})
    assert fresh["season"] == "s2"
    assert late["season"] == "s1"
    assert led.rating_of("p1", season="s1") == 1010
    assert led.rating_of("p1", season="s2") == 1050
    assert [e["match_id"] for e in led.history_of("p1", season="s1")] == ["late-1"]


def test_season_boundary_belongs_to_later_season():
    led = make()
    assert led.resolve_season(0) == "s1"
    assert led.resolve_season(999) == "s1"
    assert led.resolve_season(1000) == "s2"
    assert led.resolve_season(1999) == "s2"
    assert led.resolve_season(2000) == "s3"
    out = led.submit_result("b1", ended_at=1000, results={"p1": result(1)})
    assert out["season"] == "s2"
    with pytest.raises(ml.MatchLedgerError) as exc:
        led.resolve_season(-1)
    assert exc.value.code == "LEDGER_INVALID_RESULT"
    narrow = make(season_starts={"s1": 100})
    with pytest.raises(ml.MatchLedgerError) as exc:
        narrow.resolve_season(99)
    assert exc.value.code == "LEDGER_UNKNOWN_SEASON"


def test_rating_of_defaults_to_base_for_unknown_players():
    led = make()
    assert led.rating_of("ghost", season="s1") == BASE
    assert led.rating_of("ghost", season="s9") == BASE


def test_amend_appends_reversal_then_new_entries():
    led = make()
    led.submit_result("m1", ended_at=10,
                      results={"p1": result(30), "p2": result(-20, False)})
    out = led.amend_result("m1",
                           results={"p1": result(5, False), "p2": result(8)},
                           reason="scoreboard fix")
    assert out["status"] == "amended"
    assert out["amendment"] == 1
    assert out["revision"] == 2
    assert out["ratings"] == {"p1": 1005, "p2": 1008}
    hist = led.history_of("p1", season="s1")
    assert [e["kind"] for e in hist] == ["settle", "amend_reversal", "amend"]
    assert [e["delta"] for e in hist] == [30, -30, 5]
    assert led.match_state("m1") == "amended"
    assert led.stats()["amended"] == 1
    assert led.stats()["entries"] == 6


def test_settlement_of_reports_net_deltas_after_amendment():
    led = make()
    led.submit_result("m1", ended_at=10,
                      results={"p1": result(30), "p2": result(-20, False)})
    assert led.settlement_of("m1") == {
        "match_id": "m1",
        "season": "s1",
        "status": "settled",
        "players": ["p1", "p2"],
        "deltas": {"p1": 30, "p2": -20},
        "entry_count": 2,
        "revision": 1,
    }
    led.amend_result("m1", results={"p1": result(5, False), "p2": result(8)})
    after = led.settlement_of("m1")
    assert after["status"] == "amended"
    assert after["deltas"] == {"p1": 5, "p2": 8}
    assert after["entry_count"] == 6
    assert after["revision"] == 2
    with pytest.raises(ml.MatchLedgerError) as exc:
        led.amend_result("m1", results={"p1": result(1)})
    assert exc.value.code == "LEDGER_INVALID_RESULT"


def test_invalid_result_payload_is_rejected_without_side_effects():
    led = make()
    bad = [
        ("", {"ended_at": 10, "results": {"p1": result(1)}}),
        ("m", {"ended_at": -1, "results": {"p1": result(1)}}),
        ("m", {"ended_at": 10, "results": {}}),
        ("m", {"ended_at": 10, "results": {"": result(1)}}),
        ("m", {"ended_at": 10, "results": {"p1": {"delta": True, "win": True}}}),
        ("m", {"ended_at": 10, "results": {"p1": {"delta": 1}}}),
        ("m", {"ended_at": 10, "results": {"p1": "win"}}),
    ]
    for match_id, kwargs in bad:
        with pytest.raises(ml.MatchLedgerError) as exc:
            led.submit_result(match_id, **kwargs)
        assert exc.value.code == "LEDGER_INVALID_RESULT"
    assert led.stats()["rejected"] == len(bad)
    assert led.stats()["entries"] == 0
    assert led.stats()["accepted"] == 0


def test_unknown_match_lookups_raise_and_state_is_unknown():
    led = make()
    assert led.match_state("nope") == "unknown"
    calls = [
        lambda: led.settlement_of("nope"),
        lambda: led.amend_result("nope", results={"p1": result(1)}),
        lambda: led.void_match("nope"),
    ]
    for call in calls:
        with pytest.raises(ml.MatchLedgerError) as exc:
            call()
        assert exc.value.code == "LEDGER_UNKNOWN_MATCH"


def test_rating_floor_keeps_original_delta_in_history():
    led = make()
    out = led.submit_result("m1", ended_at=10, results={"p1": result(-1500, False)})
    assert out["ratings"] == {"p1": 0}
    hist = led.history_of("p1", season="s1")
    assert hist[0]["delta"] == -1500
    assert hist[0]["balance_after"] == 0
    second = led.submit_result("m2", ended_at=20, results={"p1": result(12)})
    assert second["ratings"] == {"p1": 12}


def test_entry_limit_rejects_whole_submission():
    led = make(max_entries=3)
    led.submit_result("m1", ended_at=1, results={"p1": result(1), "p2": result(2)})
    with pytest.raises(ml.MatchLedgerError) as exc:
        led.submit_result("m2", ended_at=2,
                          results={"p3": result(3), "p4": result(4)})
    assert exc.value.code == "LEDGER_ENTRY_LIMIT"
    assert led.match_state("m2") == "unknown"
    assert led.rating_of("p3", season="s1") == BASE
    assert led.stats()["entries"] == 2
    assert led.stats()["accepted"] == 1
    assert led.stats()["rejected"] == 1


def test_history_is_append_only_and_returns_detached_copies():
    led = make()
    led.submit_result("m1", ended_at=10, results={"p1": result(5)})
    led.submit_result("m2", ended_at=20, results={"p1": result(-2, False)})
    led.submit_result("m3", ended_at=30, results={"p1": result(7)})
    hist = led.history_of("p1", season="s1")
    assert [e["seq"] for e in hist] == [1, 2, 3]
    assert [e["match_id"] for e in hist] == ["m1", "m2", "m3"]
    assert [e["balance_after"] for e in hist] == [1005, 1003, 1010]
    hist[0]["delta"] = 999
    hist.append({"seq": 99})
    fresh = led.history_of("p1", season="s1")
    assert [e["delta"] for e in fresh] == [5, -2, 7]
    assert len(fresh) == 3


def test_stats_track_outcomes_and_reset():
    led = make()
    led.submit_result("m1", ended_at=1,
                      results={"p1": result(3), "p2": result(-1, False)})
    led.submit_result("m1", ended_at=1,
                      results={"p1": result(3), "p2": result(-1, False)})
    with pytest.raises(ml.MatchLedgerError):
        led.submit_result("m2", ended_at=2, results={})
    led.amend_result("m1", results={"p1": result(4), "p2": result(-2, False)})
    assert led.stats() == {
        "accepted": 1,
        "duplicates": 1,
        "conflicts": 0,
        "amended": 1,
        "voided": 0,
        "entries": 6,
        "rejected": 1,
    }
    led.reset_stats()
    assert led.stats() == {
        "accepted": 0, "duplicates": 0, "conflicts": 0, "amended": 0,
        "voided": 0, "entries": 0, "rejected": 0,
    }


def test_snapshot_restore_preserves_ledger_and_rejects_corruption():
    led = make()
    led.submit_result("m1", ended_at=100,
                      results={"p1": result(25), "p2": result(-10, False)})
    led.submit_result("m2", ended_at=1500, results={"p1": result(40)})
    snap = led.snapshot()
    assert snap["version"] == 1
    pristine = copy.deepcopy(snap)
    assert pristine["balances"]["s1"] == {"p1": 1025, "p2": 990}

    snap["balances"].clear()
    snap["entries"].append({"seq": 999})
    assert led.rating_of("p1", season="s1") == 1025
    assert len(led.history_of("p1", season="s1")) == 1

    restored = make()
    restored.restore(pristine)
    assert restored.rating_of("p1", season="s1") == 1025
    assert restored.rating_of("p1", season="s2") == 1040
    assert restored.history_of("p1", season="s1") == led.history_of("p1", season="s1")
    assert restored.match_state("m1") == "settled"
    assert restored.stats() == led.stats()

    broken = [
        None,
        {},
        {"version": 2},
        {"version": 1, "balances": [], "matches": {}, "entries": [],
         "season_matches": {}, "stats": {}, "seq": 0, "base_rating": 1000,
         "season_starts": {"s1": 0}},
    ]
    for bad in broken:
        with pytest.raises(ml.MatchLedgerError) as exc:
            restored.restore(bad)
        assert exc.value.code == "LEDGER_SNAPSHOT_INVALID"
    assert restored.rating_of("p1", season="s1") == 1025


def test_concurrent_duplicate_submissions_apply_once():
    led = make()
    payload = {"p1": result(10), "p2": result(-4, False)}
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            out = led.submit_result("m-race", ended_at=42, results=payload)
            tag = out["status"]
        except Exception as exc:  # pragma: no cover - reported as a failure
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
    assert led.rating_of("p1", season="s1") == 1010
    assert led.rating_of("p2", season="s1") == 996
    assert len(led.history_of("p1", season="s1")) == 1
    assert led.stats()["accepted"] == 1
    assert led.stats()["duplicates"] == 7
