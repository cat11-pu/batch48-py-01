import pytest

import perception_alert_guard as pg


def guard(**overrides):
    kwargs = {
        "view_range": 15.0,
        "view_angle_deg": 90.0,
        "memory_ms": 4000,
        "alarm_ttl_ms": 6000,
    }
    kwargs.update(overrides)
    return pg.PerceptionGuard(**kwargs)


def sight(distance=10.0, bearing=20.0, blocked=False):
    return {"distance": distance, "bearing": bearing, "blocked": blocked}


def test_fresh_state_is_patrol():
    g = guard()
    assert g.State("guard1", 1000) == {
        "npc": "guard1",
        "level": "patrol",
        "last_seen": None,
        "target": None,
        "sources": [],
        "decay_at": None,
    }
    assert g.Stats() == {
        "observed": 0,
        "heard": 0,
        "propagated": 0,
        "decays": 0,
    }


def test_observe_inside_cone_raises_to_combat():
    g = guard()
    out = g.Observe("guard1", "thief", sight(10.0, 20.0, False), 1000)
    assert out["visible"] is True
    assert out["reason"] is None
    assert out["effective_distance"] == 10.0
    assert out["level"] == "combat"
    state = g.State("guard1", 1000)
    assert state["level"] == "combat"
    assert state["last_seen"] == 1000
    assert state["target"] == "thief"
    assert state["sources"] == ["sight:thief"]
    assert state["decay_at"] == 5000
    assert g.Stats()["observed"] == 1


def test_observe_blocks_beyond_range():
    g = guard()
    out = g.Observe("guard1", "thief", sight(20.0, 0.0, False), 1000)
    assert out["visible"] is False
    assert out["reason"] == "range"
    assert out["level"] == "patrol"
    state = g.State("guard1", 1000)
    assert state["level"] == "patrol"
    assert state["sources"] == []


def test_observe_blocks_outside_angle():
    g = guard()
    out = g.Observe("guard1", "thief", sight(5.0, 60.0, False), 1000)
    assert out["visible"] is False
    assert out["reason"] == "angle"
    assert out["level"] == "patrol"


def test_observe_blocks_when_occluded():
    g = guard()
    out = g.Observe("guard1", "thief", sight(5.0, 0.0, True), 1000)
    assert out["visible"] is False
    assert out["reason"] == "blocked"
    assert g.State("guard1", 1000)["level"] == "patrol"


def test_stealth_factor_defeats_in_range_sight():
    g = guard()
    out = g.SetStealth("thief", 2.0)
    assert out["target"] == "thief"
    assert out["factor"] == 2.0
    seen = g.Observe("guard1", "thief", sight(10.0, 0.0, False), 1000)
    assert seen["visible"] is False
    assert seen["reason"] == "stealth"
    assert seen["effective_distance"] == 20.0
    assert g.State("guard1", 1000)["level"] == "patrol"
    with pytest.raises(pg.PerceptionError) as exc:
        g.SetStealth("thief", 0.0)
    assert exc.value.code == "PERCEPTION_STEALTH_INVALID"


def test_hear_within_loudness_raises_to_search():
    g = guard()
    out = g.Hear("guard1", {"origin": "bell", "distance": 8.0, "loudness": 10.0, "at": 1000})
    assert out["heard"] is True
    assert out["reason"] is None
    assert out["level"] == "search"
    state = g.State("guard1", 1000)
    assert state["level"] == "search"
    assert state["sources"] == ["hearing:bell"]
    assert state["decay_at"] == 5000
    assert g.Stats()["heard"] == 1


def test_hear_beyond_loudness_is_ignored():
    g = guard()
    out = g.Hear("guard1", {"origin": "bell", "distance": 30.0, "loudness": 10.0, "at": 1000})
    assert out["heard"] is False
    assert out["reason"] == "faint"
    assert out["level"] == "patrol"
    state = g.State("guard1", 1000)
    assert state["level"] == "patrol"
    assert state["sources"] == []


def test_alert_rejects_unknown_level_and_sets_known_level():
    g = guard()
    with pytest.raises(pg.PerceptionError) as exc:
        g.Alert("guard1", "suspicious", 1000)
    assert exc.value.code == "PERCEPTION_LEVEL_INVALID"
    out = g.Alert("guard1", "alert", 1000)
    assert out["level"] == "alert"
    state = g.State("guard1", 1000)
    assert state["level"] == "alert"
    assert state["decay_at"] == 5000


def test_propagate_raises_neighbours_and_records_source():
    g = guard()
    g.Place("guard1", (0.0, 0.0))
    g.Place("guard2", (5.0, 0.0))
    g.Place("guard3", (30.0, 0.0))
    g.Alert("guard1", "alert", 1000)
    out = g.Propagate("guard1", 10.0, 1000)
    assert out["propagated_to"] == ["guard2"]
    assert out["count"] == 1
    near = g.State("guard2", 1000)
    assert near["level"] == "alert"
    assert near["sources"] == ["alarm:guard1"]
    far = g.State("guard3", 1000)
    assert far["level"] == "patrol"
    assert g.Stats()["propagated"] == 1


def test_propagate_from_patrol_is_noop():
    g = guard()
    g.Place("guard1", (0.0, 0.0))
    g.Place("guard2", (5.0, 0.0))
    out = g.Propagate("guard1", 100.0, 1000)
    assert out == {"propagated_to": [], "count": 0}
    assert g.State("guard2", 1000)["level"] == "patrol"
    assert g.Stats()["propagated"] == 0


def test_memory_decays_one_level_per_interval():
    g = guard()
    g.Observe("guard1", "thief", sight(10.0, 0.0, False), 1000)
    before = g.State("guard1", 4999)
    assert before["level"] == "combat"
    assert g.Stats()["decays"] == 0
    after = g.State("guard1", 5000)
    assert after["level"] == "alert"
    assert after["last_seen"] == 1000
    assert after["decay_at"] == 9000
    assert g.Stats()["decays"] == 1
    again = g.State("guard1", 5000)
    assert again["level"] == "alert"
    assert g.Stats()["decays"] == 1


def test_forget_clears_level_target_sources_and_anchor():
    g = guard()
    g.Observe("guard1", "thief", sight(10.0, 0.0, False), 1000)
    g.Hear("guard1", {"origin": "bell", "distance": 5.0, "loudness": 10.0, "at": 1200})
    out = g.Forget("guard1")
    assert out["level"] == "patrol"
    assert g.State("guard1", 5000) == {
        "npc": "guard1",
        "level": "patrol",
        "last_seen": None,
        "target": None,
        "sources": [],
        "decay_at": None,
    }


def test_snapshot_restore_roundtrip():
    g = guard()
    g.Observe("guard1", "thief", sight(10.0, 0.0, False), 1000)
    g.Hear("guard1", {"origin": "bell", "distance": 5.0, "loudness": 10.0, "at": 2000})
    snap = g.Snapshot()
    g.Forget("guard1")
    assert g.State("guard1", 2000)["level"] == "patrol"
    fresh = guard()
    fresh.Restore(snap)
    assert fresh.State("guard1", 2000) == {
        "npc": "guard1",
        "level": "combat",
        "last_seen": 1000,
        "target": "thief",
        "sources": ["sight:thief", "hearing:bell"],
        "decay_at": 6000,
    }
    assert fresh.Stats() == {
        "observed": 1,
        "heard": 1,
        "propagated": 0,
        "decays": 0,
    }


def test_restore_rejects_malformed_snapshot():
    g = guard()
    g.Alert("guard1", "combat", 1000)
    with pytest.raises(pg.PerceptionError) as exc:
        g.Restore({"npcs": []})
    assert exc.value.code == "PERCEPTION_SNAPSHOT_INVALID"
    assert g.State("guard1", 1000)["level"] == "combat"
