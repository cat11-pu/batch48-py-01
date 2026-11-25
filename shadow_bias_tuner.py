"""Shadow bias tuning from slope, normal offset and receiver depth."""

from __future__ import annotations

import copy
import math
import threading
from collections.abc import Mapping, Sequence
from typing import Any


class ShadowBiasError(ValueError):
    """Raised when a light, receiver or bias limit is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_HARD_TEXEL_MIN = 1e-4
_HARD_TEXEL_MAX = 10.0
_STAT_KEYS = ("resolves", "floating", "clamped", "texel_clamped")
_UNSET = object()


def _is_real_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: Any, code: str, detail: str) -> float:
    if not _is_real_number(value):
        raise ShadowBiasError(code, detail)
    result = float(value)
    if not math.isfinite(result):
        raise ShadowBiasError(code, detail)
    return result


def _non_empty_id(value: Any, code: str) -> str:
    if not isinstance(value, str) or not value:
        raise ShadowBiasError(code, "id must be a non-empty string")
    return value


def _vector3(value: Any, code: str, *, zero_allowed: bool) -> tuple[float, float, float]:
    if (not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray))
            or len(value) != 3):
        raise ShadowBiasError(code, "expected a three-component vector")

    components: list[float] = []
    for component in value:
        if not _is_real_number(component):
            raise ShadowBiasError(code, "vector components must be finite numbers")
        component_float = float(component)
        if not math.isfinite(component_float):
            raise ShadowBiasError(code, "vector components must be finite numbers")
        components.append(component_float)

    x, y, z = components
    norm = math.hypot(x, y, z)
    if norm == 0.0:
        if zero_allowed:
            return (0.0, 0.0, 0.0)
        raise ShadowBiasError(code, "vector must not be zero")
    return (x / norm, y / norm, z / norm)


def _validate_limits_values(
    max_offset: Any,
    texel_min: Any,
    texel_max: Any,
) -> dict[str, float]:
    if not all(_is_real_number(value) and math.isfinite(float(value))
               for value in (max_offset, texel_min, texel_max)):
        raise ShadowBiasError("SHADOW_LIMIT_INVALID", "limits must be finite numbers")

    max_offset = float(max_offset)
    texel_min = float(texel_min)
    texel_max = float(texel_max)

    if not (0.0 < max_offset <= 1.0):
        raise ShadowBiasError("SHADOW_LIMIT_INVALID",
                               "max_offset must satisfy 0 < max_offset <= 1")
    if not (_HARD_TEXEL_MIN <= texel_min < texel_max <= _HARD_TEXEL_MAX):
        raise ShadowBiasError(
            "SHADOW_LIMIT_INVALID",
            "texel limits must satisfy 1e-4 <= texel_min < texel_max <= 10",
        )

    return {
        "max_offset": max_offset,
        "texel_min": texel_min,
        "texel_max": texel_max,
    }


class ShadowBiasTuner:
    """Resolve shadow bias values per light and receiving surface."""

    def __init__(self, *, max_offset: float = 0.05,
                 texel_min: float = 0.0001,
                 texel_max: float = 10.0) -> None:
        limits = _validate_limits_values(max_offset, texel_min, texel_max)
        self._initial_limits = dict(limits)
        self._limits = dict(limits)
        self._cache: dict[tuple[str, str, float], dict[str, Any]] = {}
        self._hits = 0
        self._misses = 0
        self._stats = {key: 0 for key in _STAT_KEYS}
        self._lock = threading.RLock()

    @property
    def max_offset(self) -> float:
        with self._lock:
            return self._limits["max_offset"]

    @max_offset.setter
    def max_offset(self, value: float) -> None:
        self.set_limits(max_offset=value)

    @property
    def texel_min(self) -> float:
        with self._lock:
            return self._limits["texel_min"]

    @texel_min.setter
    def texel_min(self, value: float) -> None:
        self.set_limits(texel_min=value)

    @property
    def texel_max(self) -> float:
        with self._lock:
            return self._limits["texel_max"]

    @texel_max.setter
    def texel_max(self, value: float) -> None:
        self.set_limits(texel_max=value)

    def slope_bias(self, light_dir, normal, texel_world: float) -> float:
        with self._lock:
            limits = dict(self._limits)
            texel = self._valid_texel(texel_world, limits)
            light_unit = _vector3(light_dir, "SHADOW_LIGHT_INVALID",
                                  zero_allowed=False)
            normal_unit = _vector3(normal, "SHADOW_RECEIVER_INVALID",
                                   zero_allowed=False)
            return self._slope_bias(light_unit, normal_unit, texel, limits)

    def normal_offset(self, light: dict, texel_world: float,
                      base: float | object = _UNSET) -> float:
        with self._lock:
            limits = dict(self._limits)
            texel = self._valid_texel(texel_world, limits)

            if base is _UNSET:
                base_value = 0.0
                if light is not None:
                    if not isinstance(light, Mapping):
                        raise ShadowBiasError(
                            "SHADOW_LIGHT_INVALID",
                            "light must be a mapping when base is omitted",
                        )
                    if "normal_bias_base" in light:
                        base_value = _finite_number(
                            light["normal_bias_base"],
                            "SHADOW_LIGHT_INVALID",
                            "normal_bias_base must be a finite number",
                        )
            else:
                base_value = _finite_number(
                    base,
                    "SHADOW_LIGHT_INVALID",
                    "base must be a finite number",
                )

            return min(base_value + texel * 0.5, limits["max_offset"])

    def receiver_bias(self, depth_grad: float, texel_size: float) -> float:
        with self._lock:
            limits = dict(self._limits)
            texel = self._valid_texel(texel_size, limits)
            gradient = _finite_number(
                depth_grad,
                "SHADOW_RECEIVER_INVALID",
                "depth_grad must be a finite number",
            )
            if gradient < 0.0:
                raise ShadowBiasError("SHADOW_RECEIVER_INVALID",
                                      "depth_grad must not be negative")
            return min(gradient * texel, limits["max_offset"])

    def resolve(self, light: dict, receiver: dict) -> dict:
        with self._lock:
            limits = dict(self._limits)
            light_id, light_dir, base = self._validate_light(light)
            receiver_id, normal, raw_texel, depth_grad = self._validate_receiver(
                receiver
            )

            effective_texel = raw_texel
            texel_clamped = (
                raw_texel < limits["texel_min"]
                or raw_texel > limits["texel_max"]
            )
            if texel_clamped:
                effective_texel = min(
                    max(raw_texel, limits["texel_min"]),
                    limits["texel_max"],
                )

            key = (light_id, receiver_id, raw_texel)
            entry = self._cache.get(key)
            if entry is not None and self._entry_matches(
                entry, raw_texel, limits
            ):
                self._hits += 1
                self._stats["resolves"] += 1
                result = entry["result"]
                if result["floating"]:
                    self._stats["floating"] += 1
                if result["clamped"]:
                    self._stats["clamped"] += 1
                if entry["texel_clamped"]:
                    self._stats["texel_clamped"] += 1
                return copy.deepcopy(result)

            self._misses += 1
            self._stats["resolves"] += 1

            slope = self._slope_bias(light_dir, normal, effective_texel, limits)
            normal_bias = self._normal_offset_value(
                light, base, effective_texel, limits
            )
            receiver_bias = min(
                depth_grad * effective_texel,
                limits["max_offset"],
            )
            raw_sum = slope + normal_bias + receiver_bias
            max_offset = limits["max_offset"]
            clamped = raw_sum > max_offset
            floating = raw_sum > effective_texel * 2.0
            offset = min(raw_sum, max_offset)

            result = {
                "light": light_id,
                "receiver": receiver_id,
                "slope_bias": slope,
                "normal_offset": normal_bias,
                "receiver_bias": receiver_bias,
                "offset": offset,
                "floating": floating,
                "clamped": clamped,
            }

            self._remove_pair_keys(light_id, receiver_id, keep_key=key)
            self._cache[key] = {
                "texel_world": raw_texel,
                "effective_texel": effective_texel,
                "texel_clamped": texel_clamped,
                "limits": dict(limits),
                "result": copy.deepcopy(result),
            }

            if floating:
                self._stats["floating"] += 1
            if clamped:
                self._stats["clamped"] += 1
            if texel_clamped:
                self._stats["texel_clamped"] += 1

            return copy.deepcopy(result)

    def cache_stats(self) -> dict:
        with self._lock:
            return {
                "hits": self._hits,
                "misses": self._misses,
                "size": len(self._cache),
            }

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def invalidate(self, light_id: str) -> int:
        with self._lock:
            if not isinstance(light_id, str):
                return 0
            keys = [key for key in self._cache if key[0] == light_id]
            for key in keys:
                del self._cache[key]
            return len(keys)

    def set_limits(self, *, max_offset: float | None = None,
                   texel_min: float | None = None,
                   texel_max: float | None = None) -> dict:
        with self._lock:
            candidate = dict(self._limits)
            if max_offset is not None:
                candidate["max_offset"] = max_offset
            if texel_min is not None:
                candidate["texel_min"] = texel_min
            if texel_max is not None:
                candidate["texel_max"] = texel_max

            new_limits = _validate_limits_values(
                candidate["max_offset"],
                candidate["texel_min"],
                candidate["texel_max"],
            )
            self._limits = new_limits
            self._cache.clear()
            return dict(new_limits)

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in _STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "limits": dict(self._limits),
                "stats": dict(self._stats),
                "cache_stats": {
                    "hits": self._hits,
                    "misses": self._misses,
                },
                "cache": [
                    {
                        "key": list(key),
                        "texel_world": entry["texel_world"],
                        "effective_texel": entry["effective_texel"],
                        "texel_clamped": entry["texel_clamped"],
                        "limits": dict(entry["limits"]),
                        "result": copy.deepcopy(entry["result"]),
                    }
                    for key, entry in self._cache.items()
                ],
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        restored = self._parse_snapshot(state)
        with self._lock:
            (limits, stats, hits, misses, cache) = restored
            self._limits = limits
            self._stats = stats
            self._hits = hits
            self._misses = misses
            self._cache = cache

    def reset(self) -> None:
        with self._lock:
            self._limits = dict(self._initial_limits)
            self._cache.clear()
            self._hits = 0
            self._misses = 0
            for key in _STAT_KEYS:
                self._stats[key] = 0

    @staticmethod
    def _valid_texel(texel_world: Any, limits: Mapping[str, float]) -> float:
        texel = _finite_number(
            texel_world,
            "SHADOW_TEXEL_INVALID",
            "texel_world must be a finite number",
        )
        if not (limits["texel_min"] <= texel <= limits["texel_max"]):
            raise ShadowBiasError(
                "SHADOW_TEXEL_INVALID",
                "texel_world is outside the supported range",
            )
        return texel

    @staticmethod
    def _slope_bias(light_unit: tuple[float, float, float],
                    normal_unit: tuple[float, float, float],
                    texel: float,
                    limits: Mapping[str, float]) -> float:
        dot = abs(
            light_unit[0] * normal_unit[0]
            + light_unit[1] * normal_unit[1]
            + light_unit[2] * normal_unit[2]
        )
        dot = min(1.0, max(0.0, dot))

        if abs(dot - 1.0) < 1e-6:
            return limits["max_offset"]
        if dot == 0.0:
            return limits["max_offset"]

        sine_squared = max(0.0, 1.0 - dot * dot)
        slope = math.sqrt(sine_squared) / dot * texel
        return min(slope, limits["max_offset"])

    def _normal_offset_value(
        self,
        light: Mapping[str, Any] | None,
        base: float | object,
        texel: float,
        limits: Mapping[str, float],
    ) -> float:
        if base is _UNSET:
            base_value = 0.0
            if isinstance(light, Mapping) and "normal_bias_base" in light:
                base_value = _finite_number(
                    light["normal_bias_base"],
                    "SHADOW_LIGHT_INVALID",
                    "normal_bias_base must be a finite number",
                )
        else:
            base_value = _finite_number(
                base,
                "SHADOW_LIGHT_INVALID",
                "base must be a finite number",
            )
        return min(base_value + texel * 0.5, limits["max_offset"])

    @staticmethod
    def _validate_light(
        light: Any,
    ) -> tuple[str, tuple[float, float, float], float | object]:
        if not isinstance(light, Mapping):
            raise ShadowBiasError("SHADOW_LIGHT_INVALID",
                                  "light must be a mapping")
        light_id = _non_empty_id(light.get("id"), "SHADOW_LIGHT_INVALID")
        light_dir = _vector3(light.get("dir"), "SHADOW_LIGHT_INVALID",
                             zero_allowed=False)

        base: float | object = _UNSET
        if "normal_bias_base" in light:
            base = _finite_number(
                light["normal_bias_base"],
                "SHADOW_LIGHT_INVALID",
                "normal_bias_base must be a finite number",
            )
        return light_id, light_dir, base

    @staticmethod
    def _validate_receiver(
        receiver: Any,
    ) -> tuple[str, tuple[float, float, float], float, float]:
        if not isinstance(receiver, Mapping):
            raise ShadowBiasError("SHADOW_RECEIVER_INVALID",
                                  "receiver must be a mapping")
        receiver_id = _non_empty_id(
            receiver.get("id"), "SHADOW_RECEIVER_INVALID"
        )
        normal = _vector3(
            receiver.get("normal"),
            "SHADOW_RECEIVER_INVALID",
            zero_allowed=False,
        )

        if "texel_world" not in receiver:
            raise ShadowBiasError("SHADOW_RECEIVER_INVALID",
                                  "texel_world is required")
        raw_texel = receiver["texel_world"]
        if not _is_real_number(raw_texel):
            raise ShadowBiasError("SHADOW_RECEIVER_INVALID",
                                  "texel_world must be a number")
        texel = _finite_number(
            raw_texel,
            "SHADOW_TEXEL_INVALID",
            "texel_world must be a finite number",
        )

        depth_grad = _finite_number(
            receiver.get("depth_grad"),
            "SHADOW_RECEIVER_INVALID",
            "depth_grad is required and must be a finite number",
        )
        if depth_grad < 0.0:
            raise ShadowBiasError("SHADOW_RECEIVER_INVALID",
                                  "depth_grad must not be negative")

        return receiver_id, normal, texel, depth_grad

    @staticmethod
    def _entry_matches(
        entry: Mapping[str, Any],
        raw_texel: float,
        limits: Mapping[str, float],
    ) -> bool:
        stored_limits = entry.get("limits")
        return (
            entry.get("texel_world") == raw_texel
            and isinstance(stored_limits, Mapping)
            and stored_limits.get("max_offset") == limits["max_offset"]
            and stored_limits.get("texel_min") == limits["texel_min"]
            and stored_limits.get("texel_max") == limits["texel_max"]
        )

    def _remove_pair_keys(
        self,
        light_id: str,
        receiver_id: str,
        *,
        keep_key: tuple[str, str, float],
    ) -> None:
        stale_keys = [
            key
            for key in self._cache
            if key != keep_key and key[0] == light_id and key[1] == receiver_id
        ]
        for key in stale_keys:
            del self._cache[key]

    def _parse_snapshot(
        self, state: Any
    ) -> tuple[
        dict[str, float],
        dict[str, int],
        int,
        int,
        dict[tuple[str, str, float], dict[str, Any]],
    ]:
        if not isinstance(state, Mapping):
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                  "snapshot must be a mapping")

        limits_value = state.get("limits")
        if not isinstance(limits_value, Mapping):
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                  "snapshot limits are invalid")
        try:
            limits = _validate_limits_values(
                limits_value.get("max_offset"),
                limits_value.get("texel_min"),
                limits_value.get("texel_max"),
            )
        except ShadowBiasError as exc:
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID", exc.detail) from exc

        stats_value = state.get("stats")
        stats = self._parse_counts(
            stats_value, _STAT_KEYS, "snapshot stats are invalid"
        )

        cache_stats_value = state.get("cache_stats", {})
        if not isinstance(cache_stats_value, Mapping):
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                  "snapshot cache_stats are invalid")
        cache_counts = self._parse_counts(
            cache_stats_value,
            ("hits", "misses"),
            "snapshot cache counters are invalid",
            required=False,
        )
        hits = cache_counts.get("hits", 0)
        misses = cache_counts.get("misses", 0)

        cache_value = state.get("cache")
        if not isinstance(cache_value, list):
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                  "snapshot cache must be a list")

        cache: dict[tuple[str, str, float], dict[str, Any]] = {}
        pairs_seen: set[tuple[str, str]] = set()
        for item in cache_value:
            if not isinstance(item, Mapping):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache entry must be a mapping")

            key_value = item.get("key")
            if (not isinstance(key_value, list) or len(key_value) != 3
                    or not isinstance(key_value[0], str) or not key_value[0]
                    or not isinstance(key_value[1], str) or not key_value[1]
                    or not _is_real_number(key_value[2])):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache key is invalid")

            light_id, receiver_id, raw_texel_value = key_value
            raw_texel = float(raw_texel_value)
            if not math.isfinite(raw_texel):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache texel is invalid")

            pair = (light_id, receiver_id)
            if pair in pairs_seen:
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache is not deduplicated by pair")
            pairs_seen.add(pair)

            result = item.get("result")
            if not isinstance(result, Mapping):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache result is invalid")
            self._validate_cached_result(result, light_id, receiver_id)

            texel_clamped = item.get("texel_clamped")
            if not isinstance(texel_clamped, bool):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache texel_clamped must be boolean")

            effective_texel = item.get("effective_texel")
            effective_texel = _finite_number(
                effective_texel,
                "SHADOW_SNAPSHOT_INVALID",
                "cache effective_texel is invalid",
            )

            entry_limits_value = item.get("limits")
            if not isinstance(entry_limits_value, Mapping):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "cache limits are invalid")
            try:
                entry_limits = _validate_limits_values(
                    entry_limits_value.get("max_offset"),
                    entry_limits_value.get("texel_min"),
                    entry_limits_value.get("texel_max"),
                )
            except ShadowBiasError as exc:
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      exc.detail) from exc

            key = (light_id, receiver_id, raw_texel)
            if key in cache:
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      "duplicate cache key")
            cache[key] = {
                "texel_world": raw_texel,
                "effective_texel": effective_texel,
                "texel_clamped": texel_clamped,
                "limits": entry_limits,
                "result": copy.deepcopy(dict(result)),
            }

        return limits, stats, hits, misses, cache

    @staticmethod
    def _parse_counts(
        value: Any,
        keys: tuple[str, ...],
        detail: str,
        *,
        required: bool = True,
    ) -> dict[str, int]:
        if not isinstance(value, Mapping):
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID", detail)

        counts: dict[str, int] = {}
        for key in keys:
            if key not in value:
                if required:
                    raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID", detail)
                counts[key] = 0
                continue
            number = value[key]
            if isinstance(number, bool) or not isinstance(number, int) or number < 0:
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID", detail)
            counts[key] = number
        return counts

    @staticmethod
    def _validate_cached_result(
        result: Mapping[str, Any],
        light_id: str,
        receiver_id: str,
    ) -> None:
        expected_numeric = (
            "slope_bias",
            "normal_offset",
            "receiver_bias",
            "offset",
        )
        if result.get("light") != light_id or result.get("receiver") != receiver_id:
            raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                  "cache result identifiers do not match its key")
        for key in expected_numeric:
            value = result.get(key)
            if not _is_real_number(value) or not math.isfinite(float(value)):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      f"cache result {key} is invalid")
        for key in ("floating", "clamped"):
            if not isinstance(result.get(key), bool):
                raise ShadowBiasError("SHADOW_SNAPSHOT_INVALID",
                                      f"cache result {key} must be boolean")
