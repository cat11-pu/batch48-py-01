"""Material inheritance chain resolution for layered material instances."""

from __future__ import annotations

import copy
import numbers
import threading
from dataclasses import dataclass, field


class MaterialError(ValueError):
    """Raised for every rejected material graph operation."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_TYPE_WIDTH = {
    "scalar": 1,
    "vec2": 2,
    "vec3": 3,
    "vec4": 4,
}


def _is_number(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _normalize_value(type_name: str, value: object) -> object:
    """Return the immutable canonical representation for a declared value."""
    width = _TYPE_WIDTH[type_name]
    if width == 1:
        if not _is_number(value):
            raise MaterialError("MATERIAL_TYPE_MISMATCH", type_name)
        return float(value)

    if _is_number(value):
        return (float(value),) * width

    if not isinstance(value, (list, tuple)):
        raise MaterialError("MATERIAL_TYPE_MISMATCH", type_name)
    if len(value) != width:
        raise MaterialError("MATERIAL_TYPE_MISMATCH", type_name)
    if not all(_is_number(component) for component in value):
        raise MaterialError("MATERIAL_PARAM_INVALID", type_name)
    return tuple(float(component) for component in value)


def _validate_param_name(name: object) -> str:
    if not isinstance(name, str):
        raise MaterialError("MATERIAL_PARAM_INVALID", name)
    if len(name) > MaterialInheritanceResolver.MAX_PARAM_NAME:
        raise MaterialError("MATERIAL_PARAM_NAME_TOO_LONG", name)
    return name


@dataclass
class _Material:
    parent: str | None
    declared: dict[str, str] = field(default_factory=dict)
    values: dict[str, object] = field(default_factory=dict)
    overrides: dict[str, object] = field(default_factory=dict)
    frozen: bool = False


class MaterialInheritanceResolver:
    """Resolve parameter overrides across a material inheritance chain."""

    MAX_DEPTH = 32
    MAX_PARAM_NAME = 256
    MAX_MATERIALS = 20000
    SNAPSHOT_VERSION = 1

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._materials: dict[str, _Material] = {}

    def _get_locked(self, material_id: str) -> _Material:
        try:
            return self._materials[material_id]
        except (KeyError, TypeError):
            raise MaterialError("MATERIAL_UNKNOWN", material_id) from None

    def _chain_locked(self, material_id: str) -> list[str]:
        chain: list[str] = []
        seen: set[str] = set()
        current: str | None = material_id
        while current is not None:
            if current in seen:
                raise MaterialError("MATERIAL_CYCLE", chain + [current])
            seen.add(current)
            chain.append(current)
            current = self._get_locked(current).parent
        chain.reverse()
        return chain

    def _types_locked(self, chain: list[str]) -> dict[str, str]:
        types: dict[str, str] = {}
        for material_id in chain:
            for name, type_name in self._get_locked(material_id).declared.items():
                existing = types.get(name)
                if existing is not None and existing != type_name:
                    raise MaterialError("MATERIAL_TYPE_MISMATCH", name)
                types[name] = type_name
        return types

    def _resolved_locked(
        self, material_id: str
    ) -> tuple[list[str], dict[str, str], dict[str, object], dict[str, str]]:
        chain = self._chain_locked(material_id)
        types = self._types_locked(chain)
        values: dict[str, object] = {}
        origins: dict[str, str] = {}

        for name in sorted(types):
            for ancestor in reversed(chain):
                node = self._materials[ancestor]
                if name in node.overrides:
                    values[name] = node.overrides[name]
                    origins[name] = ancestor
                    break
                if name in node.values:
                    values[name] = node.values[name]
                    origins[name] = ancestor
                    break
            else:
                # A declaration without a value is an invalid graph state. It
                # cannot be produced through the public API.
                raise MaterialError("MATERIAL_PARAM_INVALID", name)

        return chain, types, values, origins

    def _parse_definition_params(
        self,
        params: object,
        inherited_types: dict[str, str] | None,
    ) -> tuple[dict[str, str], dict[str, object]]:
        if not isinstance(params, dict):
            raise MaterialError("MATERIAL_PARAM_INVALID", params)

        declared: dict[str, str] = {}
        values: dict[str, object] = {}
        for raw_name, spec in params.items():
            name = _validate_param_name(raw_name)
            if not isinstance(spec, dict) or "value" not in spec:
                raise MaterialError("MATERIAL_PARAM_INVALID", name)

            if inherited_types is None:
                type_name = spec.get("type")
                if not isinstance(type_name, str) or type_name not in _TYPE_WIDTH:
                    raise MaterialError("MATERIAL_PARAM_INVALID", name)
                declared[name] = type_name
                values[name] = _normalize_value(type_name, spec["value"])
                continue

            if name not in inherited_types:
                raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
            type_name = inherited_types[name]
            if "type" in spec:
                given_type = spec["type"]
                if not isinstance(given_type, str) or given_type not in _TYPE_WIDTH:
                    raise MaterialError("MATERIAL_PARAM_INVALID", name)
                if given_type != type_name:
                    raise MaterialError("MATERIAL_TYPE_MISMATCH", name)
            values[name] = _normalize_value(type_name, spec["value"])

        return declared, values

    def _children_locked(self) -> dict[str | None, list[str]]:
        children: dict[str | None, list[str]] = {}
        for material_id, node in self._materials.items():
            children.setdefault(node.parent, []).append(material_id)
        return children

    def define(self, material_id: str, parent_id: str | None, params: dict) -> None:
        with self._lock:
            if material_id in self._materials:
                raise MaterialError("MATERIAL_DUPLICATE", material_id)
            if len(self._materials) >= self.MAX_MATERIALS:
                raise MaterialError("MATERIAL_TOO_MANY_MATERIALS", material_id)

            if parent_id is None:
                declared, values = self._parse_definition_params(params, None)
            else:
                if parent_id == material_id:
                    raise MaterialError("MATERIAL_CYCLE", [material_id, material_id])
                if parent_id not in self._materials:
                    raise MaterialError("MATERIAL_UNKNOWN", parent_id)
                parent_chain = self._chain_locked(parent_id)
                if len(parent_chain) + 1 > self.MAX_DEPTH:
                    raise MaterialError("MATERIAL_DEPTH_EXCEEDED", material_id)
                inherited_types = self._types_locked(parent_chain)
                declared, values = self._parse_definition_params(
                    params, inherited_types
                )

            self._materials[material_id] = _Material(
                parent=parent_id,
                declared=declared,
                values=values,
            )

    def reparent(self, material_id: str, parent_id: str) -> None:
        with self._lock:
            node = self._get_locked(material_id)
            if parent_id == material_id:
                raise MaterialError("MATERIAL_CYCLE", [material_id, material_id])
            if parent_id not in self._materials:
                raise MaterialError("MATERIAL_UNKNOWN", parent_id)
            if node.parent == parent_id:
                return

            path = [material_id]
            current: str | None = parent_id
            seen: set[str] = set()
            while current is not None:
                if current == material_id:
                    path.append(current)
                    raise MaterialError("MATERIAL_CYCLE", path)
                if current in seen:
                    raise MaterialError("MATERIAL_CYCLE", path + [current])
                seen.add(current)
                path.append(current)
                current = self._get_locked(current).parent

            new_depth = len(self._chain_locked(parent_id)) + 1
            if new_depth > self.MAX_DEPTH:
                raise MaterialError("MATERIAL_DEPTH_EXCEEDED", material_id)

            children = self._children_locked()
            stack: list[tuple[str, int]] = [(material_id, new_depth)]
            visited: set[str] = set()
            while stack:
                current_id, depth = stack.pop()
                if current_id in visited:
                    raise MaterialError("MATERIAL_CYCLE", [current_id, current_id])
                visited.add(current_id)
                if depth > self.MAX_DEPTH:
                    raise MaterialError("MATERIAL_DEPTH_EXCEEDED", material_id)
                for child_id in children.get(current_id, ()):
                    stack.append((child_id, depth + 1))

            inherited_types = self._types_locked(self._chain_locked(parent_id))
            old_types = self._types_locked(self._chain_locked(material_id))
            local_names = set(node.declared) | set(node.values) | set(node.overrides)
            for name in local_names:
                if name not in inherited_types:
                    raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
                if old_types.get(name) != inherited_types[name]:
                    raise MaterialError("MATERIAL_TYPE_MISMATCH", name)

            node.parent = parent_id
            node.declared = {}
            node.frozen = False

    def override(self, material_id: str, name: str, value) -> None:
        with self._lock:
            node = self._get_locked(material_id)
            name = _validate_param_name(name)
            types = self._types_locked(self._chain_locked(material_id))
            if name not in types:
                raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
            normalized = _normalize_value(types[name], value)
            node.overrides[name] = normalized

    def override_many(self, material_id: str, values: dict) -> None:
        with self._lock:
            node = self._get_locked(material_id)
            if not isinstance(values, dict):
                raise MaterialError("MATERIAL_PARAM_INVALID", values)
            types = self._types_locked(self._chain_locked(material_id))

            pending: dict[str, object] = {}
            for raw_name, value in values.items():
                name = _validate_param_name(raw_name)
                if name not in types:
                    raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
                pending[name] = _normalize_value(types[name], value)

            node.overrides.update(pending)

    def clear_override(self, material_id: str, name: str) -> None:
        with self._lock:
            node = self._get_locked(material_id)
            name = _validate_param_name(name)
            types = self._types_locked(self._chain_locked(material_id))
            if name not in types:
                raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
            node.overrides.pop(name, None)

    def resolve(self, material_id: str) -> dict:
        with self._lock:
            self._get_locked(material_id)
            _, _, values, origins = self._resolved_locked(material_id)
            sorted_names = sorted(values)
            return {
                "material": material_id,
                "params": {name: values[name] for name in sorted_names},
                "origins": {name: origins[name] for name in sorted_names},
            }

    def chain(self, material_id: str) -> list:
        with self._lock:
            self._get_locked(material_id)
            return self._chain_locked(material_id)

    def detach(self, material_id: str) -> None:
        with self._lock:
            node = self._get_locked(material_id)
            _, types, values, _ = self._resolved_locked(material_id)
            node.parent = None
            node.declared = dict(types)
            node.values = dict(values)
            node.overrides = {}
            node.frozen = True

    def coerce(self, material_id: str, name: str, value):
        with self._lock:
            self._get_locked(material_id)
            name = _validate_param_name(name)
            types = self._types_locked(self._chain_locked(material_id))
            if name not in types:
                raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
            return _normalize_value(types[name], value)

    def type_of(self, material_id: str, name: str) -> str:
        with self._lock:
            self._get_locked(material_id)
            name = _validate_param_name(name)
            types = self._types_locked(self._chain_locked(material_id))
            if name not in types:
                raise MaterialError("MATERIAL_PARAM_UNKNOWN", name)
            return types[name]

    def descendants(self, material_id: str) -> list:
        with self._lock:
            self._get_locked(material_id)
            children = self._children_locked()
            found: list[str] = []
            stack = list(children.get(material_id, ()))
            while stack:
                current = stack.pop()
                found.append(current)
                stack.extend(children.get(current, ()))
            return sorted(found)

    def snapshot(self) -> dict:
        with self._lock:
            materials: dict[str, dict] = {}
            for material_id in sorted(self._materials):
                node = self._materials[material_id]
                materials[material_id] = {
                    "parent": node.parent,
                    "declared": dict(node.declared),
                    "values": dict(node.values),
                    "overrides": dict(node.overrides),
                    "frozen": node.frozen,
                }
            return copy.deepcopy(
                {"version": self.SNAPSHOT_VERSION, "materials": materials}
            )

    def _nodes_from_snapshot(self, state: object) -> dict[str, _Material]:
        try:
            return self._nodes_from_snapshot_inner(state)
        except MaterialError as exc:
            if exc.code == "MATERIAL_SNAPSHOT_INVALID":
                raise
            raise MaterialError("MATERIAL_SNAPSHOT_INVALID", exc.detail) from None
        except Exception as exc:  # malformed containers must never leak out
            raise MaterialError("MATERIAL_SNAPSHOT_INVALID", repr(exc)) from None

    def _nodes_from_snapshot_inner(self, state: object) -> dict[str, _Material]:
        def invalid(detail: object = None) -> None:
            raise MaterialError("MATERIAL_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            invalid(state)
        version = state.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != self.SNAPSHOT_VERSION
        ):
            invalid(version)
        raw_materials = state.get("materials")
        if not isinstance(raw_materials, dict):
            invalid("materials")
        if len(raw_materials) > self.MAX_MATERIALS:
            invalid("materials")

        parents: dict[str, str | None] = {}
        declared: dict[str, dict[str, str]] = {}
        values: dict[str, dict[str, object]] = {}
        overrides: dict[str, dict[str, object]] = {}
        frozen: dict[str, bool] = {}

        for material_id, raw_node in raw_materials.items():
            if not isinstance(material_id, str) or not isinstance(raw_node, dict):
                invalid(material_id)
            for key in ("parent", "declared", "values", "overrides", "frozen"):
                if key not in raw_node:
                    invalid(material_id)

            parent = raw_node["parent"]
            if parent is not None and not isinstance(parent, str):
                invalid(material_id)
            if parent is not None and parent not in raw_materials:
                invalid(material_id)

            node_declared = raw_node["declared"]
            node_values = raw_node["values"]
            node_overrides = raw_node["overrides"]
            node_frozen = raw_node["frozen"]
            if not all(
                isinstance(item, dict)
                for item in (node_declared, node_values, node_overrides)
            ):
                invalid(material_id)
            if not isinstance(node_frozen, bool):
                invalid(material_id)

            clean_declared: dict[str, str] = {}
            for raw_name, type_name in node_declared.items():
                name = _validate_param_name(raw_name)
                if not isinstance(type_name, str) or type_name not in _TYPE_WIDTH:
                    invalid(material_id)
                clean_declared[name] = type_name

            parents[material_id] = parent
            declared[material_id] = clean_declared
            values[material_id] = dict(node_values)
            overrides[material_id] = dict(node_overrides)
            frozen[material_id] = node_frozen

        # Reject cycles with an iterative three-colour walk.
        colours: dict[str, int] = {}
        for start in raw_materials:
            if colours.get(start) == 2:
                continue
            path: list[str] = []
            current: str | None = start
            while current is not None and colours.get(current, 0) == 0:
                colours[current] = 1
                path.append(current)
                current = parents[current]
            if current is not None and colours.get(current) == 1:
                invalid("cycle")
            for item in path:
                colours[item] = 2

        # Validate depth independently of insertion order.
        depths: dict[str, int] = {}
        for start in raw_materials:
            path = []
            current: str | None = start
            while current is not None and current not in depths:
                path.append(current)
                current = parents[current]
            depth = depths[current] if current is not None else 0
            for item in reversed(path):
                depth += 1
                if depth > self.MAX_DEPTH:
                    invalid("depth")
                depths[item] = depth

        nodes: dict[str, _Material] = {}
        for material_id in raw_materials:
            parent = parents[material_id]
            node_declared = declared[material_id]
            if parent is not None and (node_declared or frozen[material_id]):
                invalid(material_id)
            if parent is None:
                if set(values[material_id]) != set(node_declared):
                    invalid(material_id)

            chain: list[str] = []
            current: str | None = material_id
            while current is not None:
                chain.append(current)
                current = parents[current]
            chain.reverse()

            types: dict[str, str] = {}
            for ancestor in chain:
                for name, type_name in declared[ancestor].items():
                    existing = types.get(name)
                    if existing is not None and existing != type_name:
                        invalid(material_id)
                    types[name] = type_name

            clean_values: dict[str, object] = {}
            for raw_name, value in values[material_id].items():
                name = _validate_param_name(raw_name)
                if name not in types:
                    invalid(material_id)
                clean_values[name] = _normalize_value(types[name], value)

            clean_overrides: dict[str, object] = {}
            for raw_name, value in overrides[material_id].items():
                name = _validate_param_name(raw_name)
                if name not in types:
                    invalid(material_id)
                clean_overrides[name] = _normalize_value(types[name], value)

            nodes[material_id] = _Material(
                parent=parent,
                declared=dict(node_declared),
                values=clean_values,
                overrides=clean_overrides,
                frozen=frozen[material_id],
            )

        return nodes

    def restore(self, state: dict) -> None:
        with self._lock:
            nodes = self._nodes_from_snapshot(state)
            self._materials = nodes
