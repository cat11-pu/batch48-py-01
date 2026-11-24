"""Fragmented MP4 moof/mdat bookkeeping for a muxing pipeline."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "samples_added",
    "fragments_flushed",
    "samples_flushed",
    "bytes_flushed",
    "rejected",
    "flushes_requested",
)

SYNC_SAMPLE_FLAGS = 0x02000000
DELTA_SAMPLE_FLAGS = 0x01010000


class Fmp4Error(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class Fmp4MuxGuard:
    """Queue samples and emit fragmented MP4 fragments with stable offsets."""

    def __init__(self, *, stream_id: str = "default",
                 max_fragment_bytes: int = 524288,
                 timescale: int = 1000) -> None:
        self.stream_id = stream_id
        self.max_fragment_bytes = max_fragment_bytes
        self.timescale = timescale
        self._lock = threading.RLock()

    def set_track(self, track: str, *, timescale: int | None = None) -> dict:
        raise NotImplementedError

    def tracks(self) -> dict:
        raise NotImplementedError

    def add_sample(self, track: str, *, dts: int, duration: int, size: int,
                   keyframe: bool = False) -> dict:
        raise NotImplementedError

    def pending_bytes(self) -> int:
        raise NotImplementedError

    def should_flush(self) -> bool:
        raise NotImplementedError

    def flush(self) -> dict:
        raise NotImplementedError

    def fragments(self) -> list:
        raise NotImplementedError

    def sequence(self) -> int:
        raise NotImplementedError

    def sample_offsets(self, fragment_seq: int) -> list:
        raise NotImplementedError

    def tfdt_for(self, track: str) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
