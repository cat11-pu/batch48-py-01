"""Equipment slot conflict resolution with set bonuses and derived stats.

The resolver owns per-player equipment slots, a bounded bag, item definitions,
set composition rules and a monotonically increasing derived-stats version.
"""

from __future__ import annotations

import threading


SLOT_ORDER = ("weapon", "offhand", "head", "chest", "legs", "ring_left", "ring_right")

PART_OF_SLOT = {
    "weapon": "weapon",
    "offhand": "offhand",
    "head": "head",
    "chest": "chest",
    "legs": "legs",
    "ring_left": "ring",
    "ring_right": "ring",
}


class EquipError(ValueError):
    """Equipment failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class EquipResolver:
    """Resolve slot conflicts and recompute derived stats for each player."""

    def __init__(self, *, bag_capacity: int = 8, set_bonus_table=None) -> None:
        self.bag_capacity = int(bag_capacity)
        self.set_bonus_table = dict(set_bonus_table or {})
        self._lock = threading.RLock()

    def register_item(self, item_id: str, *, part: str, level_req: int = 1,
                      two_handed: bool = False, stats=None, enchant=None,
                      set_id=None) -> dict:
        raise NotImplementedError

    def grant_level(self, player: str, level: int) -> int:
        raise NotImplementedError

    def add_to_bag(self, player: str, item_id: str, count: int = 1) -> int:
        raise NotImplementedError

    def set_bag_capacity(self, player: str, capacity: int) -> int:
        raise NotImplementedError

    def bag(self, player: str) -> dict:
        raise NotImplementedError

    def worn(self, player: str) -> dict:
        raise NotImplementedError

    def validate(self, player: str, item_id: str, slot: str | None = None) -> dict:
        raise NotImplementedError

    def equip(self, player: str, item_id: str, slot: str | None = None) -> dict:
        raise NotImplementedError

    def unequip(self, player: str, slot: str) -> dict:
        raise NotImplementedError

    def swap(self, player: str, slot: str, item_id: str) -> dict:
        raise NotImplementedError

    def stats(self, player: str) -> dict:
        raise NotImplementedError

    def stats_version(self, player: str) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
