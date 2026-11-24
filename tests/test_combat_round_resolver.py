import copy
import threading

import pytest

import combat_round_resolver as crr


def make(**kwargs):
    settings = {"seed": 7, "max_rounds": 50}
    settings.update(kwargs)
    return crr.CombatRoundResolver(**settings)


def unit(resolver, unit_id, **kwargs):
    stats = {"hp": 30, "attack": 10, "defense": 2}
    stats.update(kwargs)
    return resolver.define_unit(unit_id, **stats)


def test_define_unit_registers_and_rejects_bad_statistics():
    resolver = make()
    unit(resolver, "hero")
    assert resolver.unit_state("hero") == {
        "unit": "hero",
        "hp": 30,
        "max_hp": 30,
        "attack": 10,
        "defense": 2,
        "dodge": 0.0,
        "block": 0.0,
        "crit": 0.0,
        "priority": 0,
        "targets": 1,
        "alive": True,
    }
    with pytest.raises(crr.CombatError) as exc:
        unit(resolver, "hero")
    assert exc.value.code == "CR_DUPLICATE_UNIT"
    with pytest.raises(crr.CombatError) as exc:
        unit(resolver, "wide", dodge=1.5)
    assert exc.value.code == "CR_INVALID_UNIT"
    with pytest.raises(crr.CombatError) as exc:
        unit(resolver, "empty", hp=0)
    assert exc.value.code == "CR_INVALID_UNIT"
    with pytest.raises(crr.CombatError) as exc:
        resolver.unit_state("ghost")
    assert exc.value.code == "CR_UNKNOWN_UNIT"


def test_action_priority_order_overrides_submission_order():
    resolver = make()
    for name in ("a", "b", "c"):
        unit(resolver, name, attack=1, defense=0)
    actions = [
        {"actor": "a", "kind": "move", "target": "c", "priority": 5},
        {"actor": "b", "kind": "attack", "target": "a", "priority": 9},
        {"actor": "c", "kind": "item", "target": "a", "priority": 5},
    ]
    log = resolver.resolve_round(actions)
    assert [entry["actor"] for entry in log["entries"]] == ["b", "a", "c"]
    assert [entry["priority"] for entry in log["entries"]] == [9, 5, 5]


def test_default_kind_priority_orders_skill_before_attack():
    resolver = make()
    unit(resolver, "mage", attack=5, defense=0)
    unit(resolver, "brute", attack=5, defense=0)
    unit(resolver, "target", hp=100, attack=1, defense=0)
    log = resolver.resolve_round([
        {"actor": "brute", "kind": "attack", "target": "target"},
        {"actor": "mage", "kind": "skill", "target": "target", "power": 3},
    ])
    assert [entry["kind"] for entry in log["entries"]] == ["skill", "attack"]
    assert resolver.apply_order() == ["skill", "attack", "item", "move"]


def test_single_attack_damage_and_hp_accounting():
    resolver = make()
    unit(resolver, "hero")
    unit(resolver, "dummy", hp=20, attack=0, defense=2)
    log = resolver.resolve_round([
        {"actor": "hero", "kind": "attack", "target": "dummy"},
    ])
    entry = log["entries"][0]
    assert entry["hit"] is True
    assert entry["dodged"] is False
    assert entry["crit"] is False
    assert entry["blocked"] is False
    assert entry["damage"] == 8
    assert entry["hp_before"] == 20
    assert entry["hp_after"] == 12
    assert log["round_no"] == 1
    assert log["totals"] == {
        "damage": 8,
        "hits": 1,
        "dodges": 0,
        "crits": 0,
        "blocks": 0,
    }


def test_dodge_cancels_all_damage():
    resolver = make()
    unit(resolver, "hero")
    unit(resolver, "rogue", hp=20, attack=0, defense=0, dodge=1.0)
    log = resolver.resolve_round([
        {"actor": "hero", "kind": "attack", "target": "rogue"},
    ])
    entry = log["entries"][0]
    assert entry["dodged"] is True
    assert entry["damage"] == 0
    assert entry["hp_after"] == 20
    assert log["totals"]["dodges"] == 1
    assert resolver.unit_state("rogue")["hp"] == 20


def test_block_halves_damage():
    resolver = make()
    unit(resolver, "hero")
    unit(resolver, "guard", hp=40, attack=0, defense=2, block=1.0)
    log = resolver.resolve_round([
        {"actor": "hero", "kind": "attack", "target": "guard"},
    ])
    entry = log["entries"][0]
    assert entry["blocked"] is True
    assert entry["crit"] is False
    assert entry["damage"] == 4
    assert entry["hp_after"] == 36


def test_crit_doubles_damage():
    resolver = make()
    unit(resolver, "assassin", crit=1.0)
    unit(resolver, "guard", hp=40, attack=0, defense=2, block=0.0)
    log = resolver.resolve_round([
        {"actor": "assassin", "kind": "attack", "target": "guard"},
    ])
    entry = log["entries"][0]
    assert entry["crit"] is True
    assert entry["blocked"] is False
    assert entry["damage"] == 16
    assert entry["hp_after"] == 24


def test_dead_actor_action_is_skipped():
    resolver = make()
    unit(resolver, "hero", attack=50)
    unit(resolver, "dummy", hp=20, attack=5, defense=0)
    resolver.resolve_round([
        {"actor": "hero", "kind": "attack", "target": "dummy"},
    ])
    assert resolver.unit_state("dummy")["alive"] is False
    log = resolver.resolve_round([
        {"actor": "dummy", "kind": "attack", "target": "hero"},
    ])
    entry = log["entries"][0]
    assert entry["status"] == "dead_actor"
    assert entry["damage"] == 0
    assert resolver.unit_state("hero")["hp"] == 30
    assert log["totals"]["hits"] == 0


def test_same_seed_reproduces_identical_logs():
    def build():
        resolver = make(seed=99)
        unit(resolver, "a", crit=0.5)
        unit(resolver, "b", hp=400, attack=0, defense=1,
             dodge=0.3, block=0.3, crit=0.3)
        return resolver

    first = build()
    second = build()
    actions = [{"actor": "a", "kind": "attack", "target": "b"}]
    first_logs = [first.resolve_round(actions) for _ in range(4)]
    second_logs = [second.resolve_round(actions) for _ in range(4)]
    assert first_logs == second_logs
    assert [log["round_no"] for log in first_logs] == [1, 2, 3, 4]


def test_snapshot_restore_replays_identical_round():
    resolver = make(seed=123)
    unit(resolver, "a", crit=0.4)
    unit(resolver, "b", hp=500, attack=0, defense=1, dodge=0.2, block=0.2)
    actions = [{"actor": "a", "kind": "attack", "target": "b"}]
    resolver.resolve_round(actions)
    state = resolver.snapshot()
    expected = resolver.resolve_round(actions)
    resolver.restore(state)
    replayed = resolver.resolve_round(actions)
    assert replayed == expected
    assert replayed["round_no"] == 2


def test_replay_recomputes_totals_and_detects_tampering():
    resolver = make(seed=5)
    unit(resolver, "a")
    unit(resolver, "b", hp=100, attack=0, defense=0, dodge=0.5)
    log = resolver.resolve_round([
        {"actor": "a", "kind": "attack", "target": "b"},
    ])
    summary = resolver.replay(log)
    assert summary["round_no"] == log["round_no"]
    assert summary["totals"] == log["totals"]
    tampered = copy.deepcopy(log)
    tampered["entries"][0]["damage"] += 1
    with pytest.raises(crr.CombatError) as exc:
        resolver.replay(tampered)
    assert exc.value.code == "CR_LOG_TAMPERED"


def test_roll_check_is_reproducible_and_bounded():
    kinds = ("dodge", "crit", "block", "dodge")

    def build():
        resolver = make(seed=42)
        unit(resolver, "a")
        unit(resolver, "b", dodge=0.25, block=0.5, crit=0.75)
        return resolver

    first = build()
    rolls = [first.roll_check("a", "b", kind) for kind in kinds]
    assert [item["kind"] for item in rolls] == list(kinds)
    assert all(0.0 <= item["roll"] < 1.0 for item in rolls)
    assert rolls[0]["chance"] == 0.25
    assert rolls[0]["success"] is (rolls[0]["roll"] < 0.25)
    second = build()
    assert [second.roll_check("a", "b", kind) for kind in kinds] == rolls
    with pytest.raises(crr.CombatError) as exc:
        first.roll_check("a", "b", "parry")
    assert exc.value.code == "CR_INVALID_CHECK"


def test_invalid_actions_and_unknown_units_raise_codes():
    resolver = make()
    unit(resolver, "a")
    with pytest.raises(crr.CombatError) as exc:
        resolver.resolve_round([])
    assert exc.value.code == "CR_EMPTY_ACTIONS"
    with pytest.raises(crr.CombatError) as exc:
        resolver.resolve_round([{"actor": "ghost", "kind": "attack", "target": "a"}])
    assert exc.value.code == "CR_UNKNOWN_UNIT"
    with pytest.raises(crr.CombatError) as exc:
        resolver.resolve_round([{"actor": "a", "kind": "dance", "target": "a"}])
    assert exc.value.code == "CR_INVALID_ACTION"
    with pytest.raises(crr.CombatError) as exc:
        resolver.remove_unit("ghost")
    assert exc.value.code == "CR_UNKNOWN_UNIT"


def test_concurrent_round_submission_accepts_exactly_one():
    resolver = make()
    unit(resolver, "a")
    unit(resolver, "b", hp=200, attack=0, defense=0)
    action = [{"actor": "a", "kind": "attack", "target": "b"}]
    barrier = threading.Barrier(8)
    successes = []
    errors = []
    guard = threading.Lock()

    def submit():
        barrier.wait()
        try:
            log = resolver.resolve_round(action, round_no=1)
        except Exception as exc:  # noqa: BLE001 - test records the domain code
            with guard:
                errors.append(getattr(exc, "code", type(exc).__name__))
        else:
            with guard:
                successes.append(log)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(successes) == 1
    assert sorted(errors) == ["CR_ROUND_COMMITTED"] * 7
    assert [entry["damage"] for entry in successes[0]["entries"]] == [8]


def test_stats_counters_and_reset():
    resolver = make(seed=3)
    unit(resolver, "a")
    unit(resolver, "b", hp=100, attack=0, defense=2)
    resolver.resolve_round([
        {"actor": "a", "kind": "attack", "target": "b"},
    ])
    assert resolver.stats() == {
        "rounds": 1,
        "actions": 1,
        "hits": 1,
        "dodges": 0,
        "crits": 0,
        "blocks": 0,
        "damage": 8,
    }
    resolver.reset_stats()
    assert resolver.stats() == {
        "rounds": 0,
        "actions": 0,
        "hits": 0,
        "dodges": 0,
        "crits": 0,
        "blocks": 0,
        "damage": 0,
    }
