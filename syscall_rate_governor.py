"""Per-process-group syscall admission governed by token buckets."""

from __future__ import annotations

import threading


class RateGovernorError(ValueError):
    """Refusal raised by the syscall rate governor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SyscallRateGovernor:
    """Keep one refillable token bucket per process group."""

    def __init__(self, *, max_groups: int = 64, max_calls: int = 128,
                 clock=None) -> None:
        self.max_groups = max_groups
        self.max_calls = max_calls
        self._clock = clock
        self._lock = threading.RLock()

    def configure(self, group: str, rate: float, burst: float,
                  calls=None, now: float = 0) -> dict:
        raise NotImplementedError

    def admit(self, group: str, call: str, now: float,
              pid: object = None) -> dict:
        raise NotImplementedError

    def refill(self, group: str, now: float) -> dict:
        raise NotImplementedError

    def tokens(self, group: str, now: float | None = None) -> float:
        raise NotImplementedError

    def stats(self, group: str) -> dict:
        raise NotImplementedError

    def reset(self, group: str, now: float = 0) -> dict:
        raise NotImplementedError

    def pick(self, groups, now: float) -> str:
        raise NotImplementedError

    def groups(self) -> list:
        raise NotImplementedError

    def remove(self, group: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
