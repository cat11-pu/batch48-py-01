"""Lens distortion and chromatic aberration sampling pass."""

from __future__ import annotations


class LensError(ValueError):
    """Raised when a lens profile, border mode or grid cannot be applied."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LensDistortionPass:
    """Compute distorted / undistorted sample coordinates per lens profile."""

    def __init__(self, *, max_iter: int = 16) -> None:
        self.max_iter = max_iter

    def set_profile(self, *, k1: float = 0.0, k2: float = 0.0,
                    k3: float = 0.0, ca_r: float = 0.0,
                    ca_b: float = 0.0) -> dict:
        raise NotImplementedError

    def profile(self) -> dict:
        raise NotImplementedError

    def distort(self, uv) -> dict:
        raise NotImplementedError

    def undistort(self, uv, max_iter: int | None = None) -> dict:
        raise NotImplementedError

    def border_mode(self, mode: str) -> str:
        raise NotImplementedError

    def border(self) -> str:
        raise NotImplementedError

    def border_uv(self, uv) -> dict:
        raise NotImplementedError

    def sample_grid(self, width: int, height: int, step: int) -> dict:
        raise NotImplementedError

    def last_grid(self) -> dict | None:
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
