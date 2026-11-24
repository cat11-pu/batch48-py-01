"""Container restart supervisor with exponential backoff and restart budget."""

from __future__ import annotations

import threading


class RestartSupervisorError(ValueError):
    """Refusal raised by the restart supervisor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RestartSupervisor:
    """Decide when a crashed container may be started again."""

    def __init__(self, *, base_delay: float = 1.0, factor: float = 2.0,
                 max_delay: float = 60.0, budget: int = 5,
                 window: float = 600.0, stable_period: float = 120.0,
                 oom_multiplier: float = 2.0, clock=None) -> None:
        self.base_delay = base_delay
        self.factor = factor
        self.max_delay = max_delay
        self.budget = budget
        self.window = window
        self.stable_period = stable_period
        self.oom_multiplier = oom_multiplier
        self._clock = clock
        self._lock = threading.RLock()

    def configure(self, container_id: str, base_delay: float | None = None,
                  factor: float | None = None, max_delay: float | None = None,
                  budget: int | None = None, window: float | None = None,
                  stable_period: float | None = None,
                  oom_multiplier: float | None = None) -> dict:
        raise NotImplementedError

    def report_exit(self, container_id: str, code: int, at: float) -> dict:
        raise NotImplementedError

    def next_delay(self, container_id: str, now: float) -> dict:
        raise NotImplementedError

    def allow_restart(self, container_id: str, now: float) -> dict:
        raise NotImplementedError

    def mark_stable(self, container_id: str, uptime: float) -> dict:
        raise NotImplementedError

    def budget(self, container_id: str, now: float | None = None) -> dict:
        raise NotImplementedError

    def reset(self, container_id: str) -> dict:
        raise NotImplementedError

    def get(self, container_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
