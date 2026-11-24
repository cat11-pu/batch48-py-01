"""Shadow bias tuning from slope, normal offset and receiver depth."""

from __future__ import annotations


class ShadowBiasError(ValueError):
    """Raised when a light, receiver or bias limit is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ShadowBiasTuner:
    """Resolve shadow bias values per light and receiving surface."""

    def __init__(self, *, max_offset: float = 0.05,
                 texel_min: float = 0.0001,
                 texel_max: float = 10.0) -> None:
        self.max_offset = max_offset
        self.texel_min = texel_min
        self.texel_max = texel_max

    def slope_bias(self, light_dir, normal, texel_world: float) -> float:
        raise NotImplementedError

    def normal_offset(self, light: dict, texel_world: float,
                      base: float = 0.0) -> float:
        raise NotImplementedError

    def receiver_bias(self, depth_grad: float, texel_size: float) -> float:
        raise NotImplementedError

    def resolve(self, light: dict, receiver: dict) -> dict:
        raise NotImplementedError

    def cache_stats(self) -> dict:
        raise NotImplementedError

    def clear_cache(self) -> None:
        raise NotImplementedError

    def invalidate(self, light_id: str) -> int:
        raise NotImplementedError

    def set_limits(self, *, max_offset: float | None = None,
                   texel_min: float | None = None,
                   texel_max: float | None = None) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError
