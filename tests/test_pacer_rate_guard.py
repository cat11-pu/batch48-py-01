import threading

import pytest

import pacer_rate_guard as prg


def guard(**kwargs):
    return prg.PacerRateGuard(**kwargs)


def frame(seq, size=4000, keyframe=False, droppable=False):
    return {"seq": seq, "bytes": size, "keyframe": keyframe, "droppable": droppable}


def test_new_guard_starts_at_start_rate_with_full_bucket():
    g = guard()
    assert g.target_bps() == 800000.0
    assert g.tokens_bytes() == pytest.approx(10000.0)
    assert g.mode() == "steady"
    assert g.queue_depth() == 0
    assert g.queue_bytes() == 0


def test_enqueue_accepts_and_tracks_queue_bytes():
    g = guard()
    out = g.enqueue(frame(1, 4000, keyframe=True), now_ms=0.0)
    assert out == {
        "accepted": True, "reason": None, "dropped": 0, "queue_bytes": 4000,
    }
    second = g.enqueue(frame(2, 2000), now_ms=0.0)
    assert second["queue_bytes"] == 6000
    assert g.queue_depth() == 2
    assert g.stats()["enqueued"] == 2


def test_duplicate_frame_sequence_is_refused():
    g = guard()
    g.enqueue(frame(7, 1000), now_ms=0.0)
    out = g.enqueue(frame(7, 1000), now_ms=0.0)
    assert out == {
        "accepted": False, "reason": "duplicate", "dropped": 0, "queue_bytes": 1000,
    }
    assert g.stats()["duplicates"] == 1
    assert g.queue_depth() == 1


def test_invalid_frame_and_loss_ratio_raise():
    g = guard()
    bad_frames = (
        frame(1, 0),
        frame(-1, 100),
        {"seq": 2, "bytes": "4000", "keyframe": False, "droppable": False},
    )
    for bad in bad_frames:
        with pytest.raises(prg.PacerError) as exc:
            g.enqueue(bad, now_ms=0.0)
        assert exc.value.code == "PACER_INVALID_FRAME"
    with pytest.raises(prg.PacerError) as exc:
        g.report_loss(1.5)
    assert exc.value.code == "PACER_INVALID_LOSS"


def test_queue_overflow_without_droppable_frames_is_refused():
    g = guard(max_queue_bytes=9000)
    for seq in (1, 2):
        g.enqueue(frame(seq), now_ms=0.0)
    out = g.enqueue(frame(3), now_ms=0.0)
    assert out == {
        "accepted": False, "reason": "queue_full", "dropped": 0, "queue_bytes": 8000,
    }
    assert g.stats()["rejected_frames"] == 1


def test_queue_overflow_evicts_oldest_droppable_frame():
    g = guard(max_queue_bytes=9000)
    for seq in (1, 2, 3):
        g.enqueue(frame(seq, 3000, droppable=True), now_ms=0.0)
    out = g.enqueue(frame(4, 2000), now_ms=0.0)
    assert out == {
        "accepted": True, "reason": None, "dropped": 1, "queue_bytes": 8000,
    }
    assert g.queue_depth() == 3
    assert g.stats()["dropped_frames"] == 1


def test_tick_sends_only_what_tokens_cover():
    g = guard()
    for seq in (1, 2, 3):
        g.enqueue(frame(seq), now_ms=0.0)
    out = g.tick(0.0)
    assert out["sent"] == [1, 2]
    assert out["bytes_sent"] == 8000
    assert out["tokens"] == pytest.approx(2000.0)
    assert out["target_bps"] == pytest.approx(800000.0)
    assert out["mode"] == "steady"
    assert g.queue_depth() == 1


def test_idle_refill_is_capped_at_bucket_capacity():
    g = guard()
    for seq in (1, 2, 3):
        g.enqueue(frame(seq), now_ms=0.0)
    g.tick(0.0)
    second = g.tick(100.0)
    assert second["sent"] == [3]
    assert second["tokens"] == pytest.approx(6000.0)
    idle = g.tick(100000.0)
    assert idle["sent"] == []
    assert idle["tokens"] == pytest.approx(10000.0)


def test_two_clean_cycles_allow_five_percent_raise():
    g = guard()
    first = g.on_cycle(True)
    assert first["changed"] is False
    assert first["target_bps"] == pytest.approx(800000.0)
    second = g.on_cycle(True)
    assert second["changed"] is True
    assert second["target_bps"] == pytest.approx(840000.0)
    assert g.mode() == "steady"
    assert g.stats()["cycles_up"] == 1


def test_congestion_drops_fifteen_percent_and_enters_probe():
    g = guard()
    out = g.on_cycle(False)
    assert out["changed"] is True
    assert out["target_bps"] == pytest.approx(680000.0)
    assert out["mode"] == "probe"
    assert g.target_bps() == pytest.approx(680000.0)
    assert g.mode() == "probe"
    assert g.stats()["cycles_down"] == 1


def test_loss_report_above_ten_percent_drops_immediately():
    g = guard()
    out = g.report_loss(0.12)
    assert out["adjusted"] is True
    assert out["target_bps"] == pytest.approx(680000.0)
    assert g.mode() == "probe"
    steady = g.report_loss(0.10)
    assert steady["adjusted"] is False
    assert g.target_bps() == pytest.approx(680000.0)
    assert g.stats()["loss_reports"] == 1


def test_stats_snapshot_and_reset():
    g = guard()
    g.enqueue(frame(1), now_ms=0.0)
    g.tick(0.0)
    stats = g.stats()
    assert stats["enqueued"] == 1
    assert stats["sent_frames"] == 1
    assert stats["bytes_sent"] == 4000
    assert set(stats) == {
        "enqueued", "duplicates", "rejected_frames", "dropped_frames",
        "sent_frames", "bytes_sent", "cycles_up", "cycles_down", "loss_reports",
    }
    g.reset_stats()
    assert set(g.stats().values()) == {0}


def test_snapshot_restore_and_corrupt_state():
    g = guard()
    for seq in (1, 2, 3):
        g.enqueue(frame(seq), now_ms=0.0)
    g.tick(0.0)
    snap = g.snapshot()
    assert {"target_bps", "tokens", "mode", "queue"} <= set(snap)
    snap["queue"].clear()
    assert g.queue_depth() == 1
    live = g.snapshot()
    g.tick(100.0)
    assert g.queue_depth() == 0
    g.restore(live)
    assert g.queue_depth() == 1
    assert g.tokens_bytes() == pytest.approx(2000.0)
    assert g.target_bps() == pytest.approx(800000.0)
    with pytest.raises(prg.PacerError) as exc:
        g.restore({"target_bps": "broken"})
    assert exc.value.code == "PACER_SNAPSHOT_INVALID"


def test_concurrent_enqueue_is_serialised():
    g = guard(max_queue_bytes=1000000)
    barrier = threading.Barrier(4)
    failures = []

    def worker(base):
        try:
            barrier.wait()
            for i in range(25):
                g.enqueue(frame(base + i, 100), now_ms=0.0)
        except Exception as exc:  # pragma: no cover - surfaced after join
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(base,))
               for base in (0, 25, 50, 75)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert g.stats()["enqueued"] == 100
    assert g.queue_depth() == 100
    assert g.queue_bytes() == 10000


def test_target_rate_floor_after_repeated_congestion():
    g = guard()
    for _ in range(10):
        g.on_cycle(False)
    assert g.target_bps() == pytest.approx(200000.0)
    assert g.stats()["cycles_down"] == 10
