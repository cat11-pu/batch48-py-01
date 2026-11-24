"""DKIM signing and verification guard."""

from __future__ import annotations

import threading


class DkimError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DkimSignatureGuard:
    """Canonicalise headers and bodies, hash, sign and verify mail."""

    def __init__(self, *, max_keys: int = 64, clock=None) -> None:
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.RLock()

    def add_selector_key(self, domain: str, selector: str, private_key: bytes,
                         *, algorithm: str = "hmac-sha256",
                         not_before=None, expires=None, now=None) -> dict:
        raise NotImplementedError

    def rotate_selector(self, domain: str, selector: str, private_key: bytes,
                        *, grace_seconds: int = 86400, now=None) -> dict:
        raise NotImplementedError

    def sign(self, *, domain: str, selector: str, headers: list,
             body: bytes, signed_headers=(), canonicalization: str = "relaxed",
             header_modes: dict | None = None, length: int | None = None,
             now=None) -> dict:
        raise NotImplementedError

    def verify_message(self, message: dict, *, now=None) -> dict:
        raise NotImplementedError

    def canonicalize_header(self, name: str, value: str, mode: str) -> bytes:
        raise NotImplementedError

    def canonicalize_body(self, body: bytes, *, mode: str = "relaxed",
                          length: int | None = None) -> bytes:
        raise NotImplementedError

    def body_hash(self, body: bytes, *, length: int | None = None,
                  mode: str = "relaxed") -> str:
        raise NotImplementedError

    def selector_state(self, domain: str, selector: str, *,
                       now=None) -> dict:
        raise NotImplementedError

    def expire_selectors(self, *, now=None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
