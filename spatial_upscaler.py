"""Spatial upsampling with edge directed weights and clamped sharpening."""

from __future__ import annotations

import copy
import math
import threading
from array import array


def _f32(value):
    """Round a python float to the nearest IEEE 754 binary32 value."""
    return array("f", (float(value),))[0]


class UpscaleError(ValueError):
    """Raised for rejected upscaling configurations."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


class SpatialUpscaler:
    """Plan spatial upsampling taps and edge directed sample weights."""

    MAX_DIMENSION = 16384
    MAX_SHARPEN = 1.0
    SNAPSHOT_VERSION = 1
    TAPS = 16

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._sharpen = _f32(0.0)
        self._edge_threshold = _f32(0.1)
        self._gradients = {}
        self._upscale_count = 0
        self._query_count = 0
        self._clamped_count = 0

    @staticmethod
    def _check_dimensions(src_w, src_h, dst_w, dst_h):
        for value in (src_w, src_h, dst_w, dst_h):
            if not _is_int(value) or value < 1:
                raise UpscaleError("UPSCALE_SIZE_INVALID")
        if dst_w > SpatialUpscaler.MAX_DIMENSION or dst_h > SpatialUpscaler.MAX_DIMENSION:
            raise UpscaleError("UPSCALE_SIZE_TOO_LARGE")

    @staticmethod
    def _map_center(coord, src_len, dst_len):
        """Pixel-center mapping with half-pixel offset, all math in float32."""
        center = _f32(_f32(_f32(coord + 0.5) * _f32(src_len)) / _f32(dst_len))
        mapped = _f32(center - 0.5)
        if mapped < 0.0:
            return _f32(0.0)
        upper = _f32(src_len - 1)
        if mapped > upper:
            return upper
        return mapped

    def set_sharpen(self, amount) -> None:
        value = float(amount)
        with self._lock:
            if value < 0.0 or not math.isfinite(value):
                self._sharpen = _f32(0.0)
                self._clamped_count += 1
            elif value > self.MAX_SHARPEN:
                self._sharpen = _f32(self.MAX_SHARPEN)
                self._clamped_count += 1
            else:
                self._sharpen = _f32(value)

    def sharpen_state(self) -> dict:
        with self._lock:
            return {"amount": self._sharpen, "clamped": self._clamped_count}

    def set_edge_threshold(self, value) -> None:
        candidate = float(value)
        if not math.isfinite(candidate) or candidate <= 0.0 or candidate > 1.0:
            raise UpscaleError("UPSCALE_THRESHOLD_INVALID")
        with self._lock:
            self._edge_threshold = _f32(candidate)

    def edge_threshold(self) -> float:
        with self._lock:
            return self._edge_threshold

    def set_gradient(self, x: int, y: int, gx, gy) -> None:
        if not _is_int(x) or not _is_int(y):
            raise UpscaleError("UPSCALE_SIZE_INVALID")
        with self._lock:
            self._gradients[(x, y)] = (_f32(gx), _f32(gy))

    def clear_gradients(self) -> None:
        with self._lock:
            self._gradients = {}

    def sample_point(self, src_w: int, src_h: int, dst_w: int, dst_h: int,
                     x: int, y: int) -> dict:
        self._check_dimensions(src_w, src_h, dst_w, dst_h)
        return {
            "src_x": self._map_center(x, src_w, dst_w),
            "src_y": self._map_center(y, src_h, dst_h),
        }

    def weights_at(self, src_w: int, src_h: int, dst_w: int, dst_h: int,
                   x: int, y: int) -> list:
        self._check_dimensions(src_w, src_h, dst_w, dst_h)
        src_x = self._map_center(x, src_w, dst_w)
        src_y = self._map_center(y, src_h, dst_h)
        with self._lock:
            gradients = copy.deepcopy(self._gradients)
            threshold = self._edge_threshold
            self._query_count += 1
        return self._build_weights(src_x, src_y, gradients, threshold)

    @staticmethod
    def _build_weights(src_x, src_y, gradients, threshold):
        gx = _f32(0.0)
        gy = _f32(0.0)
        key = (
            int(math.floor(src_x + 0.5)),
            int(math.floor(src_y + 0.5)),
        )
        gradient = gradients.get(key)
        if gradient is not None:
            gx, gy = gradient
        magnitude = _f32(math.sqrt(_f32(gx * gx + gy * gy)))
        if not math.isfinite(magnitude) or magnitude < threshold or magnitude == 0.0:
            tangent_x = _f32(0.0)
            tangent_y = _f32(0.0)
        else:
            tangent_x = _f32(-gy / magnitude)
            tangent_y = _f32(gx / magnitude)

        weights_x = SpatialUpscaler._axis_weights(src_x, tangent_x)
        weights_y = SpatialUpscaler._axis_weights(src_y, tangent_y)

        taps = [
            _f32(weights_y[row] * weights_x[col])
            for row in range(4)
            for col in range(4)
        ]
        total = _f32(sum(taps))
        if total <= 0.0:
            return taps
        normalized = [_f32(weight / total) for weight in taps]
        return normalized

    @staticmethod
    def _axis_weights(position, tangent):
        base_index = int(math.floor(position))
        fraction = _f32(position - base_index)
        weights = [_f32(0.0), _f32(_f32(1.0 - fraction)), _f32(fraction), _f32(0.0)]
        if tangent != 0.0:
            transfer = _f32(_f32(abs(tangent)) * 0.25)
            direction = 1.0 if tangent > 0.0 else -1.0
            for index in range(4):
                neighbor = index + (1 if direction > 0.0 else -1)
                if 0 <= neighbor < 4:
                    moved = _f32(min(transfer, weights[index]))
                    weights[index] = _f32(weights[index] - moved)
                    weights[neighbor] = _f32(weights[neighbor] + moved)
        return weights

    def upscale(self, src_w: int, src_h: int, dst_w: int, dst_h: int) -> dict:
        self._check_dimensions(src_w, src_h, dst_w, dst_h)
        with self._lock:
            sharpen = self._sharpen
            self._upscale_count += 1
        return {
            "src_width": src_w,
            "src_height": src_h,
            "dst_width": dst_w,
            "dst_height": dst_h,
            "scale_x": _f32(_f32(dst_w) / _f32(src_w)),
            "scale_y": _f32(_f32(dst_h) / _f32(src_h)),
            "sharpen": sharpen,
            "taps": self.TAPS,
        }

    def clamp_out(self, v) -> float:
        value = float(v)
        if not math.isfinite(value) or value < 0.0:
            return 0.0
        if value > 1.0:
            return 1.0
        return value

    def stats(self) -> dict:
        with self._lock:
            return {
                "upscales": self._upscale_count,
                "weight_queries": self._query_count,
                "clamped": self._clamped_count,
            }

    def reset_counters(self) -> None:
        with self._lock:
            self._upscale_count = 0
            self._query_count = 0
            self._clamped_count = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.SNAPSHOT_VERSION,
                "sharpen": self._sharpen,
                "edge_threshold": self._edge_threshold,
                "gradients": copy.deepcopy(self._gradients),
                "counters": {
                    "upscales": self._upscale_count,
                    "weight_queries": self._query_count,
                    "clamped": self._clamped_count,
                },
            }

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict) or state.get("version") != self.SNAPSHOT_VERSION:
            raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")

        try:
            sharpen = _f32(state["sharpen"])
            threshold = _f32(state["edge_threshold"])
            raw_gradients = state["gradients"]
            raw_counters = state["counters"]
        except (KeyError, TypeError, ValueError):
            raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")

        if not math.isfinite(sharpen) or sharpen < 0.0 or sharpen > 1.0:
            raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
        if not math.isfinite(threshold) or threshold <= 0.0 or threshold > 1.0:
            raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
        if not isinstance(raw_gradients, dict):
            raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")

        gradients = {}
        for key, value in raw_gradients.items():
            if (not isinstance(key, tuple) or len(key) != 2
                    or not _is_int(key[0]) or not _is_int(key[1])
                    or not isinstance(value, tuple) or len(value) != 2):
                raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
            try:
                gx = _f32(value[0])
                gy = _f32(value[1])
            except (TypeError, ValueError):
                raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
            if not math.isfinite(gx) or not math.isfinite(gy):
                raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
            gradients[(key[0], key[1])] = (gx, gy)

        if not isinstance(raw_counters, dict):
            raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
        counters = {}
        for name in ("upscales", "weight_queries", "clamped"):
            count = raw_counters.get(name)
            if not _is_int(count) or count < 0:
                raise UpscaleError("UPSCALE_SNAPSHOT_INVALID")
            counters[name] = count

        with self._lock:
            self._sharpen = sharpen
            self._edge_threshold = threshold
            self._gradients = gradients
            self._upscale_count = counters["upscales"]
            self._query_count = counters["weight_queries"]
            self._clamped_count = counters["clamped"]
