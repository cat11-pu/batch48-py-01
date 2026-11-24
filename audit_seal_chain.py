"""Audit seal chain: append events, seal segments and verify the hash chain."""

from __future__ import annotations

import threading


class SealChainError(ValueError):
    """Rejection raised by the audit seal chain."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuditSealChain:
    """Keep an append-only audit log sealed into verifiable segments."""

    def __init__(self, *, segment_size: int = 3, max_records: int = 1000,
                 digest_len: int = 16, clock=None) -> None:
        self.segment_size = segment_size
        self.max_records = max_records
        self.digest_len = digest_len
        self._clock = clock
        self._lock = threading.RLock()

    def append_event(self, event: dict) -> int:
        raise NotImplementedError

    def seal(self, up_to: int | None = None) -> dict:
        raise NotImplementedError

    def verify(self, segment: dict) -> dict:
        raise NotImplementedError

    def checkpoint(self) -> dict:
        raise NotImplementedError

    def adopt_checkpoint(self, checkpoint: dict) -> dict:
        raise NotImplementedError

    def find_gap(self, start: int, end: int) -> list:
        raise NotImplementedError

    def export(self, start: int, end: int) -> dict:
        raise NotImplementedError

    def import_payload(self, payload: dict) -> dict:
        raise NotImplementedError

    def head(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
