"""Cluster light page cache with dirty-page rebuilding and page codes."""

from __future__ import annotations


class ClusterLightError(ValueError):
    """Raised when a cluster light page operation cannot be completed."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClusterLightPageCache:
    """Cache cluster light indices in pages and rebuild dirty pages only."""

    def __init__(self, *, max_pages: int = 65536,
                 page_capacity: int = 64) -> None:
        self.max_pages = max_pages
        self.page_capacity = page_capacity

    def set_grid(self, x: int, y: int, z: int) -> dict:
        raise NotImplementedError

    def set_page_size(self, size: int) -> int:
        raise NotImplementedError

    def upsert_light(self, light: dict) -> int:
        raise NotImplementedError

    def remove_light(self, index: int) -> int:
        raise NotImplementedError

    def page(self, ix: int, iy: int, iz: int) -> list:
        raise NotImplementedError

    def page_code(self, ix: int, iy: int, iz: int) -> str:
        raise NotImplementedError

    def rebuild(self, dirty_only: bool = True) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
