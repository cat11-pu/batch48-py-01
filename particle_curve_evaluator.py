"""Particle module curve evaluation and deterministic emission budgets."""

from __future__ import annotations

import copy
import math
import operator
import threading
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass


class ParticleError(ValueError):
    """Raised for rejected curves, budgets and snapshots."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _Curve:
    times: tuple
    values: tuple
    modes: tuple


class ParticleCurveEvaluator:
    """Evaluate particle curves and accumulate per-emitter emission budgets."""

    MAX_KEYS = 256
    MAX_MODULES = 64
    MAX_REMAINDER = 100.0
    SNAPSHOT_VERSION = 1
    _MODES = frozenset(("linear", "step", "cubic"))
    _MASK64 = (1 << 64) - 1

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._curves: dict[str, _Curve] = {}
        self._budgets: dict[object, dict] = {}

    @staticmethod
    def _number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParticleError("PARTICLE_KEY_INVALID", value)
        result = float(value)
        if not math.isfinite(result):
            raise ParticleError("PARTICLE_KEY_INVALID", value)
        return result

    @staticmethod
    def _budget_number(value):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ParticleError("PARTICLE_RATE_INVALID", value)
        result = float(value)
        if not math.isfinite(result) or result < 0.0:
            raise ParticleError("PARTICLE_RATE_INVALID", value)
        return result

    @classmethod
    def _validate_keys(cls, keys) -> _Curve:
        if isinstance(keys, (str, bytes, bytearray)):
            raise ParticleError("PARTICLE_KEY_INVALID", keys)
        try:
            raw_keys = list(keys)
        except TypeError as exc:
            raise ParticleError("PARTICLE_KEY_INVALID", keys) from exc

        if len(raw_keys) > cls.MAX_KEYS:
            raise ParticleError("PARTICLE_KEY_LIMIT", len(raw_keys))
        if not raw_keys:
            raise ParticleError("PARTICLE_KEY_INVALID", "empty")

        times = []
        values = []
        modes = []
        previous = None
        for index, key in enumerate(raw_keys):
            if not isinstance(key, Mapping):
                raise ParticleError("PARTICLE_KEY_INVALID", index)
            try:
                time = key["time"]
                value = key["value"]
                mode = key["mode"]
            except (KeyError, TypeError) as exc:
                raise ParticleError("PARTICLE_KEY_INVALID", index) from exc

            time = cls._number(time)
            value = cls._number(value)
            if not isinstance(mode, str) or mode not in cls._MODES:
                raise ParticleError("PARTICLE_KEY_INVALID", mode)
            if previous is not None and time <= previous:
                raise ParticleError("PARTICLE_KEY_INVALID", index)

            times.append(time)
            values.append(value)
            modes.append(mode)
            previous = time

        return _Curve(tuple(times), tuple(values), tuple(modes))

    @staticmethod
    def _cubic(curve: _Curve, index: int, t: float) -> float:
        times = curve.times
        values = curve.values
        count = len(times)

        t0 = times[index]
        t1 = times[index + 1]
        v0 = values[index]
        v1 = values[index + 1]
        duration = t1 - t0

        if index == 0:
            slope0 = (v1 - v0) / duration
        else:
            slope0 = (v1 - values[index - 1]) / (t1 - times[index - 1])

        if index + 2 >= count:
            slope1 = (v1 - v0) / duration
        else:
            slope1 = (values[index + 2] - v0) / (times[index + 2] - t0)

        u = (t - t0) / duration
        u2 = u * u
        u3 = u2 * u
        value = (
            (2.0 * u3 - 3.0 * u2 + 1.0) * v0
            + (u3 - 2.0 * u2 + u) * duration * slope0
            + (-2.0 * u3 + 3.0 * u2) * v1
            + (u3 - u2) * duration * slope1
        )
        low = min(v0, v1)
        high = max(v0, v1)
        if value < low:
            return low
        if value > high:
            return high
        return value

    @classmethod
    def _eval_curve(cls, curve: _Curve, t: float) -> float:
        times = curve.times
        values = curve.values

        if t <= times[0]:
            return values[0]
        if t >= times[-1]:
            return values[-1]

        index = bisect_right(times, t) - 1
        mode = curve.modes[index]
        if mode == "step":
            return values[index]

        t0 = times[index]
        t1 = times[index + 1]
        v0 = values[index]
        v1 = values[index + 1]
        if mode == "linear":
            ratio = (t - t0) / (t1 - t0)
            return v0 + (v1 - v0) * ratio
        return cls._cubic(curve, index, t)

    @staticmethod
    def _mix64(value: int) -> int:
        mask = ParticleCurveEvaluator._MASK64
        value = (value + 0x9E3779B97F4A7C15) & mask
        value = ((value ^ (value >> 30)) * 0xBF58476D1CE4E5B9) & mask
        value = ((value ^ (value >> 27)) * 0x94D049BB133111EB) & mask
        return (value ^ (value >> 31)) & mask

    def set_curve(self, module_id: str, keys: list) -> None:
        if not isinstance(module_id, str):
            raise ParticleError("PARTICLE_KEY_INVALID", module_id)
        curve = self._validate_keys(keys)

        with self._lock:
            if (module_id not in self._curves
                    and len(self._curves) >= self.MAX_MODULES):
                raise ParticleError("PARTICLE_MODULE_LIMIT", module_id)
            self._curves[module_id] = curve

    def remove_curve(self, module_id: str) -> bool:
        with self._lock:
            if module_id not in self._curves:
                return False
            del self._curves[module_id]
            return True

    def curve_info(self, module_id: str) -> dict:
        with self._lock:
            curve = self._curves.get(module_id)
        if curve is None:
            raise ParticleError("PARTICLE_UNKNOWN_MODULE", module_id)
        return {
            "module_id": module_id,
            "key_count": len(curve.times),
            "start": curve.times[0],
            "end": curve.times[-1],
        }

    def list_modules(self) -> list:
        with self._lock:
            modules = list(self._curves)
        modules.sort()
        return modules

    def eval(self, module_id: str, t) -> float:
        with self._lock:
            curve = self._curves.get(module_id)
        if curve is None:
            raise ParticleError("PARTICLE_UNKNOWN_MODULE", module_id)
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ParticleError("PARTICLE_TIME_INVALID", t)
        time = float(t)
        if not math.isfinite(time):
            raise ParticleError("PARTICLE_TIME_INVALID", t)
        return self._eval_curve(curve, time)

    def emission_budget(self, emitter: str, rate, dt) -> int:
        rate = self._budget_number(rate)
        frame_ms = self._budget_number(dt)
        amount = rate * frame_ms / 1000.0
        if not math.isfinite(amount):
            raise ParticleError("PARTICLE_RATE_INVALID", rate * frame_ms)

        with self._lock:
            budget = self._budgets.get(emitter)
            if budget is None:
                budget = {"remainder": 0.0, "emitted": 0, "dropped": 0}
                self._budgets[emitter] = budget

            total = budget["remainder"] + amount
            if not math.isfinite(total):
                raise ParticleError("PARTICLE_RATE_INVALID", total)
            if total > self.MAX_REMAINDER:
                excess = total - self.MAX_REMAINDER
                budget["dropped"] += int(math.floor(excess))
                total = self.MAX_REMAINDER
            if total < 0.0:
                total = 0.0

            emitted = int(math.floor(total))
            remainder = total - emitted
            if remainder < 0.0 or not math.isfinite(remainder):
                remainder = 0.0

            budget["remainder"] = remainder
            budget["emitted"] += emitted
            return emitted

    def budget_state(self, emitter: str) -> dict:
        with self._lock:
            budget = self._budgets.get(emitter)
            if budget is None:
                return {"remainder": 0.0, "emitted": 0, "dropped": 0}
            return dict(budget)

    def reset_budget(self, emitter: str) -> None:
        with self._lock:
            self._budgets.pop(emitter, None)

    def random_stream(self, seed: int, index: int) -> float:
        try:
            seed_value = operator.index(seed) & self._MASK64
            index_value = operator.index(index) & self._MASK64
        except TypeError as exc:
            raise ParticleError("PARTICLE_KEY_INVALID", (seed, index)) from exc
        mixed = self._mix64(seed_value ^ self._mix64(index_value))
        return mixed / float(1 << 64)

    def remap_time(self, t, start, end, mode) -> float:
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            raise ParticleError("PARTICLE_TIME_INVALID", t)
        if isinstance(start, bool) or not isinstance(start, (int, float)):
            raise ParticleError("PARTICLE_TIME_INVALID", start)
        if isinstance(end, bool) or not isinstance(end, (int, float)):
            raise ParticleError("PARTICLE_TIME_INVALID", end)

        time = float(t)
        start_value = float(start)
        end_value = float(end)
        if (not math.isfinite(time)
                or not math.isfinite(start_value)
                or not math.isfinite(end_value)
                or start_value >= end_value):
            raise ParticleError("PARTICLE_TIME_INVALID", (start, end))
        if not isinstance(mode, str) or mode not in ("clamp", "loop"):
            raise ParticleError("PARTICLE_REMAP_MODE", mode)

        if mode == "clamp":
            return min(max(time, start_value), end_value)

        length = end_value - start_value
        remapped = start_value + ((time - start_value) % length)
        if remapped < start_value:
            return start_value
        if remapped >= end_value:
            return start_value
        return remapped

    def snapshot(self) -> dict:
        with self._lock:
            curves = {
                module_id: [
                    {"time": curve.times[index],
                     "value": curve.values[index],
                     "mode": curve.modes[index]}
                    for index in range(len(curve.times))
                ]
                for module_id, curve in self._curves.items()
            }
            budgets = {
                emitter: dict(budget)
                for emitter, budget in self._budgets.items()
            }
        return copy.deepcopy({
            "version": self.SNAPSHOT_VERSION,
            "curves": curves,
            "budgets": budgets,
        })

    def restore(self, state: dict) -> None:
        try:
            if not isinstance(state, Mapping):
                raise ValueError("state")
            version = state.get("version")
            if (isinstance(version, bool)
                    or not isinstance(version, int)
                    or version != self.SNAPSHOT_VERSION):
                raise ValueError("version")

            raw_curves = state.get("curves")
            if not isinstance(raw_curves, Mapping):
                raise ValueError("curves")
            if len(raw_curves) > self.MAX_MODULES:
                raise ValueError("module limit")

            curves: dict[str, _Curve] = {}
            for module_id, keys in raw_curves.items():
                if not isinstance(module_id, str):
                    raise ValueError("module id")
                curves[module_id] = self._validate_keys(keys)

            raw_budgets = state.get("budgets", {})
            if not isinstance(raw_budgets, Mapping):
                raise ValueError("budgets")

            budgets: dict[object, dict] = {}
            for emitter, budget in raw_budgets.items():
                if not isinstance(budget, Mapping):
                    raise ValueError("budget")
                try:
                    remainder = budget["remainder"]
                    emitted = budget["emitted"]
                    dropped = budget["dropped"]
                except KeyError as exc:
                    raise ValueError("budget fields") from exc
                if (isinstance(remainder, bool)
                        or not isinstance(remainder, (int, float))
                        or not math.isfinite(float(remainder))
                        or float(remainder) < 0.0
                        or float(remainder) > self.MAX_REMAINDER):
                    raise ValueError("remainder")
                if (isinstance(emitted, bool)
                        or not isinstance(emitted, int)
                        or emitted < 0):
                    raise ValueError("emitted")
                if (isinstance(dropped, bool)
                        or not isinstance(dropped, int)
                        or dropped < 0):
                    raise ValueError("dropped")
                budgets[emitter] = {
                    "remainder": float(remainder),
                    "emitted": emitted,
                    "dropped": dropped,
                }
        except (ParticleError, TypeError, ValueError) as exc:
            raise ParticleError("PARTICLE_SNAPSHOT_INVALID", exc) from exc

        with self._lock:
            self._curves = curves
            self._budgets = budgets
