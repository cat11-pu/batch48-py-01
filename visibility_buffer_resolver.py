"""Visibility buffer resolve over per pixel triangle id and depth data."""

from __future__ import annotations

import copy
import threading

STATS_KEYS = (
    "triangles_submitted",
    "pixels_resolved",
    "regions_resolved",
    "cache_hits",
    "rejected",
)

SNAPSHOT_VERSION = 1


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _insert_candidate(pixels_buffer, overflow, triangle_id: int,
                      entries, max_candidates: int) -> None:
    """Merge one triangle's pixel entries into the per pixel buffers."""
    for entry in entries:
        index = entry["y"], entry["x"]
        if index in overflow:
            continue
        bucket = pixels_buffer.get(index)
        if bucket is None:
            bucket = []
            pixels_buffer[index] = bucket
        if len(bucket) >= max_candidates:
            overflow.add(index)
            pixels_buffer.pop(index, None)
            continue
        bucket.append({
            "triangle": triangle_id,
            "depth": entry["depth"],
            "coverage": entry["coverage"],
            "sequence": entry["sequence"],
        })


class VisibilityError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class VisibilityBufferResolver:
    """Store triangle ids per pixel and resolve materials by depth."""

    def __init__(self, *, max_width: int = 512, max_height: int = 512,
                 max_candidates: int = 4, max_triangles: int = 1024,
                 clock=None) -> None:
        self.max_width = max_width
        self.max_height = max_height
        self.max_candidates = max_candidates
        self.max_triangles = max_triangles
        self._clock = clock
        self._lock = threading.RLock()
        self._width = None
        self._height = None
        self._generation = 0
        self._sequence = 0
        self._triangles = {}
        self._pixels = {}
        self._overflow = set()
        self._cache = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    def _fail(self, code: str, detail: object = None):
        raise VisibilityError(code, detail)

    def _require_resized(self) -> None:
        if self._width is None:
            self._fail("VBUF_NOT_RESIZED")

    def _validate_size(self, width: object, height: object) -> bool:
        return (_is_int(width) and _is_int(height)
                and 1 <= width <= self.max_width
                and 1 <= height <= self.max_height)

    def _validate_pixel(self, pixel_data: object) -> dict:
        if not isinstance(pixel_data, dict):
            self._fail("VBUF_INVALID_TRIANGLE")
        x = pixel_data.get("x")
        y = pixel_data.get("y")
        depth = pixel_data.get("depth")
        coverage = pixel_data.get("coverage")
        if (not _is_int(x) or not _is_int(y)
                or not _is_number(depth) or not _is_number(coverage)
                or depth <= 0 or not 0 < coverage <= 1):
            self._fail("VBUF_INVALID_TRIANGLE")
        return {"x": x, "y": y, "depth": depth, "coverage": coverage}

    def _validate_triangle(self, triangle: object) -> dict:
        if not isinstance(triangle, dict):
            self._fail("VBUF_INVALID_TRIANGLE")
        triangle_id = triangle.get("id")
        material = triangle.get("material")
        params = triangle.get("params")
        pixels = triangle.get("pixels")
        if (not _is_int(triangle_id) or triangle_id <= 0
                or not isinstance(material, str) or not material
                or not isinstance(params, dict)
                or not isinstance(pixels, list) or not pixels):
            self._fail("VBUF_INVALID_TRIANGLE")
        entries = {}
        for raw_pixel in pixels:
            entry = self._validate_pixel(raw_pixel)
            entries[(entry["y"], entry["x"])] = entry
        return {
            "id": triangle_id,
            "material": material,
            "params": copy.deepcopy(params),
            "pixels": list(entries.values()),
        }

    def _background_cell(self, x: int, y: int) -> dict:
        return {
            "x": x,
            "y": y,
            "triangle": None,
            "material": None,
            "params": {},
            "depth": None,
            "coverage": 0.0,
            "generation": self._generation,
        }

    def _build_cell(self, x: int, y: int) -> dict:
        if (y, x) in self._overflow:
            self._fail("VBUF_UNRESOLVABLE_PIXEL",
                       {"x": x, "y": y, "max_candidates": self.max_candidates})
        bucket = self._pixels.get((y, x))
        if not bucket:
            return self._background_cell(x, y)
        winner = min(bucket, key=lambda item: (item["depth"], item["sequence"]))
        triangle = self._triangles[winner["triangle"]]
        return {
            "x": x,
            "y": y,
            "triangle": winner["triangle"],
            "material": triangle["material"],
            "params": copy.deepcopy(triangle["params"]),
            "depth": winner["depth"],
            "coverage": winner["coverage"],
            "generation": self._generation,
        }

    def resize(self, width: int, height: int) -> dict:
        with self._lock:
            try:
                if not self._validate_size(width, height):
                    self._fail("VBUF_INVALID_SIZE",
                               {"width": width, "height": height,
                                "max_width": self.max_width,
                                "max_height": self.max_height})
                self._width = width
                self._height = height
                self._generation += 1
                self._sequence = 0
                self._triangles = {}
                self._pixels = {}
                self._overflow = set()
                self._cache = {}
                return {"width": width, "height": height,
                        "generation": self._generation}
            except VisibilityError:
                self._stats["rejected"] += 1
                raise

    def clear(self) -> dict:
        with self._lock:
            try:
                self._require_resized()
                self._generation += 1
                self._sequence = 0
                self._triangles = {}
                self._pixels = {}
                self._overflow = set()
                self._cache = {}
                return {"width": self._width, "height": self._height,
                        "generation": self._generation}
            except VisibilityError:
                self._stats["rejected"] += 1
                raise

    def submit_triangle(self, triangle: dict) -> dict:
        with self._lock:
            try:
                self._require_resized()
                validated = self._validate_triangle(triangle)
                triangle_id = validated["id"]
                if triangle_id in self._triangles:
                    self._fail("VBUF_DUPLICATE_TRIANGLE",
                               {"triangle_id": triangle_id})
                for entry in validated["pixels"]:
                    if not (0 <= entry["x"] < self._width
                            and 0 <= entry["y"] < self._height):
                        self._fail("VBUF_OUT_OF_BOUNDS",
                                   {"x": entry["x"], "y": entry["y"],
                                    "width": self._width,
                                    "height": self._height})
                if len(self._triangles) >= self.max_triangles:
                    self._fail("VBUF_TOO_MANY_TRIANGLES",
                               {"triangle_count": len(self._triangles),
                                "max_triangles": self.max_triangles})

                self._sequence += 1
                sequence = self._sequence
                for entry in validated["pixels"]:
                    entry["sequence"] = sequence
                _insert_candidate(self._pixels, self._overflow, triangle_id,
                                  validated["pixels"], self.max_candidates)
                self._triangles[triangle_id] = {
                    "id": triangle_id,
                    "material": validated["material"],
                    "params": validated["params"],
                    "pixels": validated["pixels"],
                }
                self._stats["triangles_submitted"] += 1
                self._cache = {}
                return {"triangle": triangle_id,
                        "generation": self._generation,
                        "sequence": sequence}
            except VisibilityError:
                self._stats["rejected"] += 1
                raise

    def resolve_pixel(self, x: int, y: int) -> dict:
        with self._lock:
            try:
                self._require_resized()
                if (not _is_int(x) or not _is_int(y)
                        or not (0 <= x < self._width and 0 <= y < self._height)):
                    self._fail("VBUF_OUT_OF_BOUNDS",
                               {"x": x, "y": y, "width": self._width,
                                "height": self._height})
                cached = self._cache.get(("pixel", y, x))
                if cached is not None:
                    self._stats["cache_hits"] += 1
                else:
                    cached = self._build_cell(x, y)
                    self._cache[("pixel", y, x)] = cached
                self._stats["pixels_resolved"] += 1
                return copy.deepcopy(cached)
            except VisibilityError:
                self._stats["rejected"] += 1
                raise

    def resolve_region(self, x0: int, y0: int, x1: int, y1: int) -> dict:
        with self._lock:
            try:
                self._require_resized()
                if (not all(_is_int(value) for value in (x0, y0, x1, y1))
                        or x0 > x1 or y0 > y1
                        or not (0 <= x0 < self._width and 0 <= x1 < self._width)
                        or not (0 <= y0 < self._height
                                and 0 <= y1 < self._height)):
                    self._fail("VBUF_INVALID_REGION",
                               {"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                                "width": self._width,
                                "height": self._height})
                cells = []
                for y in range(y0, y1 + 1):
                    row = []
                    for x in range(x0, x1 + 1):
                        row.append(copy.deepcopy(self._build_cell(x, y)))
                    cells.append(row)
                self._stats["regions_resolved"] += 1
                return {"x0": x0, "y0": y0, "x1": x1, "y1": y1,
                        "generation": self._generation, "cells": cells}
            except VisibilityError:
                self._stats["rejected"] += 1
                raise

    def generation(self) -> int:
        with self._lock:
            return self._generation

    def buffered_triangles(self) -> list:
        with self._lock:
            return list(self._triangles.keys())

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "width": self._width,
                "height": self._height,
                "generation": self._generation,
                "triangles": copy.deepcopy(list(self._triangles.values())),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                if not isinstance(state, dict):
                    raise VisibilityError("VBUF_SNAPSHOT_INVALID")
                version = state.get("version")
                width = state.get("width")
                height = state.get("height")
                generation = state.get("generation")
                triangles = state.get("triangles")
                stats = state.get("stats")
                if (version != SNAPSHOT_VERSION
                        or not self._validate_size(width, height)
                        or not _is_int(generation) or generation < 1
                        or not isinstance(triangles, list)
                        or not isinstance(stats, dict)
                        or set(stats.keys()) != set(STATS_KEYS)
                        or not all(_is_int(value) and value >= 0
                                   for value in stats.values())
                        or len(triangles) > self.max_triangles):
                    raise VisibilityError("VBUF_SNAPSHOT_INVALID")

                replayed = {}
                pixels = {}
                overflow = set()
                sequence = 0
                for stored in triangles:
                    if not isinstance(stored, dict):
                        raise VisibilityError("VBUF_SNAPSHOT_INVALID")
                    validated = self._validate_triangle(stored)
                    triangle_id = validated["id"]
                    if triangle_id in replayed:
                        raise VisibilityError("VBUF_SNAPSHOT_INVALID")
                    for entry in validated["pixels"]:
                        if not (0 <= entry["x"] < width
                                and 0 <= entry["y"] < height):
                            raise VisibilityError("VBUF_SNAPSHOT_INVALID")
                    sequence += 1
                    for entry in validated["pixels"]:
                        entry["sequence"] = sequence
                    _insert_candidate(pixels, overflow, triangle_id,
                                      validated["pixels"],
                                      self.max_candidates)
                    replayed[triangle_id] = {
                        "id": triangle_id,
                        "material": validated["material"],
                        "params": validated["params"],
                        "pixels": validated["pixels"],
                    }
            except VisibilityError as exc:
                if exc.code != "VBUF_SNAPSHOT_INVALID":
                    raise VisibilityError("VBUF_SNAPSHOT_INVALID",
                                          str(exc)) from exc
                raise

            self._width = width
            self._height = height
            self._generation = generation
            self._sequence = sequence
            self._triangles = replayed
            self._pixels = pixels
            self._overflow = overflow
            self._cache = {}
            self._stats = copy.deepcopy(stats)
