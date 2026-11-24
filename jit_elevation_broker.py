"""Just-in-time elevation broker for short-lived privileged sessions."""

from __future__ import annotations

import threading

ROLES = {"viewer": 1, "operator": 2, "admin": 3, "owner": 4}


class ElevationError(ValueError):
    """Refusal raised by the elevation broker."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ElevationBroker:
    """Accept elevation requests, collect approvals and issue session grants."""

    def __init__(self, *, max_ttl: int = 3600, default_ttl: int = 900,
                 required_approvals: int = 2, max_pending: int = 64,
                 clock=None) -> None:
        self.max_ttl = max_ttl
        self.default_ttl = default_ttl
        self.required_approvals = required_approvals
        self.max_pending = max_pending
        self._clock = clock
        self._lock = threading.RLock()

    def register_approver(self, approver: str, roles) -> dict:
        raise NotImplementedError

    def revoke_approver(self, approver: str) -> dict:
        raise NotImplementedError

    def request_access(self, subject: str, role: str, reason: str,
                       ttl: int | None = None, now: int | None = None) -> dict:
        raise NotImplementedError

    def request_status(self, request_id: str) -> dict:
        raise NotImplementedError

    def approve(self, request_id: str, approver: str,
                now: int | None = None) -> dict:
        raise NotImplementedError

    def deny(self, request_id: str, approver: str, note: str,
             now: int | None = None) -> dict:
        raise NotImplementedError

    def redeem(self, request_id: str, session_id: str, now: int) -> dict:
        raise NotImplementedError

    def validate(self, grant_id: str, session_id: str, role: str | None = None,
                 now: int | None = None) -> dict:
        raise NotImplementedError

    def revoke(self, grant_id: str, now: int | None = None) -> dict:
        raise NotImplementedError

    def expire(self, now: int) -> int:
        raise NotImplementedError

    def grants(self, subject: str | None = None) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
