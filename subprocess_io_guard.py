"""Subprocess I/O guard: bounded pipes, byte-exact reads, process-group teardown."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class IoGuardError(ValueError):
    """Raised when a subprocess I/O operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class IoLimits:
    max_buffer: int = 65536
    line_limit: int = 4096
    graceful_seconds: float = 1.0


class SubprocessIoGuard:
    """Wrap child processes with isolated, metered stdin/stdout/stderr."""

    def __init__(self, *, max_handles: int = 32) -> None:
        self.max_handles = max_handles
        self._lock = threading.RLock()

    def spawn(self, argv: object, limits: IoLimits | None = None) -> str:
        raise NotImplementedError

    def write_stdin(self, handle: str, data: bytes) -> int:
        raise NotImplementedError

    def close_stdin(self, handle: str) -> None:
        raise NotImplementedError

    def read_stdout(self, handle: str, max_bytes: int) -> bytes:
        raise NotImplementedError

    def read_stderr(self, handle: str, max_bytes: int) -> bytes:
        raise NotImplementedError

    def read_lines(self, handle: str) -> list:
        raise NotImplementedError

    def drain(self, handle: str) -> bytes:
        raise NotImplementedError

    def wait(self, handle: str, timeout: float) -> int:
        raise NotImplementedError

    def kill_tree(self, handle: str) -> str:
        raise NotImplementedError

    def close(self, handle: str) -> None:
        raise NotImplementedError

    def handles(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
