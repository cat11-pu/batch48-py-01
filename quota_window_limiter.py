"""Layered sliding-window quota limiter with lease style reservations."""

from __future__ import annotations

import threading


class QuotaError(ValueError):
    """Rejection raised by the quota limiter."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class QuotaWindowLimiter:
    """Reserve, settle and refund quota across key, tenant and global layers."""

    def __init__(self, *, window_seconds: int = 10, key_limit: int = 100,
                 tenant_limit: int = 500, global_limit: int = 1000,
                 lease_ttl: float = 5.0, max_leases: int = 1000,
                 clock=None) -> None:
        self.window_seconds = window_seconds
        self.key_limit = key_limit
        self.tenant_limit = tenant_limit
        self.global_limit = global_limit
        self.lease_ttl = lease_ttl
        self.max_leases = max_leases
        self._clock = clock
        self._lock = threading.RLock()

    def reserve(self, key: str, tenant: str, cost: int = 1, now=None) -> str:
        raise NotImplementedError

    def settle(self, lease_id: str) -> dict:
        raise NotImplementedError

    def refund(self, lease_id: str) -> int:
        raise NotImplementedError

    def expire_leases(self, now) -> int:
        raise NotImplementedError

    def remaining(self, key: str, tenant: str, now=None) -> dict:
        raise NotImplementedError

    def lease_count(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
