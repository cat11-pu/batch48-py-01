"""Container drain orchestration: readiness removal, inflight drain, process-group escalation."""

from __future__ import annotations

import threading
from dataclasses import dataclass

MAIN_PID = 0


class DrainError(ValueError):
    """Raised when a drain operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DrainPolicy:
    grace_seconds: int = 30
    sigterm_delay: int = 5
    max_children: int = 16


class DrainOrchestrator:
    """Drive one container through CREATED/RUNNING/DRAINING/STOPPING/EXITED."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def register(self, container_id: str, policy: DrainPolicy | None = None) -> dict:
        raise NotImplementedError

    def start(self, container_id: str) -> str:
        raise NotImplementedError

    def begin_drain(self, container_id: str, now: int) -> str:
        raise NotImplementedError

    def admit_request(self, container_id: str) -> bool:
        raise NotImplementedError

    def mark_idle(self, container_id: str, now: int) -> dict:
        raise NotImplementedError

    def add_child(self, container_id: str, pid: int) -> int:
        raise NotImplementedError

    def note_exit(self, container_id: str, pid: int) -> int:
        raise NotImplementedError

    def advance(self, now: int) -> dict:
        raise NotImplementedError

    def signals(self, container_id: str) -> list:
        raise NotImplementedError

    def inspect(self, container_id: str) -> dict:
        raise NotImplementedError

    def backlog_count(self, container_id: str) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
