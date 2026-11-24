"""Auth code exchange guard: PKCE verification, one-time redemption, replay revocation."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class AuthCodeError(ValueError):
    """Raised when an authorization code operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AuthCodeRecord:
    code: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    challenge: str
    method: str
    issued_at: str
    expires_at: str
    consumed: bool
    token_ids: tuple[str, ...]


class AuthCodeExchangeGuard:
    """Track authorization codes from issuance to redemption and revoke on replay."""

    def __init__(self, *, ttl: int = 600, max_codes: int = 100, clock=None) -> None:
        self.ttl = ttl
        self.max_codes = max_codes
        self._clock = clock
        self._lock = threading.RLock()

    def issue_code(self, *, client_id: str, redirect_uri: str, challenge: str,
                   method: str = "S256", scopes=(), now=None) -> str:
        raise NotImplementedError

    def redeem(self, *, code: str, client_id: str, redirect_uri: str,
               verifier: str, now=None) -> dict:
        raise NotImplementedError

    def revoke_derived(self, code: str) -> int:
        raise NotImplementedError

    def inspect_code(self, code: str) -> dict:
        raise NotImplementedError

    def active_codes(self, client_id: str) -> list[str]:
        raise NotImplementedError

    def token_usable(self, token_id: str) -> bool:
        raise NotImplementedError

    def purge_expired(self, now=None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
