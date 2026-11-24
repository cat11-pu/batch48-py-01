import threading

import pytest

import threat_table_selector as tts


def make(**kwargs):
    settings = {
        "heal_factor": 0.5,
        "melee_range": 5.0,
        "swap_margin": 0.15,
        "decay_per_second": 10.0,
    }
    settings.update(kwargs)
    return tts.ThreatSelector(**settings)


def boss(selector, sources):
    selector.add_mob("boss", position=(0.0, 0.0))
    for source, position in sources:
        selector.add_source("boss", source, position=position)


def test_registration_validates_duplicates_and_positions():
    selector = make()
    assert selector.add_mob("boss", position=(0.0, 0.0)) == {
        "mob": "boss",
        "position": (0.0, 0.0),
        "sources": 0,
    }
    assert selector.add_source("boss", "tank", position=(2.0, 0.0)) == {
        "mob": "boss",
        "source": "tank",
        "position": (2.0, 0.0),
        "alive": True,
    }
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_mob("boss")
    assert exc.value.code == "TH_DUPLICATE_MOB"
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_source("boss", "tank")
    assert exc.value.code == "TH_DUPLICATE_SOURCE"
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_source("ghost", "tank")
    assert exc.value.code == "TH_UNKNOWN_MOB"
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_mob("weird", position=(float("nan"), 0.0))
    assert exc.value.code == "TH_INVALID_POSITION"


def test_damage_threat_accumulates_and_table_reports_distances():
    selector = make()
    boss(selector, [("a", (1.0, 0.0)), ("b", (2.0, 0.0))])
    out = selector.add_threat("boss", "a", 40, "damage", now=0.0)
    assert out == {
        "mob": "boss",
        "source": "a",
        "amount": 40.0,
        "kind": "damage",
        "threat": 40.0,
        "total": 40.0,
    }
    selector.add_threat("boss", "a", 10, "damage", now=1.0)
    assert selector.threat("boss", "a") == 50.0
    assert selector.table("boss") == [
        {"source": "a", "threat": 50.0, "distance": 1.0, "alive": True},
        {"source": "b", "threat": 0.0, "distance": 2.0, "alive": True},
    ]


def test_heal_threat_is_scaled_and_kind_is_validated():
    selector = make()
    boss(selector, [("healer", (1.0, 0.0))])
    out = selector.add_threat("boss", "healer", 100, "heal", now=0.0)
    assert out["amount"] == 50.0
    assert out["kind"] == "heal"
    assert out["threat"] == 50.0
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_threat("boss", "healer", 10, "mana", now=0.0)
    assert exc.value.code == "TH_INVALID_KIND"
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_threat("boss", "healer", 0, "damage", now=0.0)
    assert exc.value.code == "TH_INVALID_AMOUNT"


def test_select_returns_highest_threat_with_reason():
    selector = make()
    boss(selector, [("a", (1.0, 0.0)), ("b", (2.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    selector.add_threat("boss", "b", 40, "damage", now=0.0)
    assert selector.select("boss", 0.0) == {
        "mob": "boss",
        "target": "a",
        "threat": 100.0,
        "reason": "highest",
    }


def test_select_skips_sources_outside_melee_range():
    selector = make(melee_range=5.0)
    boss(selector, [("far", (9.0, 0.0)), ("near", (1.0, 0.0))])
    selector.add_threat("boss", "far", 500, "damage", now=0.0)
    selector.add_threat("boss", "near", 40, "damage", now=0.0)
    assert selector.select("boss", 0.0)["target"] == "near"
    selector.add_mob("lonely", position=(0.0, 0.0))
    selector.add_source("lonely", "out", position=(20.0, 0.0))
    selector.add_threat("lonely", "out", 10, "damage", now=0.0)
    assert selector.select("lonely", 0.0) == {
        "mob": "lonely",
        "target": None,
        "threat": 0.0,
        "reason": "none",
    }


def test_taunt_forces_target_while_active():
    selector = make()
    boss(selector, [("a", (1.0, 0.0)), ("b", (2.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    selector.add_threat("boss", "b", 10, "damage", now=0.0)
    assert selector.select("boss", 0.0)["target"] == "a"
    assert selector.taunt("boss", "b", 5.0, now=1.0) == {
        "mob": "boss",
        "source": "b",
        "until": 6.0,
    }
    assert selector.select("boss", 2.0) == {
        "mob": "boss",
        "target": "b",
        "threat": 10.0,
        "reason": "taunt",
    }


def test_taunt_validates_target_and_duration():
    selector = make()
    boss(selector, [("a", (1.0, 0.0))])
    with pytest.raises(tts.ThreatError) as exc:
        selector.taunt("ghost", "a", 5.0)
    assert exc.value.code == "TH_UNKNOWN_MOB"
    with pytest.raises(tts.ThreatError) as exc:
        selector.taunt("boss", "ghost", 5.0)
    assert exc.value.code == "TH_UNKNOWN_SOURCE"
    with pytest.raises(tts.ThreatError) as exc:
        selector.taunt("boss", "a", 0.0)
    assert exc.value.code == "TH_INVALID_DURATION"


def test_decay_reduces_threat_by_elapsed_seconds():
    selector = make(decay_per_second=10.0)
    boss(selector, [("a", (1.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    assert selector.decay("boss", 4.0) == {
        "mob": "boss",
        "decayed": {"a": 40.0},
        "now": 4.0,
    }
    assert selector.threat("boss", "a") == 60.0


def test_decay_is_idempotent_at_same_instant_and_rejects_backwards_time():
    selector = make()
    boss(selector, [("a", (1.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    selector.decay("boss", 2.0)
    assert selector.threat("boss", "a") == 80.0
    again = selector.decay("boss", 2.0)
    assert sum(again["decayed"].values()) == 0.0
    assert selector.threat("boss", "a") == 80.0
    with pytest.raises(tts.ThreatError) as exc:
        selector.decay("boss", 1.0)
    assert exc.value.code == "TH_STALE_TIME"


def test_current_target_is_kept_until_challenger_exceeds_margin():
    selector = make(swap_margin=0.15)
    boss(selector, [("a", (1.0, 0.0)), ("b", (2.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    assert selector.select("boss", 0.0)["target"] == "a"
    selector.add_threat("boss", "b", 110, "damage", now=0.0)
    kept = selector.select("boss", 0.0)
    assert kept == {
        "mob": "boss",
        "target": "a",
        "threat": 100.0,
        "reason": "hysteresis",
    }
    selector.add_threat("boss", "b", 10, "damage", now=0.0)
    assert selector.select("boss", 0.0) == {
        "mob": "boss",
        "target": "b",
        "threat": 120.0,
        "reason": "highest",
    }


def test_kill_removes_source_and_is_idempotent():
    selector = make()
    boss(selector, [("a", (1.0, 0.0)), ("b", (2.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    selector.add_threat("boss", "b", 50, "damage", now=0.0)
    assert selector.kill("boss", "a") == {
        "mob": "boss",
        "source": "a",
        "removed": True,
        "sources": 1,
    }
    assert selector.select("boss", 0.0)["target"] == "b"
    assert [row["source"] for row in selector.table("boss")] == ["b"]
    assert selector.kill("boss", "a") == {
        "mob": "boss",
        "source": "a",
        "removed": False,
        "sources": 1,
    }


def test_unknown_identifiers_raise_stable_codes():
    selector = make()
    boss(selector, [("a", (1.0, 0.0))])
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_threat("ghost", "a", 10, "damage")
    assert exc.value.code == "TH_UNKNOWN_MOB"
    with pytest.raises(tts.ThreatError) as exc:
        selector.add_threat("boss", "ghost", 10, "damage")
    assert exc.value.code == "TH_UNKNOWN_SOURCE"
    with pytest.raises(tts.ThreatError) as exc:
        selector.kill("boss", "ghost")
    assert exc.value.code == "TH_UNKNOWN_SOURCE"
    with pytest.raises(tts.ThreatError) as exc:
        selector.threat("boss", "ghost")
    assert exc.value.code == "TH_UNKNOWN_SOURCE"
    with pytest.raises(tts.ThreatError) as exc:
        selector.table("ghost")
    assert exc.value.code == "TH_UNKNOWN_MOB"


def test_snapshot_restore_preserves_threat_taunt_and_selection():
    selector = make()
    boss(selector, [("a", (1.0, 0.0)), ("b", (2.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    selector.add_threat("boss", "b", 60, "damage", now=0.0)
    assert selector.select("boss", 0.0)["target"] == "a"
    selector.taunt("boss", "b", 5.0, now=1.0)
    state = selector.snapshot()
    assert selector.select("boss", 2.0)["target"] == "b"
    selector.add_threat("boss", "a", 500, "damage", now=2.0)
    selector.restore(state)
    assert selector.threat("boss", "a") == 100.0
    assert selector.select("boss", 2.0) == {
        "mob": "boss",
        "target": "b",
        "threat": 60.0,
        "reason": "taunt",
    }


def test_invalid_snapshot_leaves_state_untouched():
    selector = make()
    boss(selector, [("a", (1.0, 0.0))])
    selector.add_threat("boss", "a", 100, "damage", now=0.0)
    with pytest.raises(tts.ThreatError) as exc:
        selector.restore({"mobs": 7})
    assert exc.value.code == "TH_SNAPSHOT_INVALID"
    assert selector.threat("boss", "a") == 100.0
    assert selector.select("boss", 0.0)["target"] == "a"


def test_concurrent_threat_updates_and_selections_stay_consistent():
    selector = make()
    sources = [(f"s{i}", (float(i + 1), 0.0)) for i in range(6)]
    boss(selector, sources)
    barrier = threading.Barrier(12)
    errors = []
    targets = []
    guard = threading.Lock()

    def writer(index):
        barrier.wait()
        try:
            selector.add_threat("boss", f"s{index}", 10, "damage", now=0.0)
        except Exception as exc:  # noqa: BLE001 - test records the domain code
            with guard:
                errors.append(getattr(exc, "code", type(exc).__name__))

    def reader():
        barrier.wait()
        try:
            out = selector.select("boss", 0.0)
        except Exception as exc:  # noqa: BLE001 - test records the domain code
            with guard:
                errors.append(getattr(exc, "code", type(exc).__name__))
        else:
            with guard:
                targets.append(out["target"])

    workers = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    workers += [threading.Thread(target=reader) for _ in range(6)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert errors == []
    known = {name for name, _ in sources}
    assert all(target is None or target in known for target in targets)
    assert selector.stats()["threats_added"] == 6
