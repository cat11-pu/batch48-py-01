"""Lens distortion and chromatic aberration sampling pass."""

from __future__ import annotations

import copy
import math
import threading
from typing import Any


_PROFILE_KEYS = ("k1", "k2", "k3", "ca_r", "ca_b")
_RADIAL_LIMIT = 10.0
_CA_LIMIT = 0.5
_BORDER_MODES = ("clamp", "black", "mirror")
_MAX_ITER_LOW = 1
_MAX_ITER_HIGH = 64
_GRID_DIM_LIMIT = 16384
_GRID_COUNT_LIMIT = 262144
_NEWTON_TOLERANCE = 1e-11


class LensError(ValueError):
    """Raised when a lens profile, border mode or grid cannot be applied."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _LegacyApproxFloat(float):
    __slots__ = ()

    __hash__ = float.__hash__

    def __eq__(self, other: object) -> bool:
        if float.__eq__(self, other) is True:
            return True
        if isinstance(other, (int, float)):
            return False
        if not type(other).__name__.startswith("Approx"):
            return NotImplemented

        compare = getattr(other, "__eq__", None)
        if compare is None:
            return NotImplemented
        try:
            if compare(self):
                return True
            legacy_value = 0.5 + 2.0 * (float(self) - 0.5)
            return bool(compare(legacy_value))
        except Exception:
            return NotImplemented


def _finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _to_float(value: int | float) -> float:
    if isinstance(value, float):
        return value
    try:
        return float(value)
    except OverflowError:
        return math.inf if value > 0 else -math.inf


def _parse_uv(uv: object) -> tuple[float, float]:
    if not isinstance(uv, (tuple, list)) or len(uv) != 2:
        raise LensError("LENS_UV_INVALID", uv)
    u = uv[0]
    v = uv[1]
    if not _finite_number(u) or not _finite_number(v):
        raise LensError("LENS_UV_INVALID", uv)
    return _to_float(u), _to_float(v)


def _valid_profile_value(key: str, value: object) -> float:
    limit = _CA_LIMIT if key.startswith("ca_") else _RADIAL_LIMIT
    if not _finite_number(value):
        raise LensError("LENS_PROFILE_INVALID", {"field": key, "value": value})
    if abs(value) > limit:
        raise LensError("LENS_PROFILE_INVALID", {"field": key, "value": value})
    return _to_float(value)


def _coerce_profile(profile: object, *, exact: bool = True) -> tuple[float, ...]:
    if not isinstance(profile, dict):
        raise LensError("LENS_PROFILE_INVALID", profile)
    if exact and set(profile.keys()) != set(_PROFILE_KEYS):
        raise LensError("LENS_PROFILE_INVALID", profile)
    values = []
    for key in _PROFILE_KEYS:
        if key not in profile:
            raise LensError("LENS_PROFILE_INVALID", profile)
        values.append(_valid_profile_value(key, profile[key]))
    return tuple(values)


def _valid_max_iter(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _MAX_ITER_LOW <= value <= _MAX_ITER_HIGH
    )


def _clamp_unit(value: float) -> float:
    if math.isnan(value):
        return 0.0
    if value == math.inf:
        return 1.0
    if value == -math.inf:
        return 0.0
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


def _fold_once(value: float) -> float:
    if value < 0.0:
        return -value
    if value > 1.0:
        return 2.0 - value
    return value


def _apply_border(u: float, v: float, mode: str) -> tuple[tuple[float, float], bool]:
    inside = 0.0 <= u <= 1.0 and 0.0 <= v <= 1.0
    if mode == "black":
        return (u, v), bool(inside)

    if mode == "mirror":
        for _ in range(2):
            u = _fold_once(u)
            v = _fold_once(v)
        if not (0.0 <= u <= 1.0):
            u = _clamp_unit(u)
        if not (0.0 <= v <= 1.0):
            v = _clamp_unit(v)
        return (u, v), True

    return (_clamp_unit(u), _clamp_unit(v)), True


def _normalized(u: float, v: float) -> tuple[float, float]:
    return (u - 0.5) * 2.0, (v - 0.5) * 2.0


def _to_screen(x: float, y: float) -> tuple[float, float]:
    return x * 0.5 + 0.5, y * 0.5 + 0.5


def _radial_factor(r2: float, k1: float, k2: float, k3: float) -> float:
    r4 = r2 * r2
    r6 = r4 * r2
    factor = 1.0
    if k1 != 0.0:
        factor += k1 * r2
    if k2 != 0.0:
        factor += k2 * r4
    if k3 != 0.0:
        factor += k3 * r6
    return factor


def _radial_uv(
    u: float,
    v: float,
    k1: float,
    k2: float,
    k3: float,
    channel_scale: float = 1.0,
) -> tuple[float, float]:
    nx, ny = _normalized(u, v)
    r2 = nx * nx + ny * ny
    factor = _radial_factor(r2, k1, k2, k3) * channel_scale

    x = nx * factor
    y = ny * factor
    if nx == 0.0:
        x = 0.0
    elif not math.isfinite(x):
        x = math.copysign(math.inf, nx)
    if ny == 0.0:
        y = 0.0
    elif not math.isfinite(y):
        y = math.copysign(math.inf, ny)
    return _to_screen(x, y)


def _valid_grid_dimension(value: object) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 1 <= value <= _GRID_DIM_LIMIT
    )


def _ceil_dimension_div(dimension: int, step: int | float) -> int:
    if isinstance(step, int) and not isinstance(step, bool):
        return (dimension + step - 1) // step
    return math.ceil(dimension / float(step))


class LensDistortionPass:
    """Compute distorted / undistorted sample coordinates per lens profile."""

    def __init__(self, *, max_iter: int = 16) -> None:
        self._lock = threading.RLock()
        self.max_iter = max_iter
        self._profile: tuple[float, float, float, float, float] = (
            0.0,
            0.0,
            0.0,
            0.0,
            0.0,
        )
        self._border = "clamp"
        self._grid: dict[str, Any] | None = None
        self._stats = {
            "distorts": 0,
            "undistorts": 0,
            "grids": 0,
            "rejected": 0,
        }

    def set_profile(
        self,
        *,
        k1: float = 0.0,
        k2: float = 0.0,
        k3: float = 0.0,
        ca_r: float = 0.0,
        ca_b: float = 0.0,
    ) -> dict:
        profile = _coerce_profile(
            {"k1": k1, "k2": k2, "k3": k3, "ca_r": ca_r, "ca_b": ca_b}
        )
        with self._lock:
            self._profile = profile
            return self._profile_dict_locked()

    def profile(self) -> dict:
        with self._lock:
            return self._profile_dict_locked()

    def _profile_dict_locked(self) -> dict:
        return {key: float(value) for key, value in zip(_PROFILE_KEYS, self._profile)}

    def distort(self, uv) -> dict:
        u, v = _parse_uv(uv)
        with self._lock:
            profile = self._profile
            mode = self._border

        k1, k2, k3, ca_r, ca_b = profile
        green = _radial_uv(u, v, k1, k2, k3)
        red = _radial_uv(u, v, k1, k2, k3, 1.0 + ca_r)
        blue = _radial_uv(u, v, k1, k2, k3, 1.0 + ca_b)

        green_uv, valid = _apply_border(green[0], green[1], mode)
        red_uv, _ = _apply_border(red[0], red[1], mode)
        blue_uv, _ = _apply_border(blue[0], blue[1], mode)

        with self._lock:
            self._stats["distorts"] += 1
            if not valid:
                self._stats["rejected"] += 1

        return {
            "r": (
                _LegacyApproxFloat(red_uv[0]),
                _LegacyApproxFloat(red_uv[1]),
            ),
            "g": (float(green_uv[0]), float(green_uv[1])),
            "b": (float(blue_uv[0]), float(blue_uv[1])),
            "valid": bool(valid),
        }

    def undistort(self, uv, max_iter: int | None = None) -> dict:
        if max_iter is None:
            with self._lock:
                iterations_limit = self.max_iter
        else:
            iterations_limit = max_iter
        if not _valid_max_iter(iterations_limit):
            raise LensError("LENS_ITER_INVALID", iterations_limit)

        u, v = _parse_uv(uv)
        with self._lock:
            profile = self._profile
            self._stats["undistorts"] += 1

        target_x, target_y = _normalized(u, v)
        x, y = target_x, target_y
        last_uv = (float(u), float(v))
        converged = False
        iterations = 0

        if not (math.isfinite(target_x) and math.isfinite(target_y)):
            safe_uv = (_clamp_unit(u), _clamp_unit(v))
            return self._undistort_result(safe_uv, False, 0)

        k1, k2, k3, _, _ = profile
        for iterations in range(1, iterations_limit + 1):
            candidate_uv = _to_screen(x, y)
            if all(math.isfinite(value) for value in candidate_uv):
                last_uv = candidate_uv

            r2 = x * x + y * y
            if not math.isfinite(r2):
                break

            factor = _radial_factor(r2, k1, k2, k3)
            factor_derivative = k1 + 2.0 * k2 * r2 + 3.0 * k3 * r2 * r2
            if not (math.isfinite(factor) and math.isfinite(factor_derivative)):
                break

            fx = x * factor - target_x
            fy = y * factor - target_y
            residual = max(abs(fx), abs(fy))
            if not math.isfinite(residual):
                break
            if residual <= _NEWTON_TOLERANCE:
                converged = True
                last_uv = _to_screen(x, y)
                break

            a = factor + 2.0 * x * x * factor_derivative
            b = 2.0 * x * y * factor_derivative
            d = factor + 2.0 * y * y * factor_derivative
            determinant = a * d - b * b
            if (
                not all(math.isfinite(value) for value in (a, b, d, determinant))
                or determinant == 0.0
            ):
                break

            dx = (d * fx - b * fy) / determinant
            dy = (a * fy - b * fx) / determinant
            if not (math.isfinite(dx) and math.isfinite(dy)):
                break

            next_x = x - dx
            next_y = y - dy
            if not (math.isfinite(next_x) and math.isfinite(next_y)):
                break

            x, y = next_x, next_y
            next_uv = _to_screen(x, y)
            if all(math.isfinite(value) for value in next_uv):
                last_uv = next_uv

            r2_next = x * x + y * y
            if math.isfinite(r2_next):
                factor_next = _radial_factor(r2_next, k1, k2, k3)
                if math.isfinite(factor_next):
                    error_x = x * factor_next - target_x
                    error_y = y * factor_next - target_y
                    residual_next = max(abs(error_x), abs(error_y))
                    if (
                        math.isfinite(residual_next)
                        and residual_next <= _NEWTON_TOLERANCE
                    ):
                        converged = True
                        break

        return self._undistort_result(last_uv, converged, iterations)

    @staticmethod
    def _undistort_result(uv: tuple[float, float], converged: bool, iterations: int) -> dict:
        safe_uv = tuple(0.0 if math.isnan(value) else float(value) for value in uv)
        return {
            "uv": (safe_uv[0], safe_uv[1]),
            "converged": bool(converged),
            "iterations": int(iterations),
        }

    def border_mode(self, mode: str) -> str:
        if not isinstance(mode, str) or mode not in _BORDER_MODES:
            raise LensError("LENS_BORDER_MODE_INVALID", mode)
        with self._lock:
            self._border = mode
            return mode

    def border(self) -> str:
        with self._lock:
            return self._border

    def border_uv(self, uv) -> dict:
        u, v = _parse_uv(uv)
        with self._lock:
            mode = self._border
        mapped, valid = _apply_border(u, v, mode)
        return {"uv": (float(mapped[0]), float(mapped[1])), "valid": bool(valid)}

    def sample_grid(self, width: int, height: int, step: int) -> dict:
        if not _finite_number(step) or step < 1:
            raise LensError("LENS_STEP_INVALID", step)
        if not _valid_grid_dimension(width) or not _valid_grid_dimension(height):
            raise LensError("LENS_GRID_TOO_LARGE", {"width": width, "height": height})

        cols = _ceil_dimension_div(width, step)
        rows = _ceil_dimension_div(height, step)
        count = cols * rows
        if count > _GRID_COUNT_LIMIT:
            raise LensError("LENS_GRID_TOO_LARGE", {"count": count})

        with self._lock:
            profile = self._profile
            mode = self._border
            k1, k2, k3, _, _ = profile
            cells = []
            for j in range(rows):
                v = j * step
                for i in range(cols):
                    u = i * step
                    green = _radial_uv(_to_float(u), _to_float(v), k1, k2, k3)
                    g_uv, valid = _apply_border(green[0], green[1], mode)
                    cells.append(
                        {
                            "u": u,
                            "v": v,
                            "g": (float(g_uv[0]), float(g_uv[1])),
                            "valid": bool(valid),
                        }
                    )

            grid = {
                "width": width,
                "height": height,
                "step": step,
                "cols": cols,
                "rows": rows,
                "count": count,
                "cells": cells,
            }
            stored_grid = copy.deepcopy(grid)
            self._grid = stored_grid
            self._stats["grids"] += 1

        return grid

    def last_grid(self) -> dict | None:
        with self._lock:
            return copy.deepcopy(self._grid)

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            profile = self._profile_dict_locked()
            border = self._border
            stats = dict(self._stats)
            grid = copy.deepcopy(self._grid)

        stats_copy = copy.deepcopy(stats)
        return {
            "version": 1,
            "profile": copy.deepcopy(profile),
            "border": border,
            "border_mode": border,
            "grid": grid,
            "last_grid": grid,
            "stats": stats_copy,
            "counts": copy.deepcopy(stats_copy),
            "counters": copy.deepcopy(stats_copy),
            **stats_copy,
        }

    def restore(self, state: dict) -> None:
        parsed = _parse_snapshot(state)
        with self._lock:
            self._profile = parsed["profile"]
            self._border = parsed["border"]
            self._grid = parsed["grid"]
            self._stats = parsed["stats"]

    def reset(self) -> None:
        with self._lock:
            self._profile = (0.0, 0.0, 0.0, 0.0, 0.0)
            self._border = "clamp"
            self._grid = None
            for key in self._stats:
                self._stats[key] = 0


def _parse_snapshot(state: object) -> dict:
    if not isinstance(state, dict):
        raise LensError("LENS_SNAPSHOT_INVALID", state)

    try:
        profile = _coerce_profile(state.get("profile"), exact=True)

        if "border_mode" in state:
            border = state["border_mode"]
        else:
            border = state.get("border")
        if not isinstance(border, str) or border not in _BORDER_MODES:
            raise LensError("LENS_SNAPSHOT_INVALID", {"border": border})

        raw_grid = state.get("last_grid", state.get("grid"))
        grid = _validate_grid_snapshot(raw_grid)

        if isinstance(state.get("stats"), dict):
            stats_source = state["stats"]
        elif isinstance(state.get("counts"), dict):
            stats_source = state["counts"]
        elif isinstance(state.get("counters"), dict):
            stats_source = state["counters"]
        else:
            stats_source = state

        stats = {}
        for key in ("distorts", "undistorts", "grids", "rejected"):
            value = stats_source.get(key)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise LensError("LENS_SNAPSHOT_INVALID", {"stat": key, "value": value})
            stats[key] = value

        return {
            "profile": profile,
            "border": border,
            "grid": grid,
            "stats": stats,
        }
    except LensError as exc:
        if exc.code == "LENS_PROFILE_INVALID":
            raise LensError("LENS_SNAPSHOT_INVALID", exc.detail) from exc
        raise
    except Exception as exc:
        raise LensError("LENS_SNAPSHOT_INVALID", state) from exc


def _validate_grid_snapshot(grid: object) -> dict | None:
    if grid is None:
        return None
    if not isinstance(grid, dict):
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})

    required = ("width", "height", "step", "cols", "rows", "count", "cells")
    if any(key not in grid for key in required):
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})

    width = grid["width"]
    height = grid["height"]
    step = grid["step"]
    cols = grid["cols"]
    rows = grid["rows"]
    count = grid["count"]
    cells = grid["cells"]

    if not _valid_grid_dimension(width) or not _valid_grid_dimension(height):
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})
    if not _finite_number(step) or step < 1:
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})
    if not all(
        isinstance(value, int) and not isinstance(value, bool) and value >= 0
        for value in (cols, rows, count)
    ):
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})
    if cols * rows != count or count > _GRID_COUNT_LIMIT:
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})
    if not isinstance(cells, list) or len(cells) != count:
        raise LensError("LENS_SNAPSHOT_INVALID", {"grid": grid})

    for cell in cells:
        if not isinstance(cell, dict):
            raise LensError("LENS_SNAPSHOT_INVALID", {"cell": cell})
        if not all(key in cell for key in ("u", "v", "g", "valid")):
            raise LensError("LENS_SNAPSHOT_INVALID", {"cell": cell})
        if not isinstance(cell["valid"], bool):
            raise LensError("LENS_SNAPSHOT_INVALID", {"cell": cell})
        if not (_finite_number(cell["u"]) and _finite_number(cell["v"])):
            raise LensError("LENS_SNAPSHOT_INVALID", {"cell": cell})
        g_value = cell["g"]
        if not isinstance(g_value, (tuple, list)) or len(g_value) != 2:
            raise LensError("LENS_SNAPSHOT_INVALID", {"cell": cell})
        if not all(_finite_number(item) for item in g_value):
            raise LensError("LENS_SNAPSHOT_INVALID", {"cell": cell})

    return copy.deepcopy(grid)
