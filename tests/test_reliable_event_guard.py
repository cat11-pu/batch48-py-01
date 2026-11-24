import copy
import threading

import pytest

import reliable_event_guard as reg


def make_guard(**kwargs):
    guard = reg.ReliableEventGuard(**kwargs)
    guard.register_channel("loot", reliable=True)
    guard.register_channel("hud", reliable=False)
    return guard


def test_register_channel_reports_the_declared_class():
    guard = reg.ReliableEventGuard()
    assert guard.register_channel("loot", reliable=True) == {
        "channel": "loot",
        "reliable": True,
        "mergeable": False,
    }
    assert guard.register_channel("hud", reliable=False, mergeable=True) == {
        "channel": "hud",
        "reliable": False,
        "mergeable": True,
    }
    with pytest.raises(reg.EventError) as exc:
        guard.register_channel("loot", reliable=False)
    assert exc.value.code == "EVENT_DUPLICATE_CHANNEL"
    with pytest.raises(reg.EventError) as exc:
        guard.register_channel("", reliable=True)
    assert exc.value.code == "EVENT_INVALID_EVENT"
    with pytest.raises(reg.EventError) as exc:
        guard.publish("missing", "e1")
    assert exc.value.code == "EVENT_UNKNOWN_CHANNEL"
    with pytest.raises(reg.EventError) as exc:
        guard.ack("missing", 1)
    assert exc.value.code == "EVENT_UNKNOWN_CHANNEL"


def test_publish_reliable_assigns_monotonic_sequences():
    guard = make_guard()
    assert guard.publish("loot", "e1", {"item": "sword"}, tick=0) == {
        "channel": "loot",
        "event_id": "e1",
        "seq": 1,
        "status": "queued",
    }
    assert guard.publish("loot", "e2", tick=0)["seq"] == 2
    assert guard.publish("loot", "e3", tick=0)["seq"] == 3
    assert guard.pending("loot") == [1, 2, 3]
    assert guard.pending_count() == 3
    assert guard.next_seq("loot") == 4
    assert guard.watermark("loot") == 0
    assert guard.pending("hud") == []
    assert guard.next_seq("hud") == 1
    assert guard.watermark("hud") == 0
    assert guard.channels()["hud"] == {"reliable": False, "mergeable": False}


def test_unreliable_events_are_sent_once_and_never_queued():
    guard = make_guard()
    first = guard.publish("hud", "float-1", {"text": "+10"}, tick=0)
    assert first == {
        "channel": "hud",
        "event_id": "float-1",
        "seq": None,
        "status": "fire_and_forget",
    }
    assert guard.publish("hud", "float-2", tick=0)["seq"] is None
    assert guard.pending("hud") == []
    assert guard.pending_count() == 0
    traffic = guard.collect(4)
    assert traffic["resend"] == []
    assert traffic["bandwidth_used"] == 0
    assert guard.stats()["sent_once"] == 2
    assert guard.stats()["published"] == 2


def test_same_idempotency_key_is_delivered_once():
    guard = make_guard()
    assert guard.publish("loot", "kill-7", tick=0)["status"] == "queued"
    again = guard.publish("loot", "kill-7", tick=0)
    assert again == {
        "channel": "loot",
        "event_id": "kill-7",
        "seq": 1,
        "status": "duplicate",
    }
    assert guard.pending("loot") == [1]
    guard.ack("loot", 1)
    late = guard.publish("loot", "kill-7", tick=1)
    assert late["status"] == "duplicate"
    assert late["seq"] == 1
    assert guard.pending("loot") == []
    assert guard.stats()["duplicates"] == 2


def test_mergeable_channel_collapses_pending_events():
    guard = reg.ReliableEventGuard()
    guard.register_channel("progress", reliable=True, mergeable=True)
    assert guard.publish("progress", "quest-3", {"step": 1}, tick=0)["seq"] == 1
    assert guard.publish("progress", "quest-4", {"step": 1}, tick=0)["seq"] == 2
    merged = guard.publish("progress", "quest-3", {"step": 2}, tick=2)
    assert merged == {
        "channel": "progress",
        "event_id": "quest-3",
        "seq": 1,
        "status": "merged",
    }
    assert guard.pending("progress") == [1, 2]
    assert guard.stats()["merged"] == 1
    assert guard.stats()["published"] == 2


def test_acknowledgement_is_cumulative_and_watermark_never_regresses():
    guard = make_guard()
    for name in ("e1", "e2", "e3", "e4"):
        guard.publish("loot", name, tick=0)
    first = guard.ack("loot", 2)
    assert first == {"channel": "loot", "acked": [1, 2], "watermark": 2}
    assert guard.pending("loot") == [3, 4]
    assert guard.watermark("loot") == 2
    replay = guard.ack("loot", 1)
    assert replay == {"channel": "loot", "acked": [], "watermark": 2}
    assert guard.pending("loot") == [3, 4]
    assert guard.stats()["acked"] == 2


def test_ack_beyond_the_queue_clears_pending_and_clamps_watermark():
    guard = make_guard()
    for name in ("e1", "e2", "e3"):
        guard.publish("loot", name, tick=0)
    assert guard.ack("loot", 99) == {
        "channel": "loot",
        "acked": [1, 2, 3],
        "watermark": 3,
    }
    assert guard.pending("loot") == []
    assert guard.ack("loot", 99) == {"channel": "loot", "acked": [], "watermark": 3}
    with pytest.raises(reg.EventError) as exc:
        guard.ack("loot", -1)
    assert exc.value.code == "EVENT_INVALID_ARGUMENT"
    with pytest.raises(reg.EventError) as exc:
        guard.ack("hud", 1)
    assert exc.value.code == "EVENT_UNRELIABLE_CHANNEL"


def test_retransmission_waits_for_backoff_and_stops_at_the_limit():
    guard = reg.ReliableEventGuard(max_retransmits=2, backoff_ticks=2,
                                   bandwidth_quota=8)
    guard.register_channel("loot", reliable=True)
    guard.publish("loot", "e1", tick=0, ttl_ticks=100)
    assert guard.collect(1)["resend"] == []
    assert guard.collect(2)["resend"] == [1]
    assert guard.collect(4)["resend"] == [1]
    assert guard.collect(6)["resend"] == []
    assert guard.collect(8)["resend"] == []
    assert guard.stats()["retransmits"] == 2


def test_ttl_expiry_is_reported_once_and_never_resent():
    guard = reg.ReliableEventGuard(max_retransmits=5, backoff_ticks=1,
                                   bandwidth_quota=4)
    guard.register_channel("loot", reliable=True)
    guard.publish("loot", "e1", tick=0, ttl_ticks=3)
    assert guard.collect(1)["resend"] == [1]
    assert guard.collect(2)["resend"] == [1]
    third = guard.collect(3)
    assert third["expired"] == [1]
    assert third["resend"] == []
    assert third["bandwidth_used"] == 0
    later = guard.collect(5)
    assert later["expired"] == []
    assert later["resend"] == []
    assert guard.pending("loot") == []
    assert guard.stats()["expired"] == 1
    assert guard.stats()["retransmits"] == 2


def test_bandwidth_quota_defers_candidates_without_spending_attempts():
    guard = reg.ReliableEventGuard(max_retransmits=4, backoff_ticks=1,
                                   bandwidth_quota=1)
    guard.register_channel("loot", reliable=True)
    guard.publish("loot", "e1", tick=0, ttl_ticks=50)
    guard.publish("loot", "e2", tick=0, ttl_ticks=50)
    first = guard.collect(1)
    assert first["resend"] == [1]
    assert first["bandwidth_used"] == 1
    second = guard.collect(2)
    assert second["resend"] == [2]
    assert second["bandwidth_used"] == 1
    third = guard.collect(3)
    assert third["resend"] == [1]
    assert guard.stats()["retransmits"] == 3


def test_acknowledgement_cancels_pending_retransmission():
    guard = reg.ReliableEventGuard(max_retransmits=3, backoff_ticks=1,
                                   bandwidth_quota=2)
    guard.register_channel("loot", reliable=True)
    guard.publish("loot", "e1", tick=0, ttl_ticks=10)
    assert guard.collect(1)["resend"] == [1]
    assert guard.ack("loot", 1)["acked"] == [1]
    quiet = guard.collect(2)
    assert quiet["resend"] == []
    assert quiet["expired"] == []
    assert quiet["dead"] == []
    assert guard.pending("loot") == []
    assert guard.stats()["retransmits"] == 1


def test_invalid_tick_ttl_and_event_names_are_refused():
    guard = make_guard()
    with pytest.raises(reg.EventError) as exc:
        guard.collect(-1)
    assert exc.value.code == "EVENT_INVALID_ARGUMENT"
    with pytest.raises(reg.EventError) as exc:
        guard.publish("loot", "e1", tick=0, ttl_ticks=0)
    assert exc.value.code == "EVENT_INVALID_ARGUMENT"
    with pytest.raises(reg.EventError) as exc:
        guard.publish("loot", "", tick=0)
    assert exc.value.code == "EVENT_INVALID_EVENT"
    assert guard.pending("loot") == []
    assert guard.stats()["published"] == 0


def test_stats_cover_every_outcome_and_reset_clears_counters():
    guard = make_guard(max_retransmits=3, backoff_ticks=1, bandwidth_quota=4)
    guard.publish("loot", "e1", tick=0, ttl_ticks=20)
    guard.publish("loot", "e2", tick=0, ttl_ticks=20)
    guard.publish("loot", "e1", tick=0)
    guard.publish("hud", "h1", tick=0)
    guard.ack("loot", 1)
    guard.collect(1)
    assert guard.stats() == {
        "published": 3,
        "sent_once": 1,
        "merged": 0,
        "duplicates": 1,
        "acked": 1,
        "retransmits": 1,
        "expired": 0,
        "dead_letters": 0,
        "collects": 1,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "published": 0,
        "sent_once": 0,
        "merged": 0,
        "duplicates": 0,
        "acked": 0,
        "retransmits": 0,
        "expired": 0,
        "dead_letters": 0,
        "collects": 0,
    }
    assert guard.pending("loot") == [2]
    assert guard.next_seq("loot") == 3
    assert guard.watermark("loot") == 1


def test_snapshot_is_isolated_from_live_state_and_restorable():
    guard = make_guard(backoff_ticks=1)
    guard.publish("loot", "e1", {"n": 1}, tick=0, ttl_ticks=9)
    guard.publish("loot", "e2", tick=0, ttl_ticks=9)
    guard.ack("loot", 1)
    state = guard.snapshot()
    assert state["version"] == 1
    frozen = copy.deepcopy(state)
    state["stats"]["published"] = 999
    state["channels"].clear()
    assert guard.stats()["published"] == 2
    assert guard.channels()["loot"]["reliable"] is True
    guard.publish("loot", "e3", tick=0)
    twin = reg.ReliableEventGuard()
    twin.restore(frozen)
    assert twin.pending("loot") == [2]
    assert twin.watermark("loot") == 1
    assert twin.stats()["published"] == 2
    assert twin.next_seq("loot") == 3
    assert twin.collect(1)["resend"] == [2]
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(reg.EventError) as exc:
            twin.restore(bad)
        assert exc.value.code == "EVENT_SNAPSHOT_INVALID"
    assert twin.pending("loot") == [2]


def test_concurrent_publishes_receive_unique_sequences():
    guard = make_guard(bandwidth_quota=16)
    errors = []

    def worker(index):
        try:
            for step in range(5):
                guard.publish("loot", f"w{index}-{step}", tick=0, ttl_ticks=50)
        except Exception as exc:  # pragma: no cover - collected below
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    seqs = guard.pending("loot")
    assert len(seqs) == 40
    assert sorted(seqs) == list(range(1, 41))
    assert guard.stats()["published"] == 40
    assert guard.next_seq("loot") == 41
