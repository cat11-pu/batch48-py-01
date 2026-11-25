"""Cluster light page cache with dirty-page rebuilding and page codes."""

from __future__ import annotations

import math
import threading


class ClusterLightError(ValueError):
    """Raised when a cluster light page operation cannot be completed."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_SNAPSHOT_VERSION = 1
_DEFAULT_PAGE_SIZE = 8
_STATS_KEYS = ("lights", "dirty_pages", "hot_pages", "rebuilds",
               "overflow_pages")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    return (isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value))


class ClusterLightPageCache:
    """Cache cluster light indices in pages and rebuild dirty pages only."""

    def __init__(self, *, max_pages: int = 65536,
                 page_capacity: int = 64) -> None:
        self.max_pages = max_pages
        self.page_capacity = page_capacity
        self._lock = threading.RLock()
        self._grid = None
        self._page_size = _DEFAULT_PAGE_SIZE
        self._page_dims = (0, 0, 0)
        self._lights = {}
        self._removed = set()
        self._pages = {}
        self._committed = {}
        self._dirty = set()
        self._rebuilds = 0
        self._overflow_pages = 0

    def set_grid(self, x: int, y: int, z: int) -> dict:
        with self._lock:
            for value in (x, y, z):
                if not _is_int(value) or value <= 0:
                    raise ClusterLightError(
                        "CLUSTER_GRID_INVALID",
                        {"grid": [x, y, z]})
            dims = tuple(-(-value // self._page_size) for value in (x, y, z))
            total = dims[0] * dims[1] * dims[2]
            if total > self.max_pages:
                raise ClusterLightError(
                    "CLUSTER_PAGE_LIMIT",
                    {"pages": total, "max_pages": self.max_pages})
            self._grid = (x, y, z)
            self._page_dims = dims
            self._invalidate_pages()
            return {"grid": [x, y, z], "page_size": self._page_size,
                    "pages": total}

    def set_page_size(self, size: int) -> int:
        with self._lock:
            if not _is_int(size) or size <= 0:
                raise ClusterLightError(
                    "CLUSTER_PAGE_SIZE_INVALID", {"page_size": size})
            if self._grid is None:
                self._page_size = size
                return 0
            dims = tuple(-(-value // size) for value in self._grid)
            total = dims[0] * dims[1] * dims[2]
            if total > self.max_pages:
                raise ClusterLightError(
                    "CLUSTER_PAGE_LIMIT",
                    {"pages": total, "max_pages": self.max_pages})
            self._page_size = size
            self._page_dims = dims
            self._invalidate_pages()
            return total

    def upsert_light(self, light: dict) -> int:
        with self._lock:
            record = self._validate_light(light)
            index = record["index"]
            pages = self._light_pages(record["position"], record["radius"])
            old = self._lights.get(index)
            if old is not None:
                self._dirty |= old["pages"]
            self._dirty |= pages
            self._lights[index] = {
                "position": record["position"],
                "radius": record["radius"],
                "intensity": record["intensity"],
                "pages": pages,
            }
            self._removed.discard(index)
            return len(pages)

    def remove_light(self, index: int) -> int:
        with self._lock:
            if index not in self._lights:
                if index in self._removed:
                    return 0
                raise ClusterLightError(
                    "CLUSTER_LIGHT_UNKNOWN", {"index": index})
            del self._lights[index]
            self._removed.add(index)
            committed = self._committed.pop(index, set())
            self._dirty |= committed
            return len(committed)

    def page(self, ix: int, iy: int, iz: int) -> list:
        with self._lock:
            key = self._page_key(ix, iy, iz)
            return list(self._pages.get(key, ()))

    def page_code(self, ix: int, iy: int, iz: int) -> str:
        with self._lock:
            key = self._page_key(ix, iy, iz)
            gx, gy, gz = self._grid
            return (f"g{gx}x{gy}x{gz}-s{self._page_size}"
                    f"-p{key[0]}.{key[1]}.{key[2]}")

    def rebuild(self, dirty_only: bool = True) -> int:
        with self._lock:
            if self._grid is None:
                return 0
            if dirty_only:
                targets = sorted(self._dirty)
            else:
                nx, ny, nz = self._page_dims
                targets = [(ix, iy, iz)
                           for ix in range(nx)
                           for iy in range(ny)
                           for iz in range(nz)]
            for key in targets:
                self._rebuild_page(key)
            self._rebuilds += 1
            return len(targets)

    def stats(self) -> dict:
        with self._lock:
            return {
                "lights": len(self._lights),
                "dirty_pages": len(self._dirty),
                "hot_pages": sum(1 for content in self._pages.values()
                                 if content),
                "rebuilds": self._rebuilds,
                "overflow_pages": self._overflow_pages,
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._rebuilds = 0
            self._overflow_pages = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "grid": list(self._grid) if self._grid is not None else None,
                "page_size": self._page_size,
                "lights": {
                    str(index): {
                        "index": index,
                        "position": list(light["position"]),
                        "radius": light["radius"],
                        "intensity": light["intensity"],
                    }
                    for index, light in self._lights.items()
                },
                "pages": {
                    f"{key[0]}.{key[1]}.{key[2]}": list(content)
                    for key, content in self._pages.items()
                },
                "stats": self.stats(),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            parsed = self._validate_snapshot(state)
            self._grid = parsed["grid"]
            self._page_size = parsed["page_size"]
            self._page_dims = parsed["page_dims"]
            self._lights = {}
            for index, record in parsed["lights"].items():
                pages = self._light_pages(record["position"], record["radius"])
                self._lights[index] = {
                    "position": record["position"],
                    "radius": record["radius"],
                    "intensity": record["intensity"],
                    "pages": pages,
                }
            self._pages = {key: list(content)
                           for key, content in parsed["pages"].items()}
            self._committed = {}
            for key, content in self._pages.items():
                for index in content:
                    if index in self._lights:
                        self._committed.setdefault(index, set()).add(key)
            self._dirty = set()
            self._removed = set()
            self._rebuilds = parsed["stats"]["rebuilds"]
            self._overflow_pages = parsed["stats"]["overflow_pages"]

    def _invalidate_pages(self) -> None:
        nx, ny, nz = self._page_dims
        self._pages = {}
        self._committed = {}
        self._dirty = {(ix, iy, iz)
                       for ix in range(nx)
                       for iy in range(ny)
                       for iz in range(nz)}
        for light in self._lights.values():
            light["pages"] = self._light_pages(light["position"],
                                               light["radius"])

    def _light_pages(self, position: tuple, radius: float) -> set:
        if self._grid is None:
            return set()
        size = self._page_size
        ranges = []
        for coord, dim in zip(position, self._page_dims):
            lo = max(math.floor((coord - radius) / size), 0)
            hi = min(math.floor((coord + radius) / size), dim - 1)
            if lo > hi:
                return set()
            ranges.append(range(lo, hi + 1))
        return {(ix, iy, iz)
                for ix in ranges[0]
                for iy in ranges[1]
                for iz in ranges[2]}

    def _page_key(self, ix: int, iy: int, iz: int) -> tuple:
        coords = (ix, iy, iz)
        if self._grid is None:
            raise ClusterLightError(
                "CLUSTER_PAGE_RANGE", {"page": list(coords)})
        for coord, dim in zip(coords, self._page_dims):
            if not _is_int(coord) or coord < 0 or coord >= dim:
                raise ClusterLightError(
                    "CLUSTER_PAGE_RANGE",
                    {"page": list(coords), "page_dims": list(self._page_dims)})
        return coords

    def _rebuild_page(self, key: tuple) -> None:
        entries = [(light["intensity"], index)
                   for index, light in self._lights.items()
                   if key in light["pages"]]
        entries.sort(key=lambda entry: (-entry[0], entry[1]))
        indices = [index for _, index in entries]
        for index in self._pages.get(key, ()):
            committed = self._committed.get(index)
            if committed is not None:
                committed.discard(key)
        if len(indices) > self.page_capacity:
            self._overflow_pages += 1
            indices = indices[:self.page_capacity]
        for index in indices:
            self._committed.setdefault(index, set()).add(key)
        self._pages[key] = indices
        self._dirty.discard(key)

    @staticmethod
    def _validate_light(light: object) -> dict:
        if not isinstance(light, dict):
            raise ClusterLightError("CLUSTER_LIGHT_INVALID",
                                    {"light": light})
        missing = [field for field in ("index", "position", "radius",
                                       "intensity")
                   if field not in light]
        if missing:
            raise ClusterLightError("CLUSTER_LIGHT_INVALID",
                                    {"missing": missing})
        index = light["index"]
        if not _is_int(index) or index < 0:
            raise ClusterLightError("CLUSTER_LIGHT_INVALID",
                                    {"index": index})
        position = light["position"]
        if (not isinstance(position, (list, tuple)) or len(position) != 3
                or not all(_is_finite_number(coord) for coord in position)):
            raise ClusterLightError("CLUSTER_LIGHT_INVALID",
                                    {"position": position})
        radius = light["radius"]
        intensity = light["intensity"]
        if not _is_finite_number(radius) or radius < 0:
            raise ClusterLightError("CLUSTER_LIGHT_INVALID",
                                    {"radius": radius})
        if not _is_finite_number(intensity) or intensity < 0:
            raise ClusterLightError("CLUSTER_LIGHT_INVALID",
                                    {"intensity": intensity})
        return {"index": index, "position": tuple(position),
                "radius": radius, "intensity": intensity}

    def _validate_snapshot(self, state: object) -> dict:
        def invalid(detail: object) -> ClusterLightError:
            return ClusterLightError("CLUSTER_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            raise invalid({"state": type(state).__name__})
        if state.get("version") != _SNAPSHOT_VERSION:
            raise invalid({"version": state.get("version")})
        grid = state.get("grid")
        if grid is None:
            page_dims = (0, 0, 0)
            grid_tuple = None
        else:
            if (not isinstance(grid, (list, tuple)) or len(grid) != 3
                    or not all(_is_int(value) and value > 0
                               for value in grid)):
                raise invalid({"grid": grid})
            grid_tuple = tuple(grid)
        page_size = state.get("page_size")
        if not _is_int(page_size) or page_size <= 0:
            raise invalid({"page_size": page_size})
        if grid_tuple is not None:
            page_dims = tuple(-(-value // page_size)
                              for value in grid_tuple)
            total = page_dims[0] * page_dims[1] * page_dims[2]
            if total > self.max_pages:
                raise invalid({"pages": total,
                               "max_pages": self.max_pages})
        lights = state.get("lights")
        if not isinstance(lights, dict):
            raise invalid({"lights": lights})
        parsed_lights = {}
        for key, record in lights.items():
            try:
                parsed = self._validate_light(record)
            except ClusterLightError:
                raise invalid({"light": record})
            if key != str(parsed["index"]):
                raise invalid({"light_key": key})
            if parsed["index"] in parsed_lights:
                raise invalid({"index": parsed["index"]})
            parsed_lights[parsed["index"]] = parsed
        pages = state.get("pages")
        if not isinstance(pages, dict):
            raise invalid({"pages": pages})
        parsed_pages = {}
        for key, content in pages.items():
            parts = key.split(".") if isinstance(key, str) else []
            if len(parts) != 3:
                raise invalid({"page_key": key})
            try:
                coords = tuple(int(part) for part in parts)
            except ValueError:
                raise invalid({"page_key": key})
            if any(coord < 0 or coord >= dim
                   for coord, dim in zip(coords, page_dims)):
                raise invalid({"page_key": key})
            if (not isinstance(content, list)
                    or not all(_is_int(index) and index >= 0
                               for index in content)):
                raise invalid({"page_content": content})
            parsed_pages[coords] = list(content)
        stats = state.get("stats")
        if (not isinstance(stats, dict)
                or any(name not in stats or not _is_int(stats[name])
                       or stats[name] < 0 for name in _STATS_KEYS)):
            raise invalid({"stats": stats})
        return {
            "grid": grid_tuple,
            "page_size": page_size,
            "page_dims": page_dims,
            "lights": parsed_lights,
            "pages": parsed_pages,
            "stats": {name: stats[name] for name in _STATS_KEYS},
        }
