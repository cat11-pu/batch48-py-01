"""Ready check coordination between a successful match and match start."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "checks_opened",
    "tickets_issued",
    "accepts",
    "declines",
    "timeouts",
    "closed",
    "rejected",
)


class ReadyCheckError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReadyCheckCoordinator:
    """Issue ready tickets, collect answers, time out and requeue players."""

    def __init__(self, *, ticket_limit: int = 64, player_limit: int = 512,
                 mode_windows=None, default_window: int = 5,
                 penalty_ticks: int = 3, clock=None) -> None:
        self.ticket_limit = ticket_limit
        self.player_limit = player_limit
        self.mode_windows = {} if mode_windows is None else dict(mode_windows)
        self.default_window = default_window
        self.penalty_ticks = penalty_ticks
        self._clock = clock
        self._lock = threading.RLock()

    def window_for(self, mode: str) -> int:
        raise NotImplementedError

    def open_check(self, check_id: str, *, mode: str, players) -> dict:
        raise NotImplementedError

    def submit_ready(self, check_id: str, player: str, *, accept: bool, tick: int = 0) -> dict:
        raise NotImplementedError

    def tick(self, check_id: str, *, ticks: int = 1) -> dict:
        raise NotImplementedError

    def close_check(self, check_id: str) -> dict:
        raise NotImplementedError

    def disconnect(self, check_id: str, player: str) -> dict:
        raise NotImplementedError

    def status(self, check_id: str) -> dict:
        raise NotImplementedError

    def ticket_for(self, player: str) -> dict:
        raise NotImplementedError

    def requeue_penalty(self, player: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
