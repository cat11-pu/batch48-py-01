"""Keyframe request coalescing, per subscriber budgets and retry backoff."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "subscribers_added",
    "subscribers_removed",
    "streams_published",
    "requests_sent",
    "coalesced",
    "throttled",
    "denied",
    "retries",
    "retries_exhausted",
    "keyframes",
)


class KeyframeRequestError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class KeyframeRequestGuard:
    """Merge subscriber keyframe requests and throttle them per subscriber."""

    def __init__(self, *, min_interval_ms: float = 500.0, budget: int = 3,
                 budget_window_ms: float = 10000.0, retry_delay_ms: float = 1000.0,
                 max_retries: int = 2, max_subscribers: int = 64,
                 clock=None) -> None:
        self.min_interval_ms = min_interval_ms
        self.budget = budget
        self.budget_window_ms = budget_window_ms
        self.retry_delay_ms = retry_delay_ms
        self.max_retries = max_retries
        self.max_subscribers = max_subscribers
        self._clock = clock
        self._lock = threading.RLock()

    def add_subscriber(self, stream_id: str, subscriber_id: str) -> dict:
        raise NotImplementedError

    def remove_subscriber(self, stream_id: str, subscriber_id: str) -> dict:
        raise NotImplementedError

    def request_keyframe(self, stream_id: str, subscriber_id: str, *,
                         now_ms: float) -> dict:
        raise NotImplementedError

    def on_keyframe(self, stream_id: str, *, now_ms: float) -> dict:
        raise NotImplementedError

    def publish_stream(self, stream_id: str) -> dict:
        raise NotImplementedError

    def stream_info(self, stream_id: str) -> dict:
        raise NotImplementedError

    def pending(self, stream_id: str) -> dict | None:
        raise NotImplementedError

    def next_retry_ms(self, stream_id: str) -> float | None:
        raise NotImplementedError

    def due_retries(self, now_ms: float) -> list:
        raise NotImplementedError

    def budget_remaining(self, stream_id: str, subscriber_id: str, *,
                         now_ms: float) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
