"""Adaptive playout delay and sequence handling for an RTP video jitter buffer."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "accepted",
    "duplicates",
    "late",
    "rejected",
    "played",
    "fast_forward",
)


class PlayoutError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RtpPlayoutGuard:
    """Track arrival jitter, the adaptive delay target and the playout clock."""

    def __init__(self, *, min_delay_ms: float = 40.0, max_delay_ms: float = 400.0,
                 jitter_factor: float = 3.0, step_ms: float = 20.0,
                 frame_ms: float = 20.0, capacity: int = 256,
                 jitter_window: int = 10, drop_after_ms: float = 2000.0,
                 clock=None) -> None:
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.jitter_factor = jitter_factor
        self.step_ms = step_ms
        self.frame_ms = frame_ms
        self.capacity = capacity
        self.jitter_window = jitter_window
        self.drop_after_ms = drop_after_ms
        self._clock = clock
        self._lock = threading.RLock()

    def push(self, seq: int, arrival_ms: float, timestamp_ms: float | None = None,
             *, keyframe: bool = False, ref: bool = False) -> dict:
        raise NotImplementedError

    def tick(self, now_ms: float) -> dict:
        raise NotImplementedError

    def target_delay_ms(self) -> float:
        raise NotImplementedError

    def jitter_ms(self) -> float:
        raise NotImplementedError

    def playout_ms(self) -> float:
        raise NotImplementedError

    def queue_depth(self) -> int:
        raise NotImplementedError

    def queued_seqs(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
