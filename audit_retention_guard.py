"""Audit retention guard: retention policies, legal holds and purge receipts."""

from __future__ import annotations

import threading


class RetentionError(ValueError):
    """Rejection raised by the retention guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RetentionGuard:
    """Enforce retention periods, legal holds and auditable destruction."""

    def __init__(self, *, max_records: int = 500, max_holds: int = 32,
                 clock=None) -> None:
        self.max_records = max_records
        self.max_holds = max_holds
        self._clock = clock
        self._lock = threading.RLock()

    def set_policy(self, tenant: str, category: str, days: int) -> dict:
        raise NotImplementedError

    def policy_for(self, tenant: str, category: str) -> dict:
        raise NotImplementedError

    def add_record(self, tenant: str, category: str, at: int,
                   record_id: str | None = None) -> dict:
        raise NotImplementedError

    def hold(self, tenant: str, scope: str, until: int, reason: str) -> dict:
        raise NotImplementedError

    def release(self, hold_id: str, now: int | None = None) -> dict:
        raise NotImplementedError

    def hold_status(self, hold_id: str) -> dict:
        raise NotImplementedError

    def purge(self, now: int) -> dict:
        raise NotImplementedError

    def query(self, tenant: str, start: int, end: int) -> list:
        raise NotImplementedError

    def describe_receipt(self, receipt_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
