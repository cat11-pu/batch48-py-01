"""Client snapshot interpolation buffer with extrapolation guard."""

from __future__ import annotations

import threading


class InterpError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SnapshotInterpGuard:
    """Buffers server snapshots and samples entity positions on a timeline."""

    def __init__(self, *, buffer_size: int = 32, max_extrapolation: int = 250) -> None:
        self.buffer_size = int(buffer_size)
        self.max_extrapolation = int(max_extrapolation)
        self._lock = threading.RLock()

    def ingest(self, snapshot: dict) -> dict:
        raise NotImplementedError

    def sample(self, render_time: int) -> dict:
        raise NotImplementedError

    def teleport(self, entity_id: str, x: float, y: float, at_time: int) -> dict:
        raise NotImplementedError

    def despawn(self, entity_id: str, at_time: int) -> dict:
        raise NotImplementedError

    def entity_state(self, entity_id: str) -> dict | None:
        raise NotImplementedError

    def bounds(self) -> tuple:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
