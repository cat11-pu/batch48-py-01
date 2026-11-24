"""Post-processing chain planning, conversion insertion and caching."""

from __future__ import annotations


class ChainError(ValueError):
    """Raised when a post-processing chain cannot be planned or restored."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PostProcessChainGuard:
    """Order post-process passes, insert conversions and reuse buffers."""

    def __init__(self, *, frame_budget_ms: float = 8.0) -> None:
        self.frame_budget_ms = frame_budget_ms

    def set_source(self, name: str, *, fmt: str = "rgba8",
                   color_space: str = "srgb") -> dict:
        raise NotImplementedError

    def add_pass(self, name: str, *, deps=(), reads=(), writes=(),
                 fmt: str = "rgba8", color_space: str = "srgb",
                 cost_ms: float = 1.0, optional: bool = False) -> dict:
        raise NotImplementedError

    def set_enabled(self, name: str, enabled: bool) -> bool:
        raise NotImplementedError

    def build(self) -> dict:
        raise NotImplementedError

    def last_plan(self) -> dict:
        raise NotImplementedError

    def run(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
