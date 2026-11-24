"""Skin influence pruning, duplicate-free merging and renormalization."""

from __future__ import annotations


class SkinInfluenceError(ValueError):
    """Raised when a vertex influence set cannot be accepted or pruned."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SkinInfluencePruner:
    """Trim per-vertex bone influences and renormalize the surviving weights."""

    def __init__(self, *, max_bones: int = 65535,
                 max_influences: int = 32,
                 max_vertices: int = 1000000) -> None:
        self.max_bones = max_bones
        self.max_influences = max_influences
        self.max_vertices = max_vertices

    def add_vertex(self, vertex_id: int, influences) -> dict:
        raise NotImplementedError

    def prune(self, vertex_id: int, max_bones: int = 4) -> list:
        raise NotImplementedError

    def normalize(self, weights) -> list:
        raise NotImplementedError

    def remap_bones(self, mapping: dict) -> int:
        raise NotImplementedError

    def vertex(self, vertex_id: int) -> dict:
        raise NotImplementedError

    def kept(self, vertex_id: int):
        raise NotImplementedError

    def errors(self) -> dict:
        raise NotImplementedError

    def remove_vertex(self, vertex_id: int) -> bool:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
