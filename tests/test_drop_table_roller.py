import random

import pytest

import drop_table_roller as dr

GEAR = [
    {"key": "sword", "weight": 60, "min": 1, "max": 1},
    {"key": "shield", "weight": 40, "min": 1, "max": 2},
    {"key": "cursed", "weight": 0, "min": 1, "max": 1},
]


def roller(**overrides):
    kwargs = {
        "seed": 1337,
        "max_rolls_per_kill": 6,
        "pity_threshold": 8,
    }
    kwargs.update(overrides)
    return dr.DropRoller(**kwargs)


def single_pool_table(**extra):
    spec = {
        "guaranteed": [],
        "pools": [{"name": "gear", "picks": 1, "entries": [dict(e) for e in GEAR]}],
    }
    spec.update(extra)
    return spec


def test_register_and_budget_counts_pool_picks_only():
    r = roller()
    r.RegisterTable(
        "boss",
        {
            "guaranteed": [],
            "pools": [
                {"name": "gear", "picks": 1, "entries": [dict(e) for e in GEAR]},
                {"name": "mat", "picks": 2, "entries": [{"key": "ore", "weight": 100, "min": 1, "max": 3}]},
            ],
        },
    )
    assert r.Budget("boss") == 3
    assert r.Settled("boss", "k0") is False


def test_register_rejects_budget_over_limit():
    r = roller(max_rolls_per_kill=3)
    with pytest.raises(dr.DropTableError) as exc:
        r.RegisterTable(
            "big",
            {"guaranteed": [], "pools": [{"name": "gear", "picks": 4, "entries": [dict(e) for e in GEAR]}]},
        )
    assert exc.value.code == "DROP_BUDGET_EXCEEDED"
    with pytest.raises(dr.DropTableError) as exc:
        r.Roll("big", {"kill_id": "k1"}, random.Random(1))
    assert exc.value.code == "DROP_TABLE_UNKNOWN"


def test_register_rejects_duplicate_and_malformed_specs():
    r = roller()
    r.RegisterTable("boss", single_pool_table())
    with pytest.raises(dr.DropTableError) as exc:
        r.RegisterTable("boss", single_pool_table())
    assert exc.value.code == "DROP_TABLE_ALREADY_REGISTERED"
    with pytest.raises(dr.DropTableError) as exc:
        r.RegisterTable(
            "negative",
            {
                "guaranteed": [],
                "pools": [{"name": "gear", "picks": 1, "entries": [
                    {"key": "x", "weight": -5, "min": 1, "max": 1},
                ]}],
            },
        )
    assert exc.value.code == "DROP_TABLE_INVALID"
    with pytest.raises(dr.DropTableError) as exc:
        r.RegisterTable(
            "allzero",
            {
                "guaranteed": [],
                "pools": [{"name": "gear", "picks": 1, "entries": [
                    {"key": "x", "weight": 0, "min": 1, "max": 1},
                ]}],
            },
        )
    assert exc.value.code == "DROP_TABLE_INVALID"


def test_roll_emits_guaranteed_items():
    r = roller()
    r.RegisterTable(
        "boss",
        {
            "guaranteed": [{"key": "gold", "min": 5, "max": 9}],
            "pools": [{"name": "gear", "picks": 1, "entries": [dict(e) for e in GEAR]}],
        },
    )
    drops = r.Roll("boss", {"kill_id": "k1"}, random.Random(1))
    gold = [d for d in drops if d["key"] == "gold"]
    assert len(gold) == 1
    assert gold[0]["source"] == "guaranteed"
    assert 5 <= gold[0]["amount"] <= 9
    assert all(d["source"] in {"guaranteed", "pool:gear"} for d in drops)
    assert all(d["amount"] >= 1 for d in drops)


def test_zero_weight_entries_are_never_drawn():
    r = roller()
    r.RegisterTable("boss", single_pool_table())
    drawn = set()
    for i in range(20):
        drops = r.Roll("boss", {"kill_id": "k%d" % i}, random.Random(i))
        for drop in drops:
            assert drop["key"] in {"sword", "shield"}
            assert drop["source"] == "pool:gear"
            drawn.add(drop["key"])
    assert "cursed" not in drawn
    assert drawn == {"sword", "shield"}


def test_empty_table_returns_empty_list():
    r = roller()
    r.RegisterTable("void", {"guaranteed": [], "pools": []})
    out = r.Roll("void", {"kill_id": "k1"}, random.Random(2))
    assert out == []
    assert isinstance(out, list)
    assert r.Settled("void", "k1") is True
    assert r.Stats()["kills"] == 1
    assert r.Stats()["drops"] == 0


def test_same_kill_is_settled_once():
    r = roller()
    r.RegisterTable("boss", single_pool_table())
    first = r.Roll("boss", {"kill_id": "k1"}, random.Random(7))
    again = r.Roll("boss", {"kill_id": "k1"}, random.Random(999))
    assert again == first
    assert r.Stats()["kills"] == 1
    again[0]["amount"] = 999
    third = r.Roll("boss", {"kill_id": "k1"}, random.Random(1000))
    assert third == first


def test_kill_context_requires_non_empty_kill_id():
    r = roller()
    r.RegisterTable("boss", single_pool_table())
    for bad in ({}, {"kill_id": ""}, {"kill_id": None}):
        with pytest.raises(dr.DropTableError) as exc:
            r.Roll("boss", bad, random.Random(1))
        assert exc.value.code == "DROP_KILL_INVALID"
    assert r.Stats()["kills"] == 0


def test_unknown_table_and_guarantee_lookup():
    r = roller()
    r.RegisterTable(
        "boss",
        {
            "guaranteed": [{"key": "gold", "min": 1, "max": 1}],
            "pools": [{"name": "gear", "picks": 1, "entries": [dict(e) for e in GEAR]}],
        },
    )
    assert r.Guarantee("boss", "gold") is True
    assert r.Guarantee("boss", "sword") is False
    with pytest.raises(dr.DropTableError) as exc:
        r.Guarantee("nope", "gold")
    assert exc.value.code == "DROP_TABLE_UNKNOWN"
    with pytest.raises(dr.DropTableError) as exc:
        r.Roll("nope", {"kill_id": "k1"}, random.Random(1))
    assert exc.value.code == "DROP_TABLE_UNKNOWN"


def test_pity_counter_and_reset():
    r = roller()
    r.RegisterTable(
        "boss",
        single_pool_table(pity={"key": "relic", "threshold": 3}),
    )
    assert r.Pity("boss", "relic") == 0
    r.Roll("boss", {"kill_id": "k1"}, random.Random(1))
    r.Roll("boss", {"kill_id": "k2"}, random.Random(2))
    assert r.Pity("boss", "relic") == 2
    assert r.ResetPity("boss", "relic") == 2
    assert r.Pity("boss", "relic") == 0
    with pytest.raises(dr.DropTableError) as exc:
        r.Pity("boss", "gold")
    assert exc.value.code == "DROP_PITY_UNKNOWN"


def test_pity_trigger_emits_key_at_threshold():
    r = roller()
    r.RegisterTable(
        "boss",
        single_pool_table(pity={"key": "relic", "threshold": 3}),
    )
    first = r.Roll("boss", {"kill_id": "k1"}, random.Random(1))
    second = r.Roll("boss", {"kill_id": "k2"}, random.Random(2))
    for drops in (first, second):
        assert all(drop["key"] != "relic" for drop in drops)
    third = r.Roll("boss", {"kill_id": "k3"}, random.Random(3))
    relics = [drop for drop in third if drop["key"] == "relic"]
    assert len(relics) == 1
    assert relics[0]["source"] == "pity"
    assert r.Stats()["pity_triggers"] == 1


def test_pity_counters_are_per_table():
    r = roller()
    spec = single_pool_table(pity={"key": "relic", "threshold": 5})
    r.RegisterTable("alpha", spec)
    r.RegisterTable("beta", spec)
    r.Roll("alpha", {"kill_id": "a1"}, random.Random(1))
    r.Roll("alpha", {"kill_id": "a2"}, random.Random(2))
    assert r.Pity("alpha", "relic") == 2
    assert r.Pity("beta", "relic") == 0


def test_snapshot_restore_replays_pending_kill():
    r = roller()
    r.RegisterTable("boss", single_pool_table(pity={"key": "relic", "threshold": 4}))
    first = r.Roll("boss", {"kill_id": "k1"})
    snap = r.Snapshot()
    expected = r.Roll("boss", {"kill_id": "k2"})
    r.Restore(snap)
    assert r.Settled("boss", "k2") is False
    replay = r.Roll("boss", {"kill_id": "k2"})
    assert replay == expected
    assert r.Roll("boss", {"kill_id": "k1"}) == first


def test_restore_rejects_malformed_snapshot():
    r = roller()
    r.RegisterTable("boss", single_pool_table())
    r.Roll("boss", {"kill_id": "k1"}, random.Random(1))
    with pytest.raises(dr.DropTableError) as exc:
        r.Restore({"tables": []})
    assert exc.value.code == "DROP_SNAPSHOT_INVALID"
    assert r.Settled("boss", "k1") is True
    assert r.Stats()["kills"] == 1


def test_stats_report_kills_and_guaranteed_items():
    r = roller()
    r.RegisterTable(
        "boss",
        {
            "guaranteed": [{"key": "gold", "min": 1, "max": 1}],
            "pools": [{"name": "gear", "picks": 1, "entries": [dict(e) for e in GEAR]}],
        },
    )
    r.Roll("boss", {"kill_id": "k1"}, random.Random(1))
    r.Roll("boss", {"kill_id": "k2"}, random.Random(2))
    stats = r.Stats()
    assert set(stats) == {"kills", "drops", "guaranteed", "pity_triggers"}
    assert stats["kills"] == 2
    assert stats["guaranteed"] == 2
    assert stats["pity_triggers"] == 0
