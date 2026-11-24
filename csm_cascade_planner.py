"""Cascaded shadow map split planning and texel snapping."""

from __future__ import annotations


class CsmError(ValueError):
    """Raised when a cascade plan cannot be produced."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CsmCascadePlanner:
    """Plan cascade splits, snap light-space origins and expose blend bands."""

    def __init__(self, *, max_cascades: int = 4, max_resolution: int = 4096,
                 far_cap: float = 10000.0, resolution: int = 2048) -> None:
        self.max_cascades = max_cascades
        self.max_resolution = max_resolution
        self.far_cap = far_cap
        self.resolution = resolution

    def set_resolution(self, resolution: int) -> int:
        raise NotImplementedError

    def plan(self, camera: dict, split_lambda: float = 0.5,
             count: int = 4) -> dict:
        raise NotImplementedError

    def snap_to_texel(self, cascade: dict, world_pos, texel_size: float) -> tuple:
        raise NotImplementedError

    def blend_band(self, cascade: dict) -> dict:
        raise NotImplementedError

    def stable_split_count(self, distance: float) -> int:
        raise NotImplementedError

    def current_plan(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
