import copy
import threading

import pytest

import resume_alignment_guard as rag

ACCOUNT = "acct-1"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value


def make(**kwargs):
    kwargs.setdefault("clock", Clock())
    return rag.ResumeAlignmentGuard(**kwargs)


def start(guard):
    return guard.begin_session(ACCOUNT)["epoch"]


def install(guard, epoch, frame=10, state=None):
    return guard.install_baseline(
        ACCOUNT,
        epoch=epoch,
        frame=frame,
        state=state if state is not None else {"pos": 0},
    )


def frame(number, events=()):
    return {"frame": number, "input": f"in-{number}", "events": list(events)}


def batch(epoch, *entries):
    return {"epoch": epoch, "frames": list(entries)}


def primed(**kwargs):
    guard = make(**kwargs)
    epoch = start(guard)
    install(guard, epoch, frame=10)
    return guard, epoch


def test_begin_session_assigns_monotonic_epochs():
    guard = make()
    assert guard.session_epoch(ACCOUNT) == 0
    assert guard.begin_session(ACCOUNT) == {"account": ACCOUNT, "epoch": 1}
    assert guard.begin_session(ACCOUNT) == {"account": ACCOUNT, "epoch": 2}
    assert guard.session_epoch(ACCOUNT) == 2
    assert guard.stats()["sessions"] == 2


def test_reconnect_storm_is_rate_limited():
    clock = Clock()
    guard = make(clock=clock)
    for _ in range(3):
        guard.begin_session(ACCOUNT)
    with pytest.raises(rag.ResumeError) as exc:
        guard.begin_session(ACCOUNT)
    assert exc.value.code == "RESUME_RATE_LIMITED"
    assert guard.session_epoch(ACCOUNT) == 3
    clock.value = 100.0
    assert guard.begin_session(ACCOUNT)["epoch"] == 4


def test_install_baseline_sets_the_authority_frame():
    guard = make()
    epoch = start(guard)
    out = install(guard, epoch, frame=10, state={"pos": 7})
    assert out == {"account": ACCOUNT, "frame": 10, "epoch": epoch}
    assert guard.applied_frame(ACCOUNT) == 10
    assert guard.baseline_of(ACCOUNT) == {"pos": 7}
    assert guard.applied_inputs(ACCOUNT) == []
    assert guard.stats()["baselines"] == 1


def test_batch_is_applied_in_ascending_frame_order():
    guard, epoch = primed()
    out = guard.accept_batch(ACCOUNT, batch(epoch, frame(13), frame(11), frame(12)))
    assert out == {"status": "applied", "accepted": 3}
    assert [item["frame"] for item in guard.applied_inputs(ACCOUNT)] == [11, 12, 13]
    assert guard.applied_frame(ACCOUNT) == 13
    assert guard.stats()["accepted"] == 3


def test_duplicate_frames_apply_once():
    guard, epoch = primed()
    first = guard.accept_batch(ACCOUNT, batch(epoch, frame(11), frame(11), frame(12)))
    assert first == {"status": "applied", "accepted": 2}
    assert guard.stats()["duplicates"] == 1
    second = guard.accept_batch(ACCOUNT, batch(epoch, frame(11), frame(13)))
    assert second == {"status": "applied", "accepted": 1}
    assert guard.stats()["duplicates"] == 2
    assert [item["frame"] for item in guard.applied_inputs(ACCOUNT)] == [11, 12, 13]
    assert guard.applied_frame(ACCOUNT) == 13


def test_frames_at_or_before_the_authority_frame_are_stale():
    guard, epoch = primed()
    out = guard.accept_batch(ACCOUNT, batch(epoch, frame(9), frame(10), frame(12)))
    assert out == {"status": "applied", "accepted": 1}
    assert [item["frame"] for item in guard.applied_inputs(ACCOUNT)] == [12]
    assert guard.stats()["stale"] == 2
    late = guard.accept_batch(ACCOUNT, batch(epoch, frame(10), frame(11)))
    assert late == {"status": "stale", "accepted": 0}
    assert guard.applied_frame(ACCOUNT) == 12


def test_batch_beyond_the_replay_window_rejects_everything():
    guard, epoch = primed(replay_window=5)
    guard.accept_batch(ACCOUNT, batch(epoch, frame(12)))
    with pytest.raises(rag.ResumeError) as exc:
        guard.accept_batch(ACCOUNT, batch(epoch, frame(16), frame(18)))
    assert exc.value.code == "RESUME_WINDOW_EXCEEDED"
    assert exc.value.detail["require_baseline"] is True
    assert guard.applied_frame(ACCOUNT) == 12
    assert [item["frame"] for item in guard.applied_inputs(ACCOUNT)] == [12]
    assert guard.stats()["rejected"] == 1


def test_replay_window_boundary_is_inclusive():
    guard, epoch = primed(replay_window=5)
    out = guard.accept_batch(ACCOUNT, batch(epoch, frame(15)))
    assert out == {"status": "applied", "accepted": 1}
    assert guard.applied_frame(ACCOUNT) == 15


def test_one_shot_events_fire_once_during_replay():
    guard, epoch = primed()
    payload = batch(epoch, frame(11, ["jump"]), frame(12, ["shoot"]))
    guard.accept_batch(ACCOUNT, payload)
    assert guard.fired_events(ACCOUNT) == [
        {"frame": 11, "event": "jump"},
        {"frame": 12, "event": "shoot"},
    ]
    guard.accept_batch(ACCOUNT, payload)
    assert guard.fired_events(ACCOUNT) == [
        {"frame": 11, "event": "jump"},
        {"frame": 12, "event": "shoot"},
    ]
    assert guard.stats()["events"] == 2


def test_late_packets_from_a_previous_epoch_are_discarded():
    guard = make()
    first = start(guard)
    install(guard, first)
    guard.accept_batch(ACCOUNT, batch(first, frame(11)))
    second = start(guard)
    with pytest.raises(rag.ResumeError) as exc:
        guard.accept_batch(ACCOUNT, batch(first, frame(12)))
    assert exc.value.code == "RESUME_STALE_EPOCH"
    assert guard.stats()["discarded"] == 1
    assert guard.session_epoch(ACCOUNT) == second


def test_closed_session_rejects_trailing_batches():
    guard = make()
    epoch = start(guard)
    install(guard, epoch)
    assert guard.close_session(ACCOUNT) == {"account": ACCOUNT, "epoch": epoch}
    with pytest.raises(rag.ResumeError) as exc:
        guard.accept_batch(ACCOUNT, batch(epoch, frame(12)))
    assert exc.value.code == "RESUME_STALE_EPOCH"


def test_stats_and_reset():
    guard, epoch = primed()
    guard.accept_batch(ACCOUNT, batch(epoch, frame(11), frame(12)))
    guard.accept_batch(ACCOUNT, batch(epoch, frame(11), frame(12)))
    assert guard.stats() == {
        "sessions": 1,
        "baselines": 1,
        "batches": 2,
        "accepted": 2,
        "duplicates": 2,
        "stale": 0,
        "rejected": 0,
        "events": 0,
        "discarded": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "sessions": 0,
        "baselines": 0,
        "batches": 0,
        "accepted": 0,
        "duplicates": 0,
        "stale": 0,
        "rejected": 0,
        "events": 0,
        "discarded": 0,
    }
    assert guard.applied_frame(ACCOUNT) == 12


def test_snapshot_restore_keeps_session_state():
    clock = Clock()
    guard = make(clock=clock)
    epoch = start(guard)
    install(guard, epoch, frame=10, state={"pos": 4})
    guard.accept_batch(ACCOUNT, batch(epoch, frame(11, ["jump"]), frame(12)))
    state = guard.snapshot()
    locked = copy.deepcopy(state)
    assert state["version"] == 1
    guard.begin_session(ACCOUNT)
    assert state == locked
    twin = make(clock=clock)
    twin.restore(state)
    assert twin.session_epoch(ACCOUNT) == epoch
    assert twin.applied_frame(ACCOUNT) == 12
    assert twin.baseline_of(ACCOUNT) == {"pos": 4}
    assert twin.fired_events(ACCOUNT) == [{"frame": 11, "event": "jump"}]
    out = twin.accept_batch(ACCOUNT, batch(epoch, frame(13)))
    assert out == {"status": "applied", "accepted": 1}


def test_corrupt_snapshot_is_rejected_without_damage():
    guard, epoch = primed()
    guard.accept_batch(ACCOUNT, batch(epoch, frame(11)))
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(rag.ResumeError) as exc:
            guard.restore(bad)
        assert exc.value.code == "RESUME_SNAPSHOT_INVALID"
    assert guard.applied_frame(ACCOUNT) == 11
    out = guard.accept_batch(ACCOUNT, batch(epoch, frame(12)))
    assert out == {"status": "applied", "accepted": 1}


def test_concurrent_duplicate_batches_apply_once():
    guard, epoch = primed()
    payload = batch(epoch, frame(11), frame(12), frame(13), frame(14))
    errors = []

    def worker():
        try:
            guard.accept_batch(ACCOUNT, payload)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["accepted"] == 4
    assert guard.stats()["duplicates"] == 28
    assert guard.applied_frame(ACCOUNT) == 14
    assert [item["frame"] for item in guard.applied_inputs(ACCOUNT)] == [11, 12, 13, 14]
