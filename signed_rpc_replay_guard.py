"""Replay guard for HMAC signed RPC requests."""

from __future__ import annotations

import threading


class RpcGuardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SignedRpcReplayGuard:
    """Verify signed RPC envelopes and keep nonce windows per client."""

    def __init__(self, *, clock_skew: int = 30, window: int = 300,
                 per_client_capacity: int = 256, max_clients: int = 1000,
                 clock=None) -> None:
        self.clock_skew = clock_skew
        self.window = window
        self.per_client_capacity = per_client_capacity
        self.max_clients = max_clients
        self._clock = clock
        self._lock = threading.RLock()

    def add_client(self, client_id: str, secret: bytes) -> dict:
        raise NotImplementedError

    def revoke_client(self, client_id: str) -> None:
        raise NotImplementedError

    def sign_request(self, *, client_id: str, method: str, path: str,
                     body: bytes, timestamp: int, nonce: str) -> dict:
        raise NotImplementedError

    def verify_request(self, request: dict, *, now=None) -> dict:
        raise NotImplementedError

    def record_response(self, client_id: str, nonce: str,
                        response: dict) -> dict:
        raise NotImplementedError

    def execute_once(self, request: dict, handler, *, now=None) -> dict:
        raise NotImplementedError

    def nonce_window(self, client_id: str, *, now=None) -> list[str]:
        raise NotImplementedError

    def purge(self, *, now=None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
