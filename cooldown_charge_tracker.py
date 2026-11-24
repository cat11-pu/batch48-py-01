"""Cooldown and charge tracking with haste, pause and snapshot support."""

from __future__ import annotations

import threading


class CooldownError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CooldownTracker:
    """Tracks per skill charges, recovery timers, global cooldown and pause."""

    def __init__(self, *, gcd_ms: int = 1500, haste_cap: float = 0.5) -> None:
        self.gcd_ms = int(gcd_ms)
        self.haste_cap = float(haste_cap)
        self._lock = threading.RLock()

    def register(self, skill_id: str, *, cooldown_ms: int, charges: int = 1,
                 recovery_ms=None, gcd_ms=None) -> dict:
        raise NotImplementedError

    def use(self, skill_id: str, now: int) -> dict:
        raise NotImplementedError

    def ready(self, skill_id: str, now: int) -> bool:
        raise NotImplementedError

    def charges(self, skill_id: str) -> int:
        raise NotImplementedError

    def remaining(self, skill_id: str, now: int) -> int:
        raise NotImplementedError

    def apply_haste(self, pct: float) -> dict:
        raise NotImplementedError

    def pause(self, now: int) -> dict:
        raise NotImplementedError

    def resume(self, now: int) -> dict:
        raise NotImplementedError

    def advance(self, now: int) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
