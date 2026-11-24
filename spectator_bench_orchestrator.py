"""Spectator stand and bench seat orchestration for hosted matches."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "spectators_joined",
    "bench_joined",
    "promotions",
    "demotions",
    "swaps",
    "left",
    "settled",
    "rejected",
    "stale_replacements",
)


class SpectatorError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SpectatorBenchOrchestrator:
    """Own active seats, the ordered bench and read-only spectator slots."""

    def __init__(self, *, active_slots: int = 5, spectator_limit: int = 8,
                 bench_limit: int = 4, clock=None) -> None:
        self.active_slots = active_slots
        self.spectator_limit = spectator_limit
        self.bench_limit = bench_limit
        self._clock = clock
        self._lock = threading.RLock()

    def assign_active(self, seat: int, player: str) -> dict:
        raise NotImplementedError

    def join_spectator(self, player: str) -> dict:
        raise NotImplementedError

    def join_bench(self, player: str) -> dict:
        raise NotImplementedError

    def upgrade_spectator(self, player: str) -> dict:
        raise NotImplementedError

    def promote(self, player: str, seat: int, *, reason: str = "voluntary",
                expect_occupant=None) -> dict:
        raise NotImplementedError

    def demote(self, player: str, *, reason: str = "timeout") -> dict:
        raise NotImplementedError

    def swap_seat(self, player_a: str, player_b: str) -> dict:
        raise NotImplementedError

    def set_permission(self, player: str, allowed: bool) -> dict:
        raise NotImplementedError

    def can_operate(self, player: str) -> bool:
        raise NotImplementedError

    def leave(self, player: str) -> dict:
        raise NotImplementedError

    def seat_state(self, player: str) -> dict:
        raise NotImplementedError

    def roster(self) -> dict:
        raise NotImplementedError

    def settle_match(self, result: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
