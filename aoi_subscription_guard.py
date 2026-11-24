"""AOI subscriptions with cell moves, visibility budget and a sequenced stream."""

from __future__ import annotations

import threading


class AoiError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AoiSubscriptionGuard:
    """Maintains per-cell observer subscriptions and a budgeted visibility set."""

    def __init__(self, *, radius: int = 1, leave_lag: int = 1, budget: int = 4,
                 cell_size: float = 10.0) -> None:
        self.radius = int(radius)
        self.leave_lag = int(leave_lag)
        self.default_budget = int(budget)
        self.cell_size = float(cell_size)
        self._lock = threading.RLock()

    def cell_of(self, x: float, y: float) -> tuple:
        raise NotImplementedError

    def add_observer(self, observer_id: str, *, x: float, y: float,
                     budget: int | None = None) -> dict:
        raise NotImplementedError

    def move_observer(self, observer_id: str, *, x: float, y: float) -> dict:
        raise NotImplementedError

    def add_entity(self, entity_id: str, *, x: float, y: float,
                   kind: str = "npc", priority: int = 0) -> dict:
        raise NotImplementedError

    def move_entity(self, entity_id: str, *, x: float, y: float) -> dict:
        raise NotImplementedError

    def remove_entity(self, entity_id: str) -> dict:
        raise NotImplementedError

    def visible(self, observer_id: str) -> list:
        raise NotImplementedError

    def subscribers(self, entity_id: str) -> list:
        raise NotImplementedError

    def budget(self, observer_id: str) -> int:
        raise NotImplementedError

    def set_budget(self, observer_id: str, limit: int) -> dict:
        raise NotImplementedError

    def end_tick(self) -> dict:
        raise NotImplementedError

    def next_seq(self) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
