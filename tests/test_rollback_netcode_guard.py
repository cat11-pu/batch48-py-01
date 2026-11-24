import copy
import threading

import pytest

import rollback_netcode_guard as rng


def make(**kwargs):
    return rng.RollbackNetcode(**kwargs)


def predict_all(guard, frames, value=1):
    results = []
    for frame in frames:
        results.append(guard.predict(frame, value))
    return results


def test_predict_advances_frames_and_accumulates_state():
    guard = make()
    assert guard.current_frame() == 0
    assert guard.predict(1, 5) == {"frame": 1, "pos": 5, "confirmed": False}
    assert guard.predict(2, 7) == {"frame": 2, "pos": 12, "confirmed": False}
    assert guard.current_frame() == 2
    assert guard.state() == {"frame": 2, "pos": 12}


def test_predict_requires_the_next_frame():
    guard = make()
    guard.predict(1, 1)
    with pytest.raises(rng.RollbackError) as exc:
        guard.predict(3, 1)
    assert exc.value.code == "ROLLBACK_FRAME_GAP"
    assert guard.state() == {"frame": 1, "pos": 1}


def test_confirmed_frame_requires_contiguous_remote_inputs():
    guard = make()
    predict_all(guard, (1, 2, 3))
    assert guard.confirmed_frame() == 0
    assert guard.on_remote_input(2, 1) == {"status": "none", "frame": 2}
    assert guard.confirmed_frame() == 0
    assert guard.on_remote_input(1, 1) == {"status": "none", "frame": 1}
    assert guard.confirmed_frame() == 2


def test_matching_remote_input_needs_no_rollback():
    guard = make()
    guard.predict(1, 3)
    guard.predict(2, 4)
    out = guard.on_remote_input(2, 4)
    assert out == {"status": "none", "frame": 2}
    assert guard.state() == {"frame": 2, "pos": 7}
    assert guard.stats()["rollbacks"] == 0


def test_divergent_remote_input_rolls_back_and_resimulates():
    guard = make()
    guard.predict(1, 1)
    guard.predict(2, 2)
    guard.predict(3, 3)
    out = guard.on_remote_input(2, 10)
    assert out["status"] == "rolled_back"
    assert out["from"] == 2
    assert out["depth"] == 1
    assert out["pos"] == 14
    assert guard.state() == {"frame": 3, "pos": 14}


def test_rollback_depth_equal_to_window_is_allowed():
    guard = make(window=3)
    predict_all(guard, (1, 2, 3, 4))
    out = guard.on_remote_input(1, 5)
    assert out["status"] == "rolled_back"
    assert out["from"] == 1
    assert out["depth"] == 3
    assert guard.state() == {"frame": 4, "pos": 8}


def test_future_remote_input_is_buffered_then_used():
    guard = make()
    guard.predict(1, 1)
    out = guard.on_remote_input(3, 7)
    assert out == {"status": "buffered", "frame": 3}
    assert guard.stats()["buffered"] == 1
    guard.predict(2, 1)
    assert guard.predict(3, 5) == {"frame": 3, "pos": 9, "confirmed": True}
    assert guard.on_remote_input(3, 7) == {"status": "none", "frame": 3}


def test_earlier_confirmed_frame_forces_a_deeper_rollback():
    guard = make()
    predict_all(guard, (1, 2, 3, 4, 5))
    guard.on_remote_input(4, 5)
    assert guard.state() == {"frame": 5, "pos": 9}
    out = guard.on_remote_input(2, 3)
    assert out["from"] == 2
    assert out["depth"] == 3
    assert guard.state() == {"frame": 5, "pos": 11}


def test_digest_mismatch_is_reported():
    guard = make()
    guard.predict(1, 4)
    guard.predict(2, 6)
    local = guard.digest(2)
    out = guard.on_server_digest(2, local + 1)
    assert out == {"status": "divergent", "frame": 2, "local": local, "remote": local + 1}
    assert guard.stats()["divergences"] == 1


def test_digest_match_is_quiet():
    guard = make()
    guard.predict(1, 4)
    guard.predict(2, 6)
    local = guard.digest(2)
    out = guard.on_server_digest(2, local)
    assert out == {"status": "ok", "frame": 2, "local": local, "remote": local}
    assert guard.stats()["divergences"] == 0


def test_stats_track_rollbacks_and_resimulation_cost():
    guard = make()
    predict_all(guard, (1, 2, 3))
    guard.on_remote_input(2, 9)
    guard.on_remote_input(3, 1)
    local = guard.digest(3)
    guard.on_server_digest(3, local)
    guard.on_server_digest(3, local + 1)
    assert guard.stats() == {
        "predicts": 3,
        "rollbacks": 1,
        "rolled_frames": 2,
        "resyncs": 0,
        "divergences": 1,
        "buffered": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "predicts": 0,
        "rollbacks": 0,
        "rolled_frames": 0,
        "resyncs": 0,
        "divergences": 0,
        "buffered": 0,
    }


def test_snapshot_restore_continues_the_same_timeline():
    guard = make()
    guard.predict(1, 1)
    guard.on_remote_input(1, 1)
    guard.predict(2, 2)
    guard.on_remote_input(2, 5)
    state = guard.snapshot()
    locked = copy.deepcopy(state)
    assert state["version"] == 1
    guard.predict(3, 3)
    assert state == locked
    twin = make()
    twin.restore(state)
    assert twin.state() == {"frame": 2, "pos": 6}
    assert twin.confirmed_frame() == 2
    assert twin.digest(2) == guard.digest(2)
    assert twin.predict(3, 3) == {"frame": 3, "pos": 9, "confirmed": False}


def test_corrupt_snapshot_is_rejected_without_damage():
    guard = make()
    guard.predict(1, 2)
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(rng.RollbackError) as exc:
            guard.restore(bad)
        assert exc.value.code == "ROLLBACK_SNAPSHOT_INVALID"
    assert guard.state() == {"frame": 1, "pos": 2}
    assert guard.predict(2, 3) == {"frame": 2, "pos": 5, "confirmed": False}


def test_concurrent_duplicate_remote_input_rolls_back_once():
    guard = make()
    predict_all(guard, (1, 2, 3))
    errors = []

    def worker():
        try:
            guard.on_remote_input(2, 9)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["rollbacks"] == 1
    assert guard.stats()["rolled_frames"] == 2
    assert guard.state() == {"frame": 3, "pos": 11}


def test_reset_clears_the_timeline_but_keeps_stats():
    guard = make()
    guard.predict(1, 1)
    guard.predict(2, 2)
    before = guard.stats()["predicts"]
    guard.reset()
    assert guard.state() == {"frame": 0, "pos": 0}
    assert guard.confirmed_frame() == 0
    assert guard.stats()["predicts"] == before
    assert guard.predict(1, 4) == {"frame": 1, "pos": 4, "confirmed": False}
