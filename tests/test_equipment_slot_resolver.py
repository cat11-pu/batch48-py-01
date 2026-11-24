import copy
import threading

import pytest

import equipment_slot_resolver as eq


def test_equip_moves_item_from_bag_and_derives_stats():
    resolver = eq.EquipResolver()
    resolver.register_item("blade", part="weapon", stats={"attack": 12})
    resolver.add_to_bag("hero", "blade")
    resolver.grant_level("hero", 10)
    out = resolver.equip("hero", "blade")
    assert out["slot"] == "weapon"
    assert out["item"] == "blade"
    assert out["version"] == 1
    assert resolver.worn("hero") == {"weapon": "blade"}
    assert resolver.bag("hero") == {}
    stats = resolver.stats("hero")
    assert stats["attack"] == 12
    assert stats["defense"] == 0
    assert stats["version"] == 1
    assert stats["set_pieces"] == {}
    assert resolver.stats_version("hero") == 1


def test_bag_capacity_is_a_hard_limit():
    resolver = eq.EquipResolver(bag_capacity=2)
    resolver.register_item("cap", part="head")
    resolver.register_item("cloak", part="chest")
    assert resolver.add_to_bag("hero", "cap", 2) == 2
    with pytest.raises(eq.EquipError) as exc:
        resolver.add_to_bag("hero", "cloak")
    assert exc.value.code == "EQUIP_BAG_FULL"
    assert resolver.bag("hero") == {"cap": 2}
    assert resolver.set_bag_capacity("hero", 3) == 3
    assert resolver.add_to_bag("hero", "cloak") == 1
    with pytest.raises(eq.EquipError) as exc:
        resolver.add_to_bag("hero", "unknown-piece")
    assert exc.value.code == "EQUIP_UNKNOWN_ITEM"
    assert resolver.bag("hero") == {"cap": 2, "cloak": 1}


def test_level_requirement_blocks_equip_and_reports_reason():
    resolver = eq.EquipResolver()
    resolver.register_item("plate", part="chest", level_req=5,
                           stats={"defense": 9})
    resolver.add_to_bag("hero", "plate")
    resolver.grant_level("hero", 4)
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "plate")
    assert exc.value.code == "EQUIP_LEVEL_TOO_LOW"
    assert resolver.worn("hero") == {}
    assert resolver.bag("hero") == {"plate": 1}
    assert resolver.stats_version("hero") == 0
    assert resolver.grant_level("hero", 5) == 5
    resolver.equip("hero", "plate")
    assert resolver.stats("hero")["defense"] == 9


def test_occupied_slot_rejects_second_item():
    resolver = eq.EquipResolver()
    resolver.register_item("old_helm", part="head", stats={"defense": 2})
    resolver.register_item("new_helm", part="head", stats={"defense": 4})
    resolver.add_to_bag("hero", "old_helm")
    resolver.add_to_bag("hero", "new_helm")
    resolver.equip("hero", "old_helm")
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "new_helm")
    assert exc.value.code == "EQUIP_SLOT_OCCUPIED"
    assert resolver.worn("hero") == {"head": "old_helm"}
    assert resolver.bag("hero") == {"new_helm": 1}
    assert resolver.stats_version("hero") == 1


def test_validate_reports_reasons_without_touching_state():
    resolver = eq.EquipResolver()
    resolver.register_item("boots", part="legs", level_req=3,
                           stats={"defense": 3})
    resolver.add_to_bag("hero", "boots")
    resolver.grant_level("hero", 1)
    blocked = resolver.validate("hero", "boots", "legs")
    assert blocked["ok"] is False
    assert blocked["reasons"] == ["EQUIP_LEVEL_TOO_LOW"]
    assert resolver.worn("hero") == {}
    resolver.grant_level("hero", 3)
    ready = resolver.validate("hero", "boots")
    assert ready["ok"] is True
    assert ready["reasons"] == []
    assert resolver.worn("hero") == {}
    with pytest.raises(eq.EquipError) as exc:
        resolver.validate("hero", "ghost_boots")
    assert exc.value.code == "EQUIP_UNKNOWN_ITEM"


def test_unequip_returns_item_and_drops_enchant_bonus():
    resolver = eq.EquipResolver()
    resolver.register_item("helm", part="head", stats={"defense": 4},
                           enchant={"defense": 1})
    resolver.register_item("sword", part="weapon", stats={"attack": 7})
    resolver.add_to_bag("hero", "helm")
    resolver.add_to_bag("hero", "sword")
    resolver.equip("hero", "helm")
    resolver.equip("hero", "sword")
    assert resolver.stats("hero")["defense"] == 5
    assert resolver.stats("hero")["attack"] == 7
    out = resolver.unequip("hero", "head")
    assert out["item"] == "helm"
    assert resolver.bag("hero") == {"helm": 1}
    assert resolver.stats("hero")["defense"] == 0
    assert resolver.stats("hero")["attack"] == 7
    assert resolver.stats_version("hero") == 3
    with pytest.raises(eq.EquipError) as exc:
        resolver.unequip("hero", "head")
    assert exc.value.code == "EQUIP_UNKNOWN_SLOT"


def test_swap_returns_previous_item_to_bag():
    resolver = eq.EquipResolver()
    resolver.register_item("shortsword", part="weapon", stats={"attack": 5})
    resolver.register_item("longsword", part="weapon", stats={"attack": 11})
    resolver.add_to_bag("hero", "shortsword")
    resolver.add_to_bag("hero", "longsword")
    resolver.equip("hero", "shortsword")
    out = resolver.swap("hero", "weapon", "longsword")
    assert out["removed"] == "shortsword"
    assert out["equipped"] == "longsword"
    assert resolver.worn("hero") == {"weapon": "longsword"}
    assert resolver.bag("hero") == {"shortsword": 1}
    assert resolver.stats("hero")["attack"] == 11
    assert resolver.stats_version("hero") == 2


def test_two_handed_weapon_and_offhand_are_mutually_exclusive():
    resolver = eq.EquipResolver()
    resolver.register_item("greatsword", part="weapon", two_handed=True,
                           stats={"attack": 20})
    resolver.register_item("buckler", part="offhand", stats={"defense": 5})
    resolver.add_to_bag("hero", "greatsword")
    resolver.add_to_bag("hero", "buckler")
    resolver.equip("hero", "greatsword")
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "buckler")
    assert exc.value.code == "EQUIP_TWO_HANDED_CONFLICT"
    assert resolver.worn("hero") == {"weapon": "greatsword"}
    assert resolver.bag("hero") == {"buckler": 1}
    resolver.unequip("hero", "weapon")
    resolver.equip("hero", "buckler")
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "greatsword")
    assert exc.value.code == "EQUIP_TWO_HANDED_CONFLICT"
    assert resolver.worn("hero") == {"offhand": "buckler"}


def test_ring_slots_share_one_item_identity():
    resolver = eq.EquipResolver()
    resolver.register_item("ruby_band", part="ring", stats={"attack": 2},
                           set_id="ember")
    resolver.register_item("opal_band", part="ring", stats={"attack": 3},
                           set_id="ember")
    resolver.register_item("bone_ring", part="ring", stats={"defense": 1})
    resolver.add_to_bag("hero", "ruby_band")
    resolver.add_to_bag("hero", "opal_band")
    resolver.add_to_bag("hero", "bone_ring", 2)
    first = resolver.equip("hero", "ruby_band")
    assert first["slot"] == "ring_left"
    second = resolver.equip("hero", "opal_band")
    assert second["slot"] == "ring_right"
    assert resolver.worn("hero") == {"ring_left": "ruby_band",
                                     "ring_right": "opal_band"}
    assert resolver.stats("hero")["attack"] == 5
    resolver.unequip("hero", "ring_left")
    resolver.equip("hero", "bone_ring", "ring_left")
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "bone_ring", "ring_right")
    assert exc.value.code == "EQUIP_ALREADY_WORN"
    assert resolver.bag("hero") == {"ruby_band": 1, "bone_ring": 1}


def test_set_bonus_counts_distinct_body_parts():
    table = {"iron": {2: {"defense": 5}, 3: {"attack": 4}}}
    resolver = eq.EquipResolver(set_bonus_table=table)
    resolver.register_item("iron_helm", part="head", stats={"defense": 6},
                           set_id="iron")
    resolver.register_item("iron_chest", part="chest", stats={"defense": 8},
                           set_id="iron")
    resolver.register_item("iron_legs", part="legs", stats={"defense": 7},
                           set_id="iron")
    for item_id in ("iron_helm", "iron_chest", "iron_legs"):
        resolver.add_to_bag("hero", item_id)
    resolver.equip("hero", "iron_helm")
    resolver.equip("hero", "iron_chest")
    stats = resolver.stats("hero")
    assert stats["set_pieces"] == {"iron": 2}
    assert stats["defense"] == 19
    assert stats["attack"] == 0
    resolver.equip("hero", "iron_legs")
    stats = resolver.stats("hero")
    assert stats["set_pieces"] == {"iron": 3}
    assert stats["defense"] == 26
    assert stats["attack"] == 4


def test_unequip_recomputes_set_bonus_immediately():
    table = {"iron": {2: {"defense": 5}}}
    resolver = eq.EquipResolver(set_bonus_table=table)
    resolver.register_item("iron_helm", part="head", stats={"defense": 6},
                           set_id="iron")
    resolver.register_item("iron_chest", part="chest", stats={"defense": 8},
                           set_id="iron")
    resolver.add_to_bag("hero", "iron_helm")
    resolver.add_to_bag("hero", "iron_chest")
    resolver.equip("hero", "iron_helm")
    resolver.equip("hero", "iron_chest")
    assert resolver.stats("hero")["defense"] == 19
    resolver.unequip("hero", "chest")
    stats = resolver.stats("hero")
    assert stats["set_pieces"] == {"iron": 1}
    assert stats["defense"] == 6
    assert stats["attack"] == 0
    assert stats["version"] == 3


def test_snapshot_restore_reproduces_worn_and_stats():
    table = {"iron": {2: {"defense": 5}}}
    resolver = eq.EquipResolver(set_bonus_table=table)
    resolver.register_item("iron_helm", part="head", stats={"defense": 6},
                           set_id="iron")
    resolver.register_item("iron_chest", part="chest", stats={"defense": 8},
                           set_id="iron")
    resolver.register_item("spare", part="legs", stats={"defense": 2})
    resolver.add_to_bag("hero", "iron_helm")
    resolver.add_to_bag("hero", "iron_chest")
    resolver.equip("hero", "iron_helm")
    resolver.equip("hero", "iron_chest")
    state = resolver.snapshot()
    pristine = copy.deepcopy(state)
    state["players"]["hero"]["worn"].clear()
    assert resolver.worn("hero") == {"head": "iron_helm", "chest": "iron_chest"}
    restored = eq.EquipResolver(set_bonus_table=table)
    restored.restore(pristine)
    assert restored.worn("hero") == resolver.worn("hero")
    assert restored.bag("hero") == resolver.bag("hero")
    assert restored.stats("hero") == resolver.stats("hero")
    assert restored.stats_version("hero") == resolver.stats_version("hero")
    restored.add_to_bag("hero", "spare")
    restored.equip("hero", "spare")
    assert restored.stats("hero")["version"] == 3


def test_invalid_snapshot_is_rejected_without_changing_state():
    resolver = eq.EquipResolver()
    resolver.register_item("helm", part="head", stats={"defense": 3})
    resolver.add_to_bag("hero", "helm")
    resolver.equip("hero", "helm")
    bad_states = [
        None,
        {},
        {"version": 2, "items": {}, "players": {}},
        {"version": 1, "items": {}, "players": {"hero": "bad"}},
        {"version": 1, "items": {}, "players": {
            "hero": {"level": 1, "bag": {"helm": -1}, "worn": {},
                     "stats_version": 0}}},
    ]
    for bad in bad_states:
        with pytest.raises(eq.EquipError) as exc:
            resolver.restore(bad)
        assert exc.value.code == "EQUIP_SNAPSHOT_INVALID"
    assert resolver.worn("hero") == {"head": "helm"}
    assert resolver.stats("hero")["defense"] == 3


def test_unknown_player_item_and_slot_are_refused():
    resolver = eq.EquipResolver()
    resolver.register_item("ring", part="ring")
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("ghost", "ring")
    assert exc.value.code == "EQUIP_UNKNOWN_PLAYER"
    resolver.add_to_bag("hero", "ring")
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "ring", "tail")
    assert exc.value.code == "EQUIP_INVALID_SLOT"
    with pytest.raises(eq.EquipError) as exc:
        resolver.equip("hero", "ring", "head")
    assert exc.value.code == "EQUIP_INVALID_SLOT"
    with pytest.raises(eq.EquipError) as exc:
        resolver.unequip("hero", "head")
    assert exc.value.code == "EQUIP_UNKNOWN_SLOT"
    with pytest.raises(eq.EquipError) as exc:
        resolver.swap("hero", "head", "ring")
    assert exc.value.code == "EQUIP_UNKNOWN_SLOT"
    assert resolver.bag("hero") == {"ring": 1}


def test_concurrent_equip_into_one_slot_has_single_winner():
    resolver = eq.EquipResolver()
    resolver.register_item("left_helm", part="head", stats={"defense": 1})
    resolver.register_item("right_helm", part="head", stats={"defense": 2})
    resolver.add_to_bag("hero", "left_helm")
    resolver.add_to_bag("hero", "right_helm")
    barrier = threading.Barrier(2)
    outcomes = []

    def worker(item_id):
        barrier.wait()
        try:
            outcomes.append(("ok", resolver.equip("hero", item_id)["item"]))
        except eq.EquipError as exc:
            outcomes.append(("err", exc.code))

    threads = [threading.Thread(target=worker, args=(item_id,))
               for item_id in ("left_helm", "right_helm")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(kind for kind, _ in outcomes) == ["err", "ok"]
    assert [code for kind, code in outcomes if kind == "err"] == [
        "EQUIP_SLOT_OCCUPIED"]
    assert resolver.worn("hero")["head"] in ("left_helm", "right_helm")
    assert resolver.stats_version("hero") == 1
    assert sum(resolver.bag("hero").values()) == 1
