"""Pre-match readiness barrier with staged gates, takeover and grace slots."""

from __future__ import annotations

import threading

STAGES = ("assets", "scene", "baseline")


class MatchBarrierError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MatchBarrierGuard:
    """Gates the match start on staged readiness and arbitrates late entries."""

    def __init__(self, *, threshold: str = "baseline", timeout_ticks: int = 10,
                 grace_ticks: int = 5, max_players: int = 8) -> None:
        self.threshold = threshold
        self.timeout_ticks = int(timeout_ticks)
        self.grace_ticks = int(grace_ticks)
        self.max_players = int(max_players)
        self._lock = threading.RLock()

    def join(self, player: str, *, slot: int | None = None, tick: int = 0) -> dict:
        raise NotImplementedError

    def report_ready(self, player: str, stage: str, *, tick: int = 0) -> dict:
        raise NotImplementedError

    def barrier(self, tick: int) -> dict:
        raise NotImplementedError

    def drop(self, player: str, *, tick: int) -> dict:
        raise NotImplementedError

    def substitute(self, player: str, *, slot: int | None = None,
                   tick: int = 0) -> dict:
        raise NotImplementedError

    def advance(self, tick: int) -> dict:
        raise NotImplementedError

    def state(self) -> str:
        raise NotImplementedError

    def released_at(self) -> int | None:
        raise NotImplementedError

    def deadline(self) -> int:
        raise NotImplementedError

    def short_handed(self) -> list:
        raise NotImplementedError

    def players(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
