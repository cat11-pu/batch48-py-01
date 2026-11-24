import threading

import pytest

import pts_reorder_guard as prg


def make(**kwargs):
    return prg.PtsReorderGuard(**kwargs)


def test_empty_guard_has_no_buffered_frames():
    guard = make()
    assert guard.buffered() == 0
    assert guard.pop() == []
    assert guard.last_output_pts() is None
    assert guard.last_output_dts() is None


def test_push_reports_buffer_state():
    guard = make()
    out = guard.push(10, 10, payload_size=100, keyframe=True)
    assert out == {"accepted": True, "dropped": False, "buffered": 1,
                   "released": [], "reason": None}
    assert guard.buffered() == 1


def test_pop_releases_frames_in_pts_order():
    guard = make()
    guard.push(30, 30)
    guard.push(10, 10)
    guard.push(20, 20)
    frames = guard.pop(count=2)
    assert [frame["pts"] for frame in frames] == [10, 20]
    assert guard.buffered() == 1
    assert guard.pop()[0]["pts"] == 30


def test_pop_respects_count_and_leaves_the_rest_buffered():
    guard = make()
    for pts in range(5):
        guard.push(pts, pts)
    frames = guard.pop(count=3)
    assert [frame["pts"] for frame in frames] == [0, 1, 2]
    assert guard.buffered() == 2
    assert guard.last_output_pts() == 2


def test_duplicate_pts_is_rejected():
    guard = make()
    guard.push(10, 10)
    with pytest.raises(prg.ReorderError) as exc:
        guard.push(10, 11)
    assert exc.value.code == "REORDER_DUPLICATE_PTS"
    assert guard.buffered() == 1


def test_negative_pts_is_rejected():
    guard = make()
    with pytest.raises(prg.ReorderError) as exc:
        guard.push(-1, 0)
    assert exc.value.code == "REORDER_PTS_INVALID"


def test_non_integer_dts_is_rejected():
    guard = make()
    with pytest.raises(prg.ReorderError) as exc:
        guard.push(10, 10.5)
    assert exc.value.code == "REORDER_DTS_INVALID"


def test_frame_metadata_survives_the_buffer():
    guard = make()
    guard.push(5, 5, payload_size=640, keyframe=True)
    frame = guard.pop()[0]
    assert frame == {"pts": 5, "dts": 5, "keyframe": True, "payload_size": 640}


def test_large_pts_step_counts_as_gap():
    guard = make(gap_threshold_ms=500)
    guard.push(0, 0)
    guard.pop()
    guard.push(1000, 1000)
    guard.pop()
    assert guard.gap_count() == 1


def test_small_pts_step_is_not_a_gap():
    guard = make(gap_threshold_ms=500)
    guard.push(0, 0)
    guard.pop()
    guard.push(100, 100)
    guard.pop()
    assert guard.gap_count() == 0


def test_reset_stats_zeroes_running_counters():
    guard = make()
    guard.push(1, 1)
    guard.push(2, 2)
    guard.pop()
    assert guard.stats()["pushed"] == 2
    assert guard.stats()["popped"] == 1
    guard.reset_stats()
    assert guard.stats() == {"pushed": 0, "popped": 0, "released": 0,
                             "late_dropped": 0, "flushed": 0}


def test_in_order_frames_are_never_counted_as_late():
    guard = make()
    guard.push(1, 1)
    guard.push(2, 2)
    guard.push(3, 3)
    assert guard.late_dropped() == 0


def test_snapshot_holds_metadata_without_payload_and_restores():
    guard = make()
    guard.push(1, 1, payload_size=10, keyframe=True)
    guard.push(2, 2, payload_size=20)
    snap = guard.snapshot()
    assert "payload" not in snap["frames"][0]
    other = make()
    other.restore(snap)
    assert other.buffered() == 2
    assert [frame["pts"] for frame in other.pop(count=2)] == [1, 2]


def test_invalid_snapshot_is_rejected_without_changing_state():
    guard = make()
    guard.push(9, 9)
    with pytest.raises(prg.ReorderError) as exc:
        guard.restore({"frames": "broken"})
    assert exc.value.code == "REORDER_SNAPSHOT_INVALID"
    assert guard.buffered() == 1


def test_concurrent_pushes_do_not_lose_frames():
    guard = make(max_reorder_depth=64)

    def worker(pts):
        guard.push(pts, pts)

    threads = [threading.Thread(target=worker, args=(pts,)) for pts in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert guard.buffered() == 8
    assert guard.stats()["pushed"] == 8
    assert [frame["pts"] for frame in guard.pop(count=8)] == list(range(8))
