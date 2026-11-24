"""PTS/DTS reorder buffer guard for decode pipelines."""

from __future__ import annotations

import threading
import time


class ReorderError(ValueError):
    """Rejection raised by the reorder guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PtsReorderGuard:
    """Buffer frames by PTS and release them in presentation order."""

    def __init__(self, *, max_reorder_depth: int = 16,
                 gap_threshold_ms: int = 500, clock=None) -> None:
        self.max_reorder_depth = max_reorder_depth
        self.gap_threshold_ms = gap_threshold_ms
        self._clock = clock
        self._lock = threading.RLock()

    def _now_ms(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.monotonic() * 1000)

    def push(self, pts: int, dts: int | None = None, *, payload_size: int = 0,
             keyframe: bool = False) -> dict:
        raise NotImplementedError

    def pop(self, *, count: int = 1) -> list[dict]:
        raise NotImplementedError

    def flush_eos(self) -> list[dict]:
        raise NotImplementedError

    def buffered(self) -> int:
        raise NotImplementedError

    def last_output_pts(self) -> int | None:
        raise NotImplementedError

    def last_output_dts(self) -> int | None:
        raise NotImplementedError

    def late_dropped(self) -> int:
        raise NotImplementedError

    def gap_count(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
