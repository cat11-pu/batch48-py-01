"""Receiver-side RTCP feedback accounting and report block generation."""

from __future__ import annotations

import threading
import time


class RtcpFeedbackError(ValueError):
    """Rejection raised by the feedback guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


DEFAULT_MAX_REPORT_BLOCKS = 31
DEFAULT_MAX_STREAMS = 32


class RtcpFeedbackGuard:
    """Track per-SSRC loss, jitter and sender report timing."""

    def __init__(self, *, max_report_blocks: int = DEFAULT_MAX_REPORT_BLOCKS,
                 max_streams: int = DEFAULT_MAX_STREAMS,
                 jitter_scale: int = 16, clock=None) -> None:
        self.max_report_blocks = max_report_blocks
        self.max_streams = max_streams
        self.jitter_scale = jitter_scale
        self._clock = clock
        self._lock = threading.RLock()

    def _now_ms(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.monotonic() * 1000)

    def on_packet(self, ssrc: int, seq: int, *, arrival_ms: int | None = None,
                  payload_size: int = 1000) -> dict:
        raise NotImplementedError

    def on_sender_report(self, ssrc: int, *, ntp_seconds: int,
                         ntp_fraction: int,
                         arrival_ms: int | None = None) -> dict:
        raise NotImplementedError

    def highest_seq(self, ssrc: int) -> int:
        raise NotImplementedError

    def cumulative_lost(self, ssrc: int) -> int:
        raise NotImplementedError

    def fraction_lost(self, ssrc: int) -> int:
        raise NotImplementedError

    def interarrival_jitter(self, ssrc: int) -> int:
        raise NotImplementedError

    def report_block(self, ssrc: int, *, now_ms: int | None = None) -> dict:
        raise NotImplementedError

    def build_report(self, *, reporter_ssrc: int = 0) -> dict:
        raise NotImplementedError

    def restart(self) -> None:
        raise NotImplementedError

    def ssrcs(self) -> list[int]:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
