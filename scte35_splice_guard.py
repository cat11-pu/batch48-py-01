"""SCTE-35 splice insert parsing and execution guard."""

from __future__ import annotations

import threading


class Scte35SpliceError(ValueError):
    """Rejection raised by the splice guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class Scte35SpliceGuard:
    """Normalise splice inserts, queue them and execute at keyframes."""

    def __init__(self, *, max_pending: int = 256, clock=None) -> None:
        self.max_pending = max_pending
        self._clock = clock
        self._lock = threading.RLock()

    def parse_command(self, command: dict, *,
                      program_end_ms: int | None = None) -> dict:
        raise NotImplementedError

    def schedule(self, command: dict, *,
                 program_end_ms: int | None = None) -> dict:
        raise NotImplementedError

    def on_keyframe(self, keyframe_pts_ms: int) -> list[dict]:
        raise NotImplementedError

    def cancel(self, splice_id: str) -> bool:
        raise NotImplementedError

    def pending(self) -> list[dict]:
        raise NotImplementedError

    def triggered_ids(self) -> list[str]:
        raise NotImplementedError

    def executions(self) -> list[dict]:
        raise NotImplementedError

    def is_triggered(self, splice_id: str) -> bool:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
