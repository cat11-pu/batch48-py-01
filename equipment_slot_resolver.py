"""Equipment slot resolution, bag accounting, set bonuses and derived stats."""

from __future__ import annotations

from copy import deepcopy
import threading


SLOT_ORDER = (
    "weapon",
    "offhand",
    "head",
    "chest",
    "legs",
    "ring_left",
    "ring_right",
)

PART_OF_SLOT = {
    "weapon": "weapon",
    "offhand": "offhand",
    "head": "head",
    "chest": "chest",
    "legs": "legs",
    "ring_left": "ring",
    "ring_right": "ring",
}

SLOTS_FOR_PART = {}
for _slot in SLOT_ORDER:
    SLOTS_FOR_PART.setdefault(PART_OF_SLOT[_slot], []).append(_slot)


class EquipError(ValueError):
    """Equipment failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _bag_total(bag: dict) -> int:
    return sum(bag.values())


def _numeric_bonus(value: object) -> dict:
    if not isinstance(value, dict):
        return {}
    return {
        key: amount
        for key, amount in value.items()
        if key in ("attack", "defense")
        and isinstance(amount, (int, float))
        and not isinstance(amount, bool)
    }


class EquipResolver:
    """Resolve slot conflicts and recompute derived stats for each player."""

    def __init__(self, *, bag_capacity: int = 8, set_bonus_table=None) -> None:
        self.bag_capacity = self._normalize_capacity(bag_capacity)
        self.set_bonus_table = deepcopy(dict(set_bonus_table or {}))
        self._lock = threading.RLock()
        self._items: dict[str, dict] = {}
        self._players: dict[str, dict] = {}

    @staticmethod
    def _normalize_capacity(capacity: object) -> int:
        if not _is_int(capacity):
            try:
                capacity = int(capacity)  # type: ignore[arg-type]
            except (TypeError, ValueError) as exc:
                raise EquipError("EQUIP_BAG_FULL", {"capacity": capacity}) from exc
        if capacity < 0:
            raise EquipError("EQUIP_BAG_FULL", {"capacity": capacity})
        return capacity

    def register_item(self, item_id: str, *, part: str, level_req: int = 1,
                      two_handed: bool = False, stats=None, enchant=None,
                      set_id=None) -> dict:
        item = {
            "part": part,
            "level_req": level_req,
            "two_handed": bool(two_handed),
            "stats": deepcopy(dict(stats or {})),
            "enchant": deepcopy(dict(enchant)) if enchant is not None else None,
            "set_id": set_id,
        }
        with self._lock:
            self._items[item_id] = item
            return deepcopy(item)

    def _require_item(self, item_id: str) -> dict:
        try:
            return self._items[item_id]
        except KeyError as exc:
            raise EquipError(
                "EQUIP_UNKNOWN_ITEM", {"item": item_id}
            ) from exc

    def _require_player(self, player: str) -> dict:
        try:
            return self._players[player]
        except KeyError as exc:
            raise EquipError(
                "EQUIP_UNKNOWN_PLAYER", {"player": player}
            ) from exc

    def _get_or_create_player(self, player: str) -> dict:
        state = self._players.get(player)
        if state is None:
            state = {
                "level": 1,
                "bag": {},
                "worn": {},
                "stats_version": 0,
                "bag_capacity": self.bag_capacity,
            }
            self._players[player] = state
        return state

    def grant_level(self, player: str, level: int) -> int:
        if not _is_int(level) or level < 0:
            raise EquipError("EQUIP_INVALID_LEVEL", {"level": level})
        with self._lock:
            state = self._get_or_create_player(player)
            state["level"] = level
            return level

    def add_to_bag(self, player: str, item_id: str, count: int = 1) -> int:
        with self._lock:
            self._require_item(item_id)
            if not _is_int(count) or count < 0:
                raise EquipError("EQUIP_INVALID_COUNT", {"count": count})

            state = self._get_or_create_player(player)
            bag = state["bag"]
            current_count = bag.get(item_id, 0)

            if count == 0:
                return current_count

            capacity = state["bag_capacity"]
            if _bag_total(bag) + count > capacity:
                raise EquipError(
                    "EQUIP_BAG_FULL",
                    {
                        "player": player,
                        "item": item_id,
                        "capacity": capacity,
                        "used": _bag_total(bag),
                        "requested": count,
                    },
                )

            new_count = current_count + count
            bag[item_id] = new_count
            return new_count

    def set_bag_capacity(self, player: str, capacity: int) -> int:
        capacity = self._normalize_capacity(capacity)
        with self._lock:
            state = self._get_or_create_player(player)
            used = _bag_total(state["bag"])
            if capacity < used:
                raise EquipError(
                    "EQUIP_BAG_FULL",
                    {"player": player, "capacity": capacity, "used": used},
                )
            state["bag_capacity"] = capacity
            return capacity

    def bag(self, player: str) -> dict:
        with self._lock:
            return dict(self._require_player(player)["bag"])

    def worn(self, player: str) -> dict:
        with self._lock:
            return dict(self._require_player(player)["worn"])

    @staticmethod
    def _find_worn_slot(worn: dict, item_id: str) -> str | None:
        for slot, worn_item in worn.items():
            if worn_item == item_id:
                return slot
        return None

    def _evaluate_equip(self, state: dict, item_id: str,
                        item: dict, requested_slot: str | None
                        ) -> tuple[str | None, list[str]]:
        worn = state["worn"]
        reasons: list[str] = []

        if requested_slot is not None:
            if (requested_slot not in PART_OF_SLOT
                    or PART_OF_SLOT[requested_slot] != item.get("part")):
                reasons.append("EQUIP_INVALID_SLOT")
                target_slot = None
            else:
                target_slot = requested_slot
        else:
            candidates = SLOTS_FOR_PART.get(item.get("part"))
            if not candidates:
                reasons.append("EQUIP_INVALID_SLOT")
                target_slot = None
            elif item.get("part") == "ring":
                target_slot = next(
                    (slot for slot in candidates if slot not in worn),
                    candidates[0],
                )
            else:
                target_slot = candidates[0]

        worn_slot = self._find_worn_slot(worn, item_id)
        if worn_slot is not None:
            reasons.append("EQUIP_ALREADY_WORN")

        if state["level"] < item.get("level_req", 1):
            reasons.append("EQUIP_LEVEL_TOO_LOW")

        if worn_slot is None and state["bag"].get(item_id, 0) <= 0:
            reasons.append("EQUIP_NOT_IN_BAG")

        offhand_item_id = worn.get("offhand")
        if item.get("two_handed") and offhand_item_id is not None:
            reasons.append("EQUIP_TWO_HANDED_CONFLICT")
        elif (item.get("part") == "offhand"
              and worn.get("weapon") is not None):
            weapon = self._items.get(worn["weapon"], {})
            if weapon.get("two_handed"):
                reasons.append("EQUIP_TWO_HANDED_CONFLICT")

        if target_slot is not None:
            occupant = worn.get(target_slot)
            if occupant is not None and occupant != item_id:
                reasons.append("EQUIP_SLOT_OCCUPIED")

        return target_slot, reasons

    def validate(self, player: str, item_id: str,
                 slot: str | None = None) -> dict:
        with self._lock:
            state = self._require_player(player)
            item = self._require_item(item_id)
            _, reasons = self._evaluate_equip(state, item_id, item, slot)
            return {"ok": not reasons, "reasons": reasons}

    def equip(self, player: str, item_id: str,
              slot: str | None = None) -> dict:
        with self._lock:
            state = self._require_player(player)
            item = self._require_item(item_id)
            target_slot, reasons = self._evaluate_equip(
                state, item_id, item, slot
            )
            if reasons:
                raise EquipError(
                    reasons[0],
                    {"player": player, "item": item_id, "reasons": reasons},
                )

            bag = state["bag"]
            if bag[item_id] == 1:
                del bag[item_id]
            else:
                bag[item_id] -= 1

            state["worn"][target_slot] = item_id
            state["stats_version"] += 1
            version = state["stats_version"]
            return {"slot": target_slot, "item": item_id, "version": version}

    def unequip(self, player: str, slot: str) -> dict:
        with self._lock:
            state = self._require_player(player)
            if slot not in PART_OF_SLOT:
                raise EquipError("EQUIP_INVALID_SLOT", {"slot": slot})

            worn = state["worn"]
            item_id = worn.get(slot)
            if item_id is None:
                raise EquipError("EQUIP_UNKNOWN_SLOT", {"slot": slot})

            bag = state["bag"]
            capacity = state["bag_capacity"]
            used = _bag_total(bag)
            if used + 1 > capacity:
                raise EquipError(
                    "EQUIP_BAG_FULL",
                    {"player": player, "slot": slot, "capacity": capacity,
                     "used": used},
                )

            del worn[slot]
            bag[item_id] = bag.get(item_id, 0) + 1
            state["stats_version"] += 1
            return {"item": item_id}

    def swap(self, player: str, slot: str, item_id: str) -> dict:
        with self._lock:
            state = self._require_player(player)
            if slot not in PART_OF_SLOT:
                raise EquipError("EQUIP_INVALID_SLOT", {"slot": slot})

            worn = state["worn"]
            removed_item = worn.get(slot)
            if removed_item is None:
                raise EquipError("EQUIP_UNKNOWN_SLOT", {"slot": slot})

            item = self._require_item(item_id)
            if PART_OF_SLOT[slot] != item.get("part"):
                raise EquipError(
                    "EQUIP_INVALID_SLOT",
                    {"slot": slot, "item": item_id, "part": item.get("part")},
                )

            worn_slot = self._find_worn_slot(worn, item_id)
            if worn_slot is not None and worn_slot != slot:
                raise EquipError(
                    "EQUIP_ALREADY_WORN",
                    {"item": item_id, "slot": worn_slot},
                )

            if state["level"] < item.get("level_req", 1):
                raise EquipError(
                    "EQUIP_LEVEL_TOO_LOW",
                    {"required": item.get("level_req", 1),
                     "level": state["level"]},
                )

            if state["bag"].get(item_id, 0) <= 0:
                raise EquipError(
                    "EQUIP_NOT_IN_BAG", {"player": player, "item": item_id}
                )

            if item.get("two_handed") and worn.get("offhand") is not None:
                raise EquipError(
                    "EQUIP_TWO_HANDED_CONFLICT",
                    {"weapon": item_id, "offhand": worn["offhand"]},
                )
            if item.get("part") == "offhand" and worn.get("weapon"):
                weapon = self._items.get(worn["weapon"], {})
                if weapon.get("two_handed"):
                    raise EquipError(
                        "EQUIP_TWO_HANDED_CONFLICT",
                        {"weapon": worn["weapon"], "offhand": item_id},
                    )

            bag = state["bag"]
            used = _bag_total(bag)
            if used + 1 > state["bag_capacity"]:
                raise EquipError(
                    "EQUIP_BAG_FULL",
                    {"capacity": state["bag_capacity"],
                     "used": used},
                )

            if bag[item_id] == 1:
                del bag[item_id]
            else:
                bag[item_id] -= 1
            bag[removed_item] = bag.get(removed_item, 0) + 1
            worn[slot] = item_id
            state["stats_version"] += 1

            return {"removed": removed_item, "equipped": item_id}

    def _set_bonus(self, set_id: str, pieces: int) -> dict:
        table = self.set_bonus_table.get(set_id, {})
        selected: dict = {}
        eligible = []
        for threshold, bonus in table.items():
            try:
                required = int(threshold)
            except (TypeError, ValueError):
                continue
            if required <= pieces and isinstance(bonus, dict):
                eligible.append((required, bonus))
        for _, bonus in sorted(eligible, key=lambda item: item[0]):
            for stat, value in bonus.items():
                if (stat in ("attack", "defense")
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)):
                    # Higher unlocked tiers replace lower-tier values for the
                    # same statistic; separate statistics remain active.
                    selected[stat] = value
        return selected

    def stats(self, player: str) -> dict:
        with self._lock:
            state = self._require_player(player)
            attack = 0
            defense = 0
            set_parts: dict[str, set] = {}

            for slot, item_id in state["worn"].items():
                item = self._items[item_id]
                for source in (item.get("stats"), item.get("enchant")):
                    bonus = _numeric_bonus(source)
                    attack += bonus.get("attack", 0)
                    defense += bonus.get("defense", 0)

                set_id = item.get("set_id")
                if set_id is not None:
                    part = item.get("part", PART_OF_SLOT.get(slot))
                    set_parts.setdefault(set_id, set()).add(part)

            set_pieces = {
                set_id: len(parts)
                for set_id, parts in sorted(set_parts.items())
            }
            for set_id, pieces in set_pieces.items():
                bonus = self._set_bonus(set_id, pieces)
                attack += bonus.get("attack", 0)
                defense += bonus.get("defense", 0)

            return {
                "attack": attack,
                "defense": defense,
                "version": state["stats_version"],
                "set_pieces": set_pieces,
            }

    def stats_version(self, player: str) -> int:
        with self._lock:
            return self._require_player(player)["stats_version"]

    def snapshot(self) -> dict:
        with self._lock:
            players = {}
            for player_id, state in self._players.items():
                players[player_id] = {
                    "level": state["level"],
                    "bag": deepcopy(state["bag"]),
                    "worn": deepcopy(state["worn"]),
                    "stats_version": state["stats_version"],
                }
            return {
                "version": 1,
                "items": deepcopy(self._items),
                "players": players,
            }

    def _parse_snapshot(self, state: object) -> tuple[dict, dict]:
        if not isinstance(state, dict):
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "state is not an object"
            )
        if not _is_int(state.get("version")) or state.get("version") != 1:
            raise EquipError("EQUIP_SNAPSHOT_INVALID", "unsupported version")

        raw_items = state.get("items")
        raw_players = state.get("players")
        if not isinstance(raw_items, dict) or not isinstance(raw_players, dict):
            raise EquipError("EQUIP_SNAPSHOT_INVALID", "missing sections")

        items = self._parse_snapshot_items(raw_items)
        players = {}
        for player_id, raw_player in raw_players.items():
            players[player_id] = self._parse_snapshot_player(raw_player, items)
        return items, players

    def _parse_snapshot_items(self, raw_items: dict) -> dict:
        items = {}
        for item_id, raw_item in raw_items.items():
            if not isinstance(item_id, str) or not isinstance(raw_item, dict):
                raise EquipError("EQUIP_SNAPSHOT_INVALID", "invalid item")

            part = raw_item.get("part")
            if not isinstance(part, str) or not part:
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "invalid item part"
                )

            level_req = raw_item.get("level_req", 1)
            if not _is_int(level_req) or level_req < 0:
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "invalid level requirement"
                )

            two_handed = raw_item.get("two_handed", False)
            if not isinstance(two_handed, bool):
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "invalid two_handed flag"
                )

            stats = raw_item.get("stats", {})
            enchant = raw_item.get("enchant")
            if not isinstance(stats, dict):
                raise EquipError("EQUIP_SNAPSHOT_INVALID", "invalid stats")
            if enchant is not None and not isinstance(enchant, dict):
                raise EquipError("EQUIP_SNAPSHOT_INVALID", "invalid enchant")

            set_id = raw_item.get("set_id")
            if set_id is not None and not isinstance(set_id, str):
                raise EquipError("EQUIP_SNAPSHOT_INVALID", "invalid set id")

            items[item_id] = {
                "part": part,
                "level_req": level_req,
                "two_handed": two_handed,
                "stats": deepcopy(stats),
                "enchant": deepcopy(enchant),
                "set_id": set_id,
            }
        return items

    def _parse_snapshot_player(self, raw_player: object,
                               items: dict) -> dict:
        if not isinstance(raw_player, dict):
            raise EquipError("EQUIP_SNAPSHOT_INVALID", "invalid player")

        required = ("level", "bag", "worn", "stats_version")
        if any(key not in raw_player for key in required):
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "missing player field"
            )

        level = raw_player["level"]
        stats_version = raw_player["stats_version"]
        if not _is_int(level) or level < 0:
            raise EquipError("EQUIP_SNAPSHOT_INVALID", "invalid level")
        if not _is_int(stats_version) or stats_version < 0:
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "invalid stats version"
            )

        raw_bag = raw_player["bag"]
        raw_worn = raw_player["worn"]
        if not isinstance(raw_bag, dict) or not isinstance(raw_worn, dict):
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "invalid bag or worn"
            )

        bag = {}
        for item_id, count in raw_bag.items():
            if (not isinstance(item_id, str) or item_id not in items
                    or not _is_int(count) or count <= 0):
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "invalid bag entry"
                )
            bag[item_id] = count

        has_explicit_capacity = (
            "bag_capacity" in raw_player or "capacity" in raw_player
        )
        if has_explicit_capacity:
            capacity = self._normalize_snapshot_capacity(
                raw_player.get(
                    "bag_capacity", raw_player.get("capacity")
                )
            )
        else:
            capacity = max(self.bag_capacity, _bag_total(bag))
        if _bag_total(bag) > capacity:
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "bag exceeds capacity"
            )

        worn = {}
        for slot, item_id in raw_worn.items():
            if (slot not in PART_OF_SLOT
                    or not isinstance(item_id, str)
                    or item_id not in items):
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "invalid worn entry"
                )
            item = items[item_id]
            if PART_OF_SLOT[slot] != item["part"]:
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "slot part mismatch"
                )
            if level < item["level_req"]:
                raise EquipError(
                    "EQUIP_SNAPSHOT_INVALID", "worn item level requirement"
                )
            worn[slot] = item_id

        worn_items = list(worn.values())
        if len(worn_items) != len(set(worn_items)):
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "item worn in multiple slots"
            )

        if (worn.get("offhand") is not None
                and worn.get("weapon") is not None
                and items[worn["weapon"]]["two_handed"]):
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "two-handed conflict"
            )

        return {
            "level": level,
            "bag": bag,
            "worn": worn,
            "stats_version": stats_version,
            "bag_capacity": capacity,
        }

    @staticmethod
    def _normalize_snapshot_capacity(capacity: object) -> int:
        if not _is_int(capacity) or capacity < 0:
            raise EquipError(
                "EQUIP_SNAPSHOT_INVALID", "invalid capacity"
            )
        return capacity

    def restore(self, state: dict) -> None:
        with self._lock:
            items, players = self._parse_snapshot(state)
            self._items = items
            self._players = players
