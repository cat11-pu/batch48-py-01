"""Visibility buffer resolve over per pixel triangle id and depth data."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "triangles_submitted",
    "pixels_resolved",
    "regions_resolved",
    "cache_hits",
    "rejected",
)


class VisibilityError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class VisibilityBufferResolver:
    """Store triangle ids per pixel and resolve materials by depth."""

    def __init__(self, *, max_width: int = 512, max_height: int = 512,
                 max_candidates: int = 4, max_triangles: int = 1024,
                 clock=None) -> None:
        self.max_width = max_width
        self.max_height = max_height
        self.max_candidates = max_candidates
        self.max_triangles = max_triangles
        self._clock = clock
        self._lock = threading.RLock()

    def resize(self, width: int, height: int) -> dict:
        raise NotImplementedError

    def clear(self) -> dict:
        raise NotImplementedError

    def submit_triangle(self, triangle: dict) -> dict:
        raise NotImplementedError

    def resolve_pixel(self, x: int, y: int) -> dict:
        raise NotImplementedError

    def resolve_region(self, x0: int, y0: int, x1: int, y1: int) -> dict:
        raise NotImplementedError

    def generation(self) -> int:
        raise NotImplementedError

    def buffered_triangles(self) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
