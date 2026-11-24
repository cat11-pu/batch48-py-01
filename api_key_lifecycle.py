"""Tenant API key issuing, prefix lookup, rotation and revocation."""

from __future__ import annotations

import threading


class ApiKeyError(ValueError):
    """Raised when an API key operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ApiKeyLifecycleManager:
    """Issue tenant keys, keep only prefix indexes and digests, verify them."""

    def __init__(self, *, max_keys_per_tenant: int = 5,
                 max_scopes_per_key: int = 4, prefix_len: int = 8,
                 clock=None) -> None:
        self.max_keys_per_tenant = max_keys_per_tenant
        self.max_scopes_per_key = max_scopes_per_key
        self.prefix_len = prefix_len
        self._clock = clock
        self._lock = threading.RLock()

    def issue_key(self, tenant: str, *, scopes, cidrs=(), expires_at=None,
                  now: int = 0) -> dict:
        raise NotImplementedError

    def list_keys(self, tenant: str, *, now: int = 0) -> list:
        raise NotImplementedError

    def revoke_key(self, tenant: str, key_id: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def rotate_key(self, tenant: str, key_id: str, *, overlap: int = 300,
                   now: int = 0) -> dict:
        raise NotImplementedError

    def rotation_status(self, tenant: str, key_id: str) -> dict:
        raise NotImplementedError

    def settle_rotation(self, tenant: str, key_id: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def verify_key(self, secret: str, *, tenant: str | None = None,
                   scope: str | None = None, source: str | None = None,
                   now: int = 0) -> dict:
        raise NotImplementedError

    def touch_last_used(self, key_id: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def flush_last_used(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
