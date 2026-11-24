"""Streaming archive bomb guard with fail-closed limits."""

from __future__ import annotations

import threading

DEFAULT_LIMITS = {
    "max_entries": 1024,
    "max_nesting": 3,
    "max_entry_expanded": 64 * 1024 * 1024,
    "max_total_expanded": 256 * 1024 * 1024,
    "max_ratio": 100.0,
    "ratio_floor_bytes": 64,
}


class ArchiveBombError(ValueError):
    """Refusal raised by the guard; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ArchiveBombGuard:
    """Audit a streamed archive expansion against hard safety limits."""

    def __init__(self, limits: dict | None = None) -> None:
        self._limits = dict(DEFAULT_LIMITS)
        if limits:
            self._limits.update(limits)
        self._lock = threading.RLock()

    def limits(self) -> dict:
        raise NotImplementedError

    def open(self, stream_id: str) -> dict:
        raise NotImplementedError

    def declare_entry(self, stream_id: str, entry: dict) -> dict:
        raise NotImplementedError

    def consume(self, stream_id: str, entry_name: str, chunk: bytes) -> dict:
        raise NotImplementedError

    def nesting_depth(self, stream_id: str) -> int:
        raise NotImplementedError

    def ratio(self, stream_id: str) -> float:
        raise NotImplementedError

    def verdict(self, stream_id: str) -> dict:
        raise NotImplementedError

    def close(self, stream_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
