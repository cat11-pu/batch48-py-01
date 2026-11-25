"""Skin influence pruning, duplicate-free merging and renormalization."""

from __future__ import annotations

import copy
import math
import numbers
import threading
from typing import Iterable, Mapping, Sequence


_SNAPSHOT_VERSION = 1
_STAT_KEYS = {
    "vertices",
    "pruned_influences",
    "affected_vertices",
    "rejected_vertices",
    "prunes",
}


class SkinInfluenceError(ValueError):
    """Raised when a vertex influence set cannot be accepted or pruned."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SkinInfluencePruner:
    """Trim per-vertex bone influences and renormalize the surviving weights."""

    def __init__(self, *, max_bones: int = 65535,
                 max_influences: int = 32,
                 max_vertices: int = 1000000) -> None:
        self.max_bones = max_bones
        self.max_influences = max_influences
        self.max_vertices = max_vertices
        self._lock = threading.RLock()
        self._vertices: dict[int, dict] = {}
        self._kept: dict[int, tuple[tuple[int, float], ...]] = {}
        self._errors: dict[int, dict] = {}
        self._counters = {
            "pruned_influences": 0,
            "affected_vertices": 0,
            "rejected_vertices": 0,
            "prunes": 0,
        }

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _runtime_vertex_id(cls, vertex_id: object) -> int | None:
        if not cls._is_int(vertex_id) or vertex_id < 0:
            return None
        return int(vertex_id)

    def _validate_add_vertex_id(self, vertex_id: object) -> int:
        parsed = self._runtime_vertex_id(vertex_id)
        if parsed is None:
            raise SkinInfluenceError("SKIN_VERTEX_INVALID", vertex_id)
        return parsed

    def _require_vertex(self, vertex_id: object) -> dict:
        parsed = self._runtime_vertex_id(vertex_id)
        if parsed is None or parsed not in self._vertices:
            raise SkinInfluenceError("SKIN_UNKNOWN_VERTEX", vertex_id)
        return self._vertices[parsed]

    def _validate_bone(self, bone: object) -> int:
        if not self._is_int(bone) or bone < 0 or bone > self.max_bones:
            raise SkinInfluenceError("SKIN_BONE_RANGE", bone)
        return int(bone)

    @staticmethod
    def _validate_weight(weight: object) -> float:
        if isinstance(weight, bool) or not isinstance(weight, numbers.Real):
            raise SkinInfluenceError("SKIN_WEIGHT_INVALID", weight)
        value = float(weight)
        if not math.isfinite(value) or value < 0.0:
            raise SkinInfluenceError("SKIN_WEIGHT_INVALID", weight)
        return value

    def _parse_influences(self, influences: object) -> tuple[tuple[int, float], ...]:
        if isinstance(influences, (str, bytes, bytearray, Mapping)):
            raise SkinInfluenceError("SKIN_INFLUENCE_INVALID", influences)
        try:
            items = list(influences)  # type: ignore[arg-type]
        except TypeError as exc:
            raise SkinInfluenceError(
                "SKIN_INFLUENCE_INVALID", influences
            ) from exc

        if len(items) > self.max_influences:
            raise SkinInfluenceError("SKIN_INFLUENCE_LIMIT", len(items))

        parsed: list[tuple[int, float]] = []
        for item in items:
            if (
                isinstance(item, (str, bytes, bytearray, Mapping))
                or not isinstance(item, Sequence)
                or len(item) != 2
            ):
                raise SkinInfluenceError("SKIN_INFLUENCE_INVALID", item)
            bone = self._validate_bone(item[0])
            weight = self._validate_weight(item[1])
            parsed.append((bone, weight))
        return tuple(parsed)

    @staticmethod
    def _plain_weight_list(weights: object) -> list[float]:
        if isinstance(weights, (str, bytes, bytearray, Mapping)):
            raise SkinInfluenceError("SKIN_WEIGHT_INVALID", weights)
        try:
            items = list(weights)  # type: ignore[arg-type]
        except TypeError as exc:
            raise SkinInfluenceError("SKIN_WEIGHT_INVALID", weights) from exc
        return [SkinInfluencePruner._validate_weight(value) for value in items]

    @staticmethod
    def _normalize_nonempty(weights: list[float]) -> list[float]:
        maximum = max(weights)
        if maximum == 0.0:
            raise SkinInfluenceError("SKIN_ZERO_WEIGHT", weights)

        scaled = [weight / maximum for weight in weights]
        total = math.fsum(scaled)
        return [weight / total for weight in scaled]

    def _validate_prune_limit(self, max_bones: object) -> int:
        if (
            not self._is_int(max_bones)
            or max_bones < 1
            or max_bones > self.max_influences
        ):
            raise SkinInfluenceError("SKIN_MAX_BONES_INVALID", max_bones)
        return int(max_bones)

    @staticmethod
    def _copy_influences(
        influences: Iterable[tuple[int, float]],
    ) -> list[tuple[int, float]]:
        return [(bone, weight) for bone, weight in influences]

    def add_vertex(self, vertex_id: int, influences) -> dict:
        with self._lock:
            parsed_id = self._validate_add_vertex_id(vertex_id)
            if parsed_id in self._vertices:
                raise SkinInfluenceError("SKIN_DUPLICATE_VERTEX", vertex_id)
            if len(self._vertices) >= self.max_vertices:
                raise SkinInfluenceError("SKIN_VERTEX_LIMIT", vertex_id)

            parsed = self._parse_influences(influences)
            self._vertices[parsed_id] = {
                "vertex_id": parsed_id,
                "influences": parsed,
            }
            return {
                "vertex_id": parsed_id,
                "influences": self._copy_influences(parsed),
            }

    def prune(self, vertex_id: int, max_bones: int = 4) -> list:
        with self._lock:
            parsed_limit = self._validate_prune_limit(max_bones)
            record = self._require_vertex(vertex_id)
            parsed_id = record["vertex_id"]
            raw = record["influences"]

            maximum = max((weight for _, weight in raw), default=0.0)
            if maximum == 0.0:
                detail = {"vertex_id": parsed_id}
                self._errors[parsed_id] = {
                    "code": "SKIN_ZERO_WEIGHT",
                    "detail": detail,
                }
                self._kept.pop(parsed_id, None)
                self._counters["rejected_vertices"] += 1
                raise SkinInfluenceError("SKIN_ZERO_WEIGHT", detail)

            merged: dict[int, float] = {}
            for bone, weight in raw:
                relative = weight / maximum
                merged[bone] = merged.get(bone, 0.0) + relative

            ordered = sorted(merged.items(), key=lambda item: (-item[1], item[0]))
            selected = ordered[:parsed_limit]
            normalized = self._normalize_nonempty(
                [weight for _, weight in selected]
            )
            result = tuple(
                (bone, weight)
                for (bone, _), weight in zip(selected, normalized)
            )

            self._kept[parsed_id] = result
            self._counters["pruned_influences"] += len(ordered) - len(selected)
            self._counters["affected_vertices"] += 1
            self._counters["prunes"] += 1
            self._errors.pop(parsed_id, None)
            return self._copy_influences(result)

    def normalize(self, weights) -> list:
        parsed = self._plain_weight_list(weights)
        if not parsed:
            raise SkinInfluenceError("SKIN_WEIGHT_INVALID", weights)
        return self._normalize_nonempty(parsed)

    def remap_bones(self, mapping: dict) -> int:
        with self._lock:
            if not isinstance(mapping, Mapping):
                raise SkinInfluenceError("SKIN_MAPPING_INVALID", mapping)

            parsed_mapping: dict[int, int] = {}
            for old_bone, new_bone in mapping.items():
                old = self._validate_bone(old_bone)
                new = self._validate_bone(new_bone)
                parsed_mapping[old] = new

            changed_vertices = 0
            updates: dict[int, tuple[tuple[int, float], ...]] = {}
            for vertex_id, record in self._vertices.items():
                influences = record["influences"]
                if not any(
                    bone in parsed_mapping and parsed_mapping[bone] != bone
                    for bone, _ in influences
                ):
                    continue
                updates[vertex_id] = tuple(
                    (parsed_mapping.get(bone, bone), weight)
                    for bone, weight in influences
                )
                changed_vertices += 1

            for vertex_id, influences in updates.items():
                self._vertices[vertex_id] = {
                    "vertex_id": vertex_id,
                    "influences": influences,
                }
            self._kept.clear()
            return changed_vertices

    def vertex(self, vertex_id: int) -> dict:
        with self._lock:
            record = self._require_vertex(vertex_id)
            return {
                "vertex_id": record["vertex_id"],
                "influences": self._copy_influences(record["influences"]),
            }

    def kept(self, vertex_id: int) -> list | None:
        with self._lock:
            record = self._require_vertex(vertex_id)
            parsed_id = record["vertex_id"]
            result = self._kept.get(parsed_id)
            if result is None:
                return None
            return self._copy_influences(result)

    def errors(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._errors)

    def remove_vertex(self, vertex_id: int) -> bool:
        with self._lock:
            parsed_id = self._runtime_vertex_id(vertex_id)
            if parsed_id is None or parsed_id not in self._vertices:
                return False
            del self._vertices[parsed_id]
            self._kept.pop(parsed_id, None)
            self._errors.pop(parsed_id, None)
            return True

    def stats(self) -> dict:
        with self._lock:
            return {
                "vertices": len(self._vertices),
                "pruned_influences": self._counters["pruned_influences"],
                "affected_vertices": self._counters["affected_vertices"],
                "rejected_vertices": self._counters["rejected_vertices"],
                "prunes": self._counters["prunes"],
            }

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._counters:
                self._counters[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "vertices": {
                    vertex_id: {
                        "vertex_id": record["vertex_id"],
                        "influences": [
                            [bone, weight]
                            for bone, weight in record["influences"]
                        ],
                    }
                    for vertex_id, record in self._vertices.items()
                },
                "kept": {
                    vertex_id: [
                        [bone, weight] for bone, weight in influences
                    ]
                    for vertex_id, influences in self._kept.items()
                },
                "errors": copy.deepcopy(self._errors),
                "stats": self.stats(),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                decoded = self._decode_snapshot(state)
            except SkinInfluenceError as exc:
                if exc.code == "SKIN_SNAPSHOT_INVALID":
                    raise
                raise SkinInfluenceError(
                    "SKIN_SNAPSHOT_INVALID", exc.detail
                ) from exc
            except Exception as exc:
                raise SkinInfluenceError(
                    "SKIN_SNAPSHOT_INVALID", str(exc)
                ) from exc

            vertices, kept, errors, counters = decoded
            self._vertices = vertices
            self._kept = kept
            self._errors = errors
            self._counters = counters

    @staticmethod
    def _snapshot_invalid(detail: object = None):
        raise SkinInfluenceError("SKIN_SNAPSHOT_INVALID", detail)

    def _decode_snapshot_id(self, value: object) -> int:
        if self._is_int(value):
            vertex_id = int(value)
        elif isinstance(value, str) and value.isdigit():
            vertex_id = int(value)
        else:
            self._snapshot_invalid(value)
        if vertex_id < 0:
            self._snapshot_invalid(value)
        return vertex_id

    def _decode_snapshot_influences(
        self, influences: object
    ) -> tuple[tuple[int, float], ...]:
        if isinstance(influences, (str, bytes, bytearray, Mapping)):
            self._snapshot_invalid(influences)
        if not isinstance(influences, (list, tuple)):
            self._snapshot_invalid(influences)
        if len(influences) > self.max_influences:
            self._snapshot_invalid(influences)

        parsed: list[tuple[int, float]] = []
        for item in influences:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                self._snapshot_invalid(item)
            parsed.append(
                (self._validate_bone(item[0]), self._validate_weight(item[1]))
            )
        return tuple(parsed)

    def _decode_snapshot(self, state: object):
        if not isinstance(state, dict) or set(state) != {
            "version", "vertices", "kept", "errors", "stats",
        }:
            self._snapshot_invalid(state)

        version = state["version"]
        if not self._is_int(version) or version != _SNAPSHOT_VERSION:
            self._snapshot_invalid(version)

        raw_vertices = state["vertices"]
        if not isinstance(raw_vertices, dict):
            self._snapshot_invalid(raw_vertices)
        if len(raw_vertices) > self.max_vertices:
            self._snapshot_invalid(raw_vertices)

        vertices: dict[int, dict] = {}
        for raw_id, raw_record in raw_vertices.items():
            vertex_id = self._decode_snapshot_id(raw_id)
            if vertex_id in vertices:
                self._snapshot_invalid(raw_id)
            if not isinstance(raw_record, dict):
                self._snapshot_invalid(raw_record)
            if "influences" not in raw_record:
                self._snapshot_invalid(raw_record)
            if "vertex_id" in raw_record:
                recorded_id = self._decode_snapshot_id(raw_record["vertex_id"])
                if recorded_id != vertex_id:
                    self._snapshot_invalid(raw_record)
            influences = self._decode_snapshot_influences(
                raw_record["influences"]
            )
            vertices[vertex_id] = {
                "vertex_id": vertex_id,
                "influences": influences,
            }

        raw_kept = state["kept"]
        if not isinstance(raw_kept, dict):
            self._snapshot_invalid(raw_kept)
        kept: dict[int, tuple[tuple[int, float], ...]] = {}
        for raw_id, raw_influences in raw_kept.items():
            vertex_id = self._decode_snapshot_id(raw_id)
            if vertex_id in kept or vertex_id not in vertices:
                self._snapshot_invalid(raw_id)
            kept[vertex_id] = self._decode_snapshot_influences(raw_influences)

        raw_errors = state["errors"]
        if not isinstance(raw_errors, dict):
            self._snapshot_invalid(raw_errors)
        errors: dict[int, dict] = {}
        for raw_id, raw_error in raw_errors.items():
            vertex_id = self._decode_snapshot_id(raw_id)
            if vertex_id in errors or vertex_id not in vertices:
                self._snapshot_invalid(raw_id)
            if vertex_id in kept:
                self._snapshot_invalid(raw_id)
            if not isinstance(raw_error, dict):
                self._snapshot_invalid(raw_error)
            code = raw_error.get("code")
            if not isinstance(code, str) or not code:
                self._snapshot_invalid(raw_error)
            try:
                errors[vertex_id] = copy.deepcopy(raw_error)
            except Exception as exc:
                raise SkinInfluenceError(
                    "SKIN_SNAPSHOT_INVALID", raw_error
                ) from exc

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != _STAT_KEYS:
            self._snapshot_invalid(raw_stats)
        for value in raw_stats.values():
            if not self._is_int(value) or value < 0:
                self._snapshot_invalid(raw_stats)
        if raw_stats["vertices"] != len(vertices):
            self._snapshot_invalid(raw_stats)

        counters = {
            "pruned_influences": int(raw_stats["pruned_influences"]),
            "affected_vertices": int(raw_stats["affected_vertices"]),
            "rejected_vertices": int(raw_stats["rejected_vertices"]),
            "prunes": int(raw_stats["prunes"]),
        }
        return vertices, kept, errors, counters
