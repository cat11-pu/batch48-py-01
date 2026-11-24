"""CPU bandwidth broker: per-group quotas with short-lived idle borrowing."""

from __future__ import annotations

import threading


class CpuBrokerError(ValueError):
    """Rejection raised by the CPU bandwidth broker."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CpuBandwidthBroker:
    """Hand out fixed quotas, lend idle shares and reclaim loans at period end."""

    def __init__(self, *, global_shares: int = 800, min_granularity: int = 10,
                 max_loans: int = 32, max_period: int = 1000, max_groups: int = 8,
                 clock=None) -> None:
        self.global_shares = global_shares
        self.min_granularity = min_granularity
        self.max_loans = max_loans
        self.max_period = max_period
        self.max_groups = max_groups
        self._clock = clock
        self._lock = threading.RLock()

    def set_quota(self, group: str, quota: int, period: int) -> dict:
        raise NotImplementedError

    def borrow(self, group: str, shares: int, now: int) -> dict:
        raise NotImplementedError

    def return_loan(self, loan_id: str, now: int | None = None) -> dict:
        raise NotImplementedError

    def reclaim(self, group: str, now: int | None = None) -> list:
        raise NotImplementedError

    def charge(self, group: str, shares: int, now: int) -> dict:
        raise NotImplementedError

    def usage(self, group: str, window) -> dict:
        raise NotImplementedError

    def global_free(self, now: int | None = None) -> int:
        raise NotImplementedError

    def loan_status(self, loan_id: str, now: int | None = None) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
