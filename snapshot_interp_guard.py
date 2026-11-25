"""Client snapshot interpolation buffer with extrapolation guard."""

from __future__ import annotations

import copy
import math
import threading
from bisect import bisect_right

_STAT_KEYS = (
    "ingested",
    "duplicates",
    "rejected",
    "samples",
    "teleports",
    "despawns",
)


def _empty_stats() -> dict:
    return {key: 0 for key in _STAT_KEYS}


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


class InterpError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SnapshotInterpGuard:
    """Buffers server snapshots and samples entity positions on a timeline."""

    def __init__(self, *, buffer_size: int = 32, max_extrapolation: int = 250) -> None:
        self.buffer_size = int(buffer_size)
        self.max_extrapolation = int(max_extrapolation)
        self._lock = threading.RLock()
        self._buffer: list[dict] = []
        self._seen_seqs: set = set()
        self._entities: dict = {}
        self._teleports: list[dict] = []
        self._despawns: list[dict] = []
        self._stats = _empty_stats()

    @staticmethod
    def _bad_snapshot(detail: object = None) -> InterpError:
        return InterpError("INTERP_BAD_SNAPSHOT", detail)

    @staticmethod
    def _invalid_archive(detail: object = None) -> InterpError:
        return InterpError("INTERP_SNAPSHOT_INVALID", detail)

    @classmethod
    def _normalize_snapshot(cls, snapshot: object) -> dict:
        if not isinstance(snapshot, dict):
            raise cls._bad_snapshot("snapshot must be an object")

        for field in ("server_time", "seq", "entities"):
            if field not in snapshot:
                raise cls._bad_snapshot(f"missing {field}")

        server_time = snapshot["server_time"]
        seq = snapshot["seq"]
        raw_entities = snapshot["entities"]

        if not _is_number(server_time):
            raise cls._bad_snapshot("server_time must be numeric")
        try:
            hash(seq)
        except TypeError:
            raise cls._bad_snapshot("seq must be hashable") from None
        if not isinstance(raw_entities, dict):
            raise cls._bad_snapshot("entities must be an object")

        entities: dict = {}
        for entity_id, raw_entity in raw_entities.items():
            if not isinstance(raw_entity, dict):
                raise cls._bad_snapshot("entity must be an object")
            if "x" not in raw_entity or "y" not in raw_entity:
                raise cls._bad_snapshot("entity requires x and y")

            x = raw_entity["x"]
            y = raw_entity["y"]
            vx = raw_entity.get("vx", 0.0)
            vy = raw_entity.get("vy", 0.0)
            if not all(_is_number(value) for value in (x, y, vx, vy)):
                raise cls._bad_snapshot("entity coordinates must be numeric")

            entity = copy.deepcopy(raw_entity)
            entity["x"] = float(x)
            entity["y"] = float(y)
            entity["vx"] = float(vx)
            entity["vy"] = float(vy)
            entities[entity_id] = entity

        return {
            "server_time": server_time,
            "seq": seq,
            "entities": entities,
        }

    def ingest(self, snapshot: dict) -> dict:
        with self._lock:
            try:
                normalized = self._normalize_snapshot(snapshot)
            except InterpError:
                self._stats["rejected"] += 1
                raise

            seq = normalized["seq"]
            if seq in self._seen_seqs:
                self._stats["duplicates"] += 1
                return {"accepted": False, "reason": "duplicate"}

            server_time = normalized["server_time"]
            if self._buffer and server_time < self._buffer[-1]["server_time"]:
                self._stats["rejected"] += 1
                raise InterpError(
                    "INTERP_TIME_REGRESSION",
                    {
                        "server_time": server_time,
                        "latest_time": self._buffer[-1]["server_time"],
                    },
                )

            self._buffer.append(normalized)
            self._seen_seqs.add(seq)
            if len(self._buffer) > self.buffer_size:
                self._buffer.pop(0)

            self._entities = copy.deepcopy(normalized["entities"])
            for teleport in self._teleports:
                if teleport.get("active", True) and server_time > teleport["at"]:
                    teleport["active"] = False

            self._stats["ingested"] += 1
            return {"accepted": True, "buffered": len(self._buffer)}

    def sample(self, render_time: int) -> dict:
        with self._lock:
            if not self._buffer:
                raise InterpError("INTERP_EMPTY", {"render_time": render_time})

            times = [item["server_time"] for item in self._buffer]
            if render_time < times[0]:
                mode = "clamp"
                sampled = self._sample_clamped(self._buffer[0]["entities"])
            elif render_time > times[-1]:
                mode, sampled = self._sample_extrapolated(render_time, times[-1])
            else:
                mode = "interp"
                sampled = self._sample_interpolated(render_time, times)

            self._apply_teleports(render_time, sampled)
            self._apply_despawns(render_time, sampled)

            self._stats["samples"] += 1
            return {"mode": mode, "entities": sampled}

    @staticmethod
    def _point(entity_id: object, x: float, y: float, snapped: bool) -> tuple:
        return entity_id, {
            "x": float(x),
            "y": float(y),
            "snapped": bool(snapped),
        }

    def _sample_clamped(self, entities: dict) -> dict:
        return dict(
            self._point(entity_id, entity["x"], entity["y"], False)
            for entity_id, entity in entities.items()
        )

    def _sample_extrapolated(self, render_time: float, latest_time: float) -> tuple:
        delta = render_time - latest_time
        limit = float(self.max_extrapolation)
        mode = "extrap" if delta <= limit else "frozen"
        step = min(delta, limit)

        sampled = {}
        for entity_id, entity in self._buffer[-1]["entities"].items():
            sampled[entity_id] = {
                "x": entity["x"] + entity["vx"] * step,
                "y": entity["y"] + entity["vy"] * step,
                "snapped": False,
            }
        return mode, sampled

    def _sample_interpolated(self, render_time: float, times: list) -> dict:
        right_index = bisect_right(times, render_time)
        left_index = right_index - 1

        if left_index < 0:
            return self._sample_clamped(self._buffer[0]["entities"])

        left = self._buffer[left_index]
        if right_index >= len(self._buffer):
            return self._sample_clamped(left["entities"])

        right = self._buffer[right_index]
        if left["server_time"] == right["server_time"]:
            return self._sample_clamped(right["entities"])

        span = right["server_time"] - left["server_time"]
        alpha = (render_time - left["server_time"]) / span
        sampled = {}
        entity_ids = set(left["entities"]).union(right["entities"])
        for entity_id in entity_ids:
            left_entity = left["entities"].get(entity_id)
            right_entity = right["entities"].get(entity_id)

            if left_entity is not None and right_entity is not None:
                x = left_entity["x"] + (right_entity["x"] - left_entity["x"]) * alpha
                y = left_entity["y"] + (right_entity["y"] - left_entity["y"]) * alpha
            elif right_entity is not None:
                x = right_entity["x"]
                y = right_entity["y"]
            else:
                x = left_entity["x"]
                y = left_entity["y"]

            sampled[entity_id] = {
                "x": float(x),
                "y": float(y),
                "snapped": False,
            }
        return sampled

    def _apply_teleports(self, render_time: float, sampled: dict) -> None:
        chosen: dict = {}
        for order, teleport in enumerate(self._teleports):
            if not teleport.get("active", True):
                continue
            at_time = teleport["at"]
            if render_time < at_time:
                continue

            entity_id = teleport["entity"]
            previous = chosen.get(entity_id)
            candidate = (at_time, order, teleport)
            if previous is None or (at_time, order) >= (previous[0], previous[1]):
                chosen[entity_id] = candidate

        for entity_id, (_, _, teleport) in chosen.items():
            sampled[entity_id] = {
                "x": float(teleport["x"]),
                "y": float(teleport["y"]),
                "snapped": True,
            }

    def _apply_despawns(self, render_time: float, sampled: dict) -> None:
        removed = {
            record["entity"]
            for record in self._despawns
            if render_time >= record["at"]
        }
        for entity_id in removed:
            sampled.pop(entity_id, None)

    def teleport(self, entity_id: str, x: float, y: float, at_time: int) -> dict:
        with self._lock:
            record = {
                "entity": entity_id,
                "at": at_time,
                "x": x,
                "y": y,
                "active": True,
            }
            self._teleports.append(record)
            self._stats["teleports"] += 1
            return {"entity": entity_id, "at": at_time, "x": x, "y": y}

    def despawn(self, entity_id: str, at_time: int) -> dict:
        with self._lock:
            record = {"entity": entity_id, "at": at_time}
            self._despawns.append(record)
            self._stats["despawns"] += 1
            return copy.deepcopy(record)

    def entity_state(self, entity_id: str) -> dict | None:
        with self._lock:
            entity = self._entities.get(entity_id)
            return copy.deepcopy(entity) if entity is not None else None

    def bounds(self) -> tuple:
        with self._lock:
            if not self._buffer:
                return (None, None, 0)
            return (
                self._buffer[0]["server_time"],
                self._buffer[-1]["server_time"],
                len(self._buffer),
            )

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in _STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = _empty_stats()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "config": {
                    "buffer_size": self.buffer_size,
                    "max_extrapolation": self.max_extrapolation,
                },
                "buffer": copy.deepcopy(self._buffer),
                "seen_seqs": list(self._seen_seqs),
                "entities": copy.deepcopy(self._entities),
                "teleports": copy.deepcopy(self._teleports),
                "despawns": copy.deepcopy(self._despawns),
                "stats": self.stats(),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = self._parse_archive(state)
            except InterpError:
                raise
            except Exception as exc:
                raise InterpError(
                    "INTERP_SNAPSHOT_INVALID", {"reason": str(exc)}
                ) from exc
            if parsed["config"] is not None:
                self.buffer_size = parsed["config"]["buffer_size"]
                self.max_extrapolation = parsed["config"]["max_extrapolation"]
            self._buffer = parsed["buffer"]
            self._seen_seqs = parsed["seen_seqs"]
            self._entities = parsed["entities"]
            self._teleports = parsed["teleports"]
            self._despawns = parsed["despawns"]
            self._stats = parsed["stats"]

    @classmethod
    def _parse_archive(cls, state: object) -> dict:
        if not isinstance(state, dict):
            raise cls._invalid_archive("archive must be an object")

        version = state.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise cls._invalid_archive("unsupported version")

        for field in ("buffer", "entities", "teleports", "despawns", "stats"):
            if field not in state:
                raise cls._invalid_archive(f"missing {field}")

        raw_buffer = state["buffer"]
        if not isinstance(raw_buffer, list):
            raise cls._invalid_archive("buffer must be a list")

        buffer: list[dict] = []
        seen_seqs: set = set()
        previous_time = None
        for raw_snapshot in raw_buffer:
            try:
                snapshot = cls._normalize_snapshot(raw_snapshot)
            except InterpError as exc:
                raise cls._invalid_archive(exc.detail) from None

            if snapshot["seq"] in seen_seqs:
                raise cls._invalid_archive("duplicate buffered seq")
            if previous_time is not None and snapshot["server_time"] < previous_time:
                raise cls._invalid_archive("buffer is not time ordered")

            buffer.append(snapshot)
            seen_seqs.add(snapshot["seq"])
            previous_time = snapshot["server_time"]

        raw_seen = state.get("seen_seqs")
        if raw_seen is not None:
            if not isinstance(raw_seen, (list, tuple, set, frozenset)):
                raise cls._invalid_archive("seen_seqs must be a collection")
            for seq in raw_seen:
                try:
                    hash(seq)
                except TypeError:
                    raise cls._invalid_archive("seen seq must be hashable") from None
                seen_seqs.add(seq)

        raw_entities = state["entities"]
        if not isinstance(raw_entities, dict):
            raise cls._invalid_archive("entities must be an object")
        entities = copy.deepcopy(buffer[-1]["entities"]) if buffer else {}

        raw_teleports = state["teleports"]
        teleports = cls._parse_teleports(raw_teleports)
        raw_despawns = state["despawns"]
        despawns = cls._parse_despawns(raw_despawns)

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(_STAT_KEYS):
            raise cls._invalid_archive("stats shape is invalid")
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise cls._invalid_archive(f"stat {key} must be a non-negative integer")
            stats[key] = value

        config = None
        raw_config = state.get("config")
        if raw_config is not None:
            if not isinstance(raw_config, dict):
                raise cls._invalid_archive("config must be an object")
            config = {
                "buffer_size": 32,
                "max_extrapolation": 250,
            }
            buffer_size = raw_config.get("buffer_size", config["buffer_size"])
            max_extrapolation = raw_config.get(
                "max_extrapolation", config["max_extrapolation"]
            )
            if (
                not isinstance(buffer_size, int)
                or isinstance(buffer_size, bool)
                or buffer_size <= 0
            ):
                raise cls._invalid_archive("buffer_size must be a positive integer")
            if not _is_number(max_extrapolation) or max_extrapolation < 0:
                raise cls._invalid_archive("max_extrapolation must be non-negative")
            config["buffer_size"] = buffer_size
            config["max_extrapolation"] = max_extrapolation

        return {
            "config": config,
            "buffer": buffer,
            "seen_seqs": seen_seqs,
            "entities": entities,
            "teleports": teleports,
            "despawns": despawns,
            "stats": stats,
        }

    @classmethod
    def _parse_teleports(cls, raw: object) -> list[dict]:
        if isinstance(raw, dict):
            records = list(raw.values())
        elif isinstance(raw, list):
            records = raw
        else:
            raise cls._invalid_archive("teleports must be a list or object")

        teleports = []
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise cls._invalid_archive("teleport must be an object")
            for field in ("entity", "at", "x", "y"):
                if field not in raw_record:
                    raise cls._invalid_archive(f"teleport missing {field}")
            if not _is_number(raw_record["at"]):
                raise cls._invalid_archive("teleport at must be numeric")
            if not all(_is_number(raw_record[field]) for field in ("x", "y")):
                raise cls._invalid_archive("teleport coordinates must be numeric")

            active = raw_record.get("active", True)
            if not isinstance(active, bool):
                raise cls._invalid_archive("teleport active must be boolean")

            record = copy.deepcopy(raw_record)
            record["x"] = float(record["x"])
            record["y"] = float(record["y"])
            record["active"] = active
            teleports.append(record)
        return teleports

    @classmethod
    def _parse_despawns(cls, raw: object) -> list[dict]:
        if isinstance(raw, dict):
            records = list(raw.values())
        elif isinstance(raw, list):
            records = raw
        else:
            raise cls._invalid_archive("despawns must be a list or object")

        despawns = []
        for raw_record in records:
            if not isinstance(raw_record, dict):
                raise cls._invalid_archive("despawn must be an object")
            if "entity" not in raw_record or "at" not in raw_record:
                raise cls._invalid_archive("despawn requires entity and at")
            if not _is_number(raw_record["at"]):
                raise cls._invalid_archive("despawn at must be numeric")
            despawns.append(copy.deepcopy(raw_record))
        return despawns
