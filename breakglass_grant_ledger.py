"""Break-glass grant ledger with review, action trail and retention."""

from __future__ import annotations

import threading


class BreakglassError(ValueError):
    """Refusal raised while managing a break-glass grant."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class BreakglassLedger:
    """Track emergency grants, reviewers, actions and cleanup."""

    def __init__(self, *, default_ttl: int = 600, max_ttl: int = 3600,
                 closed_retention: int = 86400, clock=None) -> None:
        self.default_ttl = default_ttl
        self.max_ttl = max_ttl
        self.closed_retention = closed_retention
        self._clock = clock
        self._lock = threading.RLock()

    def activate(self, operator: str, scope: str, incident_id: str,
                 ttl: int | None = None, now: int | None = None) -> dict:
        raise NotImplementedError

    def acknowledge(self, grant_id: str, reviewer: str,
                    now: int | None = None) -> dict:
        raise NotImplementedError

    def use(self, grant_id: str, action: str, scope: str | None = None,
            now: int | None = None) -> dict:
        raise NotImplementedError

    def close(self, grant_id: str, note: str,
              now: int | None = None) -> dict:
        raise NotImplementedError

    def sweep(self, now: int) -> int:
        raise NotImplementedError

    def audit_trail(self, grant_id: str) -> dict:
        raise NotImplementedError

    def active(self) -> list:
        raise NotImplementedError

    def get(self, grant_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
