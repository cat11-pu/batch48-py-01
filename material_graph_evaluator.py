"""Material node graph topological evaluation with versioned caching."""

from __future__ import annotations

import copy
import math
import re
import struct
import sys
import threading
from collections import deque

if sys.getrecursionlimit() < 20000:
    sys.setrecursionlimit(20000)


class MaterialGraphError(ValueError):
    """Raised when a material graph cannot be built or evaluated."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_NODE_ID_RE = re.compile(r"[A-Za-z0-9_]+\Z")

_OP_PORTS: dict[str, tuple[str, ...]] = {
    "add": ("a", "b"),
    "mul": ("a", "b"),
    "dot": ("a", "b"),
    "scale": ("v", "s"),
    "mix": ("a", "b", "t"),
    "clamp": ("v", "lo", "hi"),
    "neg": ("a",),
    "vec2": ("x", "y"),
    "vec3": ("x", "y", "z"),
    "vec4": ("x", "y", "z", "w"),
    "length": ("v",),
}

_PORT_DEFAULTS: dict[str, dict[str, object]] = {
    "add": {"a": 0.0, "b": 0.0},
    "mul": {"a": 1.0, "b": 1.0},
    "dot": {"a": 0.0, "b": 0.0},
    "scale": {"v": 0.0, "s": 1.0},
    "mix": {"a": 0.0, "b": 0.0, "t": 0.0},
    "clamp": {"v": 0.0, "lo": 0.0, "hi": 1.0},
    "neg": {"a": 0.0},
    "vec2": {"x": 0.0, "y": 0.0},
    "vec3": {"x": 0.0, "y": 0.0, "z": 0.0},
    "vec4": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 0.0},
    "length": {"v": 0.0},
}

_STAT_KEYS = ("evaluations", "cache_hits", "folds", "invalidations")


def _f32(value: float) -> float:
    """Round one scalar operation result to IEEE-754 binary32."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _freeze(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, dict):
        return {key: _freeze(item) for key, item in value.items()}
    return copy.deepcopy(value)


def _to_data(value: object) -> object:
    if isinstance(value, (list, tuple)):
        return [_to_data(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_data(item) for key, item in value.items()}
    return copy.deepcopy(value)


def _reference(value: object) -> tuple[str, str] | None:
    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and isinstance(value[0], str)
        and isinstance(value[1], str)
    ):
        return value[0], value[1]
    return None


class MaterialGraphEvaluator:
    """Evaluate scalar and vector material nodes with constant folding."""

    def __init__(self, *, max_nodes: int = 4096, max_inputs: int = 16,
                 max_depth: int = 512) -> None:
        self.max_nodes = max_nodes
        self.max_inputs = max_inputs
        self.max_depth = max_depth
        self._lock = threading.RLock()
        self._version = 1
        self._nodes: dict[str, dict] = {}
        self._dependents: dict[str, set[str]] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    def add_node(self, node_id: str, op: str, inputs: dict | None = None) -> dict:
        with self._lock:
            self._validate_node_id(node_id)
            if node_id in self._nodes:
                raise MaterialGraphError(
                    "MATERIAL_DUPLICATE_NODE", {"node": node_id}
                )
            if len(self._nodes) >= self.max_nodes:
                raise MaterialGraphError(
                    "MATERIAL_NODE_LIMIT",
                    {"node": node_id, "max_nodes": self.max_nodes},
                )
            if op not in _OP_PORTS:
                raise MaterialGraphError(
                    "MATERIAL_OP_UNSUPPORTED", {"node": node_id, "op": op}
                )

            raw_inputs = {} if inputs is None else inputs
            if not isinstance(raw_inputs, dict):
                raise MaterialGraphError(
                    "MATERIAL_INVALID_INPUTS",
                    {"node": node_id, "inputs_type": type(raw_inputs).__name__},
                )
            if len(raw_inputs) > self.max_inputs:
                raise MaterialGraphError(
                    "MATERIAL_INPUT_LIMIT",
                    {"node": node_id, "max_inputs": self.max_inputs},
                )

            frozen_inputs: dict[str, object] = {}
            for port, value in raw_inputs.items():
                if port not in _OP_PORTS[op]:
                    raise MaterialGraphError(
                        "MATERIAL_UNKNOWN_PORT",
                        {"node": node_id, "port": port},
                    )
                ref = _reference(value)
                if ref is not None:
                    source_id, source_port = ref
                    self._require_node(source_id)
                    if source_port != "out":
                        raise MaterialGraphError(
                            "MATERIAL_UNKNOWN_PORT",
                            {"node": source_id, "port": source_port},
                        )
                frozen_inputs[port] = _freeze(value)

            node = self._new_node(node_id, op, frozen_inputs)
            self._nodes[node_id] = node
            self._rebuild_dependents_locked()
            self._version += 1
            return self._node_view(node)

    def connect(self, node_id: str, port: str, source_node: str,
                source_port: str = "out") -> dict:
        with self._lock:
            node = self._require_node(node_id)
            self._require_node(source_node)
            if port not in _OP_PORTS[node["op"]]:
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_PORT",
                    {"node": node_id, "port": port},
                )
            if source_port != "out":
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_PORT",
                    {"node": source_node, "port": source_port},
                )

            cycle = self._cycle_path_locked(source_node, node_id)
            if cycle is not None:
                raise MaterialGraphError("MATERIAL_CYCLE", cycle)

            if port not in node["inputs"] and len(node["inputs"]) >= self.max_inputs:
                raise MaterialGraphError(
                    "MATERIAL_INPUT_LIMIT",
                    {"node": node_id, "port": port,
                     "max_inputs": self.max_inputs},
                )

            node["inputs"][port] = (source_node, source_port)
            node["version"] += 1
            self._version += 1
            self._rebuild_dependents_locked()
            affected = self._invalidate_closure_locked(node_id)
            self._stats["invalidations"] += affected
            return self._node_view(node)

    def set_input(self, node_id: str, name: str, value) -> dict:
        with self._lock:
            node = self._require_node(node_id)
            if name not in _OP_PORTS[node["op"]]:
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_PORT",
                    {"node": node_id, "port": name},
                )
            if name not in node["inputs"] and len(node["inputs"]) >= self.max_inputs:
                raise MaterialGraphError(
                    "MATERIAL_INPUT_LIMIT",
                    {"node": node_id, "port": name,
                     "max_inputs": self.max_inputs},
                )

            node["inputs"][name] = _freeze(value)
            node["version"] += 1
            self._version += 1
            self._rebuild_dependents_locked()
            affected = self._invalidate_closure_locked(node_id)
            self._stats["invalidations"] += affected
            return self._node_view(node)

    def evaluate(self, node_id: str):
        with self._lock:
            node = self._require_node(node_id)
            if node["valid"]:
                self._stats["cache_hits"] += 1
                return self._public_value(node["cache"])
            depth = self._longest_depth_locked(node_id, {}, set())
            if depth > self.max_depth:
                raise MaterialGraphError(
                    "MATERIAL_DEPTH_LIMIT",
                    {"node": node_id, "depth": depth,
                     "max_depth": self.max_depth},
                )
            value = self._evaluate_locked(node_id, set())
            return self._public_value(value)

    def evaluate_all(self) -> dict:
        with self._lock:
            results = {}
            for node_id in sorted(self._nodes):
                results[node_id] = self.evaluate(node_id)
            return results

    def fold_constants(self) -> int:
        with self._lock:
            candidates: list[tuple[str, object]] = []
            for node_id in sorted(self._nodes):
                node = self._nodes[node_id]
                if node["folded"] or self._has_reference_input(node["inputs"]):
                    continue
                value = self._evaluate_locked(node_id, set())
                candidates.append((node_id, value))

            folded = 0
            for node_id, value in candidates:
                node = self._nodes[node_id]
                if not node["folded"]:
                    node["folded"] = True
                    node["valid"] = True
                    node["cache"] = value
                    folded += 1

            self._stats["folds"] += folded
            return folded

    def invalidate(self, node_id: str) -> int:
        with self._lock:
            self._require_node(node_id)
            affected = self._invalidate_closure_locked(node_id)
            self._version += 1
            self._stats["invalidations"] += affected
            return affected

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            nodes = {}
            for node_id, node in self._nodes.items():
                nodes[node_id] = {
                    "id": node["id"],
                    "op": node["op"],
                    "inputs": _to_data(node["inputs"]),
                    "version": node["version"],
                    "folded": node["folded"],
                    "cache_valid": node["valid"],
                    "cache": _to_data(node["cache"]),
                }
            return copy.deepcopy({
                "version": self._version,
                "nodes": nodes,
                "stats": dict(self._stats),
            })

    def restore(self, state: dict) -> None:
        with self._lock:
            candidate = self._parse_snapshot(state)
            self._version = candidate["version"]
            self._nodes = candidate["nodes"]
            self._stats = candidate["stats"]
            self._rebuild_dependents_locked()

    def _new_node(self, node_id: str, op: str, inputs: dict) -> dict:
        return {
            "id": node_id,
            "op": op,
            "inputs": inputs,
            "version": 1,
            "valid": False,
            "cache": None,
            "folded": False,
        }

    def _parse_snapshot(self, state: object) -> dict:
        try:
            if not isinstance(state, dict):
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID",
                    {"reason": "snapshot must be a dict"},
                )
            if set(state.keys()) != {"version", "nodes", "stats"}:
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID",
                    {"reason": "invalid snapshot keys"},
                )

            version = state["version"]
            if not self._is_int(version) or version < 1:
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID", {"key": "version"}
                )

            raw_stats = state["stats"]
            if not isinstance(raw_stats, dict):
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID", {"key": "stats"}
                )
            if set(raw_stats.keys()) != set(_STAT_KEYS):
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID", {"key": "stats"}
                )
            stats = {}
            for key in _STAT_KEYS:
                value = raw_stats[key]
                if not self._is_int(value) or value < 0:
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID", {"key": f"stats.{key}"}
                    )
                stats[key] = value

            raw_nodes = state["nodes"]
            if not isinstance(raw_nodes, dict):
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID", {"key": "nodes"}
                )
            if len(raw_nodes) > self.max_nodes:
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID", {"reason": "node limit"}
                )

            nodes: dict[str, dict] = {}
            for raw_id, raw_node in raw_nodes.items():
                node_id = raw_id
                self._validate_node_id(node_id, snapshot=True)
                if node_id in nodes or not isinstance(raw_node, dict):
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID", {"node": node_id}
                    )

                supplied_id = raw_node.get("id", node_id)
                if supplied_id != node_id:
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID",
                        {"node": node_id, "key": "id"},
                    )

                op = raw_node.get("op")
                if op not in _OP_PORTS:
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID",
                        {"node": node_id, "key": "op"},
                    )

                raw_inputs = raw_node.get("inputs", {})
                if not isinstance(raw_inputs, dict):
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID",
                        {"node": node_id, "key": "inputs"},
                    )
                if len(raw_inputs) > self.max_inputs:
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID",
                        {"node": node_id, "reason": "input limit"},
                    )

                inputs = {}
                for port, value in raw_inputs.items():
                    if not isinstance(port, str) or port not in _OP_PORTS[op]:
                        raise MaterialGraphError(
                            "MATERIAL_SNAPSHOT_INVALID",
                            {"node": node_id, "port": port},
                        )
                    ref = _reference(value)
                    if ref is not None:
                        source_id, source_port = ref
                        if source_id not in raw_nodes or source_port != "out":
                            raise MaterialGraphError(
                                "MATERIAL_SNAPSHOT_INVALID",
                                {"node": node_id, "port": port},
                            )
                    inputs[port] = _freeze(copy.deepcopy(value))

                node_version = raw_node.get("version", 1)
                if not self._is_int(node_version) or node_version < 1:
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID",
                        {"node": node_id, "key": "version"},
                    )

                folded = raw_node.get("folded", False)
                if not isinstance(folded, bool):
                    raise MaterialGraphError(
                        "MATERIAL_SNAPSHOT_INVALID",
                        {"node": node_id, "key": "folded"},
                    )

                cache = None
                valid = False
                if raw_node.get("cache_valid", False):
                    cache = self._cache_value(raw_node.get("cache"))
                    if cache is not None:
                        valid = True

                nodes[node_id] = self._new_node(node_id, op, inputs)
                nodes[node_id].update({
                    "version": node_version,
                    "valid": valid,
                    "cache": cache,
                    "folded": folded and not self._has_reference_input(inputs),
                })

            cycle = self._snapshot_cycle(nodes)
            if cycle is not None:
                raise MaterialGraphError(
                    "MATERIAL_SNAPSHOT_INVALID", {"cycle": cycle}
                )

            return {"version": version, "nodes": nodes, "stats": stats}
        except MaterialGraphError as exc:
            if exc.code == "MATERIAL_SNAPSHOT_INVALID":
                raise
            raise MaterialGraphError(
                "MATERIAL_SNAPSHOT_INVALID", {"reason": exc.code}
            ) from exc
        except Exception as exc:
            raise MaterialGraphError(
                "MATERIAL_SNAPSHOT_INVALID", {"reason": "invalid structure"}
            ) from exc

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _cache_value(self, value: object) -> object | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            try:
                return _f32(float(value))
            except (TypeError, ValueError, OverflowError):
                return None
        if isinstance(value, (list, tuple)) and value and all(
            not isinstance(item, bool) and isinstance(item, (int, float))
            for item in value
        ):
            try:
                return tuple(_f32(float(item)) for item in value)
            except (TypeError, ValueError, OverflowError):
                return None
        return None

    def _validate_node_id(self, node_id: object, *,
                          snapshot: bool = False) -> None:
        if not isinstance(node_id, str) or not _NODE_ID_RE.fullmatch(node_id):
            code = ("MATERIAL_SNAPSHOT_INVALID" if snapshot
                    else "MATERIAL_INVALID_NODE")
            raise MaterialGraphError(code, {"node": node_id})

    def _require_node(self, node_id: object) -> dict:
        if not isinstance(node_id, str) or node_id not in self._nodes:
            raise MaterialGraphError(
                "MATERIAL_UNKNOWN_NODE", {"node": node_id}
            )
        return self._nodes[node_id]

    def _node_view(self, node: dict) -> dict:
        return {
            "id": node["id"],
            "op": node["op"],
            "inputs": _to_data(node["inputs"]),
            "version": node["version"],
        }

    def _rebuild_dependents_locked(self) -> None:
        dependents: dict[str, set[str]] = {
            node_id: set() for node_id in self._nodes
        }
        for node_id, node in self._nodes.items():
            for value in node["inputs"].values():
                ref = _reference(value)
                if ref is not None and ref[0] in self._nodes:
                    dependents.setdefault(ref[0], set()).add(node_id)
        self._dependents = dependents

    def _reference_targets(self, node: dict) -> list[str]:
        targets = []
        for value in node["inputs"].values():
            ref = _reference(value)
            if ref is not None:
                targets.append(ref[0])
        return targets

    def _cycle_path_locked(self, source_node: str,
                           target_node: str) -> list[str] | None:
        if source_node == target_node:
            return [target_node, source_node]

        queue: deque[tuple[str, list[str]]] = deque([
            (source_node, [source_node])
        ])
        visited = {source_node}
        while queue:
            current, path = queue.popleft()
            current_node = self._nodes.get(current)
            if current_node is None:
                continue
            for dependency in self._reference_targets(current_node):
                if dependency == target_node:
                    return [target_node] + path + [target_node]
                if dependency in self._nodes and dependency not in visited:
                    visited.add(dependency)
                    queue.append((dependency, path + [dependency]))
        return None

    def _snapshot_cycle(self, nodes: dict[str, dict]) -> list[str] | None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node_id: str, path: list[str]) -> list[str] | None:
            if node_id in visiting:
                return path[path.index(node_id):] + [node_id]
            if node_id in visited or node_id not in nodes:
                return None
            visiting.add(node_id)
            path.append(node_id)
            for dependency in self._reference_targets(nodes[node_id]):
                cycle = visit(dependency, path)
                if cycle is not None:
                    return cycle
            path.pop()
            visiting.remove(node_id)
            visited.add(node_id)
            return None

        for node_id in sorted(nodes):
            cycle = visit(node_id, [])
            if cycle is not None:
                return cycle
        return None

    @staticmethod
    def _has_reference_input(inputs: dict) -> bool:
        return any(_reference(value) is not None for value in inputs.values())

    def _invalidate_closure_locked(self, node_id: str) -> int:
        affected = set()
        queue = deque([node_id])
        while queue:
            current = queue.popleft()
            if current in affected:
                continue
            affected.add(current)
            node = self._nodes.get(current)
            if node is not None:
                node["valid"] = False
                node["cache"] = None
                node["folded"] = False
            for dependent in self._dependents.get(current, ()):
                if dependent not in affected:
                    queue.append(dependent)
        return len(affected)

    def _longest_depth_locked(
        self,
        node_id: str,
        memo: dict[str, int],
        active: set[str],
    ) -> int:
        if node_id in memo:
            return memo[node_id]
        if node_id in active:
            raise MaterialGraphError("MATERIAL_CYCLE", sorted(active))
        node = self._nodes.get(node_id)
        if node is None:
            raise MaterialGraphError(
                "MATERIAL_UNKNOWN_NODE", {"node": node_id}
            )

        active.add(node_id)
        depth = 1
        for source_id, source_port in self._checked_reference_items(node):
            depth = max(
                depth,
                1 + self._longest_depth_locked(
                    source_id, memo, active
                ),
            )
        active.remove(node_id)
        memo[node_id] = depth
        return depth

    def _checked_reference_items(
        self, node: dict
    ) -> list[tuple[str, str]]:
        refs = []
        node_id = node["id"]
        for value in node["inputs"].values():
            ref = _reference(value)
            if ref is None:
                continue
            source_id, source_port = ref
            if source_id not in self._nodes:
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_NODE",
                    {"node": source_id, "referenced_by": node_id},
                )
            if source_port != "out":
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_PORT",
                    {"node": source_id, "port": source_port},
                )
            refs.append((source_id, source_port))
        return refs

    def _evaluate_locked(self, node_id: str, active: set[str]):
        node = self._nodes[node_id]
        if node["valid"]:
            self._stats["cache_hits"] += 1
            return node["cache"]
        if node_id in active:
            raise MaterialGraphError("MATERIAL_CYCLE", sorted(active))

        active.add(node_id)
        try:
            values = {}
            for port in _OP_PORTS[node["op"]]:
                values[port] = self._resolve_input_locked(
                    node, port, active
                )
            result = self._apply_op_locked(node, values)
            self._stats["evaluations"] += 1
            node["cache"] = result
            node["valid"] = True
            return result
        finally:
            active.discard(node_id)

    def _resolve_input_locked(self, node: dict, port: str,
                              active: set[str]):
        value = node["inputs"].get(
            port, _PORT_DEFAULTS[node["op"]][port]
        )
        ref = _reference(value)
        if ref is not None:
            source_id, source_port = ref
            if source_id not in self._nodes:
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_NODE",
                    {"node": source_id, "referenced_by": node["id"]},
                )
            if source_port != "out":
                raise MaterialGraphError(
                    "MATERIAL_UNKNOWN_PORT",
                    {"node": source_id, "port": source_port},
                )
            return self._evaluate_locked(source_id, active)
        return self._coerce_value(value, node["id"], port)

    def _coerce_value(self, value: object, node_id: str, port: str):
        if isinstance(value, bool):
            return _f32(float(value))
        if isinstance(value, (int, float)):
            try:
                number = float(value)
            except OverflowError:
                number = math.copysign(math.inf, 1 if value > 0 else -1)
            return _f32(number)
        if isinstance(value, (list, tuple)):
            if not value:
                self._dim_error(node_id, port)
            components = []
            for component in value:
                if isinstance(component, bool) or not isinstance(
                    component, (int, float)
                ):
                    self._dim_error(node_id, port)
                components.append(_f32(float(component)))
            return tuple(components)
        self._dim_error(node_id, port)

    @staticmethod
    def _dim_error(node_id: str, port: str, detail: dict | None = None):
        error_detail = {"node": node_id, "port": port}
        if detail:
            error_detail.update(detail)
        raise MaterialGraphError("MATERIAL_DIM_MISMATCH", error_detail)

    def _same_shape(self, left, right, node_id: str, port: str) -> None:
        left_scalar = isinstance(left, float)
        right_scalar = isinstance(right, float)
        if left_scalar != right_scalar:
            self._dim_error(node_id, port, {
                "left": self._shape_name(left),
                "right": self._shape_name(right),
            })
        if not left_scalar and len(left) != len(right):
            self._dim_error(node_id, port, {
                "left_dim": len(left),
                "right_dim": len(right),
            })

    @staticmethod
    def _shape_name(value) -> str:
        return "scalar" if isinstance(value, float) else f"vec{len(value)}"

    def _as_scalar(self, value, node_id: str, port: str) -> float:
        if not isinstance(value, float):
            self._dim_error(node_id, port, {"expected": "scalar"})
        return value

    def _as_vector(self, value, node_id: str,
                   port: str) -> tuple[float, ...]:
        if not isinstance(value, tuple):
            self._dim_error(node_id, port, {"expected": "vector"})
        return value

    @staticmethod
    def _identity_like(value, identity: float):
        if isinstance(value, tuple):
            return (identity,) * len(value)
        return identity

    def _apply_op_locked(self, node: dict, values: dict[str, object]):
        op = node["op"]
        node_id = node["id"]

        if op in {"add", "mul"}:
            left = values["a"]
            right = values["b"]
            identity = 1.0 if op == "mul" else 0.0
            if "a" not in node["inputs"]:
                left = self._identity_like(right, identity)
            if "b" not in node["inputs"]:
                right = self._identity_like(left, identity)
            self._same_shape(left, right, node_id, "b")
            if isinstance(left, float):
                if op == "add":
                    return _f32(left + right)
                return _f32(left * right)
            if op == "add":
                return tuple(_f32(x + y) for x, y in zip(left, right))
            return tuple(_f32(x * y) for x, y in zip(left, right))

        if op == "dot":
            left = values["a"]
            right = values["b"]
            if "a" not in node["inputs"]:
                left = self._identity_like(right, 0.0)
            if "b" not in node["inputs"]:
                right = self._identity_like(left, 0.0)
            self._same_shape(left, right, node_id, "b")
            if isinstance(left, float):
                return _f32(left * right)
            total = 0.0
            for x, y in zip(left, right):
                total = _f32(total + _f32(x * y))
            return total

        if op == "scale":
            vector = self._as_vector(values["v"], node_id, "v")
            scalar = self._as_scalar(values["s"], node_id, "s")
            return tuple(_f32(component * scalar) for component in vector)

        if op == "mix":
            left = values["a"]
            right = values["b"]
            if "a" not in node["inputs"]:
                left = self._identity_like(right, 0.0)
            if "b" not in node["inputs"]:
                right = self._identity_like(left, 0.0)
            self._same_shape(left, right, node_id, "b")
            factor = self._as_scalar(values["t"], node_id, "t")
            if isinstance(left, float):
                delta = _f32(right - left)
                return _f32(left + _f32(delta * factor))
            result = []
            for x, y in zip(left, right):
                delta = _f32(y - x)
                result.append(_f32(x + _f32(delta * factor)))
            return tuple(result)

        if op == "clamp":
            value = values["v"]
            low = values["lo"]
            high = values["hi"]
            if isinstance(value, float):
                low = self._as_scalar(low, node_id, "lo")
                high = self._as_scalar(high, node_id, "hi")
                return _f32(min(high, max(low, value)))

            vector = self._as_vector(value, node_id, "v")
            if isinstance(low, float):
                lows = (low,) * len(vector)
            else:
                lows = self._as_vector(low, node_id, "lo")
                if len(lows) != len(vector):
                    self._dim_error(node_id, "lo", {"dim": len(lows)})
            if isinstance(high, float):
                highs = (high,) * len(vector)
            else:
                highs = self._as_vector(high, node_id, "hi")
                if len(highs) != len(vector):
                    self._dim_error(node_id, "hi", {"dim": len(highs)})
            return tuple(
                _f32(min(hi, max(lo, component)))
                for component, lo, hi in zip(vector, lows, highs)
            )

        if op == "neg":
            value = values["a"]
            if isinstance(value, float):
                return _f32(-value)
            return tuple(_f32(-component) for component in value)

        if op in {"vec2", "vec3", "vec4"}:
            components = []
            for port in _OP_PORTS[op]:
                components.append(
                    self._as_scalar(values[port], node_id, port)
                )
            return tuple(components)

        if op == "length":
            value = values["v"]
            if isinstance(value, float):
                return _f32(abs(value))
            vector = self._as_vector(value, node_id, "v")
            total = 0.0
            for component in vector:
                total = _f32(total + _f32(component * component))
            return _f32(math.sqrt(total))

        raise MaterialGraphError(
            "MATERIAL_OP_UNSUPPORTED", {"node": node_id, "op": op}
        )

    @staticmethod
    def _public_value(value: object):
        if isinstance(value, tuple):
            return list(value)
        return value
