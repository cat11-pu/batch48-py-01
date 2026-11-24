"""Texture compression block alignment and mip chain planning."""

from __future__ import annotations

import threading


class TextureError(ValueError):
    """Raised for rejected texture compression plans."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TextureCompressionPlanner:
    """Plan block aligned texture levels and mip chains per format."""

    MAX_DIMENSION = 16384
    MAX_LEVELS = 16
    BLOCK_SIZES = (1, 4, 6, 8)

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def block_size(self, format: str) -> int:
        raise NotImplementedError

    def format_info(self, format: str) -> dict:
        raise NotImplementedError

    def pad_to_block(self, width: int, height: int, block: int) -> dict:
        raise NotImplementedError

    def mip_count(self, width: int, height: int, block: int) -> int:
        raise NotImplementedError

    def level_blocks(self, width: int, height: int, block: int) -> dict:
        raise NotImplementedError

    def plan(self, width: int, height: int, format: str) -> dict:
        raise NotImplementedError

    def validate(self, levels: list) -> list:
        raise NotImplementedError

    def cache_key(self, width: int, height: int, format: str) -> str:
        raise NotImplementedError

    def cache_size(self) -> int:
        raise NotImplementedError

    def invalidate_cache(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
