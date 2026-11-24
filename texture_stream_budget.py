"""Texture mip streaming with per-frame bandwidth budget and residency."""

from __future__ import annotations


class TextureStreamError(ValueError):
    """Raised when a streaming request or plan cannot be satisfied."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TextureStreamBudget:
    """Schedule mip uploads, residency changes and pins under a byte budget."""

    def __init__(self, *, max_textures: int = 50000, max_mip: int = 15,
                 max_frame_bytes: int = 64 * 1024 * 1024) -> None:
        self.max_textures = max_textures
        self.max_mip = max_mip
        self.max_frame_bytes = max_frame_bytes

    def register(self, texture_id: str, base_bytes: int) -> dict:
        raise NotImplementedError

    def request(self, texture_id: str, mip: int, screen_error: float) -> dict:
        raise NotImplementedError

    def plan(self, budget_bytes: int | None = None) -> dict:
        raise NotImplementedError

    def resident(self, texture_id: str) -> int:
        raise NotImplementedError

    def pin(self, texture_id: str) -> bool:
        raise NotImplementedError

    def unpin(self, texture_id: str) -> bool:
        raise NotImplementedError

    def pinned(self) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
