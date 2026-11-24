"""Revocation effectiveness barrier with lease draining and hard expiry."""

from __future__ import annotations

import threading


class RevocationError(ValueError):
    """Raised when a revocation barrier operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RevocationDrainBarrier:
    """Stop new credentials, drain in-flight leases and enforce a hard cutoff."""

    def __init__(self, *, lease_ttl: int = 60, hard_cap: int = 600,
                 clock=None) -> None:
        self.lease_ttl = lease_ttl
        self.hard_cap = hard_cap
        self._clock = clock
        self._lock = threading.RLock()

    def configure_tenant(self, tenant: str, drain_window: int) -> dict:
        raise NotImplementedError

    def begin_revocation(self, tenant: str, *, now: int) -> dict:
        raise NotImplementedError

    def issue_token(self, tenant: str, subject: str, *, now: int) -> dict:
        raise NotImplementedError

    def acquire_lease(self, tenant: str, subject: str, request_id: str,
                      *, now: int) -> dict:
        raise NotImplementedError

    def release_lease(self, tenant: str, lease_id: str) -> bool:
        raise NotImplementedError

    def drain_status(self, tenant: str, *, now: int) -> dict:
        raise NotImplementedError

    def enforce(self, tenant: str, *, now: int) -> dict:
        raise NotImplementedError

    def token_state(self, token_id: str, *, now: int) -> dict:
        raise NotImplementedError

    def lease_state(self, tenant: str, lease_id: str, *, now: int) -> dict:
        raise NotImplementedError

    def generation(self, tenant: str) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
