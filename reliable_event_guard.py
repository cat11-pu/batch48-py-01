"""Reliable game event channels with TTL, cumulative acks and retransmit budgets."""

from __future__ import annotations

import threading


class EventError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReliableEventGuard:
    """Grades event delivery, acknowledges cumulatively and bounds retransmits."""

    def __init__(self, *, max_retransmits: int = 3, backoff_ticks: int = 2,
                 bandwidth_quota: int = 8, dead_letter_limit: int = 64) -> None:
        self.max_retransmits = int(max_retransmits)
        self.backoff_ticks = int(backoff_ticks)
        self.bandwidth_quota = int(bandwidth_quota)
        self.dead_letter_limit = int(dead_letter_limit)
        self._lock = threading.RLock()

    def register_channel(self, channel: str, *, reliable: bool,
                         mergeable: bool = False) -> dict:
        raise NotImplementedError

    def publish(self, channel: str, event_id: str, payload: object = None, *,
                tick: int = 0, ttl_ticks: int = 6) -> dict:
        raise NotImplementedError

    def ack(self, channel: str, upto_seq: int) -> dict:
        raise NotImplementedError

    def pending(self, channel: str) -> list:
        raise NotImplementedError

    def pending_count(self) -> int:
        raise NotImplementedError

    def collect(self, tick: int) -> dict:
        raise NotImplementedError

    def dead_letters(self, channel: str | None = None) -> list:
        raise NotImplementedError

    def watermark(self, channel: str) -> int:
        raise NotImplementedError

    def next_seq(self, channel: str) -> int:
        raise NotImplementedError

    def channels(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
