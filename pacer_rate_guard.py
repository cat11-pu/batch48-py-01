"""Sender side AIMD rate control with token bucket pacing and overload dropping."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "enqueued",
    "duplicates",
    "rejected_frames",
    "dropped_frames",
    "sent_frames",
    "bytes_sent",
    "cycles_up",
    "cycles_down",
    "loss_reports",
)


class PacerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PacerRateGuard:
    """Adjust the target bitrate and pace queued frames through a token bucket."""

    def __init__(self, *, min_bps: float = 200_000.0, max_bps: float = 4_000_000.0,
                 start_bps: float = 800_000.0, capacity_ms: float = 100.0,
                 max_queue_bytes: int = 120_000, clock=None) -> None:
        self.min_bps = min_bps
        self.max_bps = max_bps
        self.start_bps = start_bps
        self.capacity_ms = capacity_ms
        self.max_queue_bytes = max_queue_bytes
        self._clock = clock
        self._lock = threading.RLock()

    def enqueue(self, frame: dict, *, now_ms: float) -> dict:
        raise NotImplementedError

    def tick(self, now_ms: float) -> dict:
        raise NotImplementedError

    def on_cycle(self, no_congestion: bool) -> dict:
        raise NotImplementedError

    def report_loss(self, loss_ratio: float, *, now_ms: float | None = None) -> dict:
        raise NotImplementedError

    def target_bps(self) -> float:
        raise NotImplementedError

    def tokens_bytes(self) -> float:
        raise NotImplementedError

    def mode(self) -> str:
        raise NotImplementedError

    def queue_bytes(self) -> int:
        raise NotImplementedError

    def queue_depth(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
