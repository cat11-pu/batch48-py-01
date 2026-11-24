"""Multipath subflow scheduler with DSS mapping dedup and backup promotion."""

from __future__ import annotations

import threading


class MptcpError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MptcpScheduler:
    """Track subflows, RTT estimates, DSS mappings and backup promotion."""

    def __init__(self, *, failure_threshold: int = 3, hysteresis_ms: int = 2000,
                 max_subflows: int = 8, max_mappings: int = 4096) -> None:
        self.failure_threshold = failure_threshold
        self.hysteresis_ms = hysteresis_ms
        self.max_subflows = max_subflows
        self.max_mappings = max_mappings
        self._lock = threading.RLock()

    def add_subflow(self, sid: str, *, path_id: int | None = None,
                    priority: int = 0) -> dict:
        raise NotImplementedError

    def remove_subflow(self, sid: str) -> dict:
        raise NotImplementedError

    def report_path(self, sid: str, *, ok: bool, rtt_ms: int = 0,
                    loss: float = 0.0, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def set_cwnd(self, sid: str, cwnd: int) -> dict:
        raise NotImplementedError

    def schedule(self, size: int, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def ack(self, subflow: str, length: int) -> dict:
        raise NotImplementedError

    def dss_map(self, seq: int, length: int, subflow: str) -> dict:
        raise NotImplementedError

    def enable_backup(self, sid: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def release_backup(self, sid: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def list_subflows(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
