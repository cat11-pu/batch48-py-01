import re
import threading

import pytest

import recording_slice_guard as rsg


def guard(**kwargs):
    return rsg.RecordingSliceGuard(**kwargs)


def recording(**kwargs):
    g = guard(**kwargs)
    g.append(0, 1000, 100, keyframe=True)
    return g


def seg(g, pts, duration=1000, size=100, keyframe=False):
    return g.append(pts, duration, size, keyframe=keyframe)


def closed_one(**kwargs):
    g = recording(**kwargs)
    seg(g, 1000)
    seg(g, 2000)
    g.close()
    return g


def test_new_guard_reports_empty_manifest():
    g = guard()
    assert g.slices() == []
    assert g.manifest() == {"stream_id": "stream-0", "slice_count": 0, "slices": []}
    assert g.pending_upload() == []
    assert g.stats()["samples"] == 0
    assert g.close() is None


def test_keyframe_opens_slice_and_accumulates_bytes():
    g = guard()
    out = g.append(0, 1000, 100, keyframe=True)
    assert out["slice_id"] == "slice-1"
    assert out["seq"] == 1
    assert out["bytes"] == 100
    seg(g, 1000, size=200)
    out = seg(g, 2000, size=300)
    assert out["bytes"] == 600
    assert g.slices()[0]["start_pts"] == 0
    assert g.stats()["samples"] == 3


def test_append_rejects_nonpositive_fields():
    g = recording()
    for pts, duration, size in ((-1, 1000, 100), (1000, 0, 100), (1000, 1000, 0)):
        with pytest.raises(rsg.RecordingError) as exc:
            g.append(pts, duration, size)
        assert exc.value.code == "REC_INVALID_SAMPLE"
    assert g.stats()["rejected"] == 3


def test_close_without_open_slice_returns_none():
    g = guard()
    assert g.close() is None
    assert g.stats()["slices_closed"] == 0


def test_keyframe_past_target_duration_closes_slice():
    g = guard(target_seconds=4.0)
    g.append(0, 1000, 100, keyframe=True)
    seg(g, 1000)
    seg(g, 2000)
    seg(g, 3000)
    out = g.append(4000, 1000, 150, keyframe=True)
    assert out["slice_id"] == "slice-2"
    assert out["bytes"] == 150
    records = g.slices()
    assert [r["seq"] for r in records] == [1, 2]
    assert records[0]["status"] == "closed"
    assert records[0]["end_pts"] == 4000
    assert records[0]["bytes"] == 400
    assert records[1]["status"] == "open"
    assert records[1]["start_pts"] == 4000
    assert g.stats()["slices_closed"] == 1


def test_manifest_entry_reports_index_fields():
    g = closed_one()
    entry = g.manifest()["slices"][0]
    assert entry["seq"] == 1
    assert entry["start_pts"] == 0
    assert entry["end_pts"] == 3000
    assert entry["bytes"] == 300
    assert entry["status"] == "closed"
    assert re.fullmatch(r"[0-9a-f]{16}", entry["digest"])
    assert g.manifest()["slice_count"] == 1


def test_digest_is_stable_and_content_sensitive():
    g = closed_one()
    first = g.slice_digest("slice-1")
    assert first == g.slice_digest("slice-1")
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    g.append(3000, 1000, 500, keyframe=True)
    seg(g, 4000, size=700)
    g.close()
    assert g.slice_digest("slice-2") != first


def test_idempotency_key_is_stable_per_slice():
    g = closed_one()
    key = g.idempotency_key("slice-1")
    assert key == g.idempotency_key("slice-1")
    g.append(3000, 1000, 500, keyframe=True)
    g.close()
    assert g.idempotency_key("slice-2") != key
    with pytest.raises(rsg.RecordingError) as exc:
        g.idempotency_key("slice-9")
    assert exc.value.code == "REC_UNKNOWN_SLICE"


def test_successful_upload_leaves_pending_queue():
    g = closed_one()
    assert g.pending_upload() == ["slice-1"]
    out = g.mark_uploaded("slice-1", ok=True)
    assert out["uploaded"] is True
    assert out["status"] == "uploaded"
    assert g.pending_upload() == []
    assert g.stats()["uploads"] == 1


def test_failed_upload_backs_off_and_exhausts_retries():
    g = closed_one()
    first = g.mark_uploaded("slice-1", ok=False)
    assert first["attempts"] == 1
    assert first["retry_in_ms"] == 200
    second = g.mark_uploaded("slice-1", ok=False)
    assert second["attempts"] == 2
    assert second["retry_in_ms"] == 400
    third = g.mark_uploaded("slice-1", ok=False)
    assert third["attempts"] == 3
    assert third["retry_in_ms"] == 800
    assert g.retry_delay_ms("slice-1") == 800
    with pytest.raises(rsg.RecordingError) as exc:
        g.mark_uploaded("slice-1", ok=False)
    assert exc.value.code == "REC_RETRY_EXHAUSTED"
    record = g.slices()[0]
    assert record["status"] == "failed"
    assert g.pending_upload() == []
    assert g.stats()["retries"] == 3


def test_cleanup_removes_only_expired_uploaded_slices():
    g = guard()
    g.append(0, 1000, 100, keyframe=True)
    seg(g, 1000)
    g.close()
    g.append(2000, 1000, 100, keyframe=True)
    seg(g, 8_999_000)
    g.close()
    g.mark_uploaded("slice-1", ok=True)
    g.mark_uploaded("slice-2", ok=True)
    removed = g.cleanup(10_000_000)
    assert removed == ["slice-1"]
    assert [r["seq"] for r in g.slices()] == [2]
    assert g.stats()["cleaned"] == 1


def test_cleanup_keeps_slices_inside_retention():
    g = closed_one()
    g.mark_uploaded("slice-1", ok=True)
    assert g.cleanup(100_000) == []
    assert len(g.slices()) == 1


def test_snapshot_restore_preserves_open_slice():
    g = guard()
    g.append(0, 1000, 100, keyframe=True)
    seg(g, 1000, size=200)
    snap = g.snapshot()
    seg(g, 2000, size=300)
    assert g.slices()[0]["bytes"] == 600
    g.restore(snap)
    assert g.slices()[0]["bytes"] == 300
    assert g.slices()[0]["status"] == "open"
    out = seg(g, 2000, size=400)
    assert out["bytes"] == 700
    with pytest.raises(rsg.RecordingError) as exc:
        g.restore({"version": 1, "slices": "nope"})
    assert exc.value.code == "REC_SNAPSHOT_INVALID"
    assert g.slices()[0]["bytes"] == 700


def test_stats_track_pipeline_and_reset():
    g = closed_one()
    g.mark_uploaded("slice-1", ok=True)
    s = g.stats()
    assert s["samples"] == 3
    assert s["slices_closed"] == 1
    assert s["uploads"] == 1
    assert s["retries"] == 0
    g.reset_stats()
    fresh = g.stats()
    assert [fresh[k] for k in rsg.STAT_KEYS] == [0, 0, 0, 0, 0, 0]
    assert len(g.slices()) == 1


def test_concurrent_append_is_serialized():
    g = guard()
    g.append(0, 100, 10, keyframe=True)

    def worker(tag):
        try:
            for i in range(20):
                g.append(1000 * tag + i * 100, 100, 10)
        except Exception as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    errors = []
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert g.stats()["samples"] == 81
    record = g.slices()[0]
    assert record["bytes"] == 810
    assert record["status"] == "open"
