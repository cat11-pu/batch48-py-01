"""Vertex attribute quantisation with per semantic error budgets."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "layouts_declared",
    "meshes_quantized",
    "meshes_dequantized",
    "cache_hits",
    "rejected",
)


class QuantizerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class VertexAttributeQuantizer:
    """Declare vertex layouts, quantise meshes and decode them back."""

    def __init__(self, *, max_vertices: int = 4096, max_bits_per_vertex: int = 128,
                 max_semantics: int = 8, clock=None) -> None:
        self.max_vertices = max_vertices
        self.max_bits_per_vertex = max_bits_per_vertex
        self.max_semantics = max_semantics
        self._clock = clock
        self._lock = threading.RLock()

    def declare_layout(self, layout_id: str, spec: dict, *, version: int = 1) -> dict:
        raise NotImplementedError

    def layout_info(self, layout_id: str, *, version: int | None = None) -> dict:
        raise NotImplementedError

    def drop_layout(self, layout_id: str) -> None:
        raise NotImplementedError

    def error_bound(self, layout_id: str, semantic: str, *, extent: float = 1.0,
                    version: int | None = None) -> float:
        raise NotImplementedError

    def quantize_mesh(self, mesh: dict, layout_id: str, *, wrap_uv: bool = False) -> dict:
        raise NotImplementedError

    def dequantize_mesh(self, encoded: dict) -> dict:
        raise NotImplementedError

    def payload_bytes(self, encoded: dict) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
