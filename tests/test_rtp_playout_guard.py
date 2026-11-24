import threading

import pytest

import rtp_playout_guard as rg


def guard(**kwargs):
    return rg.RtpPlayoutGuard(**kwargs)


def test_new_guard_starts_empty_with_floor_delay():
    g = guard()
    assert g.queue_depth() == 0
    assert g.target_delay_ms() == 40.0
    assert g.jitter_ms() == 0.0
    assert g.queued_seqs() == []


def test_push_accepts_packets_and_reports_depth():
    g = guard()
    assert g.push(1, 100.0, 0.0) == {"accepted": True, "reason": None, "queued": 1}
    assert g.push(2, 120.0, 20.0) == {"accepted": True, "reason": None, "queued": 2}
    assert g.queue_depth() == 2
    assert g.queued_seqs() == [1, 2]
    assert g.stats()["accepted"] == 2


def test_duplicate_sequence_is_refused():
    g = guard()
    g.push(5, 10.0, 0.0)
    assert g.push(5, 30.0, 40.0) == {
        "accepted": False, "reason": "duplicate", "queued": 1,
    }
    assert g.stats()["duplicates"] == 1
    assert g.queue_depth() == 1


def test_sequence_outside_wrap_range_raises():
    g = guard()
    for bad in (-1, 65536, 2.5):
        with pytest.raises(rg.PlayoutError) as exc:
            g.push(bad, 0.0, 0.0)
        assert exc.value.code == "PLAYOUT_INVALID_SEQ"
    assert g.stats()["rejected"] == 3


def test_jitter_uses_transit_spread_of_recent_arrivals():
    g = guard()
    g.push(0, 100.0, 0.0)
    g.push(1, 110.0, 20.0)
    g.push(2, 130.0, 40.0)
    assert g.jitter_ms() == pytest.approx(10.0)
    g.push(3, 140.0, 60.0)
    assert g.jitter_ms() == pytest.approx(20.0)


def test_target_delay_approaches_cap_under_heavy_jitter():
    g = guard(min_delay_ms=40.0, max_delay_ms=300.0)
    g.push(0, 0.0, 0.0)
    g.push(1, 480.0, 200.0)
    g.push(2, 960.0, 400.0)
    assert g.jitter_ms() == pytest.approx(560.0)
    for step in range(20):
        g.tick(float(step))
    assert g.target_delay_ms() == pytest.approx(300.0)


def test_target_delay_returns_to_floor_when_queue_drains():
    g = guard()
    g.push(0, 0.0, 0.0)
    g.tick(0.0)
    for step in range(1, 61):
        g.tick(step * 20.0)
    assert g.queue_depth() == 0
    assert g.target_delay_ms() == pytest.approx(40.0)


def test_sequence_wrap_is_unwrapped_across_65535():
    g = guard()
    for seq, ts in ((65534, 0.0), (65535, 20.0), (0, 40.0), (1, 60.0)):
        g.push(seq, ts, ts)
    assert g.queued_seqs() == [65534, 65535, 65536, 65537]
    first = g.tick(0.0)
    assert first["played"] == [65534, 65535, 65536]
    second = g.tick(100.0)
    assert second["played"] == [65537]


def test_late_packet_is_refused_after_playout_moves_on():
    g = guard(drop_after_ms=100.0)
    g.push(0, 500.0, 500.0)
    g.tick(0.0)
    g.tick(200.0)
    assert g.playout_ms() == pytest.approx(730.0)
    assert g.push(1, 200.0, 30.0) == {
        "accepted": False, "reason": "late", "queued": 0,
    }
    assert g.stats()["late"] == 1


def test_capacity_limit_raises_for_plain_packet():
    g = guard(capacity=3)
    for seq in range(3):
        g.push(seq, seq * 20.0, seq * 20.0)
    with pytest.raises(rg.PlayoutError) as exc:
        g.push(3, 60.0, 60.0)
    assert exc.value.code == "PLAYOUT_BUFFER_FULL"
    assert exc.value.detail["capacity"] == 3
    assert exc.value.detail["queue_depth"] == 3
    assert g.queue_depth() == 3


def test_playout_clock_advances_with_speed_and_stays_monotonic():
    g = guard(min_delay_ms=40.0)
    for seq in range(4):
        ts = 200.0 + seq * 20.0
        g.push(seq, ts, ts)
    first = g.tick(0.0)
    assert first["playout_ms"] == pytest.approx(240.0)
    assert first["played"] == [0, 1, 2]
    assert first["speed"] == pytest.approx(1.0)
    second = g.tick(100.0)
    assert second["playout_ms"] == pytest.approx(340.0)
    assert second["played"] == [3]
    assert second["playout_ms"] > first["playout_ms"]


def test_fast_forward_when_backlog_exceeds_two_frames():
    g = guard()
    for seq in range(8):
        ts = seq * 20.0
        g.push(seq, ts, ts)
    g.tick(0.0)
    second = g.tick(100.0)
    assert second["speed"] == pytest.approx(1.05)
    assert second["playout_ms"] == pytest.approx(145.0)
    assert second["played"] == [3, 4, 5, 6, 7]


def test_slow_down_when_queue_is_shallower_than_target():
    g = guard(min_delay_ms=40.0)
    g.push(0, 0.0, 0.0)
    first = g.tick(10.0)
    assert first["speed"] == pytest.approx(0.95)
    assert first["playout_ms"] == pytest.approx(40.0)
    second = g.tick(60.0)
    assert second["playout_ms"] == pytest.approx(87.5)
    assert second["played"] == [0]


def test_snapshot_restore_roundtrip_and_corrupt_state():
    g = guard()
    g.push(0, 500.0, 500.0)
    g.tick(0.0)
    floor = g.target_delay_ms()
    snap = g.snapshot()
    assert {"target_ms", "playout_ms", "queue", "last_seq"} <= set(snap)
    snap["queue"].clear()
    assert g.queue_depth() == 1
    live = g.snapshot()
    g.tick(500.0)
    assert g.queue_depth() == 0
    g.restore(live)
    assert g.queue_depth() == 1
    assert g.playout_ms() == pytest.approx(540.0)
    assert g.target_delay_ms() == pytest.approx(floor)
    with pytest.raises(rg.PlayoutError) as exc:
        g.restore({"target_ms": "broken"})
    assert exc.value.code == "PLAYOUT_SNAPSHOT_INVALID"
    assert g.queue_depth() == 1


def test_concurrent_pushes_are_serialised():
    g = guard()
    barrier = threading.Barrier(4)
    failures = []

    def worker(base):
        try:
            barrier.wait()
            for i in range(40):
                seq = base + i
                ts = seq * 20.0
                g.push(seq, ts, ts)
        except Exception as exc:  # pragma: no cover - surfaced after join
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(base,))
               for base in (0, 40, 80, 120)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert g.stats()["accepted"] == 160
    assert g.queue_depth() == 160
    assert sorted(g.queued_seqs()) == list(range(160))
