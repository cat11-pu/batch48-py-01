"""Admission control for a privileged helper: peers, argv whitelist, audit."""

from __future__ import annotations

import threading


class HelperError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PrivilegedHelperGuard:
    """Check peer credentials, normalize argv, match whitelist and audit."""

    def __init__(self, *, max_audit: int = 256, max_args: int = 16,
                 clock=None) -> None:
        self.max_audit = max_audit
        self.max_args = max_args
        self._clock = clock
        self._lock = threading.RLock()

    def allow_peer(self, uid: int, gid: int, exe_prefix: str) -> None:
        raise NotImplementedError

    def revoke_peer(self, uid: int, gid: int) -> None:
        raise NotImplementedError

    def register_command(self, command: str, *, argv_prefix=(),
                         max_args: int = 8) -> dict:
        raise NotImplementedError

    def normalize(self, args) -> list:
        raise NotImplementedError

    def handle(self, request: dict, *, now=None) -> dict:
        raise NotImplementedError

    def audit_entries(self) -> list:
        raise NotImplementedError

    def whitelist_version(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
