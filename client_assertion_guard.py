"""Client assertion guard: private_key_jwt verification, kid rotation and jti ledger."""

from __future__ import annotations

import threading


class ClientAssertionError(ValueError):
    """Raised when a client assertion is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClientAssertionGuard:
    """Verify HS256 client assertions and keep a bounded one-time jti ledger."""

    def __init__(self, *, endpoint: str = "https://idp.example.com/token",
                 max_ttl: int = 300, jti_ttl: int = 600, max_jti: int = 256,
                 clock=None) -> None:
        self.endpoint = endpoint
        self.max_ttl = max_ttl
        self.jti_ttl = jti_ttl
        self.max_jti = max_jti
        self._clock = clock
        self._lock = threading.RLock()

    def register_key(self, *, client_id: str, kid: str, secret: bytes,
                     active: bool = True) -> None:
        raise NotImplementedError

    def activate_key(self, *, client_id: str, kid: str) -> None:
        raise NotImplementedError

    def retire_key(self, *, client_id: str, kid: str) -> None:
        raise NotImplementedError

    def active_kid(self, client_id: str) -> str:
        raise NotImplementedError

    def verify_assertion(self, assertion: str, *, now=None,
                         require_active: bool = False) -> dict:
        raise NotImplementedError

    def consume_jti(self, *, client_id: str, jti: str, now=None) -> None:
        raise NotImplementedError

    def is_jti_consumed(self, client_id: str, jti: str, now=None) -> bool:
        raise NotImplementedError

    def purge_jti(self, now=None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
