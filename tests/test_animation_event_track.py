import copy
import threading

import pytest

import animation_event_track as ae


def run_clip(loop=False):
    return {
        "name": "run_loop" if loop else "run",
        "duration_ms": 1000,
        "loop": loop,
        "bones": {
            "root": [
                {"t": 0, "pos": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
                {"t": 1000, "pos": [10.0, 2.0, -4.0], "scale": [2.0, 2.0, 2.0]},
            ],
            "hand": [
                {"t": 0, "pos": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
                {"t": 1000, "pos": [99.0, 99.0, 99.0], "scale": [1.0, 1.0, 1.0]},
            ],
        },
    }


def move_clip():
    return {
        "name": "move",
        "duration_ms": 1000,
        "loop": False,
        "bones": {
            "root": [
                {"t": 0, "pos": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
                {"t": 1000, "pos": [10.0, 2.0, -4.0], "scale": [1.0, 1.0, 1.0]},
            ],
            "hand": [
                {"t": 0, "pos": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]},
                {"t": 1000, "pos": [99.0, 99.0, 99.0], "scale": [1.0, 1.0, 1.0]},
            ],
        },
    }


def test_load_clip_registers_and_reports_info():
    track = ae.AnimationEventTrack()
    info = track.load_clip(run_clip())
    assert info["name"] == "run"
    assert info["duration_ms"] == 1000
    assert info["loop"] is False
    assert info["bone_count"] == 2
    assert info["event_count"] == 0
    assert track.clip_info("run") == info
    assert track.list_clips() == ["run"]


def test_load_rejects_malformed_clip_and_duplicate():
    track = ae.AnimationEventTrack()
    good_frame = {"pos": [0.0, 0.0, 0.0], "scale": [1.0, 1.0, 1.0]}
    first = {"t": 0, **good_frame}
    bad_clips = [
        {"name": "nobones", "duration_ms": 1000, "loop": False},
        {"name": "zero", "duration_ms": 0, "loop": False, "bones": {"root": [first]}},
        {"name": "sparse", "duration_ms": 1000, "loop": False,
         "bones": {"root": [{"t": 500, **good_frame}, {"t": 100, **good_frame}]}},
        {"name": "late", "duration_ms": 1000, "loop": False,
         "bones": {"root": [{"t": 1001, **good_frame}]}},
    ]
    for bad in bad_clips:
        with pytest.raises(ae.AnimationError) as exc:
            track.load_clip(bad)
        assert exc.value.code == "ANIM_CLIP_INVALID"
    assert track.list_clips() == []

    track.load_clip(run_clip())
    with pytest.raises(ae.AnimationError) as exc:
        track.load_clip(run_clip())
    assert exc.value.code == "ANIM_CLIP_DUPLICATE"


def test_unknown_clip_operations_rejected():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    for call in (lambda: track.sample("ghost", 0),
                 lambda: track.events("ghost"),
                 lambda: track.clip_info("ghost"),
                 lambda: track.add_event("ghost", {"time_ms": 0, "name": "x"})):
        with pytest.raises(ae.AnimationError) as exc:
            call()
        assert exc.value.code == "ANIM_UNKNOWN_CLIP"


def test_sample_interpolates_position_and_scale_by_milliseconds():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    out = track.sample("run", 250)
    assert out["time_ms"] == 250
    assert out["loop_index"] == 0
    assert out["bones"]["root"]["pos"] == pytest.approx([2.5, 0.5, -1.0])
    assert out["bones"]["root"]["scale"] == pytest.approx([1.25, 1.25, 1.25])
    assert out["bones"]["hand"]["pos"] == pytest.approx([24.75, 24.75, 24.75])


def test_sample_clamps_for_non_loop_clip():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    before = track.sample("run", -50)
    assert before["time_ms"] == 0
    assert before["loop_index"] == 0
    assert before["bones"]["root"]["pos"] == pytest.approx([0.0, 0.0, 0.0])

    after = track.sample("run", 5000)
    assert after["time_ms"] == 1000
    assert after["loop_index"] == 0
    assert after["bones"]["root"]["pos"] == pytest.approx([10.0, 2.0, -4.0])


def test_sample_wraps_loop_time_and_reports_loop_index():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip(loop=True))
    out = track.sample("run_loop", 1250)
    assert out["time_ms"] == 250
    assert out["loop_index"] == 1
    assert out["bones"]["root"]["pos"] == pytest.approx([2.5, 0.5, -1.0])

    later = track.sample("run_loop", 2250)
    assert later["time_ms"] == 250
    assert later["loop_index"] == 2


def test_events_sorted_by_time_priority_and_id():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    track.add_event("run", {"time_ms": 100, "name": "spawn"})
    track.add_event("run", {"time_ms": 250, "name": "hit", "priority": 5})
    track.add_event("run", {"time_ms": 250, "name": "footstep"})
    track.add_event("run", {"time_ms": 250, "name": "hit_low", "priority": -1})
    track.add_event("run", {"time_ms": 250, "name": "alpha", "priority": 5})
    track.add_event("run", {"time_ms": 250, "name": "beta", "priority": 5})

    ordered = track.events("run")
    assert [item["name"] for item in ordered] == [
        "spawn", "hit", "alpha", "beta", "footstep", "hit_low",
    ]
    assert [item["time_ms"] for item in ordered] == [100, 250, 250, 250, 250, 250]


def test_add_event_validates_time_and_name():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    with pytest.raises(ae.AnimationError) as exc:
        track.add_event("run", {"time_ms": 100})
    assert exc.value.code == "ANIM_EVENT_INVALID"
    with pytest.raises(ae.AnimationError) as exc:
        track.add_event("run", {"time_ms": 100, "name": ""})
    assert exc.value.code == "ANIM_EVENT_INVALID"
    with pytest.raises(ae.AnimationError) as exc:
        track.add_event("run", {"time_ms": -1, "name": "x"})
    assert exc.value.code == "ANIM_EVENT_OUT_OF_RANGE"
    with pytest.raises(ae.AnimationError) as exc:
        track.add_event("run", {"time_ms": 1001, "name": "x"})
    assert exc.value.code == "ANIM_EVENT_OUT_OF_RANGE"
    assert track.event_count("run") == 0

    track.add_event("run", {"time_ms": 1000, "name": "end"})
    assert track.events("run")[0]["time_ms"] == 1000


def test_event_limit_per_clip():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    for index in range(256):
        track.add_event("run", {"time_ms": 0, "name": f"e{index}"})
    with pytest.raises(ae.AnimationError) as exc:
        track.add_event("run", {"time_ms": 0, "name": "overflow"})
    assert exc.value.code == "ANIM_EVENT_LIMIT"
    assert track.event_count("run") == 256


def test_remove_event_and_clear_events():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    event_id = track.add_event("run", {"time_ms": 100, "name": "one"})
    assert track.remove_event("run", event_id) is True
    assert track.remove_event("run", event_id) is False

    track.add_event("run", {"time_ms": 100, "name": "a"})
    track.add_event("run", {"time_ms": 200, "name": "b"})
    track.add_event("run", {"time_ms": 300, "name": "c"})
    assert track.clear_events("run") == 3
    assert track.event_count("run") == 0
    assert track.clear_events("run") == 0


def test_events_between_is_left_closed_right_open():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip())
    track.add_event("run", {"time_ms": 100, "name": "a"})
    track.add_event("run", {"time_ms": 500, "name": "b"})

    assert [item["name"] for item in track.events_between("run", 100, 500)] == ["a"]
    assert [item["name"] for item in track.events_between("run", 100, 501)] == ["a", "b"]
    assert track.events_between("run", 101, 500) == []
    assert all(item["loop_index"] == 0 for item in track.events_between("run", 0, 1000))


def test_events_between_expands_loop_occurrences():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip(loop=True))
    track.add_event("run_loop", {"time_ms": 250, "name": "step"})

    occurrences = track.events_between("run_loop", 0, 2000)
    assert [(item["time_ms"], item["loop_index"]) for item in occurrences] == [(250, 0), (1250, 1)]
    assert [item["name"] for item in occurrences] == ["step", "step"]

    single = track.events_between("run_loop", 250, 1250)
    assert len(single) == 1
    assert single[0]["time_ms"] == 250


def test_root_motion_uses_root_translation_only():
    track = ae.AnimationEventTrack()
    track.load_clip(move_clip())
    whole = track.extract_root_motion("move", 0, 1000)
    assert whole["delta"] == pytest.approx([10.0, 2.0, -4.0])
    partial = track.extract_root_motion("move", 250, 750)
    assert partial["delta"] == pytest.approx([5.0, 1.0, -2.0])


def test_snapshot_restore_and_corrupt_state():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip(loop=True))
    track.add_event("run_loop", {"time_ms": 250, "name": "step"})
    state = track.snapshot()
    assert state["version"] == 1
    pristine = copy.deepcopy(state)

    state["clips"].clear()
    assert track.event_count("run_loop") == 1

    restored = ae.AnimationEventTrack()
    restored.restore(pristine)
    assert restored.clip_info("run_loop") == track.clip_info("run_loop")
    assert restored.events("run_loop") == track.events("run_loop")
    assert restored.sample("run_loop", 250) == track.sample("run_loop", 250)

    with pytest.raises(ae.AnimationError) as exc:
        restored.restore({"version": 9})
    assert exc.value.code == "ANIM_SNAPSHOT_INVALID"
    assert restored.event_count("run_loop") == 1


def test_concurrent_sample_and_add_event():
    track = ae.AnimationEventTrack()
    track.load_clip(run_clip(loop=True))
    baseline = track.sample("run_loop", 250)
    observed = []
    errors = []

    def writer(tag):
        try:
            for step in range(30):
                track.add_event("run_loop",
                                {"time_ms": (tag * 10 + step) % 1000,
                                 "name": f"{tag}-{step}"})
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(80):
                observed.append(track.sample("run_loop", 250))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(tag,)) for tag in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(observed) == 240
    assert all(item == baseline for item in observed)
    assert track.event_count("run_loop") == 90
