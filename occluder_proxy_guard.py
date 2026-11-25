"""Occluder proxy building, scoring and budgeted selection."""

from __future__ import annotations

import copy
import math


_SNAPSHOT_VERSION = 1
_STAT_KEYS = (
    "built",
    "rebuilt",
    "removed",
    "occluder_queries",
    "dropped_budget",
    "dropped_coverage",
    "frames",
)


class OccluderError(ValueError):
    """Raised when a proxy, camera or snapshot request is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _bounds(value: object) -> tuple:
    if not isinstance(value, (tuple, list)) or len(value) != 6:
        raise ValueError("bounds must contain six numbers")
    if not all(_is_number(component) for component in value):
        raise ValueError("bounds must contain finite numbers")
    if value[0] > value[3] or value[1] > value[4] or value[2] > value[5]:
        raise ValueError("bounds lower limits exceed upper limits")
    return tuple(value)


def _camera(value: object) -> tuple:
    if not isinstance(value, dict):
        raise ValueError("camera must be a dictionary")

    position = value.get("position")
    focal = value.get("focal")
    viewport = value.get("viewport")
    if (
        not isinstance(position, (tuple, list))
        or len(position) != 3
        or not all(_is_number(component) for component in position)
    ):
        raise ValueError("camera position must contain three finite numbers")
    if not _is_number(focal) or focal <= 0:
        raise ValueError("camera focal must be positive")
    if (
        not isinstance(viewport, (tuple, list))
        or len(viewport) != 2
        or not all(_is_number(component) and component > 0 for component in viewport)
    ):
        raise ValueError("camera viewport must contain two positive numbers")
    return (
        tuple(float(component) for component in position),
        float(focal),
        float(viewport[0]),
        float(viewport[1]),
    )


def _proxy_coverage(proxy: dict, camera: tuple) -> float:
    position, focal, viewport_width, viewport_height = camera
    x_min, y_min, z_min, x_max, y_max, z_max = proxy["bounds"]
    center = (
        (x_min + x_max) / 2.0,
        (y_min + y_max) / 2.0,
        (z_min + z_max) / 2.0,
    )
    distance = math.dist(position, center)
    half_extent = max(x_max - x_min, y_max - y_min, z_max - z_min) / 2.0
    if half_extent <= 0:
        return 0.0
    if distance <= 0:
        return 1.0

    pixel_radius = half_extent * focal / distance
    pixel_side = pixel_radius * 2.0
    coverage = (pixel_side * pixel_side) / (viewport_width * viewport_height)
    return min(1.0, coverage)


class OccluderProxyGuard:
    """Build box/hull occluder proxies, score them and budget the set."""

    def __init__(self, *, max_proxies: int = 16, tri_budget: int = 400,
                 min_coverage: float = 0.002) -> None:
        self.max_proxies = max_proxies
        self.tri_budget = tri_budget
        self.min_coverage = min_coverage
        self._proxies: dict[str, dict] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}
        self._last_frame = self._empty_frame()
        self._rebuilt_this_frame: set[str] = set()

    @staticmethod
    def _empty_frame() -> dict:
        return {
            "kept": [],
            "dropped_budget": [],
            "dropped_coverage": [],
        }

    @staticmethod
    def _mesh(mesh: object) -> tuple[str, str, tuple, int]:
        if not isinstance(mesh, dict):
            raise OccluderError("PROXY_MESH_INVALID", "mesh must be a dictionary")

        mesh_id = mesh.get("id")
        shape = mesh.get("shape")
        if not isinstance(mesh_id, str) or not mesh_id:
            raise OccluderError("PROXY_MESH_INVALID", "mesh id must be a non-empty string")
        if not isinstance(shape, str) or shape not in {"box", "hull"}:
            raise OccluderError("PROXY_MESH_INVALID", "shape must be box or hull")
        try:
            bounds = _bounds(mesh.get("bounds"))
        except ValueError as exc:
            raise OccluderError("PROXY_MESH_INVALID", str(exc)) from exc

        if shape == "box":
            triangles = 12
        else:
            triangles = mesh.get("hull_triangles")
            if (
                not isinstance(triangles, int)
                or isinstance(triangles, bool)
                or not 4 <= triangles <= 512
            ):
                raise OccluderError(
                    "PROXY_MESH_INVALID",
                    "hull_triangles must be an integer from 4 to 512",
                )
        return mesh_id, shape, bounds, triangles

    def _proxy(self, mesh_id: str) -> dict:
        try:
            return self._proxies[mesh_id]
        except (KeyError, TypeError) as exc:
            raise OccluderError("PROXY_UNKNOWN_MESH", mesh_id) from exc

    def _evaluate(self, proxy: dict, camera: tuple) -> tuple[float, float]:
        coverage = _proxy_coverage(proxy, camera)
        proxy["coverage"] = coverage
        position = camera[0]
        x_min, y_min, z_min, x_max, y_max, z_max = proxy["bounds"]
        center = (
            (x_min + x_max) / 2.0,
            (y_min + y_max) / 2.0,
            (z_min + z_max) / 2.0,
        )
        distance = math.dist(position, center)
        score = coverage * 64.0 / (64.0 + distance)
        return coverage, score

    def _commit_rebuild(self, proxy: dict) -> None:
        proxy["generation"] += 1
        proxy["stale"] = False
        proxy["coverage"] = 0.0
        self._stats["rebuilt"] += 1
        self._rebuilt_this_frame.add(proxy["mesh_id"])

    def _auto_rebuild(self) -> None:
        for mesh_id in sorted(self._proxies):
            proxy = self._proxies[mesh_id]
            if proxy["stale"] and mesh_id not in self._rebuilt_this_frame:
                self._commit_rebuild(proxy)

    def build(self, mesh: dict) -> dict:
        mesh_id, shape, bounds, triangles = self._mesh(mesh)
        if mesh_id in self._proxies:
            raise OccluderError("PROXY_DUPLICATE_MESH", mesh_id)
        if len(self._proxies) >= self.max_proxies:
            raise OccluderError("PROXY_LIMIT", mesh_id)
        if triangles > self.tri_budget:
            raise OccluderError("PROXY_BUDGET", triangles)

        proxy = {
            "mesh_id": mesh_id,
            "shape": shape,
            "bounds": bounds,
            "triangles": triangles,
            "generation": 1,
            "stale": False,
            "coverage": 0.0,
        }
        self._proxies[mesh_id] = proxy
        self._stats["built"] += 1
        return copy.deepcopy(proxy)

    def proxy_of(self, mesh_id: str) -> dict:
        return copy.deepcopy(self._proxy(mesh_id))

    def invalidate(self, mesh_id: str, *, bounds=None) -> None:
        proxy = self._proxy(mesh_id)
        if bounds is not None:
            try:
                new_bounds = _bounds(bounds)
            except ValueError as exc:
                raise OccluderError("PROXY_MESH_INVALID", str(exc)) from exc
            proxy["bounds"] = new_bounds
        proxy["stale"] = True

    def rebuild(self, mesh_id: str) -> dict:
        proxy = self._proxy(mesh_id)
        if proxy["stale"]:
            self._commit_rebuild(proxy)
        return copy.deepcopy(proxy)

    def rebuild_pending(self) -> int:
        rebuilt = 0
        for mesh_id in sorted(self._proxies):
            proxy = self._proxies[mesh_id]
            if proxy["stale"]:
                self._commit_rebuild(proxy)
                rebuilt += 1
        return rebuilt

    def score(self, mesh_id: str, camera: dict) -> float:
        proxy = self._proxy(mesh_id)
        try:
            valid_camera = _camera(camera)
        except ValueError as exc:
            raise OccluderError("PROXY_CAMERA_INVALID", str(exc)) from exc
        _, score = self._evaluate(proxy, valid_camera)
        return score

    def coverage(self, mesh_id: str) -> float:
        return self._proxy(mesh_id)["coverage"]

    def occluders(self, camera: dict, limit: int = 8) -> list[str]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise OccluderError("PROXY_LIMIT_INVALID", limit)
        try:
            valid_camera = _camera(camera)
        except ValueError as exc:
            raise OccluderError("PROXY_CAMERA_INVALID", str(exc)) from exc

        self._auto_rebuild()
        self._stats["occluder_queries"] += 1
        candidates = []
        dropped_coverage = []
        for mesh_id in sorted(self._proxies):
            proxy = self._proxies[mesh_id]
            coverage, score = self._evaluate(proxy, valid_camera)
            if coverage < self.min_coverage:
                dropped_coverage.append(mesh_id)
                continue
            candidates.append((mesh_id, proxy, coverage, score))

        dropped_budget = []
        triangle_total = sum(candidate[1]["triangles"] for candidate in candidates)
        if triangle_total > self.tri_budget:
            for mesh_id, proxy, _, _ in sorted(
                candidates, key=lambda candidate: (candidate[2], candidate[0])
            ):
                if triangle_total <= self.tri_budget:
                    break
                triangle_total -= proxy["triangles"]
                dropped_budget.append(mesh_id)
            dropped_ids = set(dropped_budget)
            candidates = [
                candidate for candidate in candidates
                if candidate[0] not in dropped_ids
            ]

        candidates.sort(key=lambda candidate: (-candidate[3], candidate[0]))
        kept = [candidate[0] for candidate in candidates[:limit]]
        self._stats["dropped_budget"] += len(dropped_budget)
        self._stats["dropped_coverage"] += len(dropped_coverage)
        self._last_frame = {
            "kept": kept,
            "dropped_budget": dropped_budget,
            "dropped_coverage": dropped_coverage,
        }
        return list(kept)

    def last_frame(self) -> dict:
        return copy.deepcopy(self._last_frame)

    def begin_frame(self) -> None:
        self._stats["frames"] += 1
        self._rebuilt_this_frame.clear()
        self._last_frame = self._empty_frame()

    def budget(self) -> dict:
        return {
            "proxies": len(self._proxies),
            "max_proxies": self.max_proxies,
            "triangles": sum(
                proxy["triangles"] for proxy in self._proxies.values()
            ),
            "tri_budget": self.tri_budget,
        }

    def remove(self, mesh_id: str) -> None:
        self._proxy(mesh_id)
        del self._proxies[mesh_id]
        self._rebuilt_this_frame.discard(mesh_id)
        self._stats["removed"] += 1

    def snapshot(self) -> dict:
        return {
            "version": _SNAPSHOT_VERSION,
            "proxies": copy.deepcopy(self._proxies),
            "stats": dict(self._stats),
        }

    def _snapshot_parts(self, state: object) -> tuple[dict, dict]:
        if not isinstance(state, dict):
            raise ValueError("snapshot must be a dictionary")
        if state.get("version") != _SNAPSHOT_VERSION:
            raise ValueError("unsupported snapshot version")

        raw_proxies = state.get("proxies")
        raw_stats = state.get("stats")
        if not isinstance(raw_proxies, dict):
            raise ValueError("snapshot proxies must be a dictionary")
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(_STAT_KEYS):
            raise ValueError("snapshot stats have the wrong fields")
        for value in raw_stats.values():
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError("snapshot stats must be non-negative integers")
        if len(raw_proxies) > self.max_proxies:
            raise ValueError("snapshot exceeds proxy limit")

        proxies = {}
        for mesh_id, raw_proxy in raw_proxies.items():
            if not isinstance(mesh_id, str) or not mesh_id:
                raise ValueError("snapshot proxy id is invalid")
            if not isinstance(raw_proxy, dict):
                raise ValueError("snapshot proxy must be a dictionary")
            if raw_proxy.get("mesh_id") != mesh_id:
                raise ValueError("snapshot proxy id does not match its key")

            shape = raw_proxy.get("shape")
            triangles = raw_proxy.get("triangles")
            generation = raw_proxy.get("generation")
            stale = raw_proxy.get("stale")
            coverage = raw_proxy.get("coverage")
            if not isinstance(shape, str) or shape not in {"box", "hull"}:
                raise ValueError("snapshot proxy shape is invalid")
            if (
                not isinstance(triangles, int)
                or isinstance(triangles, bool)
                or triangles < 1
            ):
                raise ValueError("snapshot triangle count is invalid")
            if shape == "box" and triangles != 12:
                raise ValueError("snapshot box triangle count is invalid")
            if shape == "hull" and not 4 <= triangles <= 512:
                raise ValueError("snapshot hull triangle count is invalid")
            if triangles > self.tri_budget:
                raise ValueError("snapshot proxy exceeds triangle budget")
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 1
            ):
                raise ValueError("snapshot generation is invalid")
            if not isinstance(stale, bool):
                raise ValueError("snapshot stale flag is invalid")
            if (
                not _is_number(coverage)
                or coverage < 0
                or coverage > 1
            ):
                raise ValueError("snapshot coverage is invalid")
            try:
                proxy_bounds = _bounds(raw_proxy.get("bounds"))
            except ValueError as exc:
                raise ValueError("snapshot proxy bounds are invalid") from exc

            proxies[mesh_id] = {
                "mesh_id": mesh_id,
                "shape": shape,
                "bounds": proxy_bounds,
                "triangles": triangles,
                "generation": generation,
                "stale": stale,
                "coverage": float(coverage),
            }
        return proxies, dict(raw_stats)

    def restore(self, state: dict) -> None:
        try:
            proxies, stats = self._snapshot_parts(state)
        except (TypeError, ValueError) as exc:
            raise OccluderError("PROXY_SNAPSHOT_INVALID", str(exc)) from exc

        self._proxies = proxies
        self._stats = stats
        self._last_frame = self._empty_frame()
        self._rebuilt_this_frame.clear()

    def stats(self) -> dict:
        return dict(self._stats)

    def reset_stats(self) -> None:
        self._stats = {key: 0 for key in _STAT_KEYS}
