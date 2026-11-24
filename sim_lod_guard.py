"""Server simulation LOD guard with distance tiers and per-tick budget."""

from __future__ import annotations

import threading

TIERS = ("each_tick", "every_2", "every_8", "sleep")


class SimError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SimLodGuard:
    """Assigns simulation tiers by distance and arbitrates the per-tick budget."""

    def __init__(self, *, budget: int = 8, hysteresis: float = 20.0) -> None:
        self._budget = int(budget)
        self.hysteresis = float(hysteresis)
        self._lock = threading.RLock()

    def register_entity(self, entity_id: str, *, distance: float,
                        region: str = "A") -> dict:
        raise NotImplementedError

    def update_distance(self, entity_id: str, distance: float) -> dict:
        raise NotImplementedError

    def set_tier(self, entity_id: str, tier: str) -> dict:
        raise NotImplementedError

    def move_region(self, entity_id: str, region: str) -> dict:
        raise NotImplementedError

    def wake(self, entity_id: str, *, urgent: bool = True) -> dict:
        raise NotImplementedError

    def tick(self) -> dict:
        raise NotImplementedError

    def tier_of(self, entity_id: str) -> str:
        raise NotImplementedError

    def last_updated(self, entity_id: str) -> int | None:
        raise NotImplementedError

    def entities(self) -> dict:
        raise NotImplementedError

    def budget(self) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
