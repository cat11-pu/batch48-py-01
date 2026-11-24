import threading

import pytest

import fmp4_mux_guard as fm


def mux(**kwargs):
    return fm.Fmp4MuxGuard(**kwargs)


def video_mux(**kwargs):
    g = mux(**kwargs)
    g.set_track("video")
    return g


def test_new_mux_reports_empty_state():
    g = mux()
    assert g.sequence() == 0
    assert g.fragments() == []
    assert g.pending_bytes() == 0
    assert g.tracks() == {}
    assert g.stats()["samples_added"] == 0
    assert g.should_flush() is False


def test_set_track_stores_timescale_and_rejects_zero():
    g = mux()
    out = g.set_track("video", timescale=90000)
    assert out == {"track": "video", "timescale": 90000}
    g.set_track("audio")
    assert g.tracks()["audio"]["timescale"] == 1000
    with pytest.raises(fm.Fmp4Error) as exc:
        g.set_track("video-b", timescale=0)
    assert exc.value.code == "FMP4_TIMESCALE_INVALID"
    with pytest.raises(fm.Fmp4Error) as exc:
        g.set_track("")
    assert exc.value.code == "FMP4_INVALID_SAMPLE"


def test_add_sample_rejects_unknown_track():
    g = mux()
    with pytest.raises(fm.Fmp4Error) as exc:
        g.add_sample("video", dts=0, duration=1000, size=100)
    assert exc.value.code == "FMP4_UNKNOWN_TRACK"
    assert g.stats()["rejected"] == 1


def test_add_sample_rejects_nonpositive_fields():
    g = video_mux()
    for kwargs in (
        {"dts": -1, "duration": 1000, "size": 100},
        {"dts": 0, "duration": 0, "size": 100},
        {"dts": 0, "duration": 1000, "size": 0},
    ):
        with pytest.raises(fm.Fmp4Error) as exc:
            g.add_sample("video", **kwargs)
        assert exc.value.code == "FMP4_INVALID_SAMPLE"
    assert g.pending_bytes() == 0


def test_add_sample_updates_queue_and_returns_record():
    g = video_mux()
    out = g.add_sample("video", dts=0, duration=1000, size=100, keyframe=True)
    assert out == {
        "track": "video",
        "dts": 0,
        "duration": 1000,
        "size": 100,
        "keyframe": True,
        "index": 0,
    }
    g.add_sample("video", dts=1000, duration=1000, size=200)
    g.add_sample("video", dts=2000, duration=1000, size=300, keyframe=True)
    assert g.pending_bytes() == 600
    assert g.should_flush() is False
    assert g.stats()["samples_added"] == 3


def test_add_sample_rejects_decode_time_regression():
    g = video_mux()
    g.add_sample("video", dts=100, duration=1000, size=50)
    with pytest.raises(fm.Fmp4Error) as exc:
        g.add_sample("video", dts=50, duration=1000, size=50)
    assert exc.value.code == "FMP4_INVALID_SAMPLE"
    assert g.pending_bytes() == 50


def test_flush_emits_one_fragment_and_clears_queue():
    g = video_mux()
    for i in range(3):
        g.add_sample("video", dts=i * 1000, duration=1000, size=100 + i * 100)
    frag = g.flush()
    assert frag["seq"] == 1
    assert len(frag["samples"]) == 3
    assert frag["mdat_size"] == 600 + 8
    assert frag["byte_length"] == frag["moof_size"] + frag["mdat_size"]
    assert frag["tfdt"] == {"video": 0}
    assert g.pending_bytes() == 0
    assert g.sequence() == 1
    assert g.stats()["samples_flushed"] == 3


def test_sample_offsets_are_relative_to_moof_start():
    g = video_mux()
    for i, size in enumerate((100, 200, 300)):
        g.add_sample("video", dts=i * 1000, duration=1000, size=size)
    frag = g.flush()
    offsets = g.sample_offsets(1)
    assert offsets[0]["data_offset"] == frag["moof_size"] + 8
    assert offsets[1]["data_offset"] == offsets[0]["data_offset"] + 100
    assert offsets[2]["data_offset"] == offsets[1]["data_offset"] + 200
    with pytest.raises(fm.Fmp4Error) as exc:
        g.sample_offsets(2)
    assert exc.value.code == "FMP4_FRAGMENT_NOT_FOUND"


def test_sequence_increases_with_every_flushed_fragment():
    g = video_mux()
    g.add_sample("video", dts=0, duration=1000, size=100)
    assert g.flush()["seq"] == 1
    g.add_sample("video", dts=1000, duration=1000, size=100)
    assert g.flush()["seq"] == 2
    assert [f["seq"] for f in g.fragments()] == [1, 2]
    assert g.stats()["fragments_flushed"] == 2


def test_flush_rejects_empty_queue_without_burning_sequence():
    g = video_mux()
    with pytest.raises(fm.Fmp4Error) as exc:
        g.flush()
    assert exc.value.code == "FMP4_EMPTY_FRAGMENT"
    assert g.sequence() == 0
    assert g.stats()["rejected"] == 1


def test_sample_flags_mark_sync_and_delta_samples():
    g = video_mux()
    g.add_sample("video", dts=0, duration=1000, size=100, keyframe=True)
    g.add_sample("video", dts=1000, duration=1000, size=100)
    frag = g.flush()
    assert frag["samples"][0]["sample_flags"] == fm.SYNC_SAMPLE_FLAGS
    assert frag["samples"][0]["sample_flags"] == 0x02000000
    assert frag["samples"][1]["sample_flags"] == fm.DELTA_SAMPLE_FLAGS
    assert frag["samples"][1]["sample_flags"] == 0x01010000


def test_tfdt_advances_by_flushed_durations():
    g = video_mux()
    g.add_sample("video", dts=0, duration=1000, size=100)
    g.add_sample("video", dts=1000, duration=2000, size=100)
    g.flush()
    assert g.tfdt_for("video") == 3000
    g.add_sample("video", dts=3000, duration=500, size=100)
    frag = g.flush()
    assert frag["tfdt"] == {"video": 3000}
    assert g.tfdt_for("video") == 3500
    with pytest.raises(fm.Fmp4Error) as exc:
        g.tfdt_for("subtitle")
    assert exc.value.code == "FMP4_UNKNOWN_TRACK"


def test_interleaved_tracks_keep_queue_order_and_run_split():
    g = mux()
    g.set_track("video")
    g.set_track("audio")
    g.add_sample("video", dts=0, duration=1000, size=100, keyframe=True)
    g.add_sample("audio", dts=0, duration=1024, size=50, keyframe=True)
    g.add_sample("video", dts=1000, duration=1000, size=100)
    g.add_sample("audio", dts=1024, duration=1024, size=50)
    frag = g.flush()
    assert [s["track"] for s in frag["samples"]] == ["video", "audio", "video", "audio"]
    assert [s["run_index"] for s in frag["samples"]] == [0, 1, 2, 3]
    assert frag["tfdt"] == {"video": 0, "audio": 0}


def test_snapshot_restore_preserves_queue_and_counter():
    g = video_mux()
    g.add_sample("video", dts=0, duration=1000, size=100)
    g.add_sample("video", dts=1000, duration=1000, size=200)
    g.flush()
    g.add_sample("video", dts=2000, duration=1000, size=300)
    snap = g.snapshot()
    assert snap["sequence"] == 1
    g.add_sample("video", dts=3000, duration=1000, size=400)
    g.restore(snap)
    assert g.pending_bytes() == 300
    assert g.sequence() == 1
    assert len(g.fragments()) == 1
    with pytest.raises(fm.Fmp4Error) as exc:
        g.restore({"version": 1, "queue": "nope"})
    assert exc.value.code == "FMP4_SNAPSHOT_INVALID"
    assert g.pending_bytes() == 300


def test_concurrent_add_sample_is_serialized():
    g = mux()
    names = ["video", "audio", "subtitle", "meta"]
    for name in names:
        g.set_track(name)

    def worker(name):
        try:
            for i in range(25):
                g.add_sample(name, dts=i * 1000, duration=1000, size=10)
        except Exception as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    errors = []
    threads = [threading.Thread(target=worker, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert g.pending_bytes() == 1000
    assert g.stats()["samples_added"] == 100
    frag = g.flush()
    assert len(frag["samples"]) == 100
