"""Per-frame input budget arbiter with priority tiers and fair aging."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "submitted",
    "accepted",
    "duplicates",
    "conflicts",
    "rejected",
    "superseded",
    "expired",
    "cancelled",
    "arbitrations",
    "selected",
)

PRIORITIES = ("urgent", "normal", "bulk")
RANK = {"urgent": 3, "normal": 2, "bulk": 1}


class InputError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class InputBudgetArbiter:
    """Arbitrates queued player inputs inside a fixed per-frame budget."""

    def __init__(self, *, budget: int = 8, player_limit: int = 8,
                 queue_limit: int = 32, default_ttl: int = 500,
                 max_cost: int = 4, clock=None) -> None:
        self._budget = int(budget)
        self.player_limit = int(player_limit)
        self.queue_limit = int(queue_limit)
        self.default_ttl = int(default_ttl)
        self.max_cost = int(max_cost)
        self._clock = clock
        self._lock = threading.RLock()

    def set_budget(self, budget: int, *, frame: int | None = None) -> dict:
        raise NotImplementedError

    def budget_of(self) -> int:
        raise NotImplementedError

    def submit(self, player: str, *, seq: int, entity: str, command: str,
               priority: str = "normal", cost: int = 1, ttl: int | None = None,
               frame: int = 0, now=None) -> dict:
        raise NotImplementedError

    def pending(self, *, player: str | None = None) -> dict:
        raise NotImplementedError

    def item_of(self, player: str, *, seq: int) -> dict | None:
        raise NotImplementedError

    def cancel(self, player: str, *, seq: int) -> dict:
        raise NotImplementedError

    def expire(self, *, now=None) -> int:
        raise NotImplementedError

    def arbitrate(self, *, frame: int, now=None) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
