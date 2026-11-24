"""TCP Fast Open cookie service with key rotation and address binding."""

from __future__ import annotations

import threading


class TfoError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TfoCookieGuard:
    """Issue, verify and rotate TFO cookies bound to client addresses."""

    def __init__(self, *, cookie_ttl_ms: int = 60000, key_grace_ms: int = 30000,
                 rate_limit: int = 3, rate_window_ms: int = 1000) -> None:
        self.cookie_ttl_ms = cookie_ttl_ms
        self.key_grace_ms = key_grace_ms
        self.rate_limit = rate_limit
        self.rate_window_ms = rate_window_ms
        self._lock = threading.RLock()

    def rotate_key(self, key_id: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def revoke_key(self, key_id: str) -> None:
        raise NotImplementedError

    def issue(self, addr: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def verify(self, addr: str, cookie: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def normalize_addr(self, addr: str) -> str:
        raise NotImplementedError

    def sfo_deliverable(self, addr: str, cookie: str, *, now_ms: int = 0) -> bool:
        raise NotImplementedError

    def list_keys(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
