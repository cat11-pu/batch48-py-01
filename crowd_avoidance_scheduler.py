"""Frame scheduled crowd avoidance with neighbor budget and stuck recovery."""

from __future__ import annotations

import threading


class CrowdError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AvoidanceScheduler:
    """Advances registered units one frame at a time under a neighbor budget."""

    def __init__(self, *, neighbor_budget: int = 32, cell_size: float = 2.0,
                 stuck_epsilon: float = 0.01) -> None:
        self.neighbor_budget = int(neighbor_budget)
        self.cell_size = float(cell_size)
        self.stuck_epsilon = float(stuck_epsilon)
        self._lock = threading.RLock()

    def register(self, unit: str, *, radius: float = 0.5, priority: int = 0,
                 position=(0.0, 0.0), goal=None, max_speed: float = 1.0) -> dict:
        raise NotImplementedError

    def unregister(self, unit: str) -> dict:
        raise NotImplementedError

    def set_goal(self, unit: str, goal) -> dict:
        raise NotImplementedError

    def tick(self, *, dt_ms: int = 16, budget=None, units=None) -> dict:
        raise NotImplementedError

    def neighbors(self, unit: str, radius: float) -> list:
        raise NotImplementedError

    def stuck(self, unit: str, window: int) -> bool:
        raise NotImplementedError

    def position(self, unit: str) -> tuple:
        raise NotImplementedError

    def state(self, unit: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
