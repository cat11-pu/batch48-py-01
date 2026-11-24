import threading

import pytest

import scte35_splice_guard as spg


def make(**kwargs):
    return spg.Scte35SpliceGuard(**kwargs)


def command(splice_id, pts, duration=15000):
    return {"splice_id": splice_id, "splice_pts_ms": pts,
            "break_duration_ms": duration}


def test_parse_command_normalises_fields():
    guard = make()
    out = guard.parse_command(command("ad-1", 10000), program_end_ms=100000)
    assert out == {
        "splice_id": "ad-1",
        "splice_pts_ms": 10000,
        "requested_duration_ms": 15000,
        "effective_duration_ms": 15000,
        "end_pts_ms": 25000,
    }


def test_parse_command_requires_splice_id():
    guard = make()
    with pytest.raises(spg.Scte35SpliceError) as exc:
        guard.parse_command({"splice_pts_ms": 0, "break_duration_ms": 10})
    assert exc.value.code == "SPLICE_INVALID"


def test_parse_command_rejects_negative_pts():
    guard = make()
    with pytest.raises(spg.Scte35SpliceError) as exc:
        guard.parse_command(command("ad-1", -5))
    assert exc.value.code == "SPLICE_INVALID"


def test_splice_point_beyond_program_is_rejected():
    guard = make()
    with pytest.raises(spg.Scte35SpliceError) as exc:
        guard.parse_command(command("ad-1", 100000), program_end_ms=100000)
    assert exc.value.code == "SPLICE_BEYOND_PROGRAM"


def test_schedule_returns_entry_and_lists_pending():
    guard = make()
    entry = guard.schedule(command("ad-1", 5000, 2000), program_end_ms=60000)
    assert entry["end_pts_ms"] == 7000
    assert guard.pending() == [entry]
    assert guard.is_triggered("ad-1") is False


def test_duplicate_pending_splice_id_is_rejected():
    guard = make()
    guard.schedule(command("ad-1", 5000, 2000))
    with pytest.raises(spg.Scte35SpliceError) as exc:
        guard.schedule(command("ad-1", 5000, 2000))
    assert exc.value.code == "SPLICE_DUPLICATE"
    assert len(guard.pending()) == 1


def test_pending_queue_respects_max_pending():
    guard = make(max_pending=1)
    guard.schedule(command("ad-1", 5000, 2000))
    with pytest.raises(spg.Scte35SpliceError) as exc:
        guard.schedule(command("ad-2", 9000, 2000))
    assert exc.value.code == "SPLICE_QUEUE_FULL"
    assert [entry["splice_id"] for entry in guard.pending()] == ["ad-1"]


def test_cancel_unknown_splice_id_returns_false():
    guard = make()
    assert guard.cancel("missing") is False


def test_cancel_pending_splice_id_removes_it():
    guard = make()
    guard.schedule(command("ad-1", 5000, 2000))
    assert guard.cancel("ad-1") is True
    assert guard.pending() == []
    assert guard.is_triggered("ad-1") is False


def test_keyframe_before_splice_point_does_not_execute():
    guard = make()
    guard.schedule(command("ad-1", 5000, 2000))
    assert guard.on_keyframe(4000) == []
    assert len(guard.pending()) == 1
    assert guard.triggered_ids() == []


def test_keyframe_at_splice_point_executes_once():
    guard = make()
    guard.schedule(command("ad-1", 5000, 2000))
    records = guard.on_keyframe(5000)
    assert len(records) == 1
    assert records[0]["splice_id"] == "ad-1"
    assert records[0]["triggered_pts_ms"] == 5000
    assert records[0]["effective_duration_ms"] == 2000
    assert records[0]["end_pts_ms"] == 7000
    assert guard.pending() == []
    assert guard.triggered_ids() == ["ad-1"]
    assert len(guard.executions()) == 1


def test_due_inserts_execute_in_splice_point_order():
    guard = make()
    guard.schedule(command("later", 5000, 1000))
    guard.schedule(command("early", 1000, 1000))
    guard.schedule(command("middle", 3000, 1000))
    records = guard.on_keyframe(5000)
    assert [record["splice_id"] for record in records] == [
        "early", "middle", "later"]
    assert guard.pending() == []
    assert guard.triggered_ids() == ["early", "later", "middle"]


def test_snapshot_restore_keeps_pending_and_triggered_sets():
    guard = make()
    guard.schedule(command("ad-1", 3000, 2000))
    guard.schedule(command("ad-2", 5000, 2000))
    guard.on_keyframe(3000)
    snap = guard.snapshot()
    other = make()
    other.restore(snap)
    assert [entry["splice_id"] for entry in other.pending()] == ["ad-2"]
    assert other.triggered_ids() == ["ad-1"]
    assert len(other.executions()) == 1


def test_invalid_snapshot_is_rejected_without_changing_state():
    guard = make()
    guard.schedule(command("ad-1", 5000, 2000))
    with pytest.raises(spg.Scte35SpliceError) as exc:
        guard.restore({"pending": "broken"})
    assert exc.value.code == "SPLICE_SNAPSHOT_INVALID"
    assert len(guard.pending()) == 1


def test_concurrent_schedules_trigger_exactly_once_per_id():
    guard = make()

    def worker(index):
        guard.schedule(command("ad-%d" % index, 1000, 500))

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(guard.pending()) == 8
    records = guard.on_keyframe(1000)
    assert len(records) == 8
    assert len(guard.executions()) == 8
    assert guard.stats()["triggered"] == 8
