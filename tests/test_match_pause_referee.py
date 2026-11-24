import copy
import threading

import pytest

import match_pause_referee as pr


def make(**kwargs):
    ref = pr.PauseReferee(**kwargs)
    ref.register_team("red", ["p1", "p2", "p3"])
    ref.register_team("blue", ["q1", "q2"])
    return ref


def pause_active(ref, *, start=1000, duration=5000):
    ref.request_pause("red", player="p1", duration=duration, now=start - 100)
    return ref.vote_pause("pause-1", player="p2", now=start)


def test_register_team_validation_and_conflict():
    ref = pr.PauseReferee()
    out = ref.register_team("red", ["p1", "p2", "p3"])
    assert out == {"team": "red", "players": ["p1", "p2", "p3"],
                   "status": "registered"}
    assert ref.register_team("red", ["p1", "p2", "p3"])["status"] == "duplicate"
    with pytest.raises(pr.PauseError) as exc:
        ref.register_team("red", ["p1", "p2"])
    assert exc.value.code == "PAUSE_TEAM_CONFLICT"
    for team, players in (("", ["p1"]), ("blue", []), ("blue", ["p1", "p1"]),
                          ("blue", "p1")):
        with pytest.raises(pr.PauseError) as exc:
            ref.register_team(team, players)
        assert exc.value.code == "PAUSE_INVALID_TEAM"
    narrow = pr.PauseReferee(max_players=2)
    narrow.register_team("t", ["a", "b"])
    with pytest.raises(pr.PauseError) as exc:
        narrow.register_team("u", ["a", "b", "c"])
    assert exc.value.code == "PAUSE_TEAM_LIMIT"
    assert ref.roster("red") == ["p1", "p2", "p3"]
    with pytest.raises(pr.PauseError) as exc:
        ref.roster("green")
    assert exc.value.code == "PAUSE_UNKNOWN_TEAM"


def test_request_pause_creates_pending_proposal():
    ref = make()
    out = ref.request_pause("red", player="p1", now=1000)
    assert out == {"proposal_id": "pause-1", "team": "red",
                   "status": "pending", "votes": 1, "quorum": 2,
                   "duration": 60000, "quota": 2}
    assert ref.state(now=1000)["state"] == "running"
    assert ref.quota_of("red") == 2
    proposal = ref.proposal_of("red")
    assert proposal == {"proposal_id": "pause-1", "team": "red",
                        "status": "pending", "votes": ["p1"], "quorum": 2,
                        "ends_at": None}
    assert ref.proposal_of("blue") is None


def test_duplicate_team_request_reuses_the_same_proposal():
    ref = make()
    first = ref.request_pause("red", player="p1", now=10)
    again = ref.request_pause("red", player="p2", now=20)
    assert first["proposal_id"] == again["proposal_id"] == "pause-1"
    assert again["status"] == "pending_duplicate"
    assert again["votes"] == 1
    assert ref.stats()["proposals"] == 1
    assert ref.stats()["duplicates"] == 1


def test_request_pause_validation():
    ref = make()
    with pytest.raises(pr.PauseError) as exc:
        ref.request_pause("green", player="p1")
    assert exc.value.code == "PAUSE_UNKNOWN_TEAM"
    with pytest.raises(pr.PauseError) as exc:
        ref.request_pause("red", player="q1")
    assert exc.value.code == "PAUSE_UNKNOWN_PLAYER"
    with pytest.raises(pr.PauseError) as exc:
        ref.request_pause("red", player="p1", duration=0)
    assert exc.value.code == "PAUSE_INVALID_DURATION"
    with pytest.raises(pr.PauseError) as exc:
        ref.request_pause("red", player="p1", duration=True)
    assert exc.value.code == "PAUSE_INVALID_DURATION"
    with pytest.raises(pr.PauseError) as exc:
        ref.request_pause("red", player="p1", duration=60001)
    assert exc.value.code == "PAUSE_DURATION_TOO_LONG"
    out = ref.request_pause("red", player="p1", duration=5000, now=1)
    assert out["duration"] == 5000
    assert out["proposal_id"] == "pause-1"


def test_vote_pause_activates_at_quorum():
    ref = make()
    ref.request_pause("red", player="p1", now=1000)
    out = ref.vote_pause("pause-1", player="p2", now=1200)
    assert out == {"proposal_id": "pause-1", "team": "red",
                   "status": "paused", "votes": 2, "quorum": 2,
                   "ends_at": 61200}
    state = ref.state(now=1200)
    assert state["state"] == "paused"
    assert state["frozen"] is True
    assert state["team"] == "red"
    assert state["ends_at"] == 61200
    assert state["remaining_ms"] == 60000
    assert ref.quota_of("red") == 1
    assert ref.proposal_of("red")["status"] == "active"
    assert ref.stats()["pauses"] == 1
    assert ref.stats()["votes"] == 2


def test_vote_rules_and_closed_proposal():
    ref = make()
    ref.request_pause("red", player="p1", now=1)
    duplicate = ref.vote_pause("pause-1", player="p1", now=2)
    assert duplicate["status"] == "duplicate_vote"
    assert duplicate["votes"] == 1
    with pytest.raises(pr.PauseError) as exc:
        ref.vote_pause("pause-1", player="q1", now=3)
    assert exc.value.code == "PAUSE_VOTE_NOT_ALLOWED"
    with pytest.raises(pr.PauseError) as exc:
        ref.vote_pause("pause-9", player="p2", now=4)
    assert exc.value.code == "PAUSE_UNKNOWN_PROPOSAL"
    ref.vote_pause("pause-1", player="p2", now=100)
    already = ref.vote_pause("pause-1", player="p3", now=110)
    assert already["status"] == "already_active"
    assert already["ends_at"] == 60100
    ref.resume("pause-1", now=200)
    with pytest.raises(pr.PauseError) as exc:
        ref.vote_pause("pause-1", player="p3", now=300)
    assert exc.value.code == "PAUSE_PROPOSAL_CLOSED"


def test_cannot_request_pause_while_paused():
    ref = make()
    pause_active(ref)
    for team, player in (("blue", "q1"), ("red", "p3")):
        with pytest.raises(pr.PauseError) as exc:
            ref.request_pause(team, player=player, now=1500)
        assert exc.value.code == "PAUSE_ALREADY_PAUSED"
    assert ref.stats()["rejected"] == 2


def test_resume_releases_pause_and_keeps_quota_spent():
    ref = make()
    pause_active(ref)
    out = ref.resume("pause-1", now=5000)
    assert out == {"proposal_id": "pause-1", "team": "red",
                   "status": "resumed", "state": "running", "quota": 1}
    state = ref.state(now=5000)
    assert state["state"] == "running"
    assert state["frozen"] is False
    assert state["remaining_ms"] == 0
    assert ref.proposal_of("red") is None
    assert ref.quota_of("red") == 1
    assert ref.stats()["resumes"] == 1


def test_resume_errors():
    ref = make()
    with pytest.raises(pr.PauseError) as exc:
        ref.resume("pause-9", now=1)
    assert exc.value.code == "PAUSE_UNKNOWN_PROPOSAL"
    ref.request_pause("red", player="p1", now=10)
    with pytest.raises(pr.PauseError) as exc:
        ref.resume("pause-1", now=20)
    assert exc.value.code == "PAUSE_NOT_PAUSED"
    ref.vote_pause("pause-1", player="p2", now=30)
    ref.resume("pause-1", now=40)
    with pytest.raises(pr.PauseError) as exc:
        ref.resume("pause-1", now=50)
    assert exc.value.code == "PAUSE_NOT_PAUSED"


def test_timeout_auto_resumes_and_keeps_quota_spent():
    ref = make()
    pause_active(ref, start=1000, duration=5000)
    early = ref.state(now=5999)
    assert early["state"] == "paused"
    assert early["remaining_ms"] == 1
    late = ref.state(now=6000)
    assert late["state"] == "running"
    assert late["last_event"] == "auto_resumed"
    assert late["proposal_id"] is None
    assert ref.stats()["auto_resumes"] == 1
    assert ref.quota_of("red") == 1
    assert ref.proposal_of("red") is None


def test_quota_exhaustion_is_per_team():
    ref = make(quota_per_match=1)
    pause_active(ref)
    ref.resume("pause-1", now=3000)
    assert ref.quota_of("red") == 0
    with pytest.raises(pr.PauseError) as exc:
        ref.request_pause("red", player="p2", now=4000)
    assert exc.value.code == "PAUSE_QUOTA_EXHAUSTED"
    assert ref.quota_of("blue") == 1
    assert ref.request_pause("blue", player="q1", now=4000)["status"] == "pending"


def test_substitution_applies_immediately_while_running():
    ref = make()
    out = ref.request_substitution("red", out_player="p2", in_player="p9", now=10)
    assert out == {"team": "red", "status": "applied",
                   "roster": ["p1", "p9", "p3"], "queue": 0}
    assert ref.roster("red") == ["p1", "p9", "p3"]
    assert ref.stats()["substitutions"] == 1
    with pytest.raises(pr.PauseError) as exc:
        ref.request_substitution("red", out_player="p2", in_player="p8")
    assert exc.value.code == "PAUSE_UNKNOWN_PLAYER"
    with pytest.raises(pr.PauseError) as exc:
        ref.request_substitution("red", out_player="p9", in_player="p1")
    assert exc.value.code == "PAUSE_ROSTER_CONFLICT"


def test_force_resume_requires_paused_state():
    ref = make()
    with pytest.raises(pr.PauseError) as exc:
        ref.force_resume(reason="admin", now=1)
    assert exc.value.code == "PAUSE_NOT_PAUSED"
    pause_active(ref)
    out = ref.force_resume(reason="admin", now=2000)
    assert out == {"proposal_id": "pause-1", "team": "red",
                   "status": "forced", "state": "running", "quota": 1}
    assert ref.stats()["forced_resumes"] == 1


def test_snapshot_restore_preserves_pause_deadline_and_quota():
    ref = make()
    pause_active(ref, start=1200, duration=60000)
    snap = ref.snapshot()
    assert snap["version"] == 1
    pristine = copy.deepcopy(snap)
    snap["teams"]["red"].append("intruder")
    assert ref.roster("red") == ["p1", "p2", "p3"]

    restored = pr.PauseReferee()
    restored.restore(pristine)
    state = restored.state(now=1200)
    assert state["state"] == "paused"
    assert state["ends_at"] == 61200
    assert state["remaining_ms"] == 60000
    assert restored.quota_of("red") == 1
    assert restored.stats() == pristine["stats"]
    assert restored.resume("pause-1", now=3000)["status"] == "resumed"
    assert restored.state(now=3000)["state"] == "running"

    broken = [None, {}, {"version": 2}, {"version": 1, "teams": []},
              {"version": 1, "teams": {}, "proposals": {}, "proposal_of_team": {},
               "quota": {}, "stats": {}, "queue": [], "seq": 0,
               "state": "paused", "active": None}]
    for bad in broken:
        with pytest.raises(pr.PauseError) as exc:
            restored.restore(bad)
        assert exc.value.code == "PAUSE_SNAPSHOT_INVALID"
    assert restored.state(now=3000)["state"] == "running"


def test_concurrent_requests_merge_into_one_proposal():
    ref = pr.PauseReferee()
    players = [f"p{i}" for i in range(1, 7)]
    ref.register_team("red", players)
    outcomes = []
    ids = []
    lock = threading.Lock()

    def worker(player):
        try:
            out = ref.request_pause("red", player=player, now=100)
            tag, pid = out["status"], out["proposal_id"]
        except Exception as exc:  # pragma: no cover - surfaced as a failure
            tag, pid = repr(exc), None
        with lock:
            outcomes.append(tag)
            ids.append(pid)

    threads = [threading.Thread(target=worker, args=(player,)) for player in players]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("pending") == 1
    assert outcomes.count("pending_duplicate") == len(players) - 1
    assert len(set(ids)) == 1
    assert ref.stats()["proposals"] == 1
