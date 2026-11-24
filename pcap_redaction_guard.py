"""Capture-file redaction and access guard."""

from __future__ import annotations

import threading


class PcapGuardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PcapRedactionGuard:
    """Redact credentials before capture files hit disk, then audit reads."""

    def __init__(self, *, rotation_seconds: int = 3600,
                 rotation_bytes: int = 64 * 1024 * 1024,
                 retention_seconds: int = 7 * 24 * 3600,
                 max_sessions: int = 2048, clock=None) -> None:
        self.rotation_seconds = rotation_seconds
        self.rotation_bytes = rotation_bytes
        self.retention_seconds = retention_seconds
        self.max_sessions = max_sessions
        self._clock = clock
        self._lock = threading.RLock()

    def authorize_reader(self, requester: str) -> None:
        raise NotImplementedError

    def begin_file(self, path: str, *, now=None) -> dict:
        raise NotImplementedError

    def append_packet(self, file_id: str, packet: dict, *,
                      now=None) -> dict:
        raise NotImplementedError

    def redact_packet(self, packet: dict) -> dict:
        raise NotImplementedError

    def detect_credentials(self, payload: bytes, *, protocol: str) -> list[dict]:
        raise NotImplementedError

    def close_file(self, file_id: str, *, now=None) -> dict:
        raise NotImplementedError

    def maybe_rotate(self, *, now=None) -> dict | None:
        raise NotImplementedError

    def rotate(self, *, now=None) -> dict:
        raise NotImplementedError

    def open_for_read(self, file_id: str, *, requester: str, start: int = 0,
                      end: int | None = None, now=None) -> dict:
        raise NotImplementedError

    def audit_log(self) -> list[dict]:
        raise NotImplementedError

    def purge_expired(self, *, now=None) -> list[str]:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
