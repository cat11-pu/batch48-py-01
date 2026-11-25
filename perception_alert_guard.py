"""NPC perception, memory decay and alarm propagation guard."""

from __future__ import annotations

import copy
import math
import threading
from collections.abc import Mapping


class PerceptionError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


LEVELS = ("patrol", "search", "alert", "combat")
_LEVEL_RANK = {level: index for index, level in enumerate(LEVELS)}
_STATS_KEYS = ("observed", "heard", "propagated", "decays")
_SNAPSHOT_VERSION = 1


def _is_ms(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _position_xy(position: object) -> tuple[float, float]:
    if isinstance(position, Mapping):
        if "x" in position and "y" in position:
            raw_x, raw_y = position["x"], position["y"]
        else:
            raise PerceptionError("PERCEPTION_POSITION_INVALID", position)
    elif isinstance(position, (tuple, list)) and len(position) >= 2:
        raw_x, raw_y = position[0], position[1]
    else:
        raise PerceptionError("PERCEPTION_POSITION_INVALID", position)

    if not _is_number(raw_x) or not _is_number(raw_y):
        raise PerceptionError("PERCEPTION_POSITION_INVALID", position)
    return float(raw_x), float(raw_y)


class PerceptionGuard:
    """Track sight, hearing, memory decay and alarm chains per NPC."""

    def __init__(self, *, view_range: float = 15.0, view_angle_deg: float = 90.0,
                 memory_ms: int = 4000, alarm_ttl_ms: int = 6000) -> None:
        self.view_range = view_range
        self.view_angle_deg = view_angle_deg
        self.memory_ms = memory_ms
        self.alarm_ttl_ms = alarm_ttl_ms
        self._lock = threading.RLock()
        self._states: dict[object, dict] = {}
        self._stealth: dict[object, object] = {}
        self._stats = {key: 0 for key in _STATS_KEYS}

    def _new_state(self) -> dict:
        return {
            "level": "patrol",
            "last_seen": None,
            "target": None,
            "sources": [],
            "decay_at": None,
            "position": None,
        }

    def _ensure_state(self, npc: object) -> dict:
        state = self._states.get(npc)
        if state is None:
            state = self._new_state()
            self._states[npc] = state
        return state

    def _require_now(self, now: object) -> int:
        if not _is_ms(now):
            raise PerceptionError("PERCEPTION_TIME_INVALID", now)
        return now

    def _optional_now(self, now: object) -> int | None:
        if now is None:
            return None
        return self._require_now(now)

    @staticmethod
    def _add_source(state: dict, source: str) -> None:
        if source not in state["sources"]:
            state["sources"].append(source)

    def _decay(self, state: dict, now: int | None) -> None:
        if now is None or state.get("decay_at") is None:
            return

        while state["decay_at"] is not None and now >= state["decay_at"]:
            rank = _LEVEL_RANK[state["level"]]
            if rank == 0:
                state["decay_at"] = None
                break

            state["level"] = LEVELS[rank - 1]
            self._stats["decays"] += 1
            if state["level"] == "patrol":
                state["decay_at"] = None
            else:
                state["decay_at"] += self.memory_ms

    def _alarm_ancestors(self, npc: object) -> set[object]:
        ancestors: set[object] = set()
        state = self._states.get(npc)
        if state is None:
            return ancestors

        pending = [
            source[len("alarm:"):]
            for source in state["sources"]
            if isinstance(source, str) and source.startswith("alarm:")
        ]
        while pending:
            source_npc = pending.pop()
            if source_npc in ancestors or source_npc == npc:
                continue
            ancestors.add(source_npc)
            source_state = self._states.get(source_npc)
            if source_state is not None:
                pending.extend(
                    source[len("alarm:"):]
                    for source in source_state["sources"]
                    if isinstance(source, str) and source.startswith("alarm:")
                )
        return ancestors

    def Place(self, npc, position):
        with self._lock:
            coordinates = _position_xy(position)
            state = self._ensure_state(npc)
            state["position"] = coordinates
            return {"npc": npc, "position": copy.deepcopy(position)}

    def SetStealth(self, target, factor):
        with self._lock:
            if not _is_number(factor) or not factor > 0:
                raise PerceptionError("PERCEPTION_STEALTH_INVALID", factor)
            self._stealth[target] = factor
            return {"target": target, "factor": factor}

    def Observe(self, npc, target, position, now):
        with self._lock:
            self._stats["observed"] += 1
            now_ms = self._require_now(now)
            if not isinstance(position, Mapping):
                raise PerceptionError("PERCEPTION_POSITION_INVALID", position)

            try:
                distance = float(position["distance"])
                bearing = float(position["bearing"])
                blocked = bool(position.get("blocked", False))
            except (KeyError, TypeError, ValueError) as exc:
                raise PerceptionError("PERCEPTION_POSITION_INVALID", position) from exc

            state = self._ensure_state(npc)
            self._decay(state, now_ms)

            factor = self._stealth.get(target, 1.0)
            effective_distance = distance * float(factor)
            half_angle = float(self.view_angle_deg) / 2.0

            reason = None
            visible = True
            if abs(bearing) > half_angle:
                reason = "angle"
                visible = False
            elif distance > float(self.view_range):
                reason = "range"
                visible = False
            elif blocked:
                reason = "blocked"
                visible = False
            elif effective_distance > float(self.view_range):
                reason = "stealth"
                visible = False

            if visible:
                state["level"] = "combat"
                state["last_seen"] = now_ms
                state["target"] = target
                self._add_source(state, f"sight:{target}")
                state["decay_at"] = now_ms + self.memory_ms

            return {
                "visible": visible,
                "reason": reason,
                "effective_distance": effective_distance,
                "level": state["level"],
            }

    def Hear(self, npc, event):
        with self._lock:
            if not isinstance(event, Mapping):
                raise PerceptionError("PERCEPTION_EVENT_INVALID", event)

            try:
                now_ms = self._require_now(event["at"])
                origin = event["origin"]
                distance = float(event["distance"])
                loudness = float(event["loudness"])
            except (KeyError, TypeError, ValueError) as exc:
                raise PerceptionError("PERCEPTION_EVENT_INVALID", event) from exc

            state = self._ensure_state(npc)
            self._decay(state, now_ms)

            if distance > loudness:
                return {"heard": False, "reason": "faint", "level": state["level"]}

            self._stats["heard"] += 1
            if _LEVEL_RANK[state["level"]] < _LEVEL_RANK["search"]:
                state["level"] = "search"
            self._add_source(state, f"hearing:{origin}")
            state["decay_at"] = now_ms + self.memory_ms

            return {"heard": True, "reason": None, "level": state["level"]}

    def Alert(self, npc, level, now=None):
        with self._lock:
            if level not in _LEVEL_RANK:
                raise PerceptionError("PERCEPTION_LEVEL_INVALID", level)
            now_ms = self._optional_now(now)
            state = self._ensure_state(npc)
            self._decay(state, now_ms)
            state["level"] = level
            if now_ms is not None:
                state["decay_at"] = now_ms + self.memory_ms
            return {"npc": npc, "level": level}

    def Propagate(self, npc, radius, now=None):
        with self._lock:
            now_ms = self._optional_now(now)
            source_state = self._states.get(npc)
            if source_state is None:
                return {"propagated_to": [], "count": 0}

            self._decay(source_state, now_ms)
            if source_state["level"] not in ("alert", "combat"):
                return {"propagated_to": [], "count": 0}

            source_position = source_state.get("position")
            if source_position is None:
                return {"propagated_to": [], "count": 0}

            ancestors = self._alarm_ancestors(npc)
            alarm_source = f"alarm:{npc}"
            propagated_to: list[object] = []

            for target_npc, target_state in list(self._states.items()):
                if target_npc == npc or target_state.get("position") is None:
                    continue
                if target_npc in ancestors:
                    continue

                source_x, source_y = source_position
                target_x, target_y = target_state["position"]
                distance = math.hypot(source_x - target_x, source_y - target_y)
                if distance > float(radius):
                    continue

                self._decay(target_state, now_ms)
                if alarm_source in target_state["sources"]:
                    continue

                if _LEVEL_RANK[target_state["level"]] < _LEVEL_RANK["alert"]:
                    target_state["level"] = "alert"
                    propagated_to.append(target_npc)
                    if now_ms is not None:
                        target_state["decay_at"] = now_ms + self.memory_ms

                self._add_source(target_state, alarm_source)

            self._stats["propagated"] += len(propagated_to)
            return {"propagated_to": propagated_to, "count": len(propagated_to)}

    def State(self, npc, now):
        with self._lock:
            now_ms = self._require_now(now)
            state = self._ensure_state(npc)
            self._decay(state, now_ms)
            sources: list[str] = []
            for source in state["sources"]:
                if source not in sources:
                    sources.append(source)
            state["sources"] = sources
            return {
                "npc": npc,
                "level": state["level"],
                "last_seen": state["last_seen"],
                "target": state["target"],
                "sources": list(sources),
                "decay_at": state["decay_at"],
            }

    def Forget(self, npc):
        with self._lock:
            state = self._ensure_state(npc)
            state["level"] = "patrol"
            state["last_seen"] = None
            state["target"] = None
            state["sources"] = []
            state["decay_at"] = None
            return {"npc": npc, "level": "patrol"}

    def Snapshot(self):
        with self._lock:
            return copy.deepcopy({
                "version": _SNAPSHOT_VERSION,
                "npcs": self._states,
                "stealth": self._stealth,
                "stats": self._stats,
            })

    def Restore(self, state):
        with self._lock:
            restored = self._parse_snapshot(state)
            self._states = restored["npcs"]
            self._stealth = restored["stealth"]
            self._stats = restored["stats"]

    def _parse_snapshot(self, snapshot: object) -> dict:
        if not isinstance(snapshot, Mapping):
            raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", "snapshot")

        version = snapshot.get("version", _SNAPSHOT_VERSION)
        if not _is_ms(version) or version != _SNAPSHOT_VERSION:
            raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", "version")

        raw_npcs = snapshot.get("npcs")
        raw_stealth = snapshot.get("stealth", {})
        raw_stats = snapshot.get("stats")
        if not isinstance(raw_npcs, Mapping):
            raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", "npcs")
        if not isinstance(raw_stealth, Mapping):
            raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", "stealth")
        if not isinstance(raw_stats, Mapping):
            raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", "stats")

        npcs: dict[object, dict] = {}
        for npc, raw_state in raw_npcs.items():
            if not isinstance(raw_state, Mapping):
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)

            level = raw_state.get("level")
            if level not in _LEVEL_RANK:
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)

            last_seen = raw_state.get("last_seen")
            if last_seen is not None and not _is_ms(last_seen):
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)

            target = raw_state.get("target")
            if target is not None and not isinstance(target, (str, int)):
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)

            decay_at = raw_state.get("decay_at")
            if decay_at is not None and not _is_ms(decay_at):
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)

            raw_sources = raw_state.get("sources", [])
            if not isinstance(raw_sources, list):
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)
            sources: list[str] = []
            for source in raw_sources:
                if not isinstance(source, str):
                    raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", npc)
                if source not in sources:
                    sources.append(source)

            position = None
            raw_position = raw_state.get("position")
            if raw_position is not None:
                try:
                    position = _position_xy(raw_position)
                except PerceptionError as exc:
                    raise PerceptionError(
                        "PERCEPTION_SNAPSHOT_INVALID", npc
                    ) from exc

            npcs[npc] = {
                "level": level,
                "last_seen": last_seen,
                "target": target,
                "sources": sources,
                "decay_at": decay_at,
                "position": position,
            }

        stealth: dict[object, object] = {}
        for target, factor in raw_stealth.items():
            if not _is_number(factor) or not factor > 0:
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", target)
            stealth[target] = factor

        stats = {key: 0 for key in _STATS_KEYS}
        for key in _STATS_KEYS:
            value = raw_stats.get(key)
            if not _is_ms(value) or value < 0:
                raise PerceptionError("PERCEPTION_SNAPSHOT_INVALID", key)
            stats[key] = value

        return {"npcs": npcs, "stealth": stealth, "stats": stats}

    def Stats(self):
        with self._lock:
            return dict(self._stats)
