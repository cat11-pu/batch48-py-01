"""GPU resource handle ledger with generations, deferred destroy and leak audit."""

from __future__ import annotations

import threading

KINDS = ("buffer", "texture", "sampler", "pipeline")

STATS_KEYS = (
    "resources_created",
    "handles_retained",
    "handles_released",
    "frames_begun",
    "frames_completed",
    "reclaimed",
    "device_lost_events",
    "rejected",
)


class LedgerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ResourceHandleLedger:
    """Track GPU resources by generation tagged handles and frame lifetime."""

    def __init__(self, *, max_resources: int = 64, max_open_frames: int = 3,
                 max_rebuild_queue: int = 8, clock=None) -> None:
        self.max_resources = max_resources
        self.max_open_frames = max_open_frames
        self.max_rebuild_queue = max_rebuild_queue
        self._clock = clock
        self._lock = threading.RLock()

    def create(self, name: str, *, kind: str = "buffer", size: int = 0) -> dict:
        raise NotImplementedError

    def resource_info(self, handle: dict) -> dict:
        raise NotImplementedError

    def retain(self, handle: dict, *, count: int = 1) -> dict:
        raise NotImplementedError

    def release(self, handle: dict) -> dict:
        raise NotImplementedError

    def begin_frame(self) -> dict:
        raise NotImplementedError

    def end_frame(self) -> dict:
        raise NotImplementedError

    def reclaim(self) -> dict:
        raise NotImplementedError

    def validate(self, handle: dict) -> dict:
        raise NotImplementedError

    def leak_report(self) -> dict:
        raise NotImplementedError

    def device_lost(self) -> dict:
        raise NotImplementedError

    def rebuild_queue(self) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
