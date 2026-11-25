"""Reorder triangle indices to raise post transform vertex cache hits."""

from __future__ import annotations

import copy
import threading
from collections import deque
from collections.abc import Sequence


WINDINGS = ("ccw", "cw")

STATS_KEYS = (
    "meshes_loaded",
    "optimizations",
    "restores",
    "cache_simulations",
    "rejected",
)

SNAPSHOT_VERSION = 1


class VertexCacheError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = {} if detail is None else detail


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_permutation(order: object, ids: set[int], count: int) -> bool:
    if isinstance(order, (str, bytes)) or not isinstance(order, Sequence):
        return False
    if len(order) != count:
        return False

    seen: set[int] = set()
    for triangle_id in order:
        if not _is_integer(triangle_id):
            return False
        if triangle_id not in ids or triangle_id in seen:
            return False
        seen.add(triangle_id)
    return True


def _simulate_order_data(by_id: dict, triangle_count: int, order: Sequence,
                         cache_size: int) -> dict:
    cache: deque[int] = deque()
    cached: set[int] = set()
    cache_hits = 0
    cache_misses = 0

    for triangle_id in order:
        indices = by_id[triangle_id]["indices"]
        for vertex_index in indices:
            if vertex_index in cached:
                cache_hits += 1
                continue

            cache_misses += 1
            if cache_size <= 0:
                continue

            if len(cache) == cache_size:
                oldest = cache.popleft()
                cached.discard(oldest)
            cache.append(vertex_index)
            cached.add(vertex_index)

    total = triangle_count * 3
    return {
        "cache_hits": cache_hits,
        "cache_misses": cache_misses,
        "hit_rate": cache_hits / total if total else 0.0,
        "total": total,
        "cache_size": cache_size,
    }


class VertexCacheOptimizer:
    """Plan a deterministic triangle order for a fixed size post transform cache."""

    def __init__(self, *, max_cache_size: int = 32, max_vertices: int = 65536,
                 max_triangles: int = 4096, clock=None) -> None:
        self.max_cache_size = max_cache_size
        self.max_vertices = max_vertices
        self.max_triangles = max_triangles
        self._clock = clock
        self._lock = threading.RLock()
        self._cache_size = max_cache_size
        self._reset_mesh_state_locked()
        self._stats = {key: 0 for key in STATS_KEYS}

    def _reset_mesh_state_locked(self) -> None:
        self._loaded = False
        self._vertices: list[int] = []
        self._vertex_set: set[int] = set()
        self._raw_triangles: list[dict] = []
        self._triangles: list[dict] = []
        self._by_id: dict[int, dict] = {}
        self._original_order: list[int] = []
        self._submeshes: list[dict] = []
        self._runs: list[tuple[int, int]] = []
        self._adjacency: dict[int, list[int]] = {}
        self._plan: dict | None = None

    def _reject_locked(self, code: str, detail: object = None, *,
                       count_rejection: bool = True) -> VertexCacheError:
        if count_rejection:
            self._stats["rejected"] += 1
        return VertexCacheError(code, detail)

    def _require_loaded_locked(self) -> None:
        if not self._loaded:
            raise self._reject_locked("VCO_NO_MESH", {"loaded": False})

    def _build_mesh_state_locked(self, vertices: object, triangles: object,
                                 original_order: object = None) -> dict:
        if not isinstance(vertices, list):
            raise self._reject_locked(
                "VCO_INVALID_MESH",
                {"reason": "VERTICES_NOT_LIST"},
            )
        if not vertices:
            raise self._reject_locked(
                "VCO_INVALID_MESH",
                {"reason": "VERTICES_EMPTY"},
            )
        if len(vertices) > self.max_vertices:
            raise self._reject_locked(
                "VCO_TOO_MANY_VERTICES",
                {
                    "vertices": len(vertices),
                    "max_vertices": self.max_vertices,
                },
            )

        seen_vertices: set[int] = set()
        for position, vertex in enumerate(vertices):
            if not _is_integer(vertex) or vertex < 0:
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {
                        "reason": "INVALID_VERTEX",
                        "position": position,
                        "value": vertex,
                    },
                )
            if vertex in seen_vertices:
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {
                        "reason": "DUPLICATE_VERTEX",
                        "position": position,
                        "vertex": vertex,
                    },
                )
            seen_vertices.add(vertex)

        if not isinstance(triangles, list):
            raise self._reject_locked(
                "VCO_INVALID_MESH",
                {"reason": "TRIANGLES_NOT_LIST"},
            )
        if not triangles:
            raise self._reject_locked(
                "VCO_INVALID_MESH",
                {"reason": "TRIANGLES_EMPTY"},
            )
        if len(triangles) > self.max_triangles:
            raise self._reject_locked(
                "VCO_TOO_MANY_TRIANGLES",
                {
                    "triangles": len(triangles),
                    "max_triangles": self.max_triangles,
                },
            )

        parsed: list[dict] = []
        for position, raw_triangle in enumerate(triangles):
            if not isinstance(raw_triangle, dict):
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {"reason": "TRIANGLE_NOT_OBJECT", "position": position},
                )

            triangle_id = raw_triangle.get("id")
            if not _is_integer(triangle_id) or triangle_id <= 0:
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {
                        "reason": "INVALID_TRIANGLE_ID",
                        "position": position,
                        "id": triangle_id,
                    },
                )

            submesh = raw_triangle.get("submesh")
            if not isinstance(submesh, str) or not submesh:
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {
                        "reason": "INVALID_SUBMESH",
                        "position": position,
                        "id": triangle_id,
                        "submesh": submesh,
                    },
                )

            indices = raw_triangle.get("indices")
            if (isinstance(indices, (str, bytes))
                    or not isinstance(indices, Sequence)
                    or len(indices) != 3):
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {
                        "reason": "INVALID_INDICES",
                        "position": position,
                        "id": triangle_id,
                    },
                )

            materialized_indices: list[int] = []
            for index_position, index in enumerate(indices):
                if not _is_integer(index):
                    raise self._reject_locked(
                        "VCO_INVALID_MESH",
                        {
                            "reason": "INVALID_INDEX_TYPE",
                            "position": position,
                            "index_position": index_position,
                            "id": triangle_id,
                            "index": index,
                        },
                    )
                materialized_indices.append(index)

            winding = raw_triangle.get("winding", "ccw")
            if winding not in WINDINGS:
                raise self._reject_locked(
                    "VCO_INVALID_MESH",
                    {
                        "reason": "INVALID_WINDING",
                        "position": position,
                        "id": triangle_id,
                        "winding": winding,
                    },
                )

            parsed.append({
                "id": triangle_id,
                "submesh": submesh,
                "indices": materialized_indices,
                "winding": winding,
                "table_position": position,
            })

        by_id: dict[int, dict] = {}
        for triangle in parsed:
            triangle_id = triangle["id"]
            if triangle_id in by_id:
                raise self._reject_locked(
                    "VCO_DUPLICATE_TRIANGLE",
                    {"id": triangle_id},
                )
            by_id[triangle_id] = triangle

        vertex_set = set(vertices)
        for triangle in parsed:
            for index in triangle["indices"]:
                if index not in vertex_set:
                    raise self._reject_locked(
                        "VCO_INDEX_OUT_OF_RANGE",
                        {
                            "index": index,
                            "id": triangle["id"],
                            "triangle_id": triangle["id"],
                        },
                    )

        if original_order is None:
            order = [triangle["id"] for triangle in parsed]
        else:
            if not _is_permutation(original_order, set(by_id), len(parsed)):
                raise self._reject_locked(
                    "VCO_INVALID_ORDER",
                    {"expected_triangles": len(parsed)},
                )
            order = list(original_order)

        ordered_triangles: list[dict] = []
        ordered_raw_triangles: list[dict] = []
        for position, triangle_id in enumerate(order):
            triangle = copy.deepcopy(by_id[triangle_id])
            triangle["position"] = position
            ordered_triangles.append(triangle)
            ordered_raw_triangles.append(
                copy.deepcopy(triangles[triangle["table_position"]])
            )

        ordered_by_id = {
            triangle["id"]: triangle for triangle in ordered_triangles
        }

        submesh_counts: dict[str, int] = {}
        submesh_order: list[str] = []
        for triangle in ordered_triangles:
            name = triangle["submesh"]
            if name not in submesh_counts:
                submesh_counts[name] = 0
                submesh_order.append(name)
            submesh_counts[name] += 1

        submeshes = [
            {"name": name, "triangles": submesh_counts[name]}
            for name in submesh_order
        ]

        runs: list[tuple[int, int]] = []
        if ordered_triangles:
            run_start = 0
            run_submesh = ordered_triangles[0]["submesh"]
            for position in range(1, len(ordered_triangles) + 1):
                if (position == len(ordered_triangles)
                        or ordered_triangles[position]["submesh"] != run_submesh):
                    runs.append((run_start, position))
                    if position < len(ordered_triangles):
                        run_start = position
                        run_submesh = ordered_triangles[position]["submesh"]

        adjacency: dict[int, list[int]] = {vertex: [] for vertex in vertices}
        for position, triangle in enumerate(ordered_triangles):
            for vertex in dict.fromkeys(triangle["indices"]):
                adjacency.setdefault(vertex, []).append(position)

        return {
            "vertices": copy.deepcopy(vertices),
            "vertex_set": vertex_set,
            "raw_triangles": ordered_raw_triangles,
            "triangles": ordered_triangles,
            "by_id": ordered_by_id,
            "original_order": list(order),
            "submeshes": submeshes,
            "runs": runs,
            "adjacency": adjacency,
        }

    def load_mesh(self, vertices, triangles) -> dict:
        with self._lock:
            mesh_state = self._build_mesh_state_locked(vertices, triangles)
            self._loaded = True
            self._vertices = mesh_state["vertices"]
            self._vertex_set = mesh_state["vertex_set"]
            self._raw_triangles = mesh_state["raw_triangles"]
            self._triangles = mesh_state["triangles"]
            self._by_id = mesh_state["by_id"]
            self._original_order = mesh_state["original_order"]
            self._submeshes = mesh_state["submeshes"]
            self._runs = mesh_state["runs"]
            self._adjacency = mesh_state["adjacency"]
            self._plan = None
            self._stats["meshes_loaded"] += 1
            return self.mesh_info()

    def set_cache_size(self, size: int) -> dict:
        with self._lock:
            if (not _is_integer(size) or size < 0
                    or size > self.max_cache_size):
                raise self._reject_locked(
                    "VCO_INVALID_CACHE_SIZE",
                    {
                        "cache_size": size,
                        "min_cache_size": 0,
                        "max_cache_size": self.max_cache_size,
                    },
                )

            self._cache_size = size
            self._plan = None
            return {"cache_size": size, "optimized": False}

    def cache_size(self) -> int:
        with self._lock:
            return self._cache_size

    def mesh_info(self) -> dict:
        with self._lock:
            return {
                "vertices": len(self._vertices),
                "triangles": len(self._triangles),
                "optimized": self._plan is not None,
                "submeshes": copy.deepcopy(self._submeshes),
            }

    def submeshes(self) -> list:
        with self._lock:
            return copy.deepcopy(self._submeshes)

    def original_order(self) -> list:
        with self._lock:
            return list(self._original_order)

    def optimized_order(self) -> list:
        with self._lock:
            if self._plan is None:
                return []
            return list(self._plan["order"])

    def _feed_indices_locked(
        self,
        cache: deque[int],
        cached: set[int],
        indices: Sequence[int],
        cache_size: int,
    ) -> tuple[int, deque[int], set[int]]:
        next_cache = deque(cache)
        next_cached = set(cached)
        hits = 0

        for vertex in indices:
            if vertex in next_cached:
                hits += 1
                continue
            if cache_size <= 0:
                continue

            if len(next_cache) == cache_size:
                oldest = next_cache.popleft()
                next_cached.discard(oldest)
            next_cache.append(vertex)
            next_cached.add(vertex)

        return hits, next_cache, next_cached

    def _future_adjacency_score_locked(self, position: int,
                                       remaining: set[int], start: int,
                                       end: int) -> int:
        triangle = self._triangles[position]
        score = 0
        for vertex in dict.fromkeys(triangle["indices"]):
            for neighbor_position in self._adjacency.get(vertex, ()):
                if (neighbor_position != position
                        and start <= neighbor_position < end
                        and neighbor_position in remaining):
                    score += 1
        return score

    def _greedy_order_locked(self) -> list[int]:
        cache_size = self._cache_size
        result: list[int] = []
        cache: deque[int] = deque()
        cached: set[int] = set()

        for start, end in self._runs:
            remaining = set(range(start, end))
            earliest = start
            recent_vertices: list[int] = []

            while remaining:
                candidates: set[int] = set()
                candidate_vertices = list(cache) + recent_vertices
                for vertex in candidate_vertices:
                    for position in self._adjacency.get(vertex, ()):
                        if start <= position < end and position in remaining:
                            candidates.add(position)

                if candidates:
                    choices = []
                    for position in candidates:
                        triangle = self._triangles[position]
                        hits, _, _ = self._feed_indices_locked(
                            cache, cached, triangle["indices"], cache_size
                        )
                        future_score = self._future_adjacency_score_locked(
                            position, remaining, start, end
                        )
                        choices.append((hits, future_score, position))

                    _, _, best_position = min(
                        choices,
                        key=lambda choice: (-choice[0], -choice[1], choice[2]),
                    )
                else:
                    best_position = earliest

                best_triangle = self._triangles[best_position]
                _, cache, cached = self._feed_indices_locked(
                    cache, cached, best_triangle["indices"], cache_size
                )
                result.append(best_triangle["id"])
                remaining.remove(best_position)
                recent_vertices = list(dict.fromkeys(best_triangle["indices"]))

                while earliest < end and earliest not in remaining:
                    earliest += 1

        return result

    def _simulate_current_locked(self, order: Sequence) -> dict:
        return _simulate_order_data(
            self._by_id,
            len(self._triangles),
            order,
            self._cache_size,
        )

    def _plan_report_locked(self, reused: bool) -> dict:
        report = copy.deepcopy(self._plan["report"])
        report["order"] = list(self._plan["order"])
        report["cache_size"] = self._cache_size
        report["reused"] = reused
        return report

    def optimize(self) -> dict:
        with self._lock:
            self._require_loaded_locked()

            if self._plan is not None:
                return self._plan_report_locked(reused=True)

            original_order = list(self._original_order)
            original_report = self._simulate_current_locked(original_order)

            if self._cache_size == 0:
                best_order = original_order
                best_report = original_report
            else:
                greedy_order = self._greedy_order_locked()
                greedy_report = self._simulate_current_locked(greedy_order)

                if greedy_report["cache_hits"] > original_report["cache_hits"]:
                    best_order = greedy_order
                    best_report = greedy_report
                else:
                    best_order = original_order
                    best_report = original_report

            stored_report = copy.deepcopy(best_report)
            stored_report["reused"] = False
            self._plan = {
                "cache_size": self._cache_size,
                "order": list(best_order),
                "report": stored_report,
            }
            self._stats["optimizations"] += 1
            return self._plan_report_locked(reused=False)

    def measure_hit_rate(self, order=None) -> dict:
        with self._lock:
            self._require_loaded_locked()

            reused_plan = False
            if order is None:
                if self._plan is not None:
                    order = list(self._plan["order"])
                    reused_plan = True
                else:
                    order = list(self._original_order)
            elif not _is_permutation(
                    order, set(self._by_id), len(self._triangles)):
                raise self._reject_locked(
                    "VCO_INVALID_ORDER",
                    {"expected_triangles": len(self._triangles)},
                )

            report = self._simulate_current_locked(order)
            report["reused"] = reused_plan
            self._stats["cache_simulations"] += 1
            return report

    def restore_order(self) -> dict:
        with self._lock:
            self._require_loaded_locked()
            order = list(self._original_order)
            self._plan = None
            self._stats["restores"] += 1
            return {"restored": True, "order": order}

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": SNAPSHOT_VERSION,
                "cache_size": self._cache_size,
                "vertices": copy.deepcopy(self._vertices),
                "triangles": copy.deepcopy(self._raw_triangles),
                "original_order": list(self._original_order),
                "optimized_order": self.optimized_order(),
                "plan": copy.deepcopy(self._plan),
                "submeshes": copy.deepcopy(self._submeshes),
                "stats": dict(self._stats),
                "config": {
                    "max_cache_size": self.max_cache_size,
                    "max_vertices": self.max_vertices,
                    "max_triangles": self.max_triangles,
                },
            }
            return copy.deepcopy(state)

    def _parse_snapshot_locked(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise VertexCacheError("VCO_SNAPSHOT_INVALID",
                                   {"reason": "STATE_NOT_OBJECT"})

        try:
            data = copy.deepcopy(state)
        except Exception as exc:
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "STATE_NOT_COPYABLE"},
            ) from exc

        required_keys = (
            "version",
            "cache_size",
            "vertices",
            "triangles",
            "original_order",
            "optimized_order",
            "plan",
            "stats",
        )
        missing = [key for key in required_keys if key not in data]
        if missing:
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "MISSING_KEYS", "missing": missing},
            )

        if data["version"] != SNAPSHOT_VERSION:
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "UNSUPPORTED_VERSION", "version": data["version"]},
            )

        cache_size = data["cache_size"]
        if (not _is_integer(cache_size) or cache_size < 0
                or cache_size > self.max_cache_size):
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "INVALID_CACHE_SIZE", "cache_size": cache_size},
            )

        mesh_state = self._build_mesh_state_locked(
            data["vertices"],
            data["triangles"],
            data["original_order"],
        )

        raw_stats = data["stats"]
        if not isinstance(raw_stats, dict):
            raise VertexCacheError("VCO_SNAPSHOT_INVALID",
                                   {"reason": "STATS_NOT_OBJECT"})

        stats: dict[str, int] = {}
        for key in STATS_KEYS:
            if key not in raw_stats:
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": "MISSING_STAT", "stat": key},
                )
            value = raw_stats[key]
            if not _is_integer(value) or value < 0:
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": "INVALID_STAT", "stat": key, "value": value},
                )
            stats[key] = value

        optimized_order = data["optimized_order"]
        if not isinstance(optimized_order, list):
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "OPTIMIZED_ORDER_NOT_LIST"},
            )
        if optimized_order and not _is_permutation(
                optimized_order, set(mesh_state["by_id"]),
                len(mesh_state["triangles"])):
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "INVALID_OPTIMIZED_ORDER"},
            )

        raw_plan = data["plan"]
        plan = None
        if raw_plan is not None:
            if not isinstance(raw_plan, dict):
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": "PLAN_NOT_OBJECT"},
                )
            plan_order = raw_plan.get("order")
            if not _is_permutation(
                    plan_order, set(mesh_state["by_id"]),
                    len(mesh_state["triangles"])):
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": "INVALID_PLAN_ORDER"},
                )
            if optimized_order and list(plan_order) != optimized_order:
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": "PLAN_ORDER_MISMATCH"},
                )
            if not optimized_order:
                optimized_order = list(plan_order)

            report = _simulate_order_data(
                mesh_state["by_id"],
                len(mesh_state["triangles"]),
                plan_order,
                cache_size,
            )
            report["reused"] = False
            plan = {
                "cache_size": cache_size,
                "order": list(plan_order),
                "report": report,
            }
        elif optimized_order:
            raise VertexCacheError(
                "VCO_SNAPSHOT_INVALID",
                {"reason": "OPTIMIZED_ORDER_WITHOUT_PLAN"},
            )

        return {
            "cache_size": cache_size,
            "mesh": mesh_state,
            "plan": plan,
            "stats": stats,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            previous_stats = dict(self._stats)
            try:
                restored = self._parse_snapshot_locked(state)
            except VertexCacheError as exc:
                self._stats = previous_stats
                if exc.code == "VCO_SNAPSHOT_INVALID":
                    raise
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": exc.code, "detail": exc.detail},
                ) from exc
            except Exception as exc:
                self._stats = previous_stats
                raise VertexCacheError(
                    "VCO_SNAPSHOT_INVALID",
                    {"reason": "SNAPSHOT_INVALID"},
                ) from exc

            mesh_state = restored["mesh"]
            self._loaded = True
            self._vertices = mesh_state["vertices"]
            self._vertex_set = mesh_state["vertex_set"]
            self._raw_triangles = mesh_state["raw_triangles"]
            self._triangles = mesh_state["triangles"]
            self._by_id = mesh_state["by_id"]
            self._original_order = mesh_state["original_order"]
            self._submeshes = mesh_state["submeshes"]
            self._runs = mesh_state["runs"]
            self._adjacency = mesh_state["adjacency"]
            self._cache_size = restored["cache_size"]
            self._plan = restored["plan"]
            self._stats = restored["stats"]
