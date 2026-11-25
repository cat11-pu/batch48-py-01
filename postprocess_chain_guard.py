"""Post-processing chain planning, conversion insertion and caching."""

from __future__ import annotations

import copy
import math
from typing import Any


FORMATS = ("rgba8", "rgba16f", "rgba32f")
COLOR_SPACES = ("srgb", "linear")
STAT_KEYS = ("runs", "executed", "cached", "skipped", "trimmed",
             "conversions")
SNAPSHOT_VERSION = 1
_EPSILON = 1e-9


class ChainError(ValueError):
    """Raised when a post-processing chain cannot be planned or restored."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _name_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]

    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ChainError("CHAIN_PASS_INVALID",
                         "pass name collections must be iterable strings") from exc

    for item in items:
        if not isinstance(item, str) or not item:
            raise ChainError(
                "CHAIN_PASS_INVALID",
                "dependencies and texture names must be non-empty strings",
            )
    return items


def _zero_stats() -> dict[str, int]:
    return {key: 0 for key in STAT_KEYS}


class PostProcessChainGuard:
    """Order post-process passes, insert conversions and reuse buffers."""

    def __init__(self, *, frame_budget_ms: float = 8.0) -> None:
        self.frame_budget_ms = float(frame_budget_ms)
        self._sources: dict[str, dict[str, Any]] = {}
        self._passes: dict[str, dict[str, Any]] = {}
        self._enabled: dict[str, bool] = {}
        self._cache: dict[str, bool] = {}
        self._buffers: dict[str, int] = {}
        self._texture_buffers: dict[str, int] = {}
        self._last_plan: dict[str, Any] | None = None
        self._stats = _zero_stats()

    def set_source(self, name: str, *, fmt: str = "rgba8",
                   color_space: str = "srgb") -> dict:
        if not isinstance(name, str) or not name:
            raise ChainError("CHAIN_PASS_INVALID",
                             "source name must be a non-empty string")
        if name in self._sources:
            raise ChainError("CHAIN_DUPLICATE_TEXTURE", name)
        self._validate_format(fmt, color_space)

        source = {
            "name": name,
            "fmt": fmt,
            "color_space": color_space,
            "buffer": -1,
        }
        self._sources[name] = source
        self._texture_buffers[name] = -1
        return copy.deepcopy(source)

    def add_pass(self, name: str, *, deps=(), reads=(), writes=(),
                 fmt: str = "rgba8", color_space: str = "srgb",
                 cost_ms: float = 1.0, optional: bool = False) -> dict:
        if not isinstance(name, str) or not name:
            raise ChainError("CHAIN_PASS_INVALID",
                             "pass name must be a non-empty string")
        if name in self._passes:
            raise ChainError("CHAIN_DUPLICATE_PASS", name)

        dep_names = _name_list(deps)
        read_names = _name_list(reads)
        write_names = _name_list(writes)

        if len(write_names) != 1:
            raise ChainError(
                "CHAIN_PASS_INVALID",
                {"name": name, "writes": write_names},
            )

        if set(read_names).intersection(write_names):
            raise ChainError(
                "CHAIN_READ_WRITE_HAZARD",
                {"name": name, "textures": write_names},
            )

        self._validate_format(fmt, color_space)

        if isinstance(cost_ms, bool) or not isinstance(cost_ms, (int, float)):
            raise ChainError("CHAIN_PASS_INVALID",
                             {"name": name, "cost_ms": cost_ms})
        cost = float(cost_ms)
        if not math.isfinite(cost) or cost < 0.0:
            raise ChainError("CHAIN_PASS_INVALID",
                             {"name": name, "cost_ms": cost_ms})

        record = {
            "name": name,
            "deps": dep_names,
            "reads": read_names,
            "writes": write_names,
            "fmt": fmt,
            "color_space": color_space,
            "cost_ms": cost,
            "optional": bool(optional),
        }
        self._passes[name] = record
        self._enabled[name] = True
        self._cache[name] = False
        return self._pass_view(name)

    def set_enabled(self, name: str, enabled: bool) -> bool:
        if name not in self._passes:
            raise ChainError("CHAIN_UNKNOWN_PASS", name)
        value = bool(enabled)
        self._enabled[name] = value
        return value

    def build(self) -> dict:
        self._validate_graph()
        topo_order = self._topological_order(list(self._passes))
        skip_set = self._disabled_descendants()
        disabled = [
            name for name in self._passes
            if not self._enabled.get(name, True)
        ]

        active_order = [name for name in topo_order if name not in skip_set]
        included, trimmed, spent, over_budget = self._select_with_budget(
            active_order
        )
        pass_order = [name for name in active_order if name in included]

        steps, buffers, conversion_count = self._build_steps(pass_order)
        cost_ms = sum(self._passes[name]["cost_ms"] for name in pass_order)

        plan = {
            "steps": steps,
            "buffers": buffers,
            "cost_ms": cost_ms,
            "conversions": conversion_count,
            "trimmed": trimmed,
            "disabled": disabled,
            "over_budget": over_budget,
        }

        self._buffers = copy.deepcopy(buffers)
        self._texture_buffers = {
            **{name: -1 for name in self._sources},
            **copy.deepcopy(buffers),
        }
        self._last_plan = copy.deepcopy(plan)
        return plan

    def last_plan(self) -> dict:
        if self._last_plan is None:
            return {}
        return copy.deepcopy(self._last_plan)

    def run(self) -> dict:
        plan = self.build()
        skipped = self._skipped_pass_names()

        writer_for_texture = {
            step["writes"][0]: step["name"]
            for step in plan["steps"]
        }
        executed_steps: set[str] = set()
        executed: list[str] = []
        cached: list[str] = []

        for step in plan["steps"]:
            input_reexecuted = any(
                writer_for_texture.get(texture) in executed_steps
                for texture in step["reads"]
            )
            cache_key = step["name"]
            use_cache = (
                not input_reexecuted
                and bool(self._cache.get(cache_key, False))
            )

            if use_cache:
                if step["kind"] == "pass":
                    cached.append(step["name"])
            else:
                executed_steps.add(step["name"])
                self._cache[cache_key] = True
                if step["kind"] == "pass":
                    executed.append(step["name"])

        for name in skipped:
            self._cache[name] = False
        for name in plan["trimmed"]:
            self._cache[name] = False

        result = {
            "executed": executed,
            "cached": cached,
            "skipped": skipped,
            "trimmed": list(plan["trimmed"]),
            "cost_ms": plan["cost_ms"],
            "conversions": plan["conversions"],
        }

        self._stats["runs"] += 1
        self._stats["executed"] += len(executed)
        self._stats["cached"] += len(cached)
        self._stats["skipped"] += len(skipped)
        self._stats["trimmed"] += len(plan["trimmed"])
        self._stats["conversions"] += int(plan["conversions"])
        return copy.deepcopy(result)

    def snapshot(self) -> dict:
        return copy.deepcopy({
            "version": SNAPSHOT_VERSION,
            "frame_budget_ms": self.frame_budget_ms,
            "sources": self._sources,
            "passes": self._passes,
            "enabled": self._enabled,
            "cache": self._cache,
            "buffers": self._buffers,
            "texture_buffers": self._texture_buffers,
            "last_plan": self._last_plan,
            "stats": self._stats,
        })

    def restore(self, state: dict) -> None:
        try:
            parsed = self._parse_snapshot(state)
        except ChainError as exc:
            if exc.code == "CHAIN_SNAPSHOT_INVALID":
                raise
            raise ChainError("CHAIN_SNAPSHOT_INVALID", exc.detail) from exc
        except Exception as exc:
            raise ChainError("CHAIN_SNAPSHOT_INVALID") from exc

        self.frame_budget_ms = parsed["frame_budget_ms"]
        self._sources = parsed["sources"]
        self._passes = parsed["passes"]
        self._enabled = parsed["enabled"]
        self._cache = parsed["cache"]
        self._buffers = parsed["buffers"]
        self._texture_buffers = parsed["texture_buffers"]
        self._last_plan = parsed["last_plan"]
        self._stats = parsed["stats"]

    def stats(self) -> dict:
        return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        self._stats = _zero_stats()

    @staticmethod
    def _validate_format(fmt: object, color_space: object) -> None:
        if fmt not in FORMATS:
            raise ChainError("CHAIN_FORMAT_UNSUPPORTED",
                             {"field": "fmt", "value": fmt})
        if color_space not in COLOR_SPACES:
            raise ChainError("CHAIN_FORMAT_UNSUPPORTED",
                             {"field": "color_space", "value": color_space})

    def _pass_view(self, name: str) -> dict:
        record = self._passes[name]
        return copy.deepcopy({
            **record,
            "enabled": self._enabled[name],
        })

    def _validate_graph(self) -> None:
        for name, record in self._passes.items():
            for dep in record["deps"]:
                if dep not in self._passes:
                    raise ChainError("CHAIN_UNKNOWN_DEP",
                                     {"pass": name, "dep": dep})

        writers: dict[str, str] = {}
        for name, record in self._passes.items():
            texture = record["writes"][0]
            if texture in writers:
                raise ChainError(
                    "CHAIN_TEXTURE_CONFLICT",
                    {"texture": texture, "passes": [writers[texture], name]},
                )
            writers[texture] = name

        for name, record in self._passes.items():
            for texture in record["reads"]:
                if texture in self._sources:
                    continue

                producer = writers.get(texture)
                if producer is None:
                    raise ChainError("CHAIN_UNKNOWN_TEXTURE",
                                    {"pass": name, "texture": texture})
                if producer == name:
                    raise ChainError("CHAIN_READ_WRITE_HAZARD",
                                     {"pass": name, "texture": texture})
                if producer not in record["deps"]:
                    raise ChainError(
                        "CHAIN_DEPENDENCY_MISSING",
                        {"pass": name, "texture": texture,
                         "producer": producer},
                    )

        for optional_name, record in self._passes.items():
            if not record["optional"]:
                continue
            output = record["writes"][0]
            for reader_name, reader in self._passes.items():
                if reader_name != optional_name and output in reader["reads"]:
                    raise ChainError(
                        "CHAIN_TRIM_CONFLICT",
                        {"optional_pass": optional_name,
                         "texture": output, "reader": reader_name},
                    )

    def _topological_order(self, names: list[str]) -> list[str]:
        wanted = set(names)
        emitted: set[str] = set()
        remaining = set(names)
        order: list[str] = []

        while remaining:
            chosen = None
            for name in self._passes:
                if name not in remaining:
                    continue
                deps_ready = all(
                    dep in emitted
                    for dep in self._passes[name]["deps"]
                    if dep in wanted
                )
                if deps_ready:
                    chosen = name
                    break

            if chosen is None:
                raise ChainError("CHAIN_CYCLE",
                                 {"remaining": sorted(remaining)})

            order.append(chosen)
            emitted.add(chosen)
            remaining.remove(chosen)

        return order

    def _disabled_descendants(self) -> set[str]:
        skipped = {
            name for name, enabled in self._enabled.items()
            if not enabled
        }

        changed = True
        while changed:
            changed = False
            for name, record in self._passes.items():
                if name in skipped:
                    continue
                if any(dep in skipped for dep in record["deps"]):
                    skipped.add(name)
                    changed = True

        return skipped

    def _skipped_pass_names(self) -> list[str]:
        skip_set = self._disabled_descendants()
        return [
            name for name in self._topological_order(list(self._passes))
            if name in skip_set
        ]

    def _select_with_budget(
        self, active_order: list[str]
    ) -> tuple[set[str], list[str], float, bool]:
        budget = float(self.frame_budget_ms)
        mandatory_cost = sum(
            self._passes[name]["cost_ms"]
            for name in active_order
            if not self._passes[name]["optional"]
        )
        over_budget = mandatory_cost > budget + _EPSILON
        spent = mandatory_cost
        included: set[str] = set()
        trimmed: list[str] = []

        for name in active_order:
            record = self._passes[name]
            if not record["optional"]:
                included.add(name)
                continue

            cost = record["cost_ms"]
            if spent + cost <= budget + _EPSILON:
                included.add(name)
                spent += cost
            else:
                trimmed.append(name)

        return included, trimmed, spent, over_budget

    def _build_steps(
        self, pass_order: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, int], int]:
        texture_specs = {
            name: (source["fmt"], source["color_space"])
            for name, source in self._sources.items()
        }
        for name, record in self._passes.items():
            texture_specs[record["writes"][0]] = (
                record["fmt"], record["color_space"]
            )

        steps: list[dict[str, Any]] = []
        known_products: set[str] = set()
        conversion_count = 0

        def ensure_conversion(texture: str, target_fmt: str,
                              target_color_space: str) -> str:
            nonlocal conversion_count
            if texture not in texture_specs:
                raise ChainError("CHAIN_UNKNOWN_TEXTURE", texture)

            source_fmt, source_color_space = texture_specs[texture]
            current_texture = texture
            current_fmt = source_fmt
            current_color_space = source_color_space
            root = texture

            if current_fmt != target_fmt:
                source_index = FORMATS.index(current_fmt)
                target_index = FORMATS.index(target_fmt)
                if abs(source_index - target_index) != 1:
                    raise ChainError(
                        "CHAIN_CONVERSION_UNSUPPORTED",
                        {"texture": texture,
                         "source_fmt": current_fmt,
                         "target_fmt": target_fmt},
                    )

                product = f"{root}~{target_fmt}|{current_color_space}"
                if product not in known_products:
                    steps.append({
                        "name": f"convert:{product}",
                        "kind": "convert",
                        "reads": [current_texture],
                        "writes": [product],
                        "fmt": target_fmt,
                        "color_space": current_color_space,
                        "cost_ms": 0.0,
                    })
                    known_products.add(product)
                    conversion_count += 1

                current_texture = product
                current_fmt = target_fmt

            if current_color_space != target_color_space:
                product = f"{root}~{current_fmt}|{target_color_space}"
                if product not in known_products:
                    steps.append({
                        "name": f"convert:{product}",
                        "kind": "convert",
                        "reads": [current_texture],
                        "writes": [product],
                        "fmt": current_fmt,
                        "color_space": target_color_space,
                        "cost_ms": 0.0,
                    })
                    known_products.add(product)
                    conversion_count += 1

                current_texture = product
                current_color_space = target_color_space

            return current_texture

        for name in pass_order:
            record = self._passes[name]
            rewritten_reads = [
                ensure_conversion(
                    texture, record["fmt"], record["color_space"]
                )
                for texture in record["reads"]
            ]
            steps.append({
                "name": name,
                "kind": "pass",
                "deps": list(record["deps"]),
                "reads": rewritten_reads,
                "writes": list(record["writes"]),
                "fmt": record["fmt"],
                "color_space": record["color_space"],
                "cost_ms": record["cost_ms"],
                "optional": record["optional"],
            })

        buffers: dict[str, int] = {}
        for position, step in enumerate(steps):
            buffer_id = position % 2
            step["buffer"] = buffer_id
            buffers[step["writes"][0]] = buffer_id

        return steps, buffers, conversion_count

    def _parse_snapshot(self, state: object) -> dict[str, Any]:
        invalid = ChainError("CHAIN_SNAPSHOT_INVALID")
        if not isinstance(state, dict):
            raise invalid
        if state.get("version") != SNAPSHOT_VERSION:
            raise invalid

        required = (
            "frame_budget_ms", "sources", "passes", "enabled", "cache",
            "buffers", "texture_buffers", "last_plan", "stats",
        )
        if any(key not in state for key in required):
            raise invalid

        budget = state["frame_budget_ms"]
        if (isinstance(budget, bool)
                or not isinstance(budget, (int, float))):
            raise invalid

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(STAT_KEYS):
            raise invalid
        stats = {}
        for key in STAT_KEYS:
            value = raw_stats[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise invalid
            stats[key] = value

        raw_sources = state["sources"]
        if not isinstance(raw_sources, dict):
            raise invalid
        sources: dict[str, dict[str, Any]] = {}
        for name, source in raw_sources.items():
            if (not isinstance(name, str) or not name
                    or not isinstance(source, dict)):
                raise invalid
            fmt = source.get("fmt", "rgba8")
            color_space = source.get("color_space", "srgb")
            if fmt not in FORMATS or color_space not in COLOR_SPACES:
                raise invalid
            sources[name] = {
                "name": name,
                "fmt": fmt,
                "color_space": color_space,
                "buffer": -1,
            }

        raw_passes = state["passes"]
        if not isinstance(raw_passes, dict):
            raise invalid
        passes: dict[str, dict[str, Any]] = {}
        for name, raw_pass in raw_passes.items():
            if (not isinstance(name, str) or not name
                    or not isinstance(raw_pass, dict)
                    or raw_pass.get("name", name) != name):
                raise invalid

            try:
                deps = _name_list(raw_pass.get("deps", []))
                reads = _name_list(raw_pass.get("reads", []))
                writes = _name_list(raw_pass.get("writes", []))
            except ChainError as exc:
                raise invalid from exc

            if len(writes) != 1 or set(reads).intersection(writes):
                raise invalid

            fmt = raw_pass.get("fmt", "rgba8")
            color_space = raw_pass.get("color_space", "srgb")
            if fmt not in FORMATS or color_space not in COLOR_SPACES:
                raise invalid

            cost = raw_pass.get("cost_ms", 1.0)
            if (isinstance(cost, bool)
                    or not isinstance(cost, (int, float))
                    or not math.isfinite(float(cost))
                    or cost < 0):
                raise invalid

            optional = raw_pass.get("optional", False)
            if not isinstance(optional, bool):
                raise invalid

            passes[name] = {
                "name": name,
                "deps": deps,
                "reads": reads,
                "writes": writes,
                "fmt": fmt,
                "color_space": color_space,
                "cost_ms": float(cost),
                "optional": optional,
            }

        raw_enabled = state["enabled"]
        if not isinstance(raw_enabled, dict):
            raise invalid
        if set(raw_enabled) != set(passes):
            raise invalid
        enabled: dict[str, bool] = {}
        for name, value in raw_enabled.items():
            if name not in passes or not isinstance(value, bool):
                raise invalid
            enabled[name] = value

        raw_cache = state["cache"]
        if not isinstance(raw_cache, dict):
            raise invalid
        cache: dict[str, bool] = {}
        for key, value in raw_cache.items():
            if not isinstance(key, str) or not isinstance(value, bool):
                raise invalid
            cache[key] = value

        raw_buffers = state["buffers"]
        if not isinstance(raw_buffers, dict):
            raise invalid
        buffers: dict[str, int] = {}
        for texture, buffer_id in raw_buffers.items():
            if (not isinstance(texture, str)
                    or isinstance(buffer_id, bool)
                    or buffer_id not in (0, 1)):
                raise invalid
            buffers[texture] = buffer_id

        raw_texture_buffers = state["texture_buffers"]
        if not isinstance(raw_texture_buffers, dict):
            raise invalid
        texture_buffers: dict[str, int] = {}
        for texture, buffer_id in raw_texture_buffers.items():
            if (not isinstance(texture, str)
                    or isinstance(buffer_id, bool)
                    or buffer_id not in (-1, 0, 1)):
                raise invalid
            texture_buffers[texture] = buffer_id

        last_plan = state["last_plan"]
        if last_plan is not None and not isinstance(last_plan, dict):
            raise invalid

        return {
            "frame_budget_ms": float(budget),
            "sources": copy.deepcopy(sources),
            "passes": copy.deepcopy(passes),
            "enabled": copy.deepcopy(enabled),
            "cache": copy.deepcopy(cache),
            "buffers": copy.deepcopy(buffers),
            "texture_buffers": copy.deepcopy(texture_buffers),
            "last_plan": copy.deepcopy(last_plan),
            "stats": copy.deepcopy(stats),
        }
