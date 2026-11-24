import pytest

import fishing_minigame_guard as fg

TABLES = {"pond": {"crucian": 70, "carp": 25, "koi": 5}}


def guard(**overrides):
    kwargs = {
        "bite_window_ms": 1200,
        "max_tension": 100,
        "line_durability": 3,
        "tables": TABLES,
    }
    kwargs.update(overrides)
    return fg.FishingMinigameGuard(**kwargs)


def casted(player="p1", spot="pond", now=1000, **overrides):
    g = guard(**overrides)
    g.Cast(player, spot, now)
    return g


def test_fresh_state_is_idle_with_full_durability():
    g = guard()
    state = g.State("p1")
    assert state == {
        "player": "p1",
        "state": "idle",
        "spot": None,
        "cast_id": 0,
        "tension": 0,
        "progress": 0,
        "durability": 3,
        "deadline": None,
    }
    assert g.Stats() == {
        "casts": 0,
        "bites": 0,
        "landed": 0,
        "escaped": 0,
        "released": 0,
    }


def test_cast_returns_waiting_line_and_counts():
    g = guard()
    line = g.Cast("p1", "pond", 1000)
    assert line["player"] == "p1"
    assert line["spot"] == "pond"
    assert line["state"] == "waiting"
    assert line["cast_id"] == 1
    assert line["durability"] == 3
    assert line["deadline"] is None
    assert g.Stats()["casts"] == 1


def test_cast_twice_on_active_line_is_busy():
    g = casted()
    with pytest.raises(fg.FishingError) as exc:
        g.Cast("p1", "pond", 1100)
    assert exc.value.code == "FISH_LINE_BUSY"
    assert g.Stats()["casts"] == 1
    assert g.State("p1")["cast_id"] == 1


def test_bite_deadline_uses_milliseconds():
    g = casted()
    bite = g.Bite("p1", 2000)
    assert bite["state"] == "biting"
    assert bite["bite_at"] == 2000
    assert bite["deadline"] == 3200
    assert bite["window_ms"] == 1200
    assert g.State("p1")["deadline"] == 3200
    assert g.Stats()["bites"] == 1


def test_bite_is_idempotent_and_keeps_deadline():
    g = casted()
    first = g.Bite("p1", 2000)
    second = g.Bite("p1", 2500)
    assert second == first
    assert second["deadline"] == 3200
    assert g.Stats()["bites"] == 1


def test_reel_advances_progress_and_tension():
    g = casted()
    g.Bite("p1", 2000)
    out = g.Reel("p1", 500)
    assert out["progress"] == 10
    assert out["tension"] == 5
    assert out["durability"] == 3
    assert out["state"] == "biting"
    assert out["result"] is None
    out = g.Reel("p1", 1000)
    assert out["progress"] == 30
    assert out["tension"] == 15
    assert out["state"] == "biting"


def test_reel_rejects_non_positive_interval():
    g = casted()
    g.Bite("p1", 2000)
    for bad in (0, -250):
        with pytest.raises(fg.FishingError) as exc:
            g.Reel("p1", bad)
        assert exc.value.code == "FISH_INTERVAL_INVALID"
    assert g.State("p1")["progress"] == 0


def test_land_requires_full_progress():
    g = casted()
    g.Bite("p1", 2000)
    g.Reel("p1", 1000)
    with pytest.raises(fg.FishingError) as exc:
        g.Land("p1", 2500)
    assert exc.value.code == "FISH_NOT_READY"
    assert g.State("p1")["state"] == "biting"
    assert g.Stats()["landed"] == 0


def test_land_returns_settlement_and_resets_line():
    g = casted()
    g.Bite("p1", 2000)
    g.Reel("p1", 5000)
    out = g.Land("p1", 3000)
    assert out["result"] == "landed"
    assert out["species"] in TABLES["pond"]
    assert 200 <= out["weight_g"] <= 999
    assert out["durability"] == 3
    assert out["progress"] == 100
    assert g.State("p1")["state"] == "idle"
    assert g.Stats()["landed"] == 1
    with pytest.raises(fg.FishingError) as exc:
        g.Land("p1", 3001)
    assert exc.value.code == "FISH_ALREADY_SETTLED"


def test_tension_above_limit_snaps_and_blocks_land():
    g = casted()
    g.Bite("p1", 2000)
    out = g.SetTension("p1", 40)
    assert out["tension"] == 40
    assert out["result"] is None
    out = g.SetTension("p1", 61)
    assert out["result"] == "snapped"
    assert out["state"] == "settled"
    assert out["durability"] == 2
    assert g.Stats()["escaped"] == 1
    with pytest.raises(fg.FishingError) as exc:
        g.Land("p1", 2100)
    assert exc.value.code == "FISH_ALREADY_SETTLED"


def test_land_after_window_returns_escape_once():
    g = casted()
    g.Bite("p1", 2000)
    out = g.Land("p1", 3201)
    assert out["result"] == "escaped"
    assert out["cause"] == "window_closed"
    assert out["durability"] == 2
    assert g.Stats()["escaped"] == 1
    with pytest.raises(fg.FishingError) as exc:
        g.Land("p1", 3300)
    assert exc.value.code == "FISH_ALREADY_SETTLED"
    assert g.Stats()["escaped"] == 1


def test_escaped_settles_once():
    g = casted()
    g.Bite("p1", 2000)
    first = g.Escaped("p1")
    second = g.Escaped("p1")
    assert first == second
    assert first["result"] == "escaped"
    assert first["cause"] == "player_escaped"
    assert first["durability"] == 3
    assert g.State("p1")["state"] == "idle"
    assert g.Stats()["escaped"] == 1


def test_release_clears_line_and_allows_recast():
    g = casted()
    out = g.Release("p1")
    assert out["state"] == "idle"
    assert out["released"] is True
    assert out["cast_id"] == 1
    assert g.Stats()["released"] == 1
    line = g.Cast("p1", "pond", 1200)
    assert line["cast_id"] == 2
    assert line["state"] == "waiting"


def test_species_odds_ignore_wall_clock():
    g = guard()
    odds = g.SpeciesOdds("pond")
    assert odds == {"crucian": 70, "carp": 25, "koi": 5}
    assert g.SpeciesOdds("pond", now=10 ** 12) == odds
    odds["carp"] = 99
    assert g.SpeciesOdds("pond") == {"crucian": 70, "carp": 25, "koi": 5}
    with pytest.raises(fg.FishingError) as exc:
        g.SpeciesOdds("lava")
    assert exc.value.code == "FISH_UNKNOWN_SPOT"


def test_snapshot_restore_roundtrip_and_validation():
    g = casted()
    g.Bite("p1", 2000)
    g.Reel("p1", 1000)
    snap = g.Snapshot()
    g.SetTension("p1", 5)
    assert g.State("p1")["tension"] == 15
    fresh = guard()
    fresh.Restore(snap)
    assert fresh.State("p1")["deadline"] == 3200
    assert fresh.State("p1")["progress"] == 20
    assert fresh.State("p1")["tension"] == 10
    assert fresh.State("p1")["state"] == "biting"
    assert fresh.Stats()["bites"] == 1
    with pytest.raises(fg.FishingError) as exc:
        fresh.Restore({"players": []})
    assert exc.value.code == "FISH_SNAPSHOT_INVALID"
    assert fresh.State("p1")["progress"] == 20
