"""Cascaded shadow map split planning and texel snapping."""

from __future__ import annotations

import copy
import math
import threading
from collections.abc import Mapping
from numbers import Integral, Real


class CsmError(ValueError):
    """Raised when a cascade plan cannot be produced."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_SNAPSHOT_VERSION = 1

_DEFAULT_STATS = {
    "plans": 0,
    "plan_failures": 0,
    "resolution_changes": 0,
    "resolution_failures": 0,
    "snap_requests": 0,
    "blend_requests": 0,
    "distance_queries": 0,
}


def _is_real(value: object) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


def _is_integral(value: object) -> bool:
    return isinstance(value, Integral) and not isinstance(value, bool)


def _is_positive_real(value: object) -> bool:
    return _is_real(value) and math.isfinite(value) and value > 0


def _validate_cascade(cascade: object) -> Mapping:
    if not isinstance(cascade, Mapping):
        raise CsmError("CSM_INVALID_CASCADE", cascade)

    near = cascade.get("near")
    far = cascade.get("far")
    if (not _is_real(near) or not _is_real(far)
            or not math.isfinite(near) or not math.isfinite(far)
            or near < 0 or far <= near):
        raise CsmError("CSM_INVALID_CASCADE", cascade)

    if "index" in cascade and (
            not _is_integral(cascade["index"]) or cascade["index"] < 0):
        raise CsmError("CSM_INVALID_CASCADE", cascade)

    for key in ("ortho_width", "ortho_height", "texel_size"):
        if key in cascade and not _is_positive_real(cascade[key]):
            raise CsmError("CSM_INVALID_CASCADE", cascade)

    return cascade


def _validate_position(world_pos: object) -> tuple:
    if not isinstance(world_pos, (tuple, list)) or len(world_pos) != 3:
        raise CsmError("CSM_INVALID_POSITION", world_pos)
    if any(not _is_real(component) or not math.isfinite(component)
           for component in world_pos):
        raise CsmError("CSM_INVALID_POSITION", world_pos)
    return tuple(world_pos)


class CsmCascadePlanner:
    """Plan cascade splits, snap light-space origins and expose blend bands."""

    def __init__(self, *, max_cascades: int = 4, max_resolution: int = 4096,
                 far_cap: float = 10000.0, resolution: int = 2048) -> None:
        if not _is_integral(max_cascades) or max_cascades <= 0:
            raise CsmError("CSM_INVALID_COUNT", max_cascades)
        if not _is_integral(max_resolution) or max_resolution <= 0:
            raise CsmError("CSM_INVALID_RESOLUTION", max_resolution)
        if not _is_positive_real(far_cap):
            raise CsmError("CSM_INVALID_DISTANCE", far_cap)
        if (not _is_integral(resolution) or resolution <= 0
                or resolution > max_resolution):
            raise CsmError("CSM_INVALID_RESOLUTION", resolution)

        self.max_cascades = int(max_cascades)
        self.max_resolution = int(max_resolution)
        self.far_cap = float(far_cap)
        self.resolution = int(resolution)

        self._lock = threading.RLock()
        self._plan: dict = {}
        self._stats = dict(_DEFAULT_STATS)

    def set_resolution(self, resolution: int) -> int:
        with self._lock:
            if (not _is_integral(resolution) or resolution <= 0
                    or resolution > self.max_resolution):
                self._stats["resolution_failures"] += 1
                raise CsmError("CSM_INVALID_RESOLUTION", resolution)
            self.resolution = int(resolution)
            self._stats["resolution_changes"] += 1
            return self.resolution

    def plan(self, camera: dict, split_lambda: float = 0.5,
             count: int = 4) -> dict:
        with self._lock:
            try:
                near, far, fov, aspect = self._camera_values(camera)

                if (not _is_real(split_lambda) or not math.isfinite(split_lambda)
                        or split_lambda < 0.0 or split_lambda > 1.0):
                    raise CsmError("CSM_INVALID_LAMBDA", split_lambda)

                if not _is_integral(count) or count <= 0:
                    raise CsmError("CSM_INVALID_COUNT", count)
                if count > self.max_cascades:
                    raise CsmError("CSM_COUNT_LIMIT", count)

                plan = self._build_plan(
                    near, far, fov, aspect, float(split_lambda), int(count)
                )
            except CsmError:
                self._stats["plan_failures"] += 1
                raise

            self._plan = plan
            self._stats["plans"] += 1
            return copy.deepcopy(plan)

    def _camera_values(self, camera: object) -> tuple:
        if not isinstance(camera, Mapping):
            raise CsmError("CSM_INVALID_CAMERA", camera)

        try:
            near = camera["near"]
            far = camera["far"]
            fov = camera["fov"]
            aspect = camera["aspect"]
        except (KeyError, TypeError):
            raise CsmError("CSM_INVALID_CAMERA", camera) from None

        if (not _is_real(near) or not math.isfinite(near) or near <= 0
                or not _is_real(far) or math.isnan(far)
                or far <= near
                or not _is_real(fov) or not math.isfinite(fov)
                or fov <= 0 or fov >= 180
                or not _is_real(aspect) or not math.isfinite(aspect)
                or aspect <= 0):
            raise CsmError("CSM_INVALID_CAMERA", camera)

        return float(near), float(far), float(fov), float(aspect)

    def _build_plan(self, near: float, far: float, fov: float, aspect: float,
                    split_lambda: float, count: int) -> dict:
        if math.isinf(far):
            effective_far = self.far_cap
        else:
            effective_far = far

        if effective_far <= near:
            raise CsmError("CSM_INVALID_CAMERA", {
                "near": near, "far": far, "far_cap": self.far_cap
            })

        split_fars = self._split_fars(
            near, effective_far, split_lambda, count
        )

        tan_half_fov = math.tan(math.radians(fov * 0.5))
        cascades = []
        cascade_near = near
        for index, cascade_far in enumerate(split_fars):
            ortho_width = 2.0 * cascade_far * tan_half_fov
            ortho_height = ortho_width / aspect
            texel_size = ortho_width / self.resolution
            cascades.append({
                "index": index,
                "near": cascade_near,
                "far": cascade_far,
                "ortho_width": ortho_width,
                "ortho_height": ortho_height,
                "texel_size": texel_size,
            })
            cascade_near = cascade_far

        return {
            "count": count,
            "lambda": split_lambda,
            "resolution": self.resolution,
            "cascades": cascades,
        }

    @staticmethod
    def _split_fars(near: float, far: float, split_lambda: float,
                    count: int) -> list:
        log_near = math.log(near)
        log_range = math.log(far) - log_near
        split_fars = []
        previous = near

        for index in range(1, count + 1):
            if index == count:
                candidate = far
            else:
                ratio = index / count
                uniform = near + (far - near) * ratio
                try:
                    logarithmic = math.exp(log_near + log_range * ratio)
                except OverflowError:
                    logarithmic = far
                candidate = (
                    (1.0 - split_lambda) * uniform
                    + split_lambda * logarithmic
                )

                if not candidate > previous or candidate >= far:
                    remaining = count - index + 1
                    candidate = previous + (far - previous) / remaining
                    if not previous < candidate < far:
                        candidate = math.nextafter(previous, far)

            if not candidate > previous:
                raise CsmError("CSM_INVALID_CAMERA", {
                    "near": near, "far": far, "count": count
                })

            split_fars.append(candidate)
            previous = candidate

        if split_fars[-1] != far:
            split_fars[-1] = far
        return split_fars

    def snap_to_texel(self, cascade: dict, world_pos, texel_size: float) -> tuple:
        with self._lock:
            _validate_cascade(cascade)
            if not _is_positive_real(texel_size):
                raise CsmError("CSM_INVALID_TEXEL", texel_size)
            position = _validate_position(world_pos)
            self._stats["snap_requests"] += 1

        return tuple(round(component / texel_size) * texel_size
                     for component in position)

    def blend_band(self, cascade: dict) -> dict:
        with self._lock:
            _validate_cascade(cascade)
            self._stats["blend_requests"] += 1

        cascade_near = float(cascade["near"])
        cascade_far = float(cascade["far"])
        width = (cascade_far - cascade_near) * 0.1
        return {
            "start": cascade_far - width,
            "end": cascade_far,
            "width": width,
        }

    def stable_split_count(self, distance: float) -> int:
        with self._lock:
            if (not _is_real(distance) or math.isnan(distance)
                    or distance <= 0):
                raise CsmError("CSM_INVALID_DISTANCE", distance)
            self._stats["distance_queries"] += 1

        if math.isinf(distance):
            return self.max_cascades
        count = math.ceil(distance / 2500.0)
        return max(1, min(self.max_cascades, count))

    def current_plan(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._plan)

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = dict(_DEFAULT_STATS)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "resolution": self.resolution,
                "plan": copy.deepcopy(self._plan),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                resolution, plan, stats = self._validated_snapshot(state)
            except CsmError:
                raise
            except Exception as exc:
                raise CsmError("CSM_SNAPSHOT_INVALID", repr(exc)) from None

            self.resolution = resolution
            self._plan = plan
            self._stats = stats

    def _validated_snapshot(self, state: object) -> tuple:
        if not isinstance(state, Mapping):
            raise CsmError("CSM_SNAPSHOT_INVALID", state)
        if set(state) != {"version", "resolution", "plan", "stats"}:
            raise CsmError("CSM_SNAPSHOT_INVALID", state)

        version = state["version"]
        if not _is_integral(version) or version != _SNAPSHOT_VERSION:
            raise CsmError("CSM_SNAPSHOT_INVALID", version)

        resolution = state["resolution"]
        if (not _is_integral(resolution) or resolution <= 0
                or resolution > self.max_resolution):
            raise CsmError("CSM_SNAPSHOT_INVALID", resolution)

        plan = self._validated_snapshot_plan(state["plan"])
        stats = self._validated_snapshot_stats(state["stats"])
        return int(resolution), plan, stats

    def _validated_snapshot_plan(self, plan: object) -> dict:
        if not isinstance(plan, Mapping):
            raise CsmError("CSM_SNAPSHOT_INVALID", plan)
        if not plan:
            return {}
        if set(plan) != {"count", "lambda", "resolution", "cascades"}:
            raise CsmError("CSM_SNAPSHOT_INVALID", plan)

        count = plan["count"]
        split_lambda = plan["lambda"]
        plan_resolution = plan["resolution"]
        cascades = plan["cascades"]

        if (not _is_integral(count) or count <= 0
                or count > self.max_cascades):
            raise CsmError("CSM_SNAPSHOT_INVALID", count)
        if (not _is_real(split_lambda) or not math.isfinite(split_lambda)
                or split_lambda < 0 or split_lambda > 1):
            raise CsmError("CSM_SNAPSHOT_INVALID", split_lambda)
        if (not _is_integral(plan_resolution) or plan_resolution <= 0
                or plan_resolution > self.max_resolution):
            raise CsmError("CSM_SNAPSHOT_INVALID", plan_resolution)
        if not isinstance(cascades, (list, tuple)) or len(cascades) != count:
            raise CsmError("CSM_SNAPSHOT_INVALID", cascades)

        previous_far = None
        normalized_cascades = []
        cascade_keys = {
            "index", "near", "far", "ortho_width", "ortho_height",
            "texel_size",
        }
        for index, cascade in enumerate(cascades):
            if not isinstance(cascade, Mapping):
                raise CsmError("CSM_SNAPSHOT_INVALID", cascade)
            if set(cascade) != cascade_keys:
                raise CsmError("CSM_SNAPSHOT_INVALID", cascade)

            try:
                _validate_cascade(cascade)
            except CsmError:
                raise CsmError("CSM_SNAPSHOT_INVALID", cascade) from None
            if cascade["index"] != index:
                raise CsmError("CSM_SNAPSHOT_INVALID", cascade["index"])
            if previous_far is not None and cascade["near"] != previous_far:
                raise CsmError("CSM_SNAPSHOT_INVALID", cascade)
            expected_texel = (
                cascade["ortho_width"] / float(plan_resolution)
            )
            if not math.isclose(
                    cascade["texel_size"], expected_texel,
                    rel_tol=1e-12, abs_tol=1e-12):
                raise CsmError("CSM_SNAPSHOT_INVALID", cascade)

            previous_far = cascade["far"]
            normalized_cascades.append({
                "index": index,
                "near": float(cascade["near"]),
                "far": float(cascade["far"]),
                "ortho_width": float(cascade["ortho_width"]),
                "ortho_height": float(cascade["ortho_height"]),
                "texel_size": float(cascade["texel_size"]),
            })

        return {
            "count": int(count),
            "lambda": float(split_lambda),
            "resolution": int(plan_resolution),
            "cascades": normalized_cascades,
        }

    @staticmethod
    def _validated_snapshot_stats(stats: object) -> dict:
        if not isinstance(stats, Mapping):
            raise CsmError("CSM_SNAPSHOT_INVALID", stats)

        normalized = {}
        for key, value in stats.items():
            if not isinstance(key, str):
                raise CsmError("CSM_SNAPSHOT_INVALID", stats)
            if not _is_integral(value) or value < 0:
                raise CsmError("CSM_SNAPSHOT_INVALID", stats)
            normalized[key] = int(value)
        return normalized
