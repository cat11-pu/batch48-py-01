"""Vertex attribute quantisation with per semantic error budgets."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import numbers
import threading
from collections import OrderedDict
from typing import Any

STATS_KEYS = (
    "layouts_declared",
    "meshes_quantized",
    "meshes_dequantized",
    "cache_hits",
    "rejected",
)

_KIND_COMPONENTS = {
    "position": 3,
    "normal": 3,
    "tangent": 4,
    "uv": 2,
    "color": 4,
}

_SNAPSHOT_VERSION = 1
_CACHE_MAX_ENTRIES = 128


class QuantizerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, numbers.Real) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


class VertexAttributeQuantizer:
    """Declare vertex layouts, quantise meshes and decode them back."""

    def __init__(self, *, max_vertices: int = 4096, max_bits_per_vertex: int = 128,
                 max_semantics: int = 8, clock=None) -> None:
        self.max_vertices = max_vertices
        self.max_bits_per_vertex = max_bits_per_vertex
        self.max_semantics = max_semantics
        self._clock = clock
        self._lock = threading.RLock()

        self._layouts: dict[str, dict[int, dict[str, dict[str, Any]]]] = {}
        self._current: dict[str, int] = {}
        self._stats = {key: 0 for key in STATS_KEYS}
        self._cache: OrderedDict[tuple[str, ...], tuple[object, dict]] = OrderedDict()
        self._cache_tick = 0

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise QuantizerError(code, detail)

    def _next_cache_stamp(self) -> tuple[float | int, int]:
        self._cache_tick += 1
        if self._clock is None:
            return self._cache_tick, self._cache_tick
        try:
            stamp = self._clock()
        except Exception:
            return self._cache_tick, self._cache_tick
        if not _is_finite_number(stamp):
            return self._cache_tick, self._cache_tick
        return float(stamp), self._cache_tick

    def _validate_spec(self, spec: object) -> tuple[dict[str, dict[str, Any]], int]:
        if not isinstance(spec, dict) or not spec:
            raise QuantizerError("QUANT_INVALID_LAYOUT", {"reason": "spec"})

        canonical: dict[str, dict[str, Any]] = {}
        total_bits = 0
        for raw_name, raw_rule in spec.items():
            if not isinstance(raw_name, str) or not raw_name:
                raise QuantizerError("QUANT_INVALID_LAYOUT", {"reason": "semantic"})
            if not isinstance(raw_rule, dict) or set(raw_rule) != {"kind", "bits"}:
                raise QuantizerError(
                    "QUANT_INVALID_LAYOUT", {"reason": "rule", "semantic": raw_name}
                )
            kind = raw_rule["kind"]
            bits = raw_rule["bits"]
            if (
                not isinstance(kind, str)
                or kind not in _KIND_COMPONENTS
                or not _is_int(bits)
                or bits < 4
                or bits > 24
                or bits % 4 != 0
            ):
                raise QuantizerError(
                    "QUANT_INVALID_LAYOUT",
                    {"reason": "kind_or_bits", "semantic": raw_name},
                )
            canonical[raw_name] = {"kind": kind, "bits": bits}
            total_bits += bits * _KIND_COMPONENTS[kind]

        semantics = len(canonical)
        if semantics > self.max_semantics or total_bits > self.max_bits_per_vertex:
            raise QuantizerError(
                "QUANT_LAYOUT_BUDGET",
                {
                    "semantics": semantics,
                    "max_semantics": self.max_semantics,
                    "total_bits_per_vertex": total_bits,
                    "max_bits_per_vertex": self.max_bits_per_vertex,
                },
            )
        return canonical, total_bits

    @staticmethod
    def _layout_info(layout_id: str, version: int,
                     spec: dict[str, dict[str, Any]], total_bits: int | None = None) -> dict:
        if total_bits is None:
            total_bits = sum(
                rule["bits"] * _KIND_COMPONENTS[rule["kind"]]
                for rule in spec.values()
            )
        return {
            "layout_id": layout_id,
            "version": version,
            "total_bits_per_vertex": total_bits,
            "semantics": [
                {
                    "name": name,
                    "kind": spec[name]["kind"],
                    "bits": spec[name]["bits"],
                    "components": _KIND_COMPONENTS[spec[name]["kind"]],
                }
                for name in sorted(spec)
            ],
        }

    def declare_layout(self, layout_id: str, spec: dict, *, version: int = 1) -> dict:
        with self._lock:
            if not isinstance(layout_id, str) or not layout_id:
                self._reject("QUANT_INVALID_LAYOUT", {"reason": "layout_id"})
            if not _is_int(version) or version < 1:
                self._reject("QUANT_INVALID_LAYOUT", {"reason": "version"})
            try:
                canonical, total_bits = self._validate_spec(spec)
            except QuantizerError as exc:
                self._reject(exc.code, exc.detail)

            versions = self._layouts.get(layout_id)
            if versions is not None:
                if version < self._current[layout_id]:
                    self._reject(
                        "QUANT_LAYOUT_CONFLICT",
                        {
                            "layout_id": layout_id,
                            "version": version,
                            "current_version": self._current[layout_id],
                        },
                    )
                existing = versions.get(version)
                if existing is not None:
                    if existing != canonical:
                        self._reject(
                            "QUANT_LAYOUT_CONFLICT",
                            {"layout_id": layout_id, "version": version},
                        )
                    return self._layout_info(layout_id, version, existing)
                if version == self._current[layout_id]:
                    self._reject(
                        "QUANT_LAYOUT_CONFLICT",
                        {"layout_id": layout_id, "version": version},
                    )
            else:
                versions = {}
                self._layouts[layout_id] = versions

            versions[version] = canonical
            self._current[layout_id] = version
            self._stats["layouts_declared"] += 1
            return self._layout_info(layout_id, version, canonical, total_bits)

    def _resolve_layout(self, layout_id: str, version: int | None) -> tuple[int, dict]:
        if not isinstance(layout_id, str) or layout_id not in self._layouts:
            self._reject("QUANT_UNKNOWN_LAYOUT", {"layout_id": layout_id})
        versions = self._layouts[layout_id]
        selected = self._current[layout_id] if version is None else version
        if not _is_int(selected) or selected not in versions:
            self._reject(
                "QUANT_UNKNOWN_LAYOUT",
                {"layout_id": layout_id, "version": selected},
            )
        return selected, versions[selected]

    def layout_info(self, layout_id: str, *, version: int | None = None) -> dict:
        with self._lock:
            selected, spec = self._resolve_layout(layout_id, version)
            return self._layout_info(layout_id, selected, spec)

    def drop_layout(self, layout_id: str) -> None:
        with self._lock:
            if not isinstance(layout_id, str) or layout_id not in self._layouts:
                self._reject("QUANT_UNKNOWN_LAYOUT", {"layout_id": layout_id})
            del self._layouts[layout_id]
            del self._current[layout_id]
            stale = [key for key in self._cache if key[0] == layout_id]
            for key in stale:
                del self._cache[key]

    def error_bound(self, layout_id: str, semantic: str, *, extent: float = 1.0,
                    version: int | None = None) -> float:
        with self._lock:
            _, spec = self._resolve_layout(layout_id, version)
            if not isinstance(semantic, str) or semantic not in spec:
                self._reject(
                    "QUANT_INVALID_LAYOUT",
                    {"reason": "semantic", "semantic": semantic},
                )
            if not _is_finite_number(extent) or float(extent) < 0.0:
                self._reject(
                    "QUANT_INVALID_LAYOUT",
                    {"reason": "extent", "extent": extent},
                )
            rule = spec[semantic]
            kind = rule["kind"]
            if kind in ("normal", "tangent"):
                span = 2.0
            elif kind in ("position", "uv"):
                span = float(extent)
            else:
                span = 1.0
            return span / float((1 << rule["bits"]) - 1)

    @staticmethod
    def _mesh_error(reason: str, **detail: object) -> QuantizerError:
        payload = {"reason": reason}
        payload.update(detail)
        return QuantizerError("QUANT_INVALID_MESH", payload)

    def _validate_mesh(
        self, mesh: object, spec: dict[str, dict[str, Any]]
    ) -> tuple[dict[str, list[list[float]]], list[int], int]:
        if not isinstance(mesh, dict):
            raise self._mesh_error("mesh")
        attributes = mesh.get("attributes")
        if not isinstance(attributes, dict):
            raise self._mesh_error("attributes")
        if set(attributes) != set(spec):
            missing = sorted(set(spec) - set(attributes))
            undeclared = sorted(
                set(attributes) - set(spec), key=lambda value: repr(value)
            )
            raise self._mesh_error(
                "semantics",
                missing=missing,
                undeclared=undeclared,
            )

        values: dict[str, list[list[float]]] = {}
        vertex_count: int | None = None
        for semantic in sorted(spec):
            rows = attributes[semantic]
            if not isinstance(rows, (list, tuple)) or not rows:
                raise self._mesh_error("rows", semantic=semantic)
            if vertex_count is None:
                vertex_count = len(rows)
            elif len(rows) != vertex_count:
                raise self._mesh_error("vertex_count", semantic=semantic)

            components = _KIND_COMPONENTS[spec[semantic]["kind"]]
            converted: list[list[float]] = []
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != components:
                    raise self._mesh_error(
                        "components", semantic=semantic, expected=components
                    )
                if not all(_is_finite_number(value) for value in row):
                    raise self._mesh_error("component_value", semantic=semantic)
                converted.append([float(value) for value in row])
            values[semantic] = converted

        if not vertex_count:
            raise self._mesh_error("vertex_count")
        if vertex_count > self.max_vertices:
            raise QuantizerError(
                "QUANT_MESH_TOO_LARGE",
                {"vertex_count": vertex_count, "max_vertices": self.max_vertices},
            )

        indices_value = mesh.get("indices", [])
        if not isinstance(indices_value, (list, tuple)):
            raise self._mesh_error("indices")
        indices: list[int] = []
        for index in indices_value:
            if not _is_int(index) or index < 0 or index >= vertex_count:
                raise self._mesh_error("index", index=index)
            indices.append(index)
        return values, indices, vertex_count

    @staticmethod
    def _unwrap_uv(
        uv_rows: list[list[float]], indices: list[int]
    ) -> list[list[float]]:
        """Choose integer UV wraps per indexed connected component.

        Offsets are propagated over triangle edges.  Differences are rounded
        to the nearest integer, so a seam from 0.99 to 0.01 becomes 0.99 to
        1.01 rather than forcing an almost unit-wide quantisation range.
        """
        vertex_count = len(uv_rows)
        components = len(uv_rows[0]) if uv_rows else 0
        shifts = [[0 for _ in range(components)] for _ in range(vertex_count)]
        adjacency: list[list[list[tuple[int, int]]]] = [
            [[] for _ in range(components)] for _ in range(vertex_count)
        ]

        def add_edge(a: int, b: int, component: int) -> None:
            delta = int(round(
                uv_rows[a][component] - uv_rows[b][component]
            ))
            adjacency[a][component].append((b, delta))
            adjacency[b][component].append((a, -delta))

        for offset in range(0, len(indices) - 2, 3):
            a, b, c = indices[offset:offset + 3]
            for component in range(components):
                add_edge(a, b, component)
                add_edge(b, c, component)
                add_edge(c, a, component)

        visited: set[tuple[int, int]] = set()
        found_components: list[list[tuple[int, int]]] = []
        for root_vertex in range(vertex_count):
            for component in range(components):
                root = (root_vertex, component)
                if root in visited:
                    continue
                if not adjacency[root_vertex][component]:
                    visited.add(root)
                    found_components.append([root])
                    continue
                visited.add(root)
                queue: list[tuple[int, int]] = [root]
                members: list[tuple[int, int]] = []
                cursor = 0
                while cursor < len(queue):
                    vertex, comp = queue[cursor]
                    cursor += 1
                    members.append((vertex, comp))
                    for neighbor, delta in sorted(adjacency[vertex][comp]):
                        candidate = shifts[vertex][comp] + delta
                        key = (neighbor, comp)
                        if key in visited:
                            continue
                        shifts[neighbor][comp] = candidate
                        visited.add(key)
                        queue.append(key)
                found_components.append(members)

        if found_components:
            anchor = min(
                uv_rows[vertex][component] + shifts[vertex][component]
                for vertex, component in found_components[0]
            )
            for members in found_components[1:]:
                component_min = min(
                    uv_rows[vertex][component] + shifts[vertex][component]
                    for vertex, component in members
                )
                delta = int(round(anchor - component_min))
                if delta:
                    for vertex, component in members:
                        shifts[vertex][component] += delta

        return [
            [uv_rows[v][c] + shifts[v][c] for c in range(components)]
            for v in range(vertex_count)
        ]

    @staticmethod
    def _encoded_fingerprint(encoded_without_fingerprint: dict) -> str:
        payload = _canonical_json(encoded_without_fingerprint).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _quantise_component(
        values: list[float], bits: int
    ) -> tuple[float, float, list[int]]:
        low = min(values)
        high = max(values)
        maximum = (1 << bits) - 1
        if high == low:
            return low, high, [0] * len(values)
        scale = maximum / (high - low)
        quantised = [
            min(maximum, max(0, math.floor((value - low) * scale + 0.5)))
            for value in values
        ]
        return low, high, quantised

    @staticmethod
    def _cache_key(
        layout_id: str, version: int, spec: dict[str, dict[str, Any]],
        values: dict[str, list[list[float]]], indices: list[int], wrap_uv: bool,
    ) -> tuple[str, ...]:
        layout_signature = hashlib.sha256(
            _canonical_json(
                {
                    "layout_id": layout_id,
                    "version": version,
                    "spec": spec,
                }
            ).encode("utf-8")
        ).hexdigest()
        mesh_digest = hashlib.sha256(
            _canonical_json(
                {
                    "attributes": {
                        semantic: values[semantic] for semantic in sorted(values)
                    },
                    "indices": indices,
                }
            ).encode("utf-8")
        ).hexdigest()
        return (
            layout_signature,
            "wrap" if wrap_uv else "linear",
            mesh_digest,
        )

    def quantize_mesh(self, mesh: dict, layout_id: str, *, wrap_uv: bool = False) -> dict:
        with self._lock:
            version, spec = self._resolve_layout(layout_id, None)
            try:
                values, indices, vertex_count = self._validate_mesh(mesh, spec)
            except QuantizerError as exc:
                self._reject(exc.code, exc.detail)

            cache_key = self._cache_key(
                layout_id, version, spec, values, indices, bool(wrap_uv)
            )
            cached = self._cache.get(cache_key)
            if cached is not None:
                _, encoded = cached
                self._cache[cache_key] = (self._next_cache_stamp(), encoded)
                self._cache.move_to_end(cache_key)
                self._stats["meshes_quantized"] += 1
                self._stats["cache_hits"] += 1
                return copy.deepcopy(encoded)

            if bool(wrap_uv):
                values = dict(values)
                uv_indices = indices or list(range(vertex_count))
                for semantic, rule in spec.items():
                    if rule["kind"] == "uv":
                        values[semantic] = self._unwrap_uv(
                            values[semantic], uv_indices
                        )

            data: dict[str, list[list[int]]] = {}
            low: dict[str, list[float]] = {}
            high: dict[str, list[float]] = {}
            bits: dict[str, int] = {}
            for semantic in sorted(spec):
                rule = spec[semantic]
                component_count = _KIND_COMPONENTS[rule["kind"]]
                rows = values[semantic]
                semantic_low: list[float] = []
                semantic_high: list[float] = []
                semantic_data: list[list[int]] = [[] for _ in range(vertex_count)]
                for component in range(component_count):
                    column = [rows[v][component] for v in range(vertex_count)]
                    component_low, component_high, component_data = (
                        self._quantise_component(column, rule["bits"])
                    )
                    semantic_low.append(component_low)
                    semantic_high.append(component_high)
                    for vertex, value in enumerate(component_data):
                        semantic_data[vertex].append(value)
                data[semantic] = semantic_data
                low[semantic] = semantic_low
                high[semantic] = semantic_high
                bits[semantic] = rule["bits"]

            encoded: dict[str, Any] = {
                "layout_id": layout_id,
                "layout_version": version,
                "vertex_count": vertex_count,
                "bits": bits,
                "lo": low,
                "hi": high,
                "data": data,
                "indices": list(indices),
            }
            encoded["fingerprint"] = self._encoded_fingerprint(encoded)

            self._cache[cache_key] = (self._next_cache_stamp(), encoded)
            self._cache.move_to_end(cache_key)
            while len(self._cache) > _CACHE_MAX_ENTRIES:
                oldest = min(
                    self._cache,
                    key=lambda key: self._cache[key][0],
                )
                del self._cache[oldest]
            self._stats["meshes_quantized"] += 1
            return copy.deepcopy(encoded)

    def _validate_encoded(
        self, encoded: object
    ) -> tuple[str, int, dict[str, dict[str, Any]], int, dict, dict, dict, list[int]]:
        if not isinstance(encoded, dict):
            raise self._mesh_error("encoded")
        layout_id = encoded.get("layout_id")
        version = encoded.get("layout_version")
        if not isinstance(layout_id, str) or not _is_int(version):
            raise self._mesh_error("encoded_layout")

        versions = self._layouts.get(layout_id)
        if versions is None or version not in versions:
            raise QuantizerError(
                "QUANT_UNKNOWN_LAYOUT",
                {"layout_id": layout_id, "version": version},
            )
        spec = versions[version]

        vertex_count = encoded.get("vertex_count")
        if not _is_int(vertex_count) or vertex_count <= 0:
            raise self._mesh_error("vertex_count")

        bits = encoded.get("bits")
        if not isinstance(bits, dict) or set(bits) != set(spec):
            raise self._mesh_error("bits")
        for semantic, rule in spec.items():
            if bits[semantic] != rule["bits"]:
                raise self._mesh_error("bits", semantic=semantic)

        low = encoded.get("lo")
        high = encoded.get("hi")
        data = encoded.get("data")
        if not all(isinstance(item, dict) for item in (low, high, data)):
            raise self._mesh_error("payload")

        for semantic in sorted(spec):
            if set(low) != set(high) or set(low) != set(data):
                raise self._mesh_error("payload_semantics")
            components = _KIND_COMPONENTS[spec[semantic]["kind"]]
            if len(low[semantic]) != components or len(high[semantic]) != components:
                raise self._mesh_error("range", semantic=semantic)
            if not all(
                _is_finite_number(value)
                for value in list(low[semantic]) + list(high[semantic])
            ):
                raise self._mesh_error("range_value", semantic=semantic)
            if any(
                float(low[semantic][component]) > float(high[semantic][component])
                for component in range(components)
            ):
                raise self._mesh_error("range_order", semantic=semantic)
            rows = data[semantic]
            if not isinstance(rows, (list, tuple)) or len(rows) != vertex_count:
                raise self._mesh_error("data_rows", semantic=semantic)
            maximum = (1 << spec[semantic]["bits"]) - 1
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != components:
                    raise self._mesh_error("data_components", semantic=semantic)
                if not all(
                    _is_int(value) and 0 <= value <= maximum for value in row
                ):
                    raise self._mesh_error("data_value", semantic=semantic)

        indices_value = encoded.get("indices", [])
        if not isinstance(indices_value, (list, tuple)):
            raise self._mesh_error("indices")
        indices = []
        for index in indices_value:
            if not _is_int(index) or index < 0 or index >= vertex_count:
                raise self._mesh_error("index", index=index)
            indices.append(index)
        return layout_id, version, spec, vertex_count, low, high, data, indices

    def dequantize_mesh(self, encoded: dict) -> dict:
        with self._lock:
            try:
                (
                    layout_id,
                    version,
                    spec,
                    vertex_count,
                    low,
                    high,
                    data,
                    indices,
                ) = self._validate_encoded(encoded)
                attributes: dict[str, list[list[float]]] = {}
                for semantic in sorted(spec):
                    rule = spec[semantic]
                    components = _KIND_COMPONENTS[rule["kind"]]
                    maximum = (1 << rule["bits"]) - 1
                    rows: list[list[float]] = []
                    for vertex in range(vertex_count):
                        row: list[float] = []
                        for component in range(components):
                            quantised = data[semantic][vertex][component]
                            component_low = float(low[semantic][component])
                            component_high = float(high[semantic][component])
                            if component_high == component_low or maximum == 0:
                                value = component_low
                            else:
                                value = component_low + (
                                    (component_high - component_low)
                                    * quantised / maximum
                                )
                            row.append(value)
                        if rule["kind"] == "normal":
                            length = math.sqrt(sum(value * value for value in row))
                            if length > 0.0:
                                row = [value / length for value in row]
                        rows.append(row)
                    attributes[semantic] = rows
            except QuantizerError as exc:
                self._reject(exc.code, exc.detail)

            self._stats["meshes_dequantized"] += 1
            decoded: dict[str, Any] = {
                "layout_id": layout_id,
                "layout_version": version,
                "vertex_count": vertex_count,
                "attributes": attributes,
                "indices": list(indices),
            }
            return decoded

    def payload_bytes(self, encoded: dict) -> int:
        with self._lock:
            if not isinstance(encoded, dict):
                self._reject("QUANT_INVALID_MESH", {"reason": "encoded"})
            vertex_count = encoded.get("vertex_count")
            bits = encoded.get("bits")
            data = encoded.get("data")
            if (
                not _is_int(vertex_count)
                or vertex_count < 0
                or not isinstance(bits, dict)
                or not isinstance(data, dict)
            ):
                self._reject("QUANT_INVALID_MESH", {"reason": "payload"})

            total = 0
            for semantic, width in bits.items():
                if not _is_int(width) or width < 0:
                    self._reject(
                        "QUANT_INVALID_MESH",
                        {"reason": "bits", "semantic": semantic},
                    )
                rows = data.get(semantic)
                if not isinstance(rows, (list, tuple)) or not rows:
                    self._reject(
                        "QUANT_INVALID_MESH",
                        {"reason": "data", "semantic": semantic},
                    )
                first_row = rows[0]
                if not isinstance(first_row, (list, tuple)):
                    self._reject(
                        "QUANT_INVALID_MESH",
                        {"reason": "data", "semantic": semantic},
                    )
                total += width * len(first_row)
            return ((total + 7) // 8) * vertex_count

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
                "version": _SNAPSHOT_VERSION,
                "layouts": {
                    layout_id: {
                        str(version): copy.deepcopy(spec)
                        for version, spec in versions.items()
                    }
                    for layout_id, versions in self._layouts.items()
                },
                "current": copy.deepcopy(self._current),
                "stats": copy.deepcopy(self._stats),
            }

    def _restore_layouts(
        self, state: dict
    ) -> tuple[dict[str, dict[int, dict[str, dict[str, Any]]]], dict[str, int], dict[str, int]]:
        if not isinstance(state, dict) or set(state) != {"version", "layouts", "current", "stats"}:
            raise QuantizerError("QUANT_SNAPSHOT_INVALID", {"reason": "keys"})
        if not _is_int(state.get("version")) or state["version"] != _SNAPSHOT_VERSION:
            raise QuantizerError("QUANT_SNAPSHOT_INVALID", {"reason": "version"})
        raw_layouts = state.get("layouts")
        raw_current = state.get("current")
        raw_stats = state.get("stats")
        if not isinstance(raw_layouts, dict) or not isinstance(raw_current, dict):
            raise QuantizerError("QUANT_SNAPSHOT_INVALID", {"reason": "layouts"})
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(STATS_KEYS):
            raise QuantizerError("QUANT_SNAPSHOT_INVALID", {"reason": "stats"})
        stats: dict[str, int] = {}
        for key in STATS_KEYS:
            value = raw_stats[key]
            if not _is_int(value) or value < 0:
                raise QuantizerError(
                    "QUANT_SNAPSHOT_INVALID", {"reason": "stats", "key": key}
                )
            stats[key] = value

        layouts: dict[str, dict[int, dict[str, dict[str, Any]]]] = {}
        for layout_id, versions_value in raw_layouts.items():
            if (
                not isinstance(layout_id, str)
                or not layout_id
                or not isinstance(versions_value, dict)
                or not versions_value
            ):
                raise QuantizerError(
                    "QUANT_SNAPSHOT_INVALID", {"reason": "layout", "layout_id": layout_id}
                )
            versions: dict[int, dict[str, dict[str, Any]]] = {}
            for raw_version, raw_spec in versions_value.items():
                if _is_int(raw_version):
                    version = raw_version
                elif isinstance(raw_version, str) and raw_version.isdigit():
                    version = int(raw_version)
                else:
                    raise QuantizerError(
                        "QUANT_SNAPSHOT_INVALID", {"reason": "version_key"}
                    )
                if version < 1 or version in versions:
                    raise QuantizerError(
                        "QUANT_SNAPSHOT_INVALID", {"reason": "version_key"}
                    )
                try:
                    canonical, _ = self._validate_spec(raw_spec)
                except QuantizerError as exc:
                    raise QuantizerError(
                        "QUANT_SNAPSHOT_INVALID",
                        {"reason": "spec", "detail": exc.detail},
                    ) from exc
                versions[version] = canonical
            layouts[layout_id] = versions

        if set(raw_current) != set(layouts):
            raise QuantizerError("QUANT_SNAPSHOT_INVALID", {"reason": "current"})
        current: dict[str, int] = {}
        for layout_id, raw_version in raw_current.items():
            if _is_int(raw_version):
                version = raw_version
            elif isinstance(raw_version, str) and raw_version.isdigit():
                version = int(raw_version)
            else:
                raise QuantizerError(
                    "QUANT_SNAPSHOT_INVALID", {"reason": "current_version"}
                )
            if version not in layouts[layout_id]:
                raise QuantizerError(
                    "QUANT_SNAPSHOT_INVALID", {"reason": "current_version"}
                )
            if version != max(layouts[layout_id]):
                raise QuantizerError(
                    "QUANT_SNAPSHOT_INVALID", {"reason": "current_version"}
                )
            current[layout_id] = version
        return layouts, current, stats

    def restore(self, state: dict) -> None:
        with self._lock:
            layouts, current, stats = self._restore_layouts(state)
            self._layouts = layouts
            self._current = current
            self._stats = stats
            self._cache.clear()
