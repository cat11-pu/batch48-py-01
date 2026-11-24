"""Reconcile declared sandbox permissions with observed usage."""

from __future__ import annotations

import threading

DEFAULT_ALIASES = {
    "read": "fs.read",
    "write": "fs.write",
    "open": "fs.open",
    "connect": "net.connect",
    "listen": "net.listen",
    "dns": "net.dns",
    "exec": "proc.exec",
    "spawn": "proc.spawn",
}


class PermissionAuditorError(ValueError):
    """Refusal raised by the sandbox permission auditor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PermissionAuditor:
    """Compare declared permissions against the operations actually seen."""

    def __init__(self, *, max_jobs: int = 32, max_permissions: int = 64,
                 aliases=None, clock=None) -> None:
        self.max_jobs = max_jobs
        self.max_permissions = max_permissions
        self.aliases = dict(DEFAULT_ALIASES if aliases is None else aliases)
        self._clock = clock
        self._lock = threading.RLock()

    def declare(self, job_id: str, permissions, ttl: int | None = None,
                now: int | None = None) -> dict:
        raise NotImplementedError

    def observe(self, job_id: str, permission: str, count: int = 1,
                now: int | None = None) -> dict:
        raise NotImplementedError

    def report(self, job_id: str) -> dict:
        raise NotImplementedError

    def expire(self, now: int) -> int:
        raise NotImplementedError

    def baseline(self, job_id: str) -> dict:
        raise NotImplementedError

    def get(self, job_id: str) -> dict:
        raise NotImplementedError

    def jobs(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
