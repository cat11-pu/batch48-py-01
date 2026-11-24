import copy
import threading

import pytest

import snapshot_interp_guard as sig


def make(**kwargs):
    return sig.SnapshotInterpGuard(**kwargs)


def entity(x, y, vx=0.0, vy=0.0):
    return {"x": x, "y": y, "vx": vx, "vy": vy}


def snapshot(server_time, seq, entities):
    return {"server_time": server_time, "seq": seq, "entities": entities}


def test_ingest_tracks_bounds_and_latest_entity_state():
    guard = make()
    assert guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)})) == {
        "accepted": True,
        "buffered": 1,
    }
    assert guard.ingest(snapshot(1050, 2, {"e1": entity(10, 0)}))["buffered"] == 2
    assert guard.ingest(snapshot(1100, 3, {"e1": entity(20, 0)}))["buffered"] == 3
    assert guard.bounds() == (1000, 1100, 3)
    assert guard.entity_state("e1")["x"] == 20
    assert guard.entity_state("ghost") is None
    assert guard.stats()["ingested"] == 3


def test_duplicate_sequence_is_ignored():
    guard = make()
    guard.ingest(snapshot(1000, 7, {"e1": entity(0, 0)}))
    out = guard.ingest(snapshot(1100, 7, {"e1": entity(99, 99)}))
    assert out == {"accepted": False, "reason": "duplicate"}
    assert guard.bounds() == (1000, 1000, 1)
    assert guard.stats()["duplicates"] == 1
    assert guard.stats()["ingested"] == 1


def test_timestamp_regression_is_rejected():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)}))
    with pytest.raises(sig.InterpError) as exc:
        guard.ingest(snapshot(999, 2, {"e1": entity(1, 1)}))
    assert exc.value.code == "INTERP_TIME_REGRESSION"
    assert guard.stats()["rejected"] == 1
    assert guard.bounds() == (1000, 1000, 1)


def test_malformed_snapshot_is_refused():
    guard = make()
    malformed = [
        {"seq": 1, "entities": {}},
        {"server_time": 1000, "entities": {}},
        {"server_time": 1000, "seq": 1, "entities": []},
    ]
    for bad in malformed:
        with pytest.raises(sig.InterpError) as exc:
            guard.ingest(bad)
        assert exc.value.code == "INTERP_BAD_SNAPSHOT"
    assert guard.stats()["rejected"] == 3
    assert guard.stats()["ingested"] == 0


def test_sample_requires_a_snapshot():
    guard = make()
    with pytest.raises(sig.InterpError) as exc:
        guard.sample(1000)
    assert exc.value.code == "INTERP_EMPTY"
    assert guard.stats()["samples"] == 0


def test_sample_interpolates_between_two_snapshots():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0, 0.1, 0.2)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 40, 0.1, 0.2)}))
    out = guard.sample(1100)
    assert out["mode"] == "interp"
    assert out["entities"]["e1"]["x"] == pytest.approx(10.0)
    assert out["entities"]["e1"]["y"] == pytest.approx(20.0)
    assert out["entities"]["e1"]["snapped"] is False
    assert guard.stats()["samples"] == 1


def test_sample_clamps_before_the_oldest_snapshot():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(4, 8)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 40)}))
    out = guard.sample(900)
    assert out["mode"] == "clamp"
    assert out["entities"]["e1"]["x"] == pytest.approx(4.0)
    assert out["entities"]["e1"]["y"] == pytest.approx(8.0)


def test_extrapolation_is_capped_and_then_frozen():
    guard = make(max_extrapolation=250)
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0, 0.1, 0.2)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 40, 0.1, 0.2)}))
    ahead = guard.sample(1300)
    assert ahead["mode"] == "extrap"
    assert ahead["entities"]["e1"]["x"] == pytest.approx(30.0)
    assert ahead["entities"]["e1"]["y"] == pytest.approx(60.0)
    held = guard.sample(1600)
    assert held["mode"] == "frozen"
    assert held["entities"]["e1"]["x"] == pytest.approx(45.0)
    assert held["entities"]["e1"]["y"] == pytest.approx(90.0)


def test_entity_appearing_only_on_the_right_side_is_not_interpolated():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 0), "e2": entity(500, 500)}))
    out = guard.sample(1100)
    assert out["mode"] == "interp"
    assert out["entities"]["e2"] == {"x": 500.0, "y": 500.0, "snapped": False}
    assert out["entities"]["e1"]["x"] == pytest.approx(10.0)


def test_despawn_stops_rendering_from_the_event_time():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 0)}))
    assert guard.despawn("e1", 1100) == {"entity": "e1", "at": 1100}
    assert "e1" in guard.sample(1050)["entities"]
    assert "e1" not in guard.sample(1100)["entities"]
    assert guard.stats()["despawns"] == 1


def test_teleport_snaps_until_a_newer_snapshot_arrives():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0, 0.1, 0.1)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 20, 0.1, 0.1)}))
    assert guard.teleport("e1", 900.0, -40.0, 1150) == {
        "entity": "e1",
        "at": 1150,
        "x": 900.0,
        "y": -40.0,
    }
    snapped = guard.sample(1180)
    assert snapped["entities"]["e1"] == {"x": 900.0, "y": -40.0, "snapped": True}
    guard.ingest(snapshot(1300, 3, {"e1": entity(950, 0, 0.0, 0.0)}))
    blended = guard.sample(1250)
    assert blended["mode"] == "interp"
    assert blended["entities"]["e1"]["snapped"] is False


def test_stats_shape_and_reset():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 0)}))
    guard.sample(1100)
    guard.teleport("e1", 1.0, 2.0, 1100)
    guard.despawn("e1", 1190)
    counters = guard.stats()
    assert set(counters) == {
        "ingested",
        "duplicates",
        "rejected",
        "samples",
        "teleports",
        "despawns",
    }
    assert counters["ingested"] == 2
    assert counters["samples"] == 1
    assert counters["teleports"] == 1
    assert counters["despawns"] == 1
    guard.reset_stats()
    assert guard.stats() == {
        "ingested": 0,
        "duplicates": 0,
        "rejected": 0,
        "samples": 0,
        "teleports": 0,
        "despawns": 0,
    }


def test_snapshot_restore_recovers_the_timeline():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 0)}))
    guard.teleport("e1", 5.0, 5.0, 1100)
    state = guard.snapshot()
    locked = copy.deepcopy(state)
    assert state["version"] == 1
    guard.ingest(snapshot(1400, 3, {"e1": entity(40, 0)}))
    assert state == locked
    twin = make()
    twin.restore(state)
    assert twin.bounds() == (1000, 1200, 2)
    assert twin.sample(1150)["entities"]["e1"] == {"x": 5.0, "y": 5.0, "snapped": True}
    assert twin.stats()["ingested"] == 2
    assert twin.stats()["teleports"] == 1


def test_corrupt_snapshot_leaves_state_usable():
    guard = make()
    guard.ingest(snapshot(1000, 1, {"e1": entity(0, 0)}))
    guard.ingest(snapshot(1200, 2, {"e1": entity(20, 0)}))
    for bad in (None, [], {}, {"version": 2}):
        with pytest.raises(sig.InterpError) as exc:
            guard.restore(bad)
        assert exc.value.code == "INTERP_SNAPSHOT_INVALID"
    assert guard.bounds() == (1000, 1200, 2)
    assert guard.sample(1100)["mode"] == "interp"


def test_concurrent_sampling_counts_every_call():
    guard = make(buffer_size=64)
    for index in range(20):
        guard.ingest(snapshot(1000 + index * 10, index + 1, {"e1": entity(float(index), 0.0)}))
    errors = []

    def worker():
        try:
            for index in range(10):
                out = guard.sample(1010 + index * 10)
                assert out["mode"] == "interp"
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["samples"] == 80
    assert guard.stats()["ingested"] == 20
