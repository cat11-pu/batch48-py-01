"""ESP extended sequence number anti-replay guard."""

from __future__ import annotations

import threading


class EspError(ValueError):
    """Rejection raised by the ESP replay guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class EspReplayGuard:
    """Track per-SA 64 bit extended sequence numbers with a sliding bitmap."""

    def __init__(self, *, default_window: int = 64, max_report_records: int = 256,
                 clock=None) -> None:
        self.default_window = default_window
        self.max_report_records = max_report_records
        self._clock = clock
        self._lock = threading.RLock()

    def add_sa(self, sa_id: str, *, spi: int, window_size: int = 64,
               soft_lifetime: float = 3600.0, hard_lifetime: float = 7200.0,
               max_bytes: int = 1 << 30, now=None) -> dict:
        raise NotImplementedError

    def remove_sa(self, sa_id: str) -> bool:
        raise NotImplementedError

    def accept(self, sa_id: str, seq: int, *, length: int = 0, now=None) -> bool:
        raise NotImplementedError

    def set_window_size(self, sa_id: str, window_size: int) -> None:
        raise NotImplementedError

    def sa_state(self, sa_id: str) -> dict:
        raise NotImplementedError

    def list_sas(self) -> list:
        raise NotImplementedError

    def replay_report(self, sa_id: str) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
