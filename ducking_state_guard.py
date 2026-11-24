"""Sidechain triggered ducking state machine for narration over music beds."""

from __future__ import annotations

import threading

DUCK_STATES = ("idle", "attack", "hold", "release", "bypass")

STAT_KEYS = (
    "sources_added",
    "sources_removed",
    "feeds",
    "advances",
    "bypass_toggles",
    "rejected",
)


class DuckingError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DuckingStateGuard:
    """Drive attack, hold and release phases for one or more trigger sources."""

    def __init__(self, *, sample_rate: int = 48000, attack_ms: int = 20,
                 hold_ms: int = 300, release_ms: int = 800,
                 depth: float = 0.35, quantum: float = 0.01,
                 clock=None) -> None:
        self.sample_rate = sample_rate
        self.attack_ms = attack_ms
        self.hold_ms = hold_ms
        self.release_ms = release_ms
        self.depth = depth
        self.quantum = quantum
        self._clock = clock
        self._lock = threading.RLock()

    def frames_for(self, ms: int) -> int:
        raise NotImplementedError

    def add_source(self, source_id: str, *, priority: int = 0,
                   depth: float | None = None) -> dict:
        raise NotImplementedError

    def remove_source(self, source_id: str) -> None:
        raise NotImplementedError

    def feed(self, source_id: str, active: bool) -> dict:
        raise NotImplementedError

    def advance(self, frames: int) -> float:
        raise NotImplementedError

    def gain(self) -> float:
        raise NotImplementedError

    def state(self) -> str:
        raise NotImplementedError

    def dominant_source(self) -> str | None:
        raise NotImplementedError

    def set_bypass(self, flag: bool) -> None:
        raise NotImplementedError

    def bypassed(self) -> bool:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
