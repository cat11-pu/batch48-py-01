"""Per-session clipboard isolation with sensitivity marks and TTL."""

from __future__ import annotations

import threading


class ClipboardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClipboardIsolationGuard:
    """Keep clipboard entries scoped to a session, with limits and expiry."""

    def __init__(self, *, max_entries: int = 8, max_bytes: int = 4096,
                 default_ttl: int = 300, clock=None) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.default_ttl = default_ttl
        self._clock = clock
        self._lock = threading.RLock()

    def open_session(self, session: str, *, user: str) -> dict:
        raise NotImplementedError

    def write(self, session: str, payload, *, mime: str = "text/plain",
              sensitive: bool = False, pinned: bool = False,
              ttl=None, now=None) -> dict:
        raise NotImplementedError

    def read(self, session: str, entry_id: str, *, now=None) -> dict:
        raise NotImplementedError

    def paste(self, source: str, target: str, entry_id: str, *,
              now=None) -> dict:
        raise NotImplementedError

    def list_entries(self, session: str, *, now=None) -> list:
        raise NotImplementedError

    def lock_session(self, session: str) -> int:
        raise NotImplementedError

    def unlock_session(self, session: str) -> dict:
        raise NotImplementedError

    def logout(self, session: str) -> int:
        raise NotImplementedError

    def expire(self, *, now=None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
