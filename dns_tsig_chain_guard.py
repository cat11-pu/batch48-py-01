"""TSIG chain guard for a secondary DNS server."""

from __future__ import annotations

import threading


class DnsTsigError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DnsTsigChainGuard:
    """Normalise TSIG records, rotate signing keys and reject replays."""

    def __init__(self, *, clock_skew: int = 300, replay_window: int = 600,
                 max_keys: int = 32, max_replay: int = 4096,
                 clock=None) -> None:
        self.clock_skew = clock_skew
        self.replay_window = replay_window
        self.max_keys = max_keys
        self.max_replay = max_replay
        self._clock = clock
        self._lock = threading.RLock()

    def register_key(self, key_name: str, secret: bytes, *,
                     algorithm: str = "hmac-sha256", now=None) -> dict:
        raise NotImplementedError

    def begin_rotation(self, key_name: str, new_secret: bytes, *,
                       algorithm: str = "hmac-sha256",
                       overlap_seconds: int = 600, now=None) -> dict:
        raise NotImplementedError

    def retire_key(self, key_name: str, generation: int, *,
                   now=None) -> None:
        raise NotImplementedError

    def active_keys(self, key_name: str, *, now=None) -> list[dict]:
        raise NotImplementedError

    def sign_message(self, message: bytes, *, key_name: str,
                     generation: int | None = None, fudge: int = 300,
                     now=None) -> dict:
        raise NotImplementedError

    def verify_tsig(self, message: bytes, tsig: dict, *, now=None) -> dict:
        raise NotImplementedError

    def verify_and_sign_error(self, message: bytes, tsig: dict, *,
                              now=None) -> dict:
        raise NotImplementedError

    def purge_replay(self, *, now=None) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
