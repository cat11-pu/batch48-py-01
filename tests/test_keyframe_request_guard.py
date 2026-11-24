import threading

import pytest

import keyframe_request_guard as krg


def guard(**kwargs):
    return krg.KeyframeRequestGuard(**kwargs)


def live(guard, stream_id="s1", subscribers=("alice",)):
    for name in subscribers:
        guard.add_subscriber(stream_id, name)
    guard.publish_stream(stream_id)
    return guard


def test_add_subscriber_creates_unpublished_stream():
    g = guard()
    out = g.add_subscriber("s1", "alice")
    assert out == {"stream_id": "s1", "subscriber_id": "alice", "subscribers": 1}
    info = g.stream_info("s1")
    assert info["published"] is False
    assert info["subscribers"] == 1
    assert info["pending"] is False
    assert info["requests"] == 0
    assert g.budget_remaining("s1", "alice", now_ms=0.0) == 3
    assert g.stats()["subscribers_added"] == 1


def test_duplicate_and_over_limit_subscribers_raise():
    g = guard()
    g.add_subscriber("s1", "alice")
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.add_subscriber("s1", "alice")
    assert exc.value.code == "KF_DUPLICATE_SUBSCRIBER"
    limited = guard(max_subscribers=1)
    limited.add_subscriber("s1", "a")
    with pytest.raises(krg.KeyframeRequestError) as exc:
        limited.add_subscriber("s1", "b")
    assert exc.value.code == "KF_SUBSCRIBER_LIMIT"


def test_unknown_stream_and_subscriber_raise():
    g = guard()
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.request_keyframe("ghost", "alice", now_ms=0.0)
    assert exc.value.code == "KF_UNKNOWN_STREAM"
    g.add_subscriber("s1", "alice")
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.request_keyframe("s1", "mallory", now_ms=0.0)
    assert exc.value.code == "KF_UNKNOWN_SUBSCRIBER"
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.remove_subscriber("s1", "mallory")
    assert exc.value.code == "KF_UNKNOWN_SUBSCRIBER"


def test_published_stream_sends_first_request():
    g = guard()
    g.add_subscriber("s1", "alice")
    out = g.publish_stream("s1")
    assert out == {"stream_id": "s1", "published": True}
    sent = g.request_keyframe("s1", "alice", now_ms=0.0)
    assert sent == {"action": "sent", "request_id": 1, "attempt": 1}
    pend = g.pending("s1")
    assert pend["request_id"] == 1
    assert pend["attempt"] == 1
    assert pend["waiters"] == 1
    assert g.stream_info("s1")["requests"] == 1
    assert g.stats()["requests_sent"] == 1


def test_second_subscriber_request_is_coalesced():
    g = live(guard(), subscribers=("alice", "bob"))
    g.request_keyframe("s1", "alice", now_ms=0.0)
    out = g.request_keyframe("s1", "bob", now_ms=100.0)
    assert out == {"action": "coalesced", "request_id": 1, "attempt": 1}
    assert g.pending("s1")["waiters"] == 2
    assert g.stats()["coalesced"] == 1
    assert g.stats()["requests_sent"] == 1


def test_request_inside_min_interval_is_throttled():
    g = live(guard())
    g.request_keyframe("s1", "alice", now_ms=0.0)
    g.on_keyframe("s1", now_ms=10.0)
    out = g.request_keyframe("s1", "alice", now_ms=100.0)
    assert out == {"action": "throttled", "request_id": None, "attempt": 0}
    assert g.budget_remaining("s1", "alice", now_ms=100.0) == 2
    assert g.stats()["throttled"] == 1


def test_budget_exhaustion_denies_request():
    g = live(guard(budget=2))
    g.request_keyframe("s1", "alice", now_ms=0.0)
    g.on_keyframe("s1", now_ms=10.0)
    second = g.request_keyframe("s1", "alice", now_ms=600.0)
    assert second["request_id"] == 2
    g.on_keyframe("s1", now_ms=610.0)
    out = g.request_keyframe("s1", "alice", now_ms=1200.0)
    assert out == {"action": "denied", "request_id": None, "attempt": 0}
    assert g.budget_remaining("s1", "alice", now_ms=1200.0) == 0
    assert g.stats()["denied"] == 1
    assert g.stats()["requests_sent"] == 2


def test_budget_window_rolls_over():
    g = live(guard(budget=2))
    g.request_keyframe("s1", "alice", now_ms=0.0)
    g.on_keyframe("s1", now_ms=10.0)
    g.request_keyframe("s1", "alice", now_ms=600.0)
    g.on_keyframe("s1", now_ms=610.0)
    denied = g.request_keyframe("s1", "alice", now_ms=1200.0)
    assert denied["action"] == "denied"
    out = g.request_keyframe("s1", "alice", now_ms=10000.0)
    assert out["action"] == "sent"
    assert out["request_id"] == 3
    assert g.budget_remaining("s1", "alice", now_ms=10000.0) == 1


def test_retry_schedule_doubles_backoff():
    g = live(guard())
    g.request_keyframe("s1", "alice", now_ms=0.0)
    assert g.next_retry_ms("s1") == pytest.approx(1000.0)
    assert g.due_retries(999.0) == []
    due = g.due_retries(1000.0)
    assert len(due) == 1
    assert due[0]["request_id"] == 1
    assert due[0]["attempt"] == 2
    assert g.next_retry_ms("s1") == pytest.approx(3000.0)
    assert g.stats()["retries"] == 1


def test_retries_stop_after_max_retries():
    g = live(guard())
    g.request_keyframe("s1", "alice", now_ms=0.0)
    g.due_retries(1000.0)
    third = g.due_retries(3000.0)
    assert third[0]["attempt"] == 3
    assert g.next_retry_ms("s1") is None
    assert g.due_retries(99999.0) == []
    assert g.stats()["retries"] == 2
    assert g.stats()["retries_exhausted"] == 1


def test_keyframe_clears_pending_waiters():
    g = live(guard(), subscribers=("alice", "bob"))
    g.request_keyframe("s1", "alice", now_ms=0.0)
    g.request_keyframe("s1", "bob", now_ms=50.0)
    out = g.on_keyframe("s1", now_ms=100.0)
    assert out == {"cleared": True, "waiters": 2}
    assert g.stats()["keyframes"] == 1


def test_publish_is_idempotent_and_reports_state():
    g = guard()
    g.add_subscriber("s1", "alice")
    assert g.stream_info("s1")["published"] is False
    first = g.publish_stream("s1")
    second = g.publish_stream("s1")
    assert first == {"stream_id": "s1", "published": True}
    assert second == {"stream_id": "s1", "published": True}
    assert g.stream_info("s1")["published"] is True
    assert g.stats()["streams_published"] == 1
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.publish_stream("ghost")
    assert exc.value.code == "KF_UNKNOWN_STREAM"


def test_remove_subscriber_reports_remaining():
    g = guard()
    g.add_subscriber("s1", "alice")
    g.add_subscriber("s1", "bob")
    out = g.remove_subscriber("s1", "alice")
    assert out == {
        "stream_id": "s1", "subscriber_id": "alice",
        "removed": True, "subscribers": 1,
    }
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.remove_subscriber("s1", "alice")
    assert exc.value.code == "KF_UNKNOWN_SUBSCRIBER"
    assert g.stats()["subscribers_removed"] == 1


def test_snapshot_restore_and_corrupt_state():
    g = live(guard())
    g.request_keyframe("s1", "alice", now_ms=0.0)
    probe = g.snapshot()
    assert {"streams", "next_request_id"} <= set(probe)
    probe["streams"]["s1"]["pending"]["attempt"] = 9
    assert g.pending("s1")["attempt"] == 1
    snap = g.snapshot()
    g.on_keyframe("s1", now_ms=100.0)
    g.restore(snap)
    assert g.pending("s1")["attempt"] == 1
    assert g.next_retry_ms("s1") == pytest.approx(1000.0)
    with pytest.raises(krg.KeyframeRequestError) as exc:
        g.restore({"streams": []})
    assert exc.value.code == "KF_SNAPSHOT_INVALID"
    assert g.pending("s1")["attempt"] == 1


def test_concurrent_requests_are_serialised():
    g = guard(budget=10)
    barrier = threading.Barrier(4)
    ids = []
    failures = []

    def worker(tid):
        try:
            barrier.wait()
            name = "s%d" % tid
            g.add_subscriber(name, "sub")
            g.publish_stream(name)
            for step in range(4):
                now = step * 600.0
                out = g.request_keyframe(name, "sub", now_ms=now)
                ids.append(out["request_id"])
                g.on_keyframe(name, now_ms=now + 10.0)
        except Exception as exc:  # pragma: no cover - surfaced after join
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert sorted(ids) == list(range(1, 17))
    assert g.stats()["requests_sent"] == 16
    assert g.stats()["streams_published"] == 4
