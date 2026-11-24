"""Sandbox scratch warden: quota-scoped work directories with path containment."""

from __future__ import annotations

import threading

BASE_ROOT = "/srv/scratch"


class ScratchError(ValueError):
    """Raised when a scratch directory operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ScratchWarden:
    """Allocate, meter and reclaim per-job scratch directories."""

    def __init__(self, *, base_root: str = BASE_ROOT, retention: int = 5) -> None:
        self.base_root = base_root
        self.retention = retention
        self._lock = threading.RLock()

    def allocate(self, job_id: str, quota_mb: int = 16, max_files: int = 64,
                 now: int = 0) -> dict:
        raise NotImplementedError

    def resolve(self, job_id: str, relative_path: str) -> str:
        raise NotImplementedError

    def link(self, job_id: str, relative_path: str, target: str) -> str:
        raise NotImplementedError

    def charge(self, job_id: str, byte_count: int, path: str | None = None) -> dict:
        raise NotImplementedError

    def release(self, job_id: str, now: int = 0) -> dict:
        raise NotImplementedError

    def sweep(self, now: int) -> list:
        raise NotImplementedError

    def usage(self, job_id: str) -> dict:
        raise NotImplementedError

    def list_jobs(self) -> list:
        raise NotImplementedError

    def orphans(self) -> list:
        raise NotImplementedError

    def open_file(self, job_id: str, relative_path: str) -> str:
        raise NotImplementedError

    def close_file(self, handle: str) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
