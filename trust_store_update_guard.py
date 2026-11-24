"""Signed incremental trust-root store with atomic root set activation."""

from __future__ import annotations

import threading


class TrustStoreError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TrustStoreUpdateGuard:
    """Verify signed bundles, keep versions monotonic and swap root sets."""

    def __init__(self, *, max_roots: int = 32, clock=None) -> None:
        self.max_roots = max_roots
        self._clock = clock
        self._lock = threading.RLock()

    def register_issuer(self, issuer_id: str, secret: bytes) -> None:
        raise NotImplementedError

    def revoke_issuer(self, issuer_id: str) -> None:
        raise NotImplementedError

    def install(self, bundle: dict, *, now=None) -> dict:
        raise NotImplementedError

    def active_roots(self, *, now=None) -> list:
        raise NotImplementedError

    def legacy_roots(self, *, now=None) -> list:
        raise NotImplementedError

    def lookup(self, subject: str, *, now=None) -> dict:
        raise NotImplementedError

    def current_version(self) -> int:
        raise NotImplementedError

    def rejection_log(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
