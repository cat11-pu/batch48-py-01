"""Interprocedural taint summaries computed to a fixed point."""

from __future__ import annotations

import threading
from dataclasses import dataclass

DEFAULT_MAX_FUNCTIONS = 50000
DEFAULT_MAX_CALLEES = 4096
DEFAULT_ITERATION_LIMIT = 64

SNAPSHOT_VERSION = 1

_RET = "ret"
_SINK = "sink"


class TaintError(ValueError):
    """Raised for every rejected definition, lookup or state."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _Call:
    target: str
    param: int
    arg: int
    ret: bool


@dataclass(frozen=True)
class _Definition:
    name: str
    params: tuple
    calls: tuple
    direct_flows: frozenset

    @property
    def callees(self):
        return tuple(sorted({call.target for call in self.calls}))

    @property
    def sinks(self):
        return tuple(sorted({
            sink for _, kind, sink in self.direct_flows if kind == _SINK
        }))


@dataclass(frozen=True)
class _Summary:
    flows: frozenset
    unstable: bool
    version: int


_COUNTER_KEYS = (
    "index_calls",
    "indexed",
    "reindexed",
    "reused",
    "summarize_calls",
    "cache_hits",
    "cache_misses",
    "analyze_calls",
    "invalidate_calls",
    "recomputed",
    "iterations",
    "unstable_summaries",
)


def _empty_counters():
    return {key: 0 for key in _COUNTER_KEYS}


def _is_int(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _flow_sort_key(flow):
    param, kind, sink = flow
    return (param, 0 if kind == _RET else 1, "" if sink is None else sink)


def _format_flow(flow):
    param, kind, sink = flow
    if kind == _RET:
        return "%d->ret" % param
    return "%d->sink:%s" % (param, sink)


def _format_flows(flows):
    return [_format_flow(flow) for flow in sorted(flows, key=_flow_sort_key)]


def _parse_flow(text, param_count):
    if not isinstance(text, str):
        raise ValueError("flow must be a string")
    head, separator, tail = text.partition("->")
    if not separator or not head.isdigit():
        raise ValueError("bad flow")
    param = int(head)
    if param < 0 or param >= param_count:
        raise ValueError("flow position out of range")
    if tail == _RET:
        return (param, _RET, None)
    prefix = "sink:"
    if tail.startswith(prefix) and len(tail) > len(prefix):
        return (param, _SINK, tail[len(prefix):])
    raise ValueError("bad flow kind")


class TaintSummary:
    """Record per-function flow summaries and iterate callee graphs."""

    def __init__(self, *, max_functions=DEFAULT_MAX_FUNCTIONS,
                 max_callees=DEFAULT_MAX_CALLEES,
                 iteration_limit=DEFAULT_ITERATION_LIMIT):
        self.max_functions = max_functions
        self.max_callees = max_callees
        self.iteration_limit = iteration_limit
        self._lock = threading.RLock()
        self._defs = {}
        self._summaries = {}
        self._versions = {}
        self._counters = _empty_counters()

    # ------------------------------------------------------------------
    # Definition handling
    def _normalize(self, name, params, callees):
        if not isinstance(name, str) or not name.strip():
            raise TaintError("TAINT_NAME_INVALID", name)
        if isinstance(params, (str, bytes)) or not isinstance(params, (list, tuple)):
            raise TaintError("TAINT_PARAMS_INVALID", params)
        normalized_params = tuple(params)
        if any(not isinstance(param, str) for param in normalized_params):
            raise TaintError("TAINT_PARAMS_INVALID", params)
        if len(set(normalized_params)) != len(normalized_params):
            raise TaintError("TAINT_PARAMS_INVALID", params)

        if isinstance(callees, (str, bytes)):
            raise TaintError("TAINT_EDGE_INVALID", callees)
        try:
            entries = list(callees)
        except TypeError:
            raise TaintError("TAINT_EDGE_INVALID", callees) from None
        if len(entries) > self.max_callees:
            raise TaintError(
                "TAINT_CALLEES_LIMIT",
                {"name": name, "callees": len(entries), "limit": self.max_callees},
            )

        calls = []
        direct_flows = set()
        for entry in entries:
            if not isinstance(entry, dict) or "param" not in entry:
                raise TaintError("TAINT_EDGE_INVALID", entry)
            param = entry["param"]
            if not _is_int(param) or param < 0 or param >= len(normalized_params):
                raise TaintError("TAINT_EDGE_INVALID", entry)

            if "target" in entry:
                target = entry["target"]
                if not isinstance(target, str) or not target.strip():
                    raise TaintError("TAINT_EDGE_INVALID", entry)
                if "arg" not in entry:
                    raise TaintError("TAINT_EDGE_INVALID", entry)
                arg = entry["arg"]
                if not _is_int(arg) or arg < 0:
                    raise TaintError("TAINT_EDGE_INVALID", entry)

                target_params = None
                if target == name:
                    target_params = normalized_params
                elif target in self._defs:
                    target_params = self._defs[target].params
                if target_params is not None and arg >= len(target_params):
                    raise TaintError("TAINT_EDGE_INVALID", entry)

                calls.append(_Call(
                    target=target,
                    param=param,
                    arg=arg,
                    ret=bool(entry.get("ret", False)),
                ))
                continue

            if "sink" in entry:
                sink = entry["sink"]
                if not isinstance(sink, str) or not sink:
                    raise TaintError("TAINT_EDGE_INVALID", entry)
                direct_flows.add((param, _SINK, sink))
                continue

            if entry.get("ret", False):
                direct_flows.add((param, _RET, None))
                continue

            raise TaintError("TAINT_EDGE_INVALID", entry)

        calls = tuple(sorted(
            set(calls),
            key=lambda call: (
                call.target, call.param, call.arg, 1 if call.ret else 0
            ),
        ))
        return _Definition(
            name=name,
            params=normalized_params,
            calls=calls,
            direct_flows=frozenset(direct_flows),
        )

    def _canonical_definition(self, definition):
        return {
            "name": definition.name,
            "params": list(definition.params),
            "callees": list(definition.callees),
            "sinks": list(definition.sinks),
        }

    @staticmethod
    def _reverse_adjacency(defs):
        reverse = {}
        for caller, definition in defs.items():
            for call in definition.calls:
                reverse.setdefault(call.target, set()).add(caller)
        return reverse

    def _dependent_closure(self, names):
        reverse = self._reverse_adjacency(self._defs)
        found = set()
        stack = list(names)
        while stack:
            current = stack.pop()
            for dependent in reverse.get(current, ()):
                if dependent not in found:
                    found.add(dependent)
                    stack.append(dependent)
        return found

    def _forward_closure(self, names):
        found = set()
        stack = list(names)
        while stack:
            current = stack.pop()
            if current in found or current not in self._defs:
                continue
            found.add(current)
            for call in self._defs[current].calls:
                if call.target in self._defs:
                    stack.append(call.target)
        return found

    def _incoming_edges_are_valid(self, name, params):
        for caller, definition in self._defs.items():
            if caller == name:
                continue
            for call in definition.calls:
                if call.target == name and call.arg >= len(params):
                    return False
        return True

    def index_function(self, name, params, callees):
        with self._lock:
            definition = self._normalize(name, params, callees)
            existing = self._defs.get(name)
            is_new = existing is None

            if is_new and len(self._defs) >= self.max_functions:
                raise TaintError(
                    "TAINT_FUNCTION_LIMIT",
                    {"name": name, "limit": self.max_functions},
                )

            if not is_new and definition.params != existing.params:
                if not self._incoming_edges_are_valid(name, definition.params):
                    raise TaintError(
                        "TAINT_EDGE_INVALID",
                        {"name": name, "params": list(definition.params)},
                    )

            if definition == existing:
                self._counters["index_calls"] += 1
                self._counters["reused"] += 1
                return self._canonical_definition(definition)

            self._defs[name] = definition
            affected = set(self._dependent_closure({name}))
            affected.add(name)
            for function in affected:
                self._summaries.pop(function, None)

            self._counters["index_calls"] += 1
            if is_new:
                self._counters["indexed"] += 1
            else:
                self._counters["reindexed"] += 1
            return self._canonical_definition(definition)

    # ------------------------------------------------------------------
    # Fixed point computation
    @staticmethod
    def _strong_components(nodes, definitions):
        node_set = set(nodes)
        adjacency = {
            node: {
                call.target for call in definitions[node].calls
                if call.target in node_set
            }
            for node in node_set
        }
        reverse = {node: set() for node in node_set}
        for node, targets in adjacency.items():
            for target in targets:
                reverse[target].add(node)

        visited = set()
        order = []
        for start in sorted(node_set):
            if start in visited:
                continue
            visited.add(start)
            stack = [(start, iter(sorted(adjacency[start])))]
            while stack:
                node, iterator = stack[-1]
                try:
                    neighbour = next(iterator)
                except StopIteration:
                    order.append(node)
                    stack.pop()
                    continue
                if neighbour not in visited:
                    visited.add(neighbour)
                    stack.append(
                        (neighbour, iter(sorted(adjacency[neighbour])))
                    )

        components = []
        visited.clear()
        for start in reversed(order):
            if start in visited:
                continue
            visited.add(start)
            component = []
            stack = [start]
            while stack:
                node = stack.pop()
                component.append(node)
                for neighbour in sorted(reverse[node]):
                    if neighbour not in visited:
                        visited.add(neighbour)
                        stack.append(neighbour)
            components.append(component)

        # Kosaraju's second pass yields source components first.  Callers
        # need their callees solved first, so walk the condensation backwards.
        components.reverse()
        return components

    def _flows_for(self, definition, current, external):
        flows = set(definition.direct_flows)
        for call in definition.calls:
            if call.target in current:
                source = current[call.target]
            else:
                summary = external.get(call.target)
                if summary is None:
                    continue
                source = summary.flows
            for param, kind, sink in source:
                if param != call.arg:
                    continue
                if kind == _SINK:
                    flows.add((call.param, _SINK, sink))
                elif call.ret:
                    flows.add((call.param, _RET, None))
        return frozenset(flows)

    def _solve(self, nodes, force):
        nodes = set(nodes)
        if not nodes:
            return

        if not force:
            nodes = {
                name for name in nodes if name not in self._summaries
            }
            if not nodes:
                return

        definitions = self._defs
        components = self._strong_components(nodes, definitions)
        for component in components:
            members = set(component)
            if not force and not any(
                member not in self._summaries for member in members
            ):
                continue

            current = {
                member: definitions[member].direct_flows
                for member in members
            }
            unstable = False

            cyclic = any(
                call.target in members
                for member in members
                for call in definitions[member].calls
            )
            if cyclic:
                rounds = 0
                limit = self.iteration_limit
                while True:
                    if rounds >= limit:
                        unstable = True
                        break
                    updated = {}
                    for member in members:
                        updated[member] = self._flows_for(
                            definitions[member], current, self._summaries
                        )
                    rounds += 1
                    self._counters["iterations"] += 1
                    if updated == current:
                        current = updated
                        break
                    current = updated
            else:
                # A non-recursive component has no equation to solve: one
                # pass over the already-final callee summaries is exact.
                current = {
                    member: self._flows_for(
                        definitions[member], {}, self._summaries
                    )
                    for member in members
                }
                self._counters["iterations"] += 1

            for member in sorted(members):
                version = self._versions.get(member, 0) + 1
                self._versions[member] = version
                self._summaries[member] = _Summary(
                    flows=current[member],
                    unstable=unstable,
                    version=version,
                )
                self._counters["recomputed"] += 1
                if unstable:
                    self._counters["unstable_summaries"] += 1

    def _ensure(self, names):
        closure = self._forward_closure(names)
        self._solve(closure, force=False)

    def _summary_copy(self, name):
        summary = self._summaries[name]
        definition = self._defs[name]
        return {
            "name": name,
            "params": len(definition.params),
            "flows": _format_flows(summary.flows),
            "unstable": summary.unstable,
            "version": summary.version,
        }

    def summarize(self, name):
        with self._lock:
            if not isinstance(name, str) or name not in self._defs:
                raise TaintError("TAINT_UNKNOWN_FUNCTION", name)
            cached = name in self._summaries
            self._ensure([name])
            self._counters["summarize_calls"] += 1
            self._counters["cache_hits" if cached else "cache_misses"] += 1
            return self._summary_copy(name)

    # ------------------------------------------------------------------
    # Queries
    def analyze(self, entry):
        with self._lock:
            if not isinstance(entry, str) or entry not in self._defs:
                raise TaintError("TAINT_UNKNOWN_FUNCTION", entry)
            closure = self._forward_closure([entry])
            self._solve(closure, force=False)

            functions = sorted(closure)
            flows = []
            unstable = []
            for name in functions:
                summary = self._summaries[name]
                if summary.unstable:
                    unstable.append(name)
                for flow in _format_flows(summary.flows):
                    flows.append("%s::%s" % (name, flow))

            self._counters["analyze_calls"] += 1
            return {
                "entry": entry,
                "functions": functions,
                "flows": flows,
                "unstable": unstable,
            }

    def invalidate(self, names):
        with self._lock:
            if isinstance(names, str):
                requested = [names]
            else:
                try:
                    requested = list(names)
                except TypeError:
                    raise TaintError(
                        "TAINT_UNKNOWN_FUNCTION", names
                    ) from None
            for name in requested:
                if not isinstance(name, str) or name not in self._defs:
                    raise TaintError("TAINT_UNKNOWN_FUNCTION", name)

            affected = set(requested)
            affected.update(self._dependent_closure(affected))

            # Upstream callees outside the invalidation closure are not
            # invalidated, so only materialise missing summaries for them.
            external = self._forward_closure(affected) - affected
            if external:
                self._solve(external, force=False)

            self._solve(affected, force=True)
            self._counters["invalidate_calls"] += 1
            return sorted(affected)

    def dependencies(self, name):
        with self._lock:
            if not isinstance(name, str) or name not in self._defs:
                raise TaintError("TAINT_UNKNOWN_FUNCTION", name)
            return list(self._defs[name].callees)

    def explain(self, name):
        with self._lock:
            if not isinstance(name, str) or name not in self._defs:
                raise TaintError("TAINT_UNKNOWN_FUNCTION", name)
            self._ensure([name])
            summary = self._summary_copy(name)
            definition = self._defs[name]
            summary["callees"] = list(definition.callees)
            summary["sinks"] = list(definition.sinks)
            summary["direct_flows"] = _format_flows(definition.direct_flows)
            summary["dependencies"] = list(definition.callees)
            return summary

    def functions(self):
        with self._lock:
            return sorted(self._defs)

    # ------------------------------------------------------------------
    # Snapshots and statistics
    def _snapshot_definition(self, definition):
        return {
            "name": definition.name,
            "params": list(definition.params),
            "callees": list(definition.callees),
            "sinks": list(definition.sinks),
            "edges": [
                {
                    "target": call.target,
                    "param": call.param,
                    "arg": call.arg,
                    "ret": call.ret,
                }
                for call in definition.calls
            ],
            "flows": _format_flows(definition.direct_flows),
        }

    def snapshot(self):
        with self._lock:
            state = {
                "version": SNAPSHOT_VERSION,
                "functions": [
                    self._snapshot_definition(self._defs[name])
                    for name in sorted(self._defs)
                ],
                "summaries": [
                    self._summary_copy(name)
                    for name in sorted(self._summaries)
                ],
                "versions": {
                    name: version
                    for name, version in sorted(self._versions.items())
                },
                "stats": self.stats(),
            }
            return state

    def _decode_snapshot(self, state):
        if not isinstance(state, dict):
            raise ValueError("snapshot must be a mapping")
        if state.get("version") != SNAPSHOT_VERSION:
            raise ValueError("unsupported snapshot version")
        if not all(
            key in state
            for key in ("functions", "summaries", "versions", "stats")
        ):
            raise ValueError("missing snapshot sections")

        raw_functions = state["functions"]
        if not isinstance(raw_functions, list):
            raise ValueError("functions must be a list")

        defs = {}
        for raw in raw_functions:
            if not isinstance(raw, dict):
                raise ValueError("bad function record")
            name = raw.get("name")
            params = raw.get("params")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("bad function name")
            if name in defs:
                raise ValueError("duplicate function")
            if not isinstance(params, list) or any(
                not isinstance(param, str) for param in params
            ):
                raise ValueError("bad function parameters")
            if len(set(params)) != len(params):
                raise ValueError("duplicate function parameters")

            edges = raw.get("edges")
            flows = raw.get("flows")
            if not isinstance(edges, list) or not isinstance(flows, list):
                raise ValueError("bad function edges")

            calls = []
            for edge in edges:
                if not isinstance(edge, dict):
                    raise ValueError("bad call edge")
                target = edge.get("target")
                param = edge.get("param")
                arg = edge.get("arg")
                ret = edge.get("ret", False)
                if not isinstance(target, str) or not target.strip():
                    raise ValueError("bad call target")
                if not _is_int(param) or param < 0 or param >= len(params):
                    raise ValueError("bad call parameter")
                if not _is_int(arg) or arg < 0:
                    raise ValueError("bad call argument")
                calls.append(_Call(target, param, arg, bool(ret)))

            direct_flows = frozenset(
                _parse_flow(flow, len(params)) for flow in flows
            )
            defs[name] = _Definition(
                name=name,
                params=tuple(params),
                calls=tuple(sorted(
                    set(calls),
                    key=lambda call: (
                        call.target, call.param, call.arg,
                        1 if call.ret else 0,
                    ),
                )),
                direct_flows=direct_flows,
            )

        for definition in defs.values():
            for call in definition.calls:
                target = defs.get(call.target)
                if target is not None and call.arg >= len(target.params):
                    raise ValueError("call argument out of range")

        raw_summaries = state["summaries"]
        if not isinstance(raw_summaries, list):
            raise ValueError("summaries must be a list")
        summaries = {}
        for raw in raw_summaries:
            if not isinstance(raw, dict):
                raise ValueError("bad summary record")
            name = raw.get("name")
            if name not in defs or name in summaries:
                raise ValueError("bad summary name")
            flows = raw.get("flows")
            unstable = raw.get("unstable")
            version = raw.get("version")
            if not isinstance(flows, list) or not isinstance(unstable, bool):
                raise ValueError("bad summary")
            if not _is_int(version) or version < 1:
                raise ValueError("bad summary version")
            parsed = frozenset(
                _parse_flow(flow, len(defs[name].params)) for flow in flows
            )
            summaries[name] = _Summary(parsed, unstable, version)

        raw_versions = state["versions"]
        if not isinstance(raw_versions, dict):
            raise ValueError("versions must be a mapping")
        versions = {}
        for name, version in raw_versions.items():
            if name not in defs or not _is_int(version) or version < 1:
                raise ValueError("bad version entry")
            versions[name] = version
        for name, summary in summaries.items():
            versions[name] = max(versions.get(name, 0), summary.version)

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict):
            raise ValueError("stats must be a mapping")
        stats = _empty_counters()
        for key, value in raw_stats.items():
            if key in stats:
                if not _is_int(value) or value < 0:
                    raise ValueError("bad statistic")
                stats[key] = value
        return defs, summaries, versions, stats

    def restore(self, state):
        with self._lock:
            try:
                defs, summaries, versions, stats = self._decode_snapshot(state)
            except TaintError:
                raise
            except Exception as exc:
                raise TaintError("TAINT_SNAPSHOT_INVALID", str(exc)) from None

            self._defs = defs
            self._summaries = summaries
            self._versions = versions
            self._counters = stats

    def stats(self):
        with self._lock:
            result = dict(self._counters)
            result["functions"] = len(self._defs)
            result["definitions"] = len(self._defs)
            result["summaries"] = len(self._summaries)
            result["edges"] = sum(
                len(definition.calls) for definition in self._defs.values()
            )
            result["recomputes"] = result["recomputed"]
            result["invalidations"] = result["invalidate_calls"]
            result["cache_misses"] = result.get("cache_misses", 0)
            result["unstable"] = result["unstable_summaries"]
            return result

    def reset_stats(self):
        with self._lock:
            self._counters = _empty_counters()
