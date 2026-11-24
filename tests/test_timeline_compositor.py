import threading

import pytest

import timeline_compositor as tc


def make(**kwargs):
    return tc.TimelineCompositor(**kwargs)


def test_tracks_are_listed_in_stacking_order():
    guard = make()
    guard.add_track("bg", order=2)
    guard.add_track("main", order=1)
    assert guard.tracks() == [
        {"track": "main", "order": 1},
        {"track": "bg", "order": 2},
    ]
    assert guard.version() == 2


def test_duplicate_track_name_is_rejected():
    guard = make()
    guard.add_track("main", order=1)
    with pytest.raises(tc.TimelineError) as exc:
        guard.add_track("main", order=5)
    assert exc.value.code == "TL_TRACK_EXISTS"
    assert guard.version() == 1


def test_add_clip_assigns_sequential_ids_and_bumps_version():
    guard = make()
    guard.add_track("main", order=1)
    first = guard.add_clip("main", start_frame=0, duration_frames=25)
    assert first == {
        "clip_id": "clip-1",
        "track": "main",
        "start_frame": 0,
        "duration_frames": 25,
        "end_frame": 25,
        "source_in_frame": 0,
        "transition_in_frames": 0,
        "transition_out_frames": 0,
    }
    second = guard.add_clip("main", start_frame=25, duration_frames=10)
    assert second["clip_id"] == "clip-2"
    assert second["end_frame"] == 35
    assert guard.version() == 3


def test_clip_on_unknown_track_is_rejected():
    guard = make()
    with pytest.raises(tc.TimelineError) as exc:
        guard.add_clip("ghost", start_frame=0, duration_frames=10)
    assert exc.value.code == "TL_TRACK_UNKNOWN"


def test_clip_with_non_positive_duration_is_rejected():
    guard = make()
    guard.add_track("main", order=1)
    with pytest.raises(tc.TimelineError) as exc:
        guard.add_clip("main", start_frame=0, duration_frames=0)
    assert exc.value.code == "TL_CLIP_INVALID"


def test_transition_handles_longer_than_clip_are_rejected():
    guard = make()
    guard.add_track("main", order=1)
    with pytest.raises(tc.TimelineError) as exc:
        guard.add_clip("main", start_frame=0, duration_frames=15,
                       transition_in_frames=10, transition_out_frames=10)
    assert exc.value.code == "TL_TRANSITION_OVERFLOW"


def test_effective_transition_returns_declared_handles_when_they_fit():
    guard = make()
    guard.add_track("main", order=1)
    guard.add_clip("main", start_frame=0, duration_frames=30,
                   transition_in_frames=10, transition_out_frames=12)
    assert guard.effective_transition("clip-1", "in") == 10
    assert guard.effective_transition("clip-1", "out") == 12


def test_render_plan_for_single_clip_maps_source_in():
    guard = make()
    guard.add_track("main", order=1)
    guard.add_clip("main", start_frame=0, duration_frames=100,
                   source_in_frame=50)
    plan = guard.render_plan(start_frame=0, end_frame=100)
    assert plan["version"] == 2
    assert plan["range"] == {"start_frame": 0, "end_frame": 100,
                             "frame_count": 100}
    assert plan["tracks"] == [{
        "track": "main",
        "order": 1,
        "segments": [{
            "kind": "clip",
            "start_frame": 0,
            "end_frame": 100,
            "clip_id": "clip-1",
            "source_in_frame": 50,
        }],
    }]
    assert plan["empty"] is False


def test_adjacent_clips_form_contiguous_segments():
    guard = make()
    guard.add_track("main", order=1)
    guard.add_clip("main", start_frame=0, duration_frames=50,
                   source_in_frame=100)
    guard.add_clip("main", start_frame=50, duration_frames=50,
                   source_in_frame=200)
    plan = guard.render_plan(start_frame=0, end_frame=100)
    segments = plan["tracks"][0]["segments"]
    assert segments == [
        {"kind": "clip", "start_frame": 0, "end_frame": 50,
         "clip_id": "clip-1", "source_in_frame": 100},
        {"kind": "clip", "start_frame": 50, "end_frame": 100,
         "clip_id": "clip-2", "source_in_frame": 200},
    ]


def test_render_plan_lists_tracks_by_stacking_order():
    guard = make()
    guard.add_track("bg", order=2)
    guard.add_track("main", order=1)
    guard.add_clip("bg", start_frame=0, duration_frames=25)
    guard.add_clip("main", start_frame=0, duration_frames=25)
    plan = guard.render_plan(start_frame=0, end_frame=25)
    assert [entry["track"] for entry in plan["tracks"]] == ["main", "bg"]
    assert [entry["order"] for entry in plan["tracks"]] == [1, 2]


def test_render_plan_rejects_empty_or_reversed_range():
    guard = make()
    for start, end in ((0, 0), (5, 5), (-1, 10)):
        with pytest.raises(tc.TimelineError) as exc:
            guard.render_plan(start_frame=start, end_frame=end)
        assert exc.value.code == "TL_RANGE_INVALID"


def test_trim_shortens_clip_and_rejects_non_shrinking_trim():
    guard = make()
    guard.add_track("main", order=1)
    guard.add_clip("main", start_frame=0, duration_frames=100)
    trimmed = guard.trim_clip("clip-1", duration_frames=40)
    assert trimmed["duration_frames"] == 40
    assert trimmed["end_frame"] == 40
    assert guard.version() == 3
    with pytest.raises(tc.TimelineError) as exc:
        guard.trim_clip("clip-1", duration_frames=40)
    assert exc.value.code == "TL_TRIM_INVALID"


def test_remove_clip_drops_it_and_bumps_version():
    guard = make()
    guard.add_track("main", order=1)
    guard.add_clip("main", start_frame=0, duration_frames=25)
    guard.remove_clip("clip-1")
    assert guard.clips() == []
    assert guard.version() == 3


def test_snapshot_restore_keeps_tracks_clips_and_version():
    guard = make()
    guard.add_track("main", order=1)
    guard.add_clip("main", start_frame=0, duration_frames=25)
    snap = guard.snapshot()
    other = make()
    other.restore(snap)
    assert other.version() == 2
    assert other.tracks() == [{"track": "main", "order": 1}]
    assert other.clips() == guard.clips()
    snap["clips"].clear()
    assert len(guard.clips()) == 1


def test_concurrent_edits_produce_one_version_per_mutation():
    guard = make()
    for index in range(8):
        guard.add_track("t%d" % index, order=index)

    def worker(index):
        guard.add_clip("t%d" % index, start_frame=0, duration_frames=25)

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(guard.clips()) == 8
    assert guard.version() == 16
