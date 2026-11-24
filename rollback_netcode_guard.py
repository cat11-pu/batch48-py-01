"""Rollback netcode guard with predictive frames and resimulation."""

from __future__ import annotations

import threading


class RollbackError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RollbackNetcode:
    """Predicts remote inputs, rolls back on divergence and verifies digests."""

    def __init__(self, *, window: int = 8) -> None:
        self.window = int(window)
        self._lock = threading.RLock()

    def predict(self, frame: int, input: object) -> dict:
        raise NotImplementedError

    def on_remote_input(self, frame: int, input: object) -> dict:
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError

    def current_frame(self) -> int:
        raise NotImplementedError

    def confirmed_frame(self) -> int:
        raise NotImplementedError

    def digest(self, frame: int) -> int:
        raise NotImplementedError

    def on_server_digest(self, frame: int, digest: int) -> dict:
        raise NotImplementedError

    def window_size(self) -> int:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
