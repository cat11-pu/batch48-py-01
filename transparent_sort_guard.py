"""Transparent draw ordering with packed 64-bit sort keys."""

from __future__ import annotations


class TransparentSortError(ValueError):
    """Raised when a transparent object or sort key is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TransparentSortGuard:
    """Build unique 64-bit keys and keep transparent draw order stable."""

    def __init__(self, *, depth_bits: int = 24, depth_min: float = 0.0,
                 depth_max: float = 1000.0, max_objects: int = 1024,
                 sort_budget: int = 64) -> None:
        self.depth_bits = depth_bits
        self.depth_min = depth_min
        self.depth_max = depth_max
        self.max_objects = max_objects
        self.sort_budget = sort_budget

    def add(self, object_id: str, *, layer: int = 0, material: str = "default",
            double_sided: bool = False, depth: float = 0.0) -> dict:
        raise NotImplementedError

    def remove(self, object_id: str) -> None:
        raise NotImplementedError

    def update_depth(self, object_id: str, depth: float) -> int:
        raise NotImplementedError

    def bucket_of(self, object_id: str) -> int:
        raise NotImplementedError

    def key_for(self, object_id: str, *, side: int = 0) -> int:
        raise NotImplementedError

    def keys(self, object_id: str) -> list[int]:
        raise NotImplementedError

    def object_count(self) -> int:
        raise NotImplementedError

    def sort(self) -> list[dict]:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
