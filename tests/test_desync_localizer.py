import copy
import threading

import pytest

import desync_localizer as dl

MATCH = "default"


def make(**kwargs):
    loc = dl.DesyncLocalizer(**kwargs)
    loc.register_peer("a", endpoint="udp-a")
    loc.register_peer("b", endpoint="udp-b")
    return loc


def report(loc, peer, frame, digest, match=MATCH):
    return loc.report_frame(peer, match_id=match, frame=frame, digest=digest)


def pair(loc, frame, left_digest, right_digest, match=MATCH):
    report(loc, "a", frame, left_digest, match)
    report(loc, "b", frame, right_digest, match)


def test_register_peer_duplicate_conflict_and_limit():
    loc = make()
    assert loc.register_peer("a", endpoint="udp-a")["status"] == "duplicate"
    with pytest.raises(dl.DesyncError) as exc:
        loc.register_peer("a", endpoint="other")
    assert exc.value.code == "DESYNC_PEER_CONFLICT"
    assert loc.peer_state("a") == "active"
    assert loc.peer_state("ghost") == "unknown"
    with pytest.raises(dl.DesyncError) as exc:
        loc.register_peer("")
    assert exc.value.code == "DESYNC_INVALID_PEER"
    narrow = dl.DesyncLocalizer(peer_limit=2)
    narrow.register_peer("p1")
    narrow.register_peer("p2")
    with pytest.raises(dl.DesyncError) as exc:
        narrow.register_peer("p3")
    assert exc.value.code == "DESYNC_PEER_LIMIT"
    assert narrow.stats()["rejected"] == 1


def test_report_frame_canonicalizes_digest_by_entity_order():
    loc = make()
    out = report(loc, "a", 1, {"pos": 12, "hp": 100})
    assert out == {"peer": "a", "match_id": MATCH, "frame": 1,
                   "digest": "hp=100|pos=12", "frames": 1,
                   "status": "recorded"}
    again = report(loc, "a", 2, {"hp": 100, "pos": 12})
    assert again["digest"] == "hp=100|pos=12"
    assert again["frames"] == 2
    literal = report(loc, "a", 3, "hp=100|pos=12")
    assert literal["digest"] == "hp=100|pos=12"
    assert loc.stats()["reports"] == 3
    assert loc.stats()["duplicates"] == 0


def test_report_frame_rejects_malformed_digests():
    loc = make()
    bad = [{}, {"hp": None}, {"hp": {"nested": 1}}, "", {"": 1}, {"hp": [1]}]
    for digest in bad:
        with pytest.raises(dl.DesyncError) as exc:
            report(loc, "a", 1, digest)
        assert exc.value.code == "DESYNC_BAD_DIGEST"
    assert loc.stats()["rejected"] == len(bad)
    assert loc.stats()["reports"] == 0
    assert report(loc, "a", 1, {"hp": 1})["frames"] == 1
    with pytest.raises(dl.DesyncError) as exc:
        loc.report_frame("a", match_id=MATCH, frame=-1, digest={"hp": 1})
    assert exc.value.code == "DESYNC_INVALID_FRAME"


def test_report_frame_duplicate_and_conflict():
    loc = make()
    report(loc, "a", 4, {"hp": 100})
    duplicate = report(loc, "a", 4, {"hp": 100})
    assert duplicate["status"] == "duplicate"
    assert duplicate["frames"] == 1
    with pytest.raises(dl.DesyncError) as exc:
        report(loc, "a", 4, {"hp": 90})
    assert exc.value.code == "DESYNC_FRAME_CONFLICT"
    assert loc.stats()["reports"] == 1
    assert loc.stats()["duplicates"] == 1
    assert loc.stats()["conflicts"] == 1
    assert loc.evidence(match_id=MATCH)["count"] == 1


def test_report_and_compare_require_active_registered_peers():
    loc = make()
    with pytest.raises(dl.DesyncError) as exc:
        report(loc, "ghost", 1, {"hp": 1})
    assert exc.value.code == "DESYNC_UNKNOWN_PEER"
    out = loc.quarantine_peer("a", reason="packet loss")
    assert out == {"peer": "a", "state": "quarantined",
                   "reason": "packet loss", "status": "quarantined"}
    assert loc.quarantine_peer("a")["status"] == "already_quarantined"
    assert loc.peer_state("a") == "quarantined"
    with pytest.raises(dl.DesyncError) as exc:
        report(loc, "a", 1, {"hp": 1})
    assert exc.value.code == "DESYNC_PEER_QUARANTINED"
    with pytest.raises(dl.DesyncError) as exc:
        loc.compare(frame=1, left="a", right="b")
    assert exc.value.code == "DESYNC_PEER_QUARANTINED"


def test_compare_match_and_divergence_details():
    loc = make()
    pair(loc, 5, {"hp": 100, "pos": 12}, {"hp": 100, "pos": 12})
    same = loc.compare(frame=5, left="a", right="b")
    assert same["status"] == "match"
    assert same["entities"] == []
    assert same["details"] == {}
    assert same["reason"] == ""
    pair(loc, 6, {"hp": 100, "pos": 12}, {"hp": 90, "pos": 12})
    out = loc.compare(frame=6, left="a", right="b")
    assert out["status"] == "diverged"
    assert out["entities"] == ["hp"]
    assert out["details"] == {"hp": {"left": "100", "right": "90"}}
    report(loc, "a", 7, {"hp": 1, "mana": 5})
    report(loc, "b", 7, {"hp": 1})
    extra = loc.compare(frame=7, left="a", right="b")
    assert extra["entities"] == ["mana"]
    assert extra["details"] == {"mana": {"left": "5", "right": None}}
    assert loc.stats()["matched"] == 1
    assert loc.stats()["diverged"] == 2


def test_compare_reports_insufficient_for_missing_side():
    loc = make()
    report(loc, "a", 9, {"hp": 1})
    out = loc.compare(frame=9, left="a", right="b")
    assert out["status"] == "insufficient"
    assert out["reason"] == "missing_right"
    assert out["entities"] == []
    flipped = loc.compare(frame=9, left="b", right="a")
    assert flipped["reason"] == "missing_left"
    assert loc.stats()["insufficient"] == 2


def test_compare_rejects_same_or_unknown_peer():
    loc = make()
    with pytest.raises(dl.DesyncError) as exc:
        loc.compare(frame=1, left="a", right="a")
    assert exc.value.code == "DESYNC_SAME_PEER"
    with pytest.raises(dl.DesyncError) as exc:
        loc.compare(frame=1, left="a", right="ghost")
    assert exc.value.code == "DESYNC_UNKNOWN_PEER"
    assert loc.stats()["rejected"] == 0


def test_first_divergence_returns_earliest_frame_then_none():
    loc = make()
    for frame in (1, 2, 3):
        pair(loc, frame, {"hp": 100}, {"hp": 100})
    for frame in (4, 5, 6):
        pair(loc, frame, {"hp": 100}, {"hp": 90})
    out = loc.first_divergence(left="a", right="b", start=0, end=10, match_id=MATCH)
    assert out["frame"] == 4
    assert out["status"] == "found"
    assert out["scanned"] == 4
    assert out["entities"] == ["hp"]

    clean = make()
    for frame in (1, 2, 3):
        pair(clean, frame, {"hp": 100}, {"hp": 100})
    none = clean.first_divergence(left="a", right="b", match_id=MATCH)
    assert none["frame"] is None
    assert none["status"] == "none"
    assert none["scanned"] == 3


def test_bisect_range_locates_first_divergent_frame():
    loc = make()
    for frame in range(1, 9):
        left = {"hp": 100} if frame < 5 else {"hp": 70}
        pair(loc, frame, left, {"hp": 100})
    out = loc.bisect_range(left="a", right="b", start=1, end=8, match_id=MATCH)
    assert out["frame"] == 5
    assert out["status"] == "found"
    assert out["entities"] == ["hp"]
    assert out["probes"] <= 7


def test_bisect_range_short_circuits_when_start_diverges():
    loc = make()
    for frame in (3, 4, 5):
        pair(loc, frame, {"hp": 10}, {"hp": 20})
    out = loc.bisect_range(left="a", right="b", start=3, end=5, match_id=MATCH)
    assert out["frame"] == 3
    assert out["probes"] == 1
    assert out["entities"] == ["hp"]


def test_frame_limit_and_clear_match():
    loc = make(frame_limit=3)
    for frame in (1, 2, 3):
        report(loc, "a", frame, {"hp": 100})
    with pytest.raises(dl.DesyncError) as exc:
        report(loc, "a", 4, {"hp": 100})
    assert exc.value.code == "DESYNC_FRAME_LIMIT"
    assert loc.stats()["rejected"] == 1
    assert loc.clear_match(MATCH) == 3
    assert loc.compare(frame=1, left="a", right="b")["status"] == "insufficient"
    assert report(loc, "a", 1, {"hp": 100})["status"] == "recorded"
    assert loc.evidence(match_id=MATCH)["count"] == 1


def test_evidence_keeps_newest_entries_under_capacity():
    loc = make(evidence_limit=3)
    for frame in range(1, 6):
        report(loc, "a", frame, {"hp": frame})
    out = loc.evidence(match_id=MATCH)
    assert out["count"] == 3
    assert [entry["frame"] for entry in out["entries"]] == [3, 4, 5]
    assert out["dropped"] == 2
    tail = loc.evidence(match_id=MATCH, limit=2)
    assert [entry["frame"] for entry in tail["entries"]] == [4, 5]
    assert loc.stats()["evidence_dropped"] == 2


def test_snapshot_restore_preserves_digests_and_rejects_corruption():
    loc = make()
    pair(loc, 1, {"hp": 100}, {"hp": 100})
    pair(loc, 2, {"hp": 100}, {"hp": 80})
    snap = loc.snapshot()
    assert snap["version"] == 1
    pristine = copy.deepcopy(snap)
    snap["digests"].clear()
    assert loc.compare(frame=2, left="a", right="b")["status"] == "diverged"

    restored = dl.DesyncLocalizer()
    restored.restore(pristine)
    assert restored.stats() == pristine["stats"]
    assert restored.compare(frame=1, left="a", right="b")["status"] == "match"
    assert restored.compare(frame=2, left="a", right="b")["status"] == "diverged"
    assert restored.peer_state("a") == "active"
    assert restored.evidence(match_id=MATCH)["count"] == 4

    broken = [None, {}, {"version": 2},
              {"version": 1, "peers": [], "digests": {}, "evidence": [],
               "stats": {}}]
    for bad in broken:
        with pytest.raises(dl.DesyncError) as exc:
            restored.restore(bad)
        assert exc.value.code == "DESYNC_SNAPSHOT_INVALID"
    assert restored.compare(frame=2, left="a", right="b")["status"] == "diverged"


def test_concurrent_identical_reports_are_recorded_once():
    loc = make()
    outcomes = []
    lock = threading.Lock()

    def worker():
        try:
            tag = report(loc, "a", 11, {"hp": 100})["status"]
        except Exception as exc:  # pragma: no cover - surfaced as a failure
            tag = repr(exc)
        with lock:
            outcomes.append(tag)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("recorded") == 1
    assert outcomes.count("duplicate") == 7
    assert loc.stats()["reports"] == 1
    assert loc.stats()["duplicates"] == 7
    assert loc.evidence(match_id=MATCH)["count"] == 1
