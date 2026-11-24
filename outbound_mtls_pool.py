"""Outbound mTLS connection pool with tenant, SNI and certificate isolation."""

from __future__ import annotations

import threading


class MtlsPoolError(ValueError):
    """Raised when an outbound mTLS pool operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class OutboundMtlsPool:
    """Track connection identities, per-target caps and draining connections."""

    def __init__(self, *, target_limit: int = 4, queue_timeout: int = 500,
                 clock=None) -> None:
        self.target_limit = target_limit
        self.queue_timeout = queue_timeout
        self._clock = clock
        self._lock = threading.RLock()

    def register_identity(self, identity_id: str, *, tenant: str,
                          fingerprint: str, not_after: int) -> dict:
        raise NotImplementedError

    def register_peer(self, target: str, *, subject: str) -> dict:
        raise NotImplementedError

    def set_target_limit(self, target: str, limit: int) -> int:
        raise NotImplementedError

    def acquire(self, tenant: str, target: str, *, sni: str,
                identity_id: str, expected_subject: str, now: int,
                wait_ms: int = 0) -> dict:
        raise NotImplementedError

    def release(self, lease_id: str, *, now: int, close: bool = False) -> dict:
        raise NotImplementedError

    def mark_failed(self, lease_id: str, *, now: int) -> dict:
        raise NotImplementedError

    def rotate_identity(self, identity_id: str, new_fingerprint: str, *,
                        now: int, overlap: int = 0) -> dict:
        raise NotImplementedError

    def drain_target(self, target: str, *, now: int) -> int:
        raise NotImplementedError

    def waiters(self, target: str) -> int:
        raise NotImplementedError

    def expire_waiters(self, now: int) -> int:
        raise NotImplementedError

    def connection_state(self, conn_id: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
