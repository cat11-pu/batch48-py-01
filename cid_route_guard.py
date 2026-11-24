"""QUIC connection-ID routing guard with retire table and stateless reset."""

from __future__ import annotations

import threading


class CidError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CidRouteGuard:
    """Allocate, route and retire destination connection IDs per worker."""

    def __init__(self, *, cid_len: int = 8, worker_capacity: int = 64,
                 retire_grace_ms: int = 5000, reset_key: bytes = b"") -> None:
        self.cid_len = cid_len
        self.worker_capacity = worker_capacity
        self.retire_grace_ms = retire_grace_ms
        self.reset_key = reset_key
        self._lock = threading.RLock()

    def allocate(self, worker: str, *, now_ms: int = 0,
                 client_cid: str | None = None) -> dict:
        raise NotImplementedError

    def route(self, dcid: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def retire(self, cid: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def reset_token(self, cid: str) -> str:
        raise NotImplementedError

    def verify_token(self, cid: str, token: str) -> bool:
        raise NotImplementedError

    def parse_cid(self, cid: str) -> dict:
        raise NotImplementedError

    def worker_stats(self, worker: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
