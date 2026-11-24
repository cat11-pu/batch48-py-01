"""TLS 1.3 key phase epochs with retention windows and per epoch sequences."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "updates_accepted",
    "updates_rejected",
    "records_encrypted",
    "records_decrypted",
    "discarded",
    "rejected",
)


class KeyPhaseError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class KeyPhaseGuard:
    """Track key epochs, retained receive keys and per epoch sequence spaces."""

    def __init__(self, *, seq_limit: int = 8, max_updates: int = 4,
                 min_update_interval_ms: int = 500, retain_epochs: int = 1,
                 retain_ms: int = 2000, clock=None) -> None:
        self.seq_limit = seq_limit
        self.max_updates = max_updates
        self.min_update_interval_ms = min_update_interval_ms
        self.retain_epochs = retain_epochs
        self.retain_ms = retain_ms
        self._clock = clock
        self._lock = threading.RLock()

    def start(self, *, now: int = 0) -> dict:
        raise NotImplementedError

    def key_phase(self) -> dict:
        raise NotImplementedError

    def request_update(self, *, now: int = 0) -> dict:
        raise NotImplementedError

    def on_key_update(self, *, now: int = 0) -> dict:
        raise NotImplementedError

    def encrypt(self, seq: int, *, now: int = 0) -> dict:
        raise NotImplementedError

    def decrypt(self, epoch: int, seq: int, *, now: int = 0) -> dict:
        raise NotImplementedError

    def track_write(self, seq: int, *, now: int = 0) -> dict:
        raise NotImplementedError

    def ack_write(self, epoch: int, seq: int) -> dict:
        raise NotImplementedError

    def pending_writes(self) -> int:
        raise NotImplementedError

    def expire(self, *, now: int = 0) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
