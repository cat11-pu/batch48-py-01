"""Spatial upsampling with edge directed weights and clamped sharpening."""

from __future__ import annotations

import threading


class UpscaleError(ValueError):
    """Raised for rejected upscaling configurations."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SpatialUpscaler:
    """Plan spatial upsampling taps and edge directed sample weights."""

    MAX_DIMENSION = 16384
    MAX_SHARPEN = 1.0

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def set_sharpen(self, amount) -> None:
        raise NotImplementedError

    def sharpen_state(self) -> dict:
        raise NotImplementedError

    def set_edge_threshold(self, value) -> None:
        raise NotImplementedError

    def edge_threshold(self) -> float:
        raise NotImplementedError

    def set_gradient(self, x: int, y: int, gx, gy) -> None:
        raise NotImplementedError

    def clear_gradients(self) -> None:
        raise NotImplementedError

    def sample_point(self, src_w: int, src_h: int, dst_w: int, dst_h: int,
                     x: int, y: int) -> dict:
        raise NotImplementedError

    def weights_at(self, src_w: int, src_h: int, dst_w: int, dst_h: int,
                   x: int, y: int) -> list:
        raise NotImplementedError

    def upscale(self, src_w: int, src_h: int, dst_w: int, dst_h: int) -> dict:
        raise NotImplementedError

    def clamp_out(self, v) -> float:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_counters(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
