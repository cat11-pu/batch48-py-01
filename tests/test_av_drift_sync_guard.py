import threading

import pytest

import av_drift_sync_guard as dg


def make(**kwargs):
    return dg.AvDriftSyncGuard(**kwargs)


def audio(guard, stream_id="mic", rate=48000):
    return guard.register_stream(stream_id, kind="audio", clock_rate=rate)


def video(guard, stream_id="cam", rate=90000):
    return guard.register_stream(stream_id, kind="video", clock_rate=rate)


def test_register_first_stream_becomes_primary():
    g = make()
    out = audio(g)
    assert out["kind"] == "audio"
    assert out["primary"] is True
    assert out["samples"] == 0
    joined = video(g)
    assert joined["primary"] is False
    assert g.primary_stream() == "mic"
    assert g.stream_info("cam")["clock_rate"] == 90000
    assert g.stats()["streams_registered"] == 2


def test_register_rejects_duplicate_kind_and_rate():
    g = make()
    audio(g)
    with pytest.raises(dg.SyncError) as exc:
        audio(g)
    assert exc.value.code == "SYNC_DUPLICATE_STREAM"
    with pytest.raises(dg.SyncError) as exc:
        g.register_stream("sub", kind="subtitle", clock_rate=1000)
    assert exc.value.code == "SYNC_INVALID_STREAM"
    with pytest.raises(dg.SyncError) as exc:
        g.register_stream("zero", kind="audio", clock_rate=0)
    assert exc.value.code == "SYNC_INVALID_STREAM"
    limited = make(max_streams=1)
    audio(limited, "a")
    with pytest.raises(dg.SyncError) as exc:
        audio(limited, "b")
    assert exc.value.code == "SYNC_STREAM_LIMIT"


def test_first_sr_builds_baseline_mapping():
    g = make()
    audio(g)
    out = g.ingest_sr("mic", ntp_ms=1000.0, rtp_ts=0)
    assert out["baseline"] is True
    assert out["drift_ms"] == pytest.approx(0.0)
    assert out["ratio"] == pytest.approx(1.0)
    assert g.stream_info("mic")["samples"] == 1
    assert g.stats()["sr_ingested"] == 1


def test_drift_milliseconds_and_ratio_follow_sr_skew():
    g = make()
    audio(g, rate=48000)
    g.ingest_sr("mic", ntp_ms=1000.0, rtp_ts=0)
    out = g.ingest_sr("mic", ntp_ms=5000.0, rtp_ts=192480)
    assert out["drift_ms"] == pytest.approx(10.0)
    assert out["ratio"] == pytest.approx(1.0025)
    assert g.drift_ms("mic") == pytest.approx(10.0)
    assert g.drift_ratio("mic") == pytest.approx(1.0025)


def test_drift_accumulates_and_warns_past_threshold():
    g = make()
    video(g, rate=1000)
    g.ingest_sr("cam", ntp_ms=0.0, rtp_ts=0)
    g.ingest_sr("cam", ntp_ms=1000.0, rtp_ts=1100)
    assert g.drift_ms("cam") == pytest.approx(100.0)
    assert g.stats()["drift_warnings"] == 0
    g.ingest_sr("cam", ntp_ms=2000.0, rtp_ts=2300)
    assert g.drift_ms("cam") == pytest.approx(300.0)
    assert g.stats()["drift_warnings"] == 1


def test_ingest_rejects_non_monotonic_reports():
    g = make()
    video(g, rate=1000)
    g.ingest_sr("cam", ntp_ms=1000.0, rtp_ts=0)
    for ntp in (1000.0, 900.0):
        with pytest.raises(dg.SyncError) as exc:
            g.ingest_sr("cam", ntp_ms=ntp, rtp_ts=1000)
        assert exc.value.code == "SYNC_INVALID_SR"
    assert g.stats()["sr_rejected"] == 2


def test_unknown_stream_operations_raise():
    g = make()
    calls = (
        lambda: g.drift_ms("ghost"),
        lambda: g.unregister_stream("ghost"),
        lambda: g.enqueue_audio("ghost", 100.0),
        lambda: g.schedule_video("ghost", 0.0, now_ms=0.0),
    )
    for call in calls:
        with pytest.raises(dg.SyncError) as exc:
            call()
        assert exc.value.code == "SYNC_UNKNOWN_STREAM"


def test_samples_capped_by_capacity():
    g = make(max_samples=3)
    video(g, rate=1000)
    for i in range(5):
        g.ingest_sr("cam", ntp_ms=i * 1000.0, rtp_ts=i * 1000)
    assert g.stream_info("cam")["samples"] == 3
    assert g.stats()["sr_ingested"] == 5
    assert g.stats()["samples_dropped"] == 2


def test_primary_switch_commits_after_confirmation():
    g = make()
    audio(g)
    video(g)
    armed = g.request_primary("cam")
    assert armed["pending"] == "cam"
    assert armed["switched"] is False
    g.ingest_sr("cam", ntp_ms=0.0, rtp_ts=0)
    done = g.ingest_sr("cam", ntp_ms=1000.0, rtp_ts=90000)
    assert done["switched"] is True
    assert done["primary"] == "cam"
    assert g.primary_stream() == "cam"


def test_audio_below_low_watermark_is_padded():
    g = make()
    audio(g)
    out = g.enqueue_audio("mic", 40.0)
    assert out == {
        "action": "pad",
        "padded_ms": 20.0,
        "dropped_ms": 0.0,
        "buffered_ms": 60.0,
    }
    ok = g.enqueue_audio("mic", 120.0)
    assert ok["action"] == "ok"
    assert ok["padded_ms"] == 0.0
    assert ok["buffered_ms"] == 120.0
    assert g.stats()["audio_padded_ms"] == pytest.approx(20.0)


def test_audio_above_high_watermark_drops_oldest():
    g = make()
    audio(g)
    out = g.enqueue_audio("mic", 300.0)
    assert out == {
        "action": "drop",
        "padded_ms": 0.0,
        "dropped_ms": 60.0,
        "buffered_ms": 240.0,
    }
    assert g.stats()["audio_dropped_ms"] == pytest.approx(60.0)


def test_video_schedule_waits_renders_and_drops():
    g = make()
    video(g, rate=1000)
    g.ingest_sr("cam", ntp_ms=0.0, rtp_ts=0)
    g.ingest_sr("cam", ntp_ms=4000.0, rtp_ts=4010)
    cases = (
        (980.0, "render"),
        (900.0, "render"),
        (800.0, "wait"),
        (1200.0, "drop"),
    )
    for now, action in cases:
        out = g.schedule_video("cam", 1000.0, now_ms=now)
        assert out["action"] == action
        assert out["corrected_ms"] == pytest.approx(1002.5)


def test_unregister_primary_clears_primary():
    g = make()
    audio(g)
    video(g)
    g.unregister_stream("mic")
    assert g.primary_stream() is None
    with pytest.raises(dg.SyncError) as exc:
        g.unregister_stream("mic")
    assert exc.value.code == "SYNC_UNKNOWN_STREAM"
    assert g.stats()["streams_removed"] == 1


def test_snapshot_restore_and_corrupt_state():
    g = make()
    audio(g)
    g.ingest_sr("mic", ntp_ms=1000.0, rtp_ts=0)
    g.ingest_sr("mic", ntp_ms=5000.0, rtp_ts=192480)
    snap = g.snapshot()
    assert {"primary", "pending", "streams"} <= set(snap)
    snap["streams"].clear()
    assert g.drift_ms("mic") == pytest.approx(10.0)
    live = g.snapshot()
    g.unregister_stream("mic")
    g.restore(live)
    assert g.drift_ms("mic") == pytest.approx(10.0)
    assert g.primary_stream() == "mic"
    with pytest.raises(dg.SyncError) as exc:
        g.restore({"streams": 5})
    assert exc.value.code == "SYNC_SNAPSHOT_INVALID"
    assert g.drift_ms("mic") == pytest.approx(10.0)


def test_concurrent_ingest_keeps_per_stream_state():
    g = make(max_samples=32)
    for name in ("a", "b", "c"):
        video(g, stream_id=name, rate=1000)
    barrier = threading.Barrier(3)
    failures = []

    def worker(name):
        try:
            barrier.wait()
            for i in range(20):
                g.ingest_sr(name, ntp_ms=i * 1000.0, rtp_ts=i * 1000)
        except Exception as exc:  # pragma: no cover - surfaced after join
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(name,))
               for name in ("a", "b", "c")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert g.stats()["sr_ingested"] == 60
    for name in ("a", "b", "c"):
        assert g.stream_info(name)["samples"] == 20
        assert g.drift_ms(name) == pytest.approx(0.0)
