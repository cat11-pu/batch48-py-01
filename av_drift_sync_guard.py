"""Clock drift estimation and watermark compensation based on RTCP sender reports."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "streams_registered",
    "streams_removed",
    "sr_ingested",
    "sr_rejected",
    "samples_dropped",
    "ratio_clamped",
    "drift_warnings",
    "audio_padded_ms",
    "audio_dropped_ms",
)


class SyncError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AvDriftSyncGuard:
    """Map RTP to NTP, estimate audio drift and schedule video on the main clock."""

    def __init__(self, *, max_streams: int = 8, max_samples: int = 32,
                 clamp_low: float = 0.98, clamp_high: float = 1.02,
                 warn_ms: float = 200.0, early_ms: float = 40.0,
                 late_ms: float = 120.0, low_watermark_ms: float = 60.0,
                 high_watermark_ms: float = 240.0, clock=None) -> None:
        self.max_streams = max_streams
        self.max_samples = max_samples
        self.clamp_low = clamp_low
        self.clamp_high = clamp_high
        self.warn_ms = warn_ms
        self.early_ms = early_ms
        self.late_ms = late_ms
        self.low_watermark_ms = low_watermark_ms
        self.high_watermark_ms = high_watermark_ms
        self._clock = clock
        self._lock = threading.RLock()

    def register_stream(self, stream_id: str, *, kind: str,
                        clock_rate: int) -> dict:
        raise NotImplementedError

    def unregister_stream(self, stream_id: str) -> None:
        raise NotImplementedError

    def ingest_sr(self, stream_id: str, *, ntp_ms: float,
                  rtp_ts: int) -> dict:
        raise NotImplementedError

    def drift_ms(self, stream_id: str) -> float:
        raise NotImplementedError

    def drift_ratio(self, stream_id: str) -> float:
        raise NotImplementedError

    def stream_info(self, stream_id: str) -> dict:
        raise NotImplementedError

    def primary_stream(self) -> str | None:
        raise NotImplementedError

    def request_primary(self, stream_id: str) -> dict:
        raise NotImplementedError

    def enqueue_audio(self, stream_id: str, watermark_ms: float, *,
                      now_ms: float | None = None) -> dict:
        raise NotImplementedError

    def schedule_video(self, stream_id: str, pts_ms: float, *,
                       now_ms: float) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
