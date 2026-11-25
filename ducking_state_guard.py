"""Sidechain triggered ducking state machine for narration over music beds."""

from __future__ import annotations

import copy
import threading

DUCK_STATES = ("idle", "attack", "hold", "release", "bypass")
SNAPSHOT_VERSION = 1

STAT_KEYS = (
    "sources_added",
    "sources_removed",
    "feeds",
    "advances",
    "bypass_toggles",
    "rejected",
)


class DuckingError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DuckingStateGuard:
    """Drive attack, hold and release phases for one or more trigger sources."""

    def __init__(self, *, sample_rate: int = 48000, attack_ms: int = 20,
                 hold_ms: int = 300, release_ms: int = 800,
                 depth: float = 0.35, quantum: float = 0.01,
                 clock=None) -> None:
        self.sample_rate = sample_rate
        self.attack_ms = attack_ms
        self.hold_ms = hold_ms
        self.release_ms = release_ms
        self.depth = depth
        self.quantum = quantum
        self._clock = clock
        self._lock = threading.RLock()
        self._stats = {key: 0 for key in STAT_KEYS}
        self._sources: dict[object, dict] = {}
        self._state = "idle"
        self._bypassed = False
        self._frozen_state: str | None = None
        self._gain = 1.0
        self._phase_pos = 0
        self._ramp_start = 1.0
        self._ramp_target = 1.0
        self._ramp_frames = 0
        self.attack_frames = self.frames_for(attack_ms)
        self.hold_frames = self.frames_for(hold_ms)
        self.release_frames = self.frames_for(release_ms)

    def _fail(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise DuckingError(code, detail)

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _quantize(self, value: float) -> float:
        steps = round(value / self.quantum)
        quantized = round(steps * self.quantum, 2)
        return min(1.0, max(self.quantum, quantized))

    def frames_for(self, ms: int) -> int:
        with self._lock:
            if not self._is_int(ms) or ms < 0:
                self._fail("DUCK_INVALID_TIMING", {"ms": ms})
            return (ms * self.sample_rate + 500) // 1000

    def add_source(self, source_id: str, *, priority: int = 0,
                   depth: float | None = None) -> dict:
        with self._lock:
            if source_id in self._sources:
                self._fail("DUCK_DUPLICATE_SOURCE", {"source_id": source_id})
            if not self._is_int(priority) or priority < 0:
                self._fail("DUCK_INVALID_PRIORITY", {"priority": priority})
            effective_depth = self.depth if depth is None else depth
            if (
                isinstance(effective_depth, bool)
                or not isinstance(effective_depth, (int, float))
                or not 0.0 < float(effective_depth) < 1.0
            ):
                self._fail("DUCK_INVALID_DEPTH", {"depth": depth})
            record = {
                "source_id": source_id,
                "priority": priority,
                "depth": float(effective_depth),
                "active": False,
            }
            self._sources[source_id] = record
            self._stats["sources_added"] += 1
            return dict(record)

    def remove_source(self, source_id: str) -> None:
        with self._lock:
            record = self._sources.get(source_id)
            if record is None:
                self._fail("DUCK_UNKNOWN_SOURCE", {"source_id": source_id})
            was_active = record["active"]
            del self._sources[source_id]
            self._stats["sources_removed"] += 1
            if was_active and not self._bypassed:
                self._reconcile()

    def feed(self, source_id: str, active: bool) -> dict:
        with self._lock:
            record = self._sources.get(source_id)
            if record is None:
                self._fail("DUCK_UNKNOWN_SOURCE", {"source_id": source_id})
            self._stats["feeds"] += 1
            record["active"] = bool(active)
            if not self._bypassed:
                self._reconcile()
            return {
                "source_id": source_id,
                "active": record["active"],
                "state": self._state,
            }

    def _dominant_record(self) -> dict | None:
        dominant = None
        for record in self._sources.values():
            if not record["active"]:
                continue
            if dominant is None or (
                record["depth"], record["priority"]
            ) > (dominant["depth"], dominant["priority"]):
                dominant = record
        return dominant

    def _target_gain(self) -> float | None:
        dominant = self._dominant_record()
        if dominant is None:
            return None
        return self._quantize(1.0 - dominant["depth"])

    def _start_attack(self, target: float) -> None:
        self._state = "attack"
        self._phase_pos = 0
        self._ramp_start = self._gain
        self._ramp_target = target
        self._ramp_frames = self.attack_frames
        if self.attack_frames <= 0:
            self._gain = target
            self._state = "hold"

    def _start_release(self, target: float) -> None:
        self._state = "release"
        self._phase_pos = 0
        self._ramp_start = self._gain
        self._ramp_target = target
        self._ramp_frames = self.release_frames
        if self.release_frames <= 0:
            self._finish_release(target)

    def _finish_release(self, target: float) -> None:
        if self._target_gain() is None:
            self._state = "idle"
            self._gain = 1.0
        else:
            self._state = "hold"
            self._phase_pos = 0
            self._gain = target

    def _reconcile(self) -> None:
        target = self._target_gain()
        if target is None:
            if self._state in ("attack", "hold"):
                self._state = "hold"
                self._phase_pos = 0
            return
        if self._state == "idle":
            self._start_attack(target)
        elif self._state == "attack":
            self._ramp_target = target
            if target >= self._gain:
                self._state = "hold"
                self._phase_pos = 0
                self._gain = target
        elif self._state == "hold":
            if target < self._gain:
                self._start_attack(target)
            else:
                self._phase_pos = 0
                self._gain = target
        elif self._state == "release":
            if target < self._gain:
                self._start_attack(target)
            else:
                self._state = "hold"
                self._phase_pos = 0
                self._gain = target

    def _ramp_gain(self) -> float:
        if self._ramp_frames <= 0:
            return self._ramp_target
        fraction = min(1.0, self._phase_pos / self._ramp_frames)
        raw = self._ramp_start + (
            self._ramp_target - self._ramp_start
        ) * fraction
        return self._quantize(raw)

    def advance(self, frames: int) -> float:
        with self._lock:
            if not self._is_int(frames) or frames < 0:
                self._fail("DUCK_INVALID_TIMING", {"frames": frames})
            self._stats["advances"] += 1
            if self._bypassed:
                return 1.0
            remaining = frames
            while remaining > 0 and self._state != "idle":
                if self._state == "attack":
                    step = min(remaining, self._ramp_frames - self._phase_pos)
                    self._phase_pos += step
                    remaining -= step
                    if self._phase_pos >= self._ramp_frames:
                        self._gain = self._ramp_target
                        self._state = "hold"
                        self._phase_pos = 0
                    else:
                        self._gain = self._ramp_gain()
                elif self._state == "hold":
                    target = self._target_gain()
                    if target is not None:
                        self._gain = target
                        remaining = 0
                    elif self.hold_frames <= 0:
                        self._start_release(1.0)
                    else:
                        step = min(
                            remaining, self.hold_frames - self._phase_pos
                        )
                        self._phase_pos += step
                        remaining -= step
                        if self._phase_pos >= self.hold_frames:
                            self._start_release(1.0)
                elif self._state == "release":
                    step = min(remaining, self._ramp_frames - self._phase_pos)
                    self._phase_pos += step
                    remaining -= step
                    if self._phase_pos >= self._ramp_frames:
                        self._finish_release(self._ramp_target)
                    else:
                        self._gain = self._ramp_gain()
            return self._gain

    def gain(self) -> float:
        with self._lock:
            return 1.0 if self._bypassed else self._gain

    def state(self) -> str:
        with self._lock:
            return self._state

    def dominant_source(self) -> str | None:
        with self._lock:
            dominant = self._dominant_record()
            return None if dominant is None else dominant["source_id"]

    def set_bypass(self, flag: bool) -> None:
        with self._lock:
            enabled = bool(flag)
            if enabled == self._bypassed:
                return
            self._stats["bypass_toggles"] += 1
            self._bypassed = enabled
            if enabled:
                self._frozen_state = self._state
                self._state = "bypass"
            else:
                self._state = self._frozen_state or "idle"
                self._frozen_state = None
                self._reconcile()

    def bypassed(self) -> bool:
        with self._lock:
            return self._bypassed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "state": self._state,
                "bypassed": self._bypassed,
                "frozen_state": self._frozen_state,
                "gain": self._gain,
                "phase_pos": self._phase_pos,
                "ramp_start": self._ramp_start,
                "ramp_target": self._ramp_target,
                "ramp_frames": self._ramp_frames,
                "sources": copy.deepcopy(list(self._sources.values())),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                if not isinstance(state, dict):
                    raise ValueError
                if state.get("version") != SNAPSHOT_VERSION:
                    raise ValueError
                restored_state = state.get("state")
                if restored_state not in DUCK_STATES:
                    raise ValueError
                bypassed = state.get("bypassed")
                if not isinstance(bypassed, bool):
                    raise ValueError
                if restored_state == "bypass" and not bypassed:
                    raise ValueError
                frozen_state = state.get("frozen_state")
                if bypassed and (
                    frozen_state not in DUCK_STATES
                    or frozen_state == "bypass"
                ):
                    raise ValueError
                gain = state.get("gain")
                if isinstance(gain, bool) or not isinstance(
                    gain, (int, float)
                ) or not 0.0 <= float(gain) <= 1.0:
                    raise ValueError
                phase_pos = state.get("phase_pos")
                if not self._is_int(phase_pos) or phase_pos < 0:
                    raise ValueError
                ramp_frames = state.get("ramp_frames")
                if not self._is_int(ramp_frames) or ramp_frames < 0:
                    raise ValueError
                ramp_values = (
                    state.get("ramp_start"),
                    state.get("ramp_target"),
                )
                for ramp_value in ramp_values:
                    if isinstance(ramp_value, bool) or not isinstance(
                        ramp_value, (int, float)
                    ) or not 0.0 <= float(ramp_value) <= 1.0:
                        raise ValueError
                raw_sources = state.get("sources")
                if not isinstance(raw_sources, list):
                    raise ValueError
                rebuilt_sources: dict[object, dict] = {}
                for item in raw_sources:
                    if not isinstance(item, dict):
                        raise ValueError
                    source_id = item.get("source_id")
                    priority = item.get("priority")
                    depth = item.get("depth")
                    active = item.get("active")
                    if source_id in rebuilt_sources:
                        raise ValueError
                    if not self._is_int(priority) or priority < 0:
                        raise ValueError
                    if (
                        isinstance(depth, bool)
                        or not isinstance(depth, (int, float))
                        or not 0.0 < float(depth) < 1.0
                    ):
                        raise ValueError
                    if not isinstance(active, bool):
                        raise ValueError
                    rebuilt_sources[source_id] = {
                        "source_id": source_id,
                        "priority": priority,
                        "depth": float(depth),
                        "active": active,
                    }
            except (ValueError, TypeError, KeyError):
                self._stats["rejected"] += 1
                raise DuckingError(
                    "DUCK_SNAPSHOT_INVALID", {"state": state}
                ) from None
            self._sources = rebuilt_sources
            self._state = restored_state
            self._bypassed = bypassed
            self._frozen_state = frozen_state
            self._gain = self._quantize(float(gain))
            self._phase_pos = phase_pos
            self._ramp_start = float(ramp_values[0])
            self._ramp_target = float(ramp_values[1])
            self._ramp_frames = ramp_frames

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0
