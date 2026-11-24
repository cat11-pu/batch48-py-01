"""Relay backpressure guard with watermarks and deficit round robin."""

from __future__ import annotations

import threading


class BackpressureError(ValueError):
    """Rejection raised by the backpressure guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class BackpressureGuard:
    """Track per-connection buffering, pause upstream reads and drain fairly."""

    def __init__(self, *, high_watermark: int = 8192, low_watermark: int = 2048,
                 hard_cap: int = 65536, grace: float = 30.0, quantum: int = 1000,
                 tenant_quota: int = 1 << 20, clock=None) -> None:
        self.high_watermark = high_watermark
        self.low_watermark = low_watermark
        self.hard_cap = hard_cap
        self.grace = grace
        self.quantum = quantum
        self.tenant_quota = tenant_quota
        self._clock = clock
        self._lock = threading.RLock()

    def register_conn(self, conn_id: str, tenant: str) -> dict:
        raise NotImplementedError

    def list_conns(self) -> list:
        raise NotImplementedError

    def on_read(self, conn_id: str, nbytes: int, now=None) -> bool:
        raise NotImplementedError

    def drain(self, conn_id: str, nbytes: int, now=None) -> int:
        raise NotImplementedError

    def read_paused(self, conn_id: str) -> bool:
        raise NotImplementedError

    def pending_bytes(self, conn_id: str) -> int:
        raise NotImplementedError

    def schedule(self, budget_bytes: int, now=None) -> list:
        raise NotImplementedError

    def expire_slow(self, now) -> list:
        raise NotImplementedError

    def close_conn(self, conn_id: str) -> int:
        raise NotImplementedError

    def tenant_usage(self, tenant: str) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
