"""Fixed-capacity sensitive buffer pool with zeroization on return."""

from __future__ import annotations

import threading


class BufferPoolError(ValueError):
    def __init__(self, code: str, detail: object = None,
                 retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


class ZeroizeBufferPool:
    """Lease fixed-size buffers, wipe them on release, never grow."""

    def __init__(self, capacity: int, buffer_size: int, *,
                 retry_after_ms: int = 50) -> None:
        self.capacity = capacity
        self.buffer_size = buffer_size
        self.retry_after_ms = retry_after_ms
        self._lock = threading.RLock()

    def acquire(self, size=None, *, owner: str = "") -> dict:
        raise NotImplementedError

    def release(self, lease) -> dict:
        raise NotImplementedError

    def release_owner(self, owner: str) -> int:
        raise NotImplementedError

    def zeroize_all(self) -> int:
        raise NotImplementedError

    def free_count(self) -> int:
        raise NotImplementedError

    def leased_count(self) -> int:
        raise NotImplementedError

    def pool_capacity(self) -> int:
        raise NotImplementedError

    def lease_info(self, lease) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
