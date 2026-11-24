import copy
import threading

import pytest

import session_tab_synchronizer as sts

NOW = 0.0
LATER = 1.0
FAR = 100.0


def sync(**kwargs):
    return sts.SessionTabSynchronizer(**kwargs)


def open_tab(s, tab_id, session_id="s1", capabilities=(), now=NOW):
    return s.open_tab(session_id, tab_id, capabilities, now=now)


def stats_shape(s):
    return {
        "opened": 0,
        "reopened": 0,
        "closed": 0,
        "expired": 0,
        "heartbeats": 0,
        "refreshes": 0,
        "refresh_failures": 0,
        "logouts": 0,
    }


def test_open_tab_keeps_sorted_capabilities_and_counts_reopen():
    s = sync()
    view = open_tab(s, "t1", capabilities=["write", "read", "read"])
    assert view.tab_id == "t1"
    assert view.session_id == "s1"
    assert view.generation == 1
    assert view.capabilities == ("read", "write")
    assert view.last_seen == NOW
    assert view.valid is True

    again = open_tab(s, "t1", capabilities=["admin"], now=LATER)
    assert again.capabilities == ("read", "write")
    assert again.last_seen == LATER
    assert again.valid is True
    assert s.stats()["opened"] == 1
    assert s.stats()["reopened"] == 1
    assert [row.tab_id for row in s.tabs_for("s1")] == ["t1"]


def test_tab_capacity_is_per_session_and_rejected_tabs_are_not_recorded():
    s = sync(max_tabs_per_session=2)
    open_tab(s, "t1")
    open_tab(s, "t2")
    with pytest.raises(sts.SessionTabError) as exc:
        open_tab(s, "t3")
    assert exc.value.code == "SESSION_TAB_CAPACITY_EXCEEDED"
    assert [row.tab_id for row in s.tabs_for("s1")] == ["t1", "t2"]
    assert s.stats()["opened"] == 2

    open_tab(s, "u1", session_id="s2")
    assert [row.tab_id for row in s.tabs_for("s2")] == ["u1"]
    assert s.stats()["opened"] == 3


def test_heartbeat_reports_generation_and_validity():
    s = sync()
    open_tab(s, "t1")
    beat = s.heartbeat("t1", now=LATER)
    assert beat["tab_id"] == "t1"
    assert beat["session_id"] == "s1"
    assert beat["generation"] == 1
    assert beat["valid"] is True

    assert s.publish_session("s1")["generation"] == 2
    stale = s.heartbeat("t1", now=2.0)
    assert stale["generation"] == 2
    assert stale["valid"] is False
    fresh = s.heartbeat("t1", now=3.0)
    assert fresh["generation"] == 2
    assert fresh["valid"] is True
    assert s.stats()["heartbeats"] == 3


def test_heartbeat_ttl_reclaims_tabs_once_past_the_boundary():
    s = sync(heartbeat_ttl=10.0)
    open_tab(s, "t1")
    assert s.expire_tabs(now=10.0) == 0
    assert s.expire_tabs(now=10.5) == 1
    assert s.stats()["expired"] == 1
    assert s.tabs_for("s1") == ()
    with pytest.raises(sts.SessionTabError) as exc:
        s.heartbeat("t1", now=10.5)
    assert exc.value.code == "SESSION_TAB_UNKNOWN"


def test_claim_refresh_elects_one_refresher_and_waiters_share_the_claim():
    s = sync()
    open_tab(s, "t1")
    open_tab(s, "t2")
    open_tab(s, "t3")

    first = s.claim_refresh("s1", "t1", now=NOW)
    assert first.status == "granted"
    assert first.owner is True
    assert first.claim_id == "rf-0001"
    assert first.generation == 1

    second = s.claim_refresh("s1", "t2", now=NOW)
    assert second.status == "wait"
    assert second.owner is False
    assert second.claim_id == "rf-0001"
    assert second.generation == 1

    third = s.claim_refresh("s1", "t3", now=NOW)
    assert third.status == "wait"
    assert third.claim_id == "rf-0001"

    repeat = s.claim_refresh("s1", "t1", now=LATER)
    assert repeat.status == "granted"
    assert repeat.claim_id == "rf-0001"
    assert s.stats()["refreshes"] == 1
    assert s.session_info("s1")["claim"]["tab_id"] == "t1"


def test_complete_refresh_success_advances_generation_exactly_once():
    s = sync()
    open_tab(s, "t1")
    open_tab(s, "t2")
    s.claim_refresh("s1", "t1", now=NOW)

    done = s.complete_refresh("s1", "rf-0001", ok=True, now=LATER)
    assert done["status"] == "ok"
    assert done["claim_id"] == "rf-0001"
    assert done["generation"] == 2
    assert s.session_info("s1")["generation"] == 2
    assert s.session_info("s1")["claim"] is None

    with pytest.raises(sts.SessionTabError) as exc:
        s.complete_refresh("s1", "rf-0001", ok=True, now=LATER)
    assert exc.value.code == "SESSION_CLAIM_UNKNOWN"


def test_complete_refresh_failure_keeps_generation_and_reason():
    s = sync()
    open_tab(s, "t1")
    open_tab(s, "t2")
    s.claim_refresh("s1", "t1", now=NOW)

    failed = s.complete_refresh("s1", "rf-0001", ok=False,
                                reason="token_rejected", now=LATER)
    assert failed["status"] == "failed"
    assert failed["claim_id"] == "rf-0001"
    assert failed["generation"] == 1
    assert failed["reason"] == "token_rejected"
    assert s.session_info("s1")["generation"] == 1
    assert s.session_info("s1")["last_result"]["status"] == "failed"
    assert s.stats()["refresh_failures"] == 1
    assert s.stats()["refreshes"] == 1


def test_publish_session_requires_strictly_increasing_generation():
    s = sync()
    open_tab(s, "t1")

    assert s.publish_session("s1")["generation"] == 2
    for bad in (2, 1, 0):
        with pytest.raises(sts.SessionTabError) as exc:
            s.publish_session("s1", generation=bad)
        assert exc.value.code == "SESSION_GENERATION_REGRESSION"

    assert s.publish_session("s1", generation=5)["generation"] == 5
    assert s.session_info("s1")["generation"] == 5
    beat = s.heartbeat("t1", now=LATER)
    assert beat["generation"] == 5
    assert beat["valid"] is False


def test_broadcast_logout_notifies_tabs_and_ends_the_session():
    s = sync()
    open_tab(s, "t1")
    open_tab(s, "t2")

    assert s.broadcast_logout("s1", reason="admin") == 2
    info = s.session_info("s1")
    assert info["logged_out"] is True
    assert info["generation"] == 2
    assert info["tabs"] == 2
    assert s.broadcast_logout("s1", reason="admin") == 0
    assert s.stats()["logouts"] == 1

    with pytest.raises(sts.SessionTabError) as exc:
        s.heartbeat("t1", now=LATER)
    assert exc.value.code == "SESSION_LOGGED_OUT"
    assert [row.valid for row in s.tabs_for("s1")] == [False, False]


def test_close_tab_is_idempotent_and_frees_the_slot():
    s = sync(max_tabs_per_session=2)
    open_tab(s, "t1")
    open_tab(s, "t2")
    s.claim_refresh("s1", "t1", now=NOW)

    assert s.close_tab("t2", now=LATER) is True
    assert s.close_tab("t2", now=LATER) is False
    assert s.close_tab("missing", now=LATER) is False
    assert [row.tab_id for row in s.tabs_for("s1")] == ["t1"]
    assert s.stats()["closed"] == 1

    open_tab(s, "t3", now=LATER)
    assert sorted(row.tab_id for row in s.tabs_for("s1")) == ["t1", "t3"]
    assert s.session_info("s1")["claim"]["claim_id"] == "rf-0001"


def test_snapshot_is_detached_and_restore_roundtrips_the_round():
    s = sync()
    open_tab(s, "t1")
    open_tab(s, "t2")
    s.claim_refresh("s1", "t1", now=NOW)

    state = s.snapshot()
    pristine = copy.deepcopy(state)
    assert set(pristine) == {"version", "sessions", "stats"}
    assert pristine["version"] == 1
    assert pristine["stats"] == s.stats()

    state["sessions"]["s1"]["tabs"].clear()
    state["stats"]["opened"] = 999
    assert s.stats()["opened"] == 2
    assert len(s.tabs_for("s1")) == 2

    clone = sync()
    clone.restore(pristine)
    assert clone.stats() == s.stats()
    info = clone.session_info("s1")
    assert info["generation"] == 1
    assert info["logged_out"] is False
    assert info["claim"]["claim_id"] == "rf-0001"
    assert [row.tab_id for row in clone.tabs_for("s1")] == ["t1", "t2"]

    resumed = clone.claim_refresh("s1", "t1", now=LATER)
    assert resumed.status == "granted"
    assert resumed.claim_id == "rf-0001"
    assert clone.complete_refresh("s1", "rf-0001", ok=True,
                                  now=LATER)["generation"] == 2


def test_restore_rejects_malformed_snapshot_without_touching_live_state():
    s = sync()
    open_tab(s, "t1")
    s.claim_refresh("s1", "t1", now=NOW)
    good_stats = dict(s.stats())
    bad_states = [
        None,
        {},
        {"version": 2, "sessions": {}, "stats": good_stats},
        {"version": 1, "sessions": [], "stats": good_stats},
        {"version": 1,
         "sessions": {"s1": {"generation": 0, "logged_out": False, "tabs": [],
                             "next_claim": 1, "claim": None,
                             "last_result": None}},
         "stats": good_stats},
        {"version": 1,
         "sessions": {"s1": {"generation": 1, "logged_out": False,
                             "tabs": "nope", "next_claim": 1, "claim": None,
                             "last_result": None}},
         "stats": good_stats},
        {"version": 1, "sessions": {}, "stats": {"opened": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(sts.SessionTabError) as exc:
            s.restore(bad)
        assert exc.value.code == "SESSION_SNAPSHOT_INVALID"

    assert s.stats()["opened"] == 1
    again = s.claim_refresh("s1", "t1", now=LATER)
    assert again.claim_id == "rf-0001"


def test_tab_identity_rules_are_enforced():
    s = sync()
    open_tab(s, "tab-a", session_id="s1")
    open_tab(s, "tab-b", session_id="s2")

    with pytest.raises(sts.SessionTabError) as exc:
        open_tab(s, "tab-a", session_id="s2")
    assert exc.value.code == "SESSION_TAB_TAKEN"

    with pytest.raises(sts.SessionTabError) as exc:
        s.claim_refresh("s2", "tab-a", now=NOW)
    assert exc.value.code == "SESSION_TAB_MISMATCH"

    with pytest.raises(sts.SessionTabError) as exc:
        s.claim_refresh("s9", "tab-a", now=NOW)
    assert exc.value.code == "SESSION_UNKNOWN"

    with pytest.raises(sts.SessionTabError) as exc:
        s.heartbeat("tab-zzz", now=NOW)
    assert exc.value.code == "SESSION_TAB_UNKNOWN"

    for bad_args in (("", "tab-x"), ("s1", "   ")):
        with pytest.raises(sts.SessionTabError) as exc:
            s.open_tab(bad_args[0], bad_args[1], now=NOW)
        assert exc.value.code == "SESSION_INVALID_REQUEST"

    with pytest.raises(sts.SessionTabError) as exc:
        s.open_tab("s1", "tab-c", [7], now=NOW)
    assert exc.value.code == "SESSION_INVALID_REQUEST"
    assert s.stats()["opened"] == 2


def test_stats_track_each_operation_exactly():
    s = sync()
    open_tab(s, "t1", now=0.0)
    open_tab(s, "t1", now=1.0)
    open_tab(s, "t2", now=1.0)
    s.heartbeat("t1", now=2.0)
    s.claim_refresh("s1", "t1", now=2.0)
    s.claim_refresh("s1", "t2", now=2.0)
    s.complete_refresh("s1", "rf-0001", ok=True, now=3.0)
    s.close_tab("t2", now=4.0)
    s.broadcast_logout("s1", now=5.0)

    expected = stats_shape(s)
    expected.update({"opened": 2, "reopened": 1, "closed": 1,
                     "heartbeats": 1, "refreshes": 1, "logouts": 1})
    assert s.stats() == expected
    assert set(s.stats()) == set(expected)


def test_concurrent_claims_produce_exactly_one_refresher():
    s = sync()
    tabs = [f"tab-{index}" for index in range(8)]
    for tab in tabs:
        open_tab(s, tab, now=NOW)

    barrier = threading.Barrier(len(tabs))
    results = []
    errors = []
    lock = threading.Lock()

    def worker(tab):
        try:
            barrier.wait(timeout=5)
            claim = s.claim_refresh("s1", tab, now=LATER)
            with lock:
                results.append((tab, claim.status, claim.owner, claim.claim_id))
        except Exception as exc:  # pragma: no cover - reported through errors
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(tab,)) for tab in tabs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    granted = [row for row in results if row[1] == "granted"]
    waiting = [row for row in results if row[1] == "wait"]
    assert len(granted) == 1
    assert granted[0][2] is True
    assert len(waiting) == len(tabs) - 1
    assert {row[2] for row in waiting} == {False}
    assert {row[3] for row in results} == {"rf-0001"}
    assert s.stats()["refreshes"] == 1
    assert s.session_info("s1")["claim"]["tab_id"] == granted[0][0]
