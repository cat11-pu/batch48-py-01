"""OAuth consent grant registry: incremental scopes, prompt suppression, revocation cascade."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class ConsentError(ValueError):
    """Raised when a consent operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GrantRecord:
    tenant: str
    user: str
    client: str
    scopes: tuple[str, ...]
    version: int
    granted_at: str
    expires_at: str | None


@dataclass(frozen=True)
class ConsentDecision:
    status: str
    record: GrantRecord | None
    missing_scopes: tuple[str, ...]
    rejected_scopes: tuple[str, ...]


class ConsentGrantRegistry:
    """Keep per (tenant, user, client) consent records and derived token state."""

    def __init__(self, *, max_grants_per_tenant: int = 100,
                 default_ttl: int = 86400, clock=None) -> None:
        self.max_grants_per_tenant = max_grants_per_tenant
        self.default_ttl = default_ttl
        self._clock = clock
        self._lock = threading.RLock()

    def grant_consent(self, *, tenant: str, user: str, client: str,
                      scopes, now=None) -> ConsentDecision:
        raise NotImplementedError

    def check_consent(self, *, tenant: str, user: str, client: str,
                      scopes, now=None) -> ConsentDecision:
        raise NotImplementedError

    def withdraw_consent(self, *, tenant: str, user: str, client: str,
                         now=None) -> int:
        raise NotImplementedError

    def register_token(self, *, tenant: str, user: str, client: str,
                       token_id: str, scopes=(), now=None) -> None:
        raise NotImplementedError

    def token_usable(self, token_id: str) -> bool:
        raise NotImplementedError

    def mark_scope_deprecated(self, scope: str) -> None:
        raise NotImplementedError

    def purge_expired(self, now=None) -> int:
        raise NotImplementedError

    def records_for(self, tenant: str) -> list[GrantRecord]:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
