"""Recording slice alignment, manifest indexing and upload idempotency."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "samples",
    "slices_closed",
    "uploads",
    "retries",
    "cleaned",
    "rejected",
)


class RecordingError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RecordingSliceGuard:
    """Cut recordings on keyframes and track the upload manifest."""

    def __init__(self, *, stream_id: str = "stream-0",
                 target_seconds: float = 10.0,
                 max_slice_bytes: int = 8388608,
                 retention_seconds: int = 3600,
                 max_retries: int = 3, backoff_ms: int = 200) -> None:
        self.stream_id = stream_id
        self.target_seconds = target_seconds
        self.max_slice_bytes = max_slice_bytes
        self.retention_seconds = retention_seconds
        self.max_retries = max_retries
        self.backoff_ms = backoff_ms
        self._lock = threading.RLock()

    def append(self, pts: int, duration: int, size: int, *,
               keyframe: bool = False) -> dict:
        raise NotImplementedError

    def close(self) -> dict | None:
        raise NotImplementedError

    def slices(self) -> list:
        raise NotImplementedError

    def manifest(self) -> dict:
        raise NotImplementedError

    def slice_digest(self, slice_id: str) -> str:
        raise NotImplementedError

    def idempotency_key(self, slice_id: str) -> str:
        raise NotImplementedError

    def mark_uploaded(self, slice_id: str, *, ok: bool) -> dict:
        raise NotImplementedError

    def retry_delay_ms(self, slice_id: str) -> int:
        raise NotImplementedError

    def pending_upload(self) -> list:
        raise NotImplementedError

    def cleanup(self, now: int) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
