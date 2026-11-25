"""Memory pressure mediator: watermarks, throttling and OOM arbitration."""

from __future__ import annotations

import copy
import math
import threading
from collections import deque
from typing import Any


class MemoryPressureError(ValueError):
    """Rejection raised by the memory pressure mediator."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MemoryPressureMediator:
    """Watch control groups, grade pressure and arbitrate OOM victims."""

    def __init__(self, *, window: int = 3, high_watermark_ratio: float = 0.9,
                 max_groups: int = 16, clock=None) -> None:
        if not _is_strict_int(window) or window <= 0:
            raise MemoryPressureError("MEMORY_THRESHOLD_INVALID", "window")
        if not _is_number(high_watermark_ratio) or not 0 < high_watermark_ratio <= 1:
            raise MemoryPressureError(
                "MEMORY_THRESHOLD_INVALID", "high_watermark_ratio"
            )
        if not _is_strict_int(max_groups) or max_groups <= 0:
            raise MemoryPressureError("MEMORY_GROUP_LIMIT", "max_groups")

        self.window = window
        self.high_watermark_ratio = high_watermark_ratio
        self.max_groups = max_groups
        self._clock = clock
        self._lock = threading.RLock()
        self._groups: dict[str, dict[str, Any]] = {}
        self._stats = {"samples": 0, "picks": 0, "reclaimed": 0}

    def register_group(self, group: str, tenant: str, limit: int, high: int | None = None) -> dict:
        with self._lock:
            if not _is_nonempty_str(group) or not _is_nonempty_str(tenant):
                raise MemoryPressureError("MEMORY_GROUP_INVALID", group)
            if group in self._groups:
                raise MemoryPressureError("MEMORY_GROUP_DUPLICATE", group)
            if len(self._groups) >= self.max_groups:
                raise MemoryPressureError("MEMORY_GROUP_LIMIT", group)
            if not _is_strict_int(limit) or limit <= 0:
                raise MemoryPressureError("MEMORY_GROUP_LIMIT", limit)

            if high is None:
                high = int(limit * self.high_watermark_ratio)
            elif not _is_strict_int(high) or not 0 < high <= limit:
                raise MemoryPressureError("MEMORY_THRESHOLD_INVALID", high)

            self._groups[group] = {
                "tenant": tenant,
                "limit": limit,
                "high": high,
                "usage": 0,
                "samples": deque(maxlen=self.window),
                "throttled": False,
                "target": None,
                "revision": 0,
                "picked": set(),
            }
            return {"group": group, "tenant": tenant, "limit": limit, "high": high}

    def list_groups(self) -> list:
        with self._lock:
            return list(self._groups)

    def observe(self, group: str, sample: dict) -> dict:
        with self._lock:
            state = self._require_group(group)
            normalized = _validate_sample(sample)
            samples = state["samples"]
            if samples and normalized["tick"] <= samples[-1]["tick"]:
                raise MemoryPressureError("MEMORY_SAMPLE_STALE", normalized["tick"])

            if normalized["limit"] is not None:
                state["limit"] = normalized["limit"]

            samples.append(normalized)
            state["usage"] = normalized["usage"]
            self._stats["samples"] += 1
            return self._pressure_locked(group, state)

    def classify(self, group: str) -> str:
        with self._lock:
            state = self._require_group(group)
            self._require_sample(state)
            return self._classify_locked(state)

    def pressure(self, group: str) -> dict:
        with self._lock:
            state = self._require_group(group)
            self._require_sample(state)
            return self._pressure_locked(group, state)

    def throttle(self, group: str, target: int) -> dict:
        with self._lock:
            state = self._require_group(group)
            self._require_sample(state)
            if not _is_strict_int(target) or target <= 0 or target >= state["usage"]:
                raise MemoryPressureError("MEMORY_TARGET_INVALID", target)

            state["throttled"] = True
            state["target"] = target
            state["revision"] += 1
            return {"group": group, "throttled": True, "target": target}

    def clear_throttle(self, group: str) -> dict:
        with self._lock:
            state = self._require_group(group)
            state["throttled"] = False
            state["target"] = None
            return {"group": group, "throttled": False, "target": None}

    def throttle_state(self, group: str) -> dict:
        with self._lock:
            state = self._require_group(group)
            return {
                "group": group,
                "throttled": state["throttled"],
                "target": state["target"],
                "revision": state["revision"],
            }

    def pick_oom_victim(self, group: str) -> dict:
        with self._lock:
            state = self._require_group(group)
            victim = self._select_victim_locked(state)
            if victim is None:
                raise MemoryPressureError("MEMORY_NO_VICTIM", group)

            state["picked"].add(victim["pid"])
            self._stats["picks"] += 1
            return {
                "pid": victim["pid"],
                "group": group,
                "tenant": victim["tenant"],
                "rss": victim["rss"],
                "score": victim["score"],
            }

    def reclaim(self, group: str, nbytes: int) -> dict:
        with self._lock:
            state = self._require_group(group)
            if not _is_strict_int(nbytes) or nbytes <= 0 or nbytes > state["usage"]:
                raise MemoryPressureError("MEMORY_RECLAIM_INVALID", nbytes)

            state["usage"] -= nbytes
            self._stats["reclaimed"] += nbytes
            if state["throttled"] and state["usage"] < state["high"]:
                state["throttled"] = False
                state["target"] = None

            return {
                "group": group,
                "freed": nbytes,
                "usage": state["usage"],
                "throttled": state["throttled"],
            }

    def group_usage(self, group: str) -> int:
        with self._lock:
            state = self._require_group(group)
            return state["usage"]

    def snapshot(self) -> dict:
        with self._lock:
            groups = {}
            for name, state in self._groups.items():
                groups[name] = {
                    "tenant": state["tenant"],
                    "limit": state["limit"],
                    "high": state["high"],
                    "usage": state["usage"],
                    "samples": copy.deepcopy(list(state["samples"])),
                    "throttled": state["throttled"],
                    "target": state["target"],
                    "revision": state["revision"],
                    "picked": sorted(state["picked"]),
                }
            return {
                "version": 1,
                "groups": groups,
                "stats": self._stats_dict_locked(),
            }

    def restore(self, state: dict) -> None:
        new_groups, new_stats = _build_restored_state(
            state, self.window, self.max_groups
        )
        with self._lock:
            self._groups = new_groups
            self._stats = new_stats

    def stats(self) -> dict:
        with self._lock:
            return self._stats_dict_locked()

    def reset_stats(self) -> None:
        with self._lock:
            self._stats["samples"] = 0
            self._stats["picks"] = 0
            self._stats["reclaimed"] = 0

    def _require_group(self, group: str) -> dict[str, Any]:
        if not isinstance(group, str) or group not in self._groups:
            raise MemoryPressureError("MEMORY_UNKNOWN_GROUP", group)
        return self._groups[group]

    @staticmethod
    def _require_sample(
        state: dict[str, Any], *, code: str = "MEMORY_NO_SAMPLE"
    ) -> None:
        if not state["samples"]:
            raise MemoryPressureError(code)

    @staticmethod
    def _metrics_locked(state: dict[str, Any]) -> dict[str, Any]:
        samples = state["samples"]
        current_usage = state["usage"]
        growth = current_usage - samples[0]["usage"]
        projected = current_usage + max(growth, 0)
        return {
            "usage": current_usage,
            "limit": state["limit"],
            "high": state["high"],
            "growth": growth,
            "projected": projected,
            "samples": len(samples),
        }

    def _classify_locked(self, state: dict[str, Any]) -> str:
        metrics = self._metrics_locked(state)
        usage = metrics["usage"]
        limit = metrics["limit"]
        high = metrics["high"]
        projected = metrics["projected"]

        if usage >= limit or projected >= limit:
            level = "critical"
        elif usage >= high or projected >= high:
            level = "elevated"
        else:
            level = "normal"

        latest_full = state["samples"][-1]["pressure"]["full"]
        if latest_full >= 60 and metrics["growth"] > 0:
            if level == "normal":
                level = "elevated"
            elif level == "elevated":
                level = "critical"
        return level

    def _pressure_locked(self, group: str, state: dict[str, Any]) -> dict:
        metrics = self._metrics_locked(state)
        return {
            "level": self._classify_locked(state),
            "usage": metrics["usage"],
            "limit": metrics["limit"],
            "high": metrics["high"],
            "growth": metrics["growth"],
            "projected": metrics["projected"],
            "samples": metrics["samples"],
        }

    @staticmethod
    def _select_victim_locked(state: dict[str, Any]) -> dict[str, Any] | None:
        if not state["samples"]:
            return None

        tenant = state["tenant"]
        picked = state["picked"]
        candidates = [
            proc
            for proc in state["samples"][-1]["procs"]
            if proc["tenant"] == tenant
            and proc["pid"] not in picked
            and proc["score"] >= 0
        ]
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda proc: (-proc["score"], -proc["rss"], proc["pid"]),
        )

    def _stats_dict_locked(self) -> dict:
        return {
            "groups": len(self._groups),
            "samples": self._stats["samples"],
            "picks": self._stats["picks"],
            "reclaimed": self._stats["reclaimed"],
        }


def _is_strict_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _validate_sample(sample: Any) -> dict[str, Any]:
    if not isinstance(sample, dict):
        raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "sample")

    for field in ("usage", "tick", "pressure", "procs"):
        if field not in sample:
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", field)

    usage = sample["usage"]
    if not _is_strict_int(usage) or usage < 0:
        raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "usage")

    tick = sample["tick"]
    if (
        isinstance(tick, bool)
        or not isinstance(tick, (int, float))
        or not math.isfinite(tick)
    ):
        raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "tick")

    pressure = sample["pressure"]
    if not isinstance(pressure, dict):
        raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "pressure")
    for field in ("some10", "full"):
        value = pressure.get(field)
        if not _is_number(value) or not 0 <= value <= 100:
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", field)

    raw_procs = sample["procs"]
    if not isinstance(raw_procs, list):
        raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "procs")

    procs = []
    seen_pids = set()
    for raw_proc in raw_procs:
        if not isinstance(raw_proc, dict):
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "proc")
        for field in ("pid", "rss", "score", "tenant"):
            if field not in raw_proc:
                raise MemoryPressureError("MEMORY_SAMPLE_INVALID", field)

        pid = raw_proc["pid"]
        rss = raw_proc["rss"]
        score = raw_proc["score"]
        tenant = raw_proc["tenant"]
        if not _is_strict_int(pid):
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "pid")
        if pid in seen_pids:
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "duplicate pid")
        if not _is_strict_int(rss) or rss < 0:
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "rss")
        if not _is_strict_int(score):
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "score")
        if not isinstance(tenant, str):
            raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "tenant")

        seen_pids.add(pid)
        procs.append({
            "pid": pid,
            "rss": rss,
            "score": score,
            "tenant": tenant,
        })

    limit = sample.get("limit")
    if limit is not None and (not _is_strict_int(limit) or limit <= 0):
        raise MemoryPressureError("MEMORY_SAMPLE_INVALID", "limit")

    return {
        "usage": usage,
        "tick": tick,
        "pressure": {
            "some10": pressure["some10"],
            "full": pressure["full"],
        },
        "procs": procs,
        "limit": limit,
    }


def _build_restored_state(
    state: Any, window: int, max_groups: int
) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    if not isinstance(state, dict):
        raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "state")
    if not _is_strict_int(state.get("version")) or state.get("version") != 1:
        raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "version")

    groups_data = state.get("groups")
    if not isinstance(groups_data, dict):
        raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "groups")
    if len(groups_data) > max_groups:
        raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "groups")

    stats_data = state.get("stats")
    if not isinstance(stats_data, dict):
        raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "stats")

    stats = {}
    for field in ("samples", "picks", "reclaimed"):
        value = stats_data.get(field, 0)
        if not _is_strict_int(value) or value < 0:
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", field)
        stats[field] = value

    groups: dict[str, dict[str, Any]] = {}
    for name, raw_group in groups_data.items():
        if not _is_nonempty_str(name) or not isinstance(raw_group, dict):
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", name)

        tenant = raw_group.get("tenant")
        limit = raw_group.get("limit")
        high = raw_group.get("high")
        usage = raw_group.get("usage", 0)
        if not _is_nonempty_str(tenant):
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "tenant")
        if not _is_strict_int(limit) or limit <= 0:
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "limit")
        if not _is_strict_int(high) or high <= 0:
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "high")
        if not _is_strict_int(usage) or usage < 0:
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "usage")

        raw_samples = raw_group.get("samples", [])
        if not isinstance(raw_samples, list):
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "samples")
        normalized_samples = []
        previous_tick = None
        for raw_sample in raw_samples:
            try:
                sample = _validate_sample(raw_sample)
            except MemoryPressureError as exc:
                raise MemoryPressureError(
                    "MEMORY_SNAPSHOT_INVALID", "samples"
                ) from exc
            if previous_tick is not None and sample["tick"] <= previous_tick:
                raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "sample tick")
            previous_tick = sample["tick"]
            normalized_samples.append(sample)
        if len(normalized_samples) > window:
            normalized_samples = normalized_samples[-window:]

        throttle_data = raw_group.get("throttle")
        if not isinstance(throttle_data, dict):
            throttle_data = raw_group
        throttled = throttle_data.get(
            "throttled", raw_group.get("throttled", False)
        )
        target = throttle_data.get("target", raw_group.get("target"))
        revision = throttle_data.get(
            "revision", raw_group.get("revision", 0)
        )
        if not isinstance(throttled, bool):
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "throttled")
        if target is not None and (not _is_strict_int(target) or target <= 0):
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "target")
        if not _is_strict_int(revision) or revision < 0:
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "revision")
        if not throttled:
            target = None

        picked_data = raw_group.get("picked", [])
        if isinstance(picked_data, (list, tuple, set, frozenset)):
            picked_values = list(picked_data)
        else:
            raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "picked")
        picked = set()
        for pid in picked_values:
            if not _is_strict_int(pid):
                raise MemoryPressureError("MEMORY_SNAPSHOT_INVALID", "picked pid")
            picked.add(pid)

        groups[name] = {
            "tenant": tenant,
            "limit": limit,
            "high": high,
            "usage": usage,
            "samples": deque(normalized_samples, maxlen=window),
            "throttled": throttled,
            "target": target,
            "revision": revision,
            "picked": picked,
        }

    return groups, stats
