"""Reconnect input replay and baseline alignment guard."""

from __future__ import annotations

import threading


class ResumeError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ResumeAlignmentGuard:
    """Aligns buffered client inputs with an authoritative baseline after reconnects."""

    def __init__(self, *, replay_window: int = 32, max_reconnects: int = 3,
                 reconnect_window: int = 10, clock=None) -> None:
        self.replay_window = int(replay_window)
        self.max_reconnects = int(max_reconnects)
        self.reconnect_window = int(reconnect_window)
        self._clock = clock
        self._lock = threading.RLock()

    def begin_session(self, account: str) -> dict:
        raise NotImplementedError

    def close_session(self, account: str) -> dict:
        raise NotImplementedError

    def install_baseline(self, account: str, *, epoch: int, frame: int,
                         state: dict) -> dict:
        raise NotImplementedError

    def accept_batch(self, account: str, batch: dict) -> dict:
        raise NotImplementedError

    def applied_inputs(self, account: str) -> list:
        raise NotImplementedError

    def applied_frame(self, account: str) -> int:
        raise NotImplementedError

    def baseline_of(self, account: str) -> dict | None:
        raise NotImplementedError

    def session_epoch(self, account: str) -> int:
        raise NotImplementedError

    def fired_events(self, account: str) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
