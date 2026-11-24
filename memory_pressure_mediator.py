"""Memory pressure mediator: watermarks, throttling and OOM arbitration."""

from __future__ import annotations

import threading


class MemoryPressureError(ValueError):
    """Rejection raised by the memory pressure mediator."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MemoryPressureMediator:
    """Watch control groups, grade pressure and arbitrate OOM victims."""

    def __init__(self, *, window: int = 3, high_watermark_ratio: float = 0.9,
                 max_groups: int = 16, clock=None) -> None:
        self.window = window
        self.high_watermark_ratio = high_watermark_ratio
        self.max_groups = max_groups
        self._clock = clock
        self._lock = threading.RLock()

    def register_group(self, group: str, tenant: str, limit: int, high: int | None = None) -> dict:
        raise NotImplementedError

    def list_groups(self) -> list:
        raise NotImplementedError

    def observe(self, group: str, sample: dict) -> dict:
        raise NotImplementedError

    def classify(self, group: str) -> str:
        raise NotImplementedError

    def pressure(self, group: str) -> dict:
        raise NotImplementedError

    def throttle(self, group: str, target: int) -> dict:
        raise NotImplementedError

    def clear_throttle(self, group: str) -> dict:
        raise NotImplementedError

    def throttle_state(self, group: str) -> dict:
        raise NotImplementedError

    def pick_oom_victim(self, group: str) -> dict:
        raise NotImplementedError

    def reclaim(self, group: str, nbytes: int) -> dict:
        raise NotImplementedError

    def group_usage(self, group: str) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
