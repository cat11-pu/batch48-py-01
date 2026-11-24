"""File lease coordination: exclusive/shared leases with FIFO waiters."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class LeaseError(ValueError):
    """Raised when a lease operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Lease:
    lease_id: str
    key: str
    owner: str
    mode: str
    acquired_at: int
    expires_at: int


class LeaseCoordinator:
    """Grant, renew, release and expire leases on resource keys."""

    def __init__(self, *, max_leases: int = 1024) -> None:
        self.max_leases = max_leases
        self._lock = threading.RLock()

    def acquire(self, key: str, owner: str, mode: str, ttl: int, now: int) -> Lease:
        raise NotImplementedError

    def renew(self, lease_id: str, now: int, ttl: int | None = None) -> Lease:
        raise NotImplementedError

    def release(self, lease_id: str, owner: str) -> bool:
        raise NotImplementedError

    def holder(self, key: str) -> list:
        raise NotImplementedError

    def waiters(self, key: str) -> list:
        raise NotImplementedError

    def cancel_wait(self, key: str, owner: str) -> bool:
        raise NotImplementedError

    def expire(self, now: int) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
