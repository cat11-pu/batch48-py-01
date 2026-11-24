"""Threat table maintenance with taunt, distance filtering and time decay."""

from __future__ import annotations

import threading


class ThreatError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ThreatSelector:
    """Maintains one threat table per hostile mob and picks its current target."""

    def __init__(self, *, heal_factor: float = 0.5, melee_range: float = 5.0,
                 swap_margin: float = 0.15, decay_per_second: float = 10.0) -> None:
        self.heal_factor = float(heal_factor)
        self.melee_range = float(melee_range)
        self.swap_margin = float(swap_margin)
        self.decay_per_second = float(decay_per_second)
        self._lock = threading.RLock()

    def add_mob(self, mob: str, *, position=(0.0, 0.0)) -> dict:
        raise NotImplementedError

    def add_source(self, mob: str, source: str, *, position=(0.0, 0.0)) -> dict:
        raise NotImplementedError

    def add_threat(self, mob: str, source: str, amount: float, kind: str,
                   *, now: float = 0.0) -> dict:
        raise NotImplementedError

    def select(self, mob: str, now: float) -> dict:
        raise NotImplementedError

    def taunt(self, mob: str, source: str, duration: float,
              *, now: float = 0.0) -> dict:
        raise NotImplementedError

    def decay(self, mob: str, now: float) -> dict:
        raise NotImplementedError

    def kill(self, mob: str, source: str) -> dict:
        raise NotImplementedError

    def table(self, mob: str) -> list:
        raise NotImplementedError

    def threat(self, mob: str, source: str) -> float:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
