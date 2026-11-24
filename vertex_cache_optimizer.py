"""Reorder triangle indices to raise post transform vertex cache hits."""

from __future__ import annotations

import threading

WINDINGS = ("ccw", "cw")

STATS_KEYS = (
    "meshes_loaded",
    "optimizations",
    "restores",
    "cache_simulations",
    "rejected",
)


class VertexCacheError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class VertexCacheOptimizer:
    """Plan a deterministic triangle order for a fixed size post transform cache."""

    def __init__(self, *, max_cache_size: int = 32, max_vertices: int = 65536,
                 max_triangles: int = 4096, clock=None) -> None:
        self.max_cache_size = max_cache_size
        self.max_vertices = max_vertices
        self.max_triangles = max_triangles
        self._clock = clock
        self._lock = threading.RLock()

    def load_mesh(self, vertices, triangles) -> dict:
        raise NotImplementedError

    def set_cache_size(self, size: int) -> dict:
        raise NotImplementedError

    def cache_size(self) -> int:
        raise NotImplementedError

    def mesh_info(self) -> dict:
        raise NotImplementedError

    def submeshes(self) -> list:
        raise NotImplementedError

    def original_order(self) -> list:
        raise NotImplementedError

    def optimized_order(self) -> list:
        raise NotImplementedError

    def optimize(self) -> dict:
        raise NotImplementedError

    def measure_hit_rate(self, order=None) -> dict:
        raise NotImplementedError

    def restore_order(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
