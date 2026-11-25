"""SBOM dependency graph with purl normalisation and advisory propagation."""

from collections import deque
import re
import threading


SCOPES = ("runtime", "dev")
_SNAPSHOT_VERSION = 1
_DIGITS_RE = re.compile(r"^[0-9]+$")
_PRERELEASE_PART_RE = re.compile(r"^[0-9A-Za-z-]+$")
_PURL_TYPE_RE = re.compile(r"^[a-z][a-z0-9.+-]*$")
_WILDCARD_RE = re.compile(r"^(?:=)?([0-9]+)\.([0-9]+)\.\*$")


class SbomError(ValueError):
    """Raised for every rejected graph or advisory operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_numeric(value):
    return bool(_DIGITS_RE.fullmatch(value))


def _strip_numeric_fields(value):
    return ".".join(
        str(int(part)) if _is_numeric(part) else part
        for part in value.split(".")
    )


def _normalize_purl_version(version):
    build = ""
    working = version
    if "+" in working:
        working, build_text = working.split("+", 1)
        build = "+" + _strip_numeric_fields(build_text)

    prerelease = ""
    if "-" in working:
        core, prerelease_text = working.split("-", 1)
        prerelease = "-" + _strip_numeric_fields(prerelease_text)
    else:
        core = working

    return _strip_numeric_fields(core) + prerelease + build


class SbomDependencyGraph:
    """Normalise purls, keep a scoped dependency graph and answer advisories."""

    def __init__(self, *, max_nodes=100000, max_edges=500000):
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self._lock = threading.RLock()
        self._originals = {}
        self._scopes = {}
        self._edges = {}
        self._outgoing = {}
        self._incoming = {}

    @staticmethod
    def _validate_scope(scope):
        if scope not in SCOPES:
            raise SbomError("SBOM_INVALID_SCOPE", scope)

    def normalize_purl(self, purl):
        if not isinstance(purl, str):
            raise SbomError("SBOM_INVALID_PURL", purl)
        if len(purl) < 4 or purl[:4].lower() != "pkg:":
            raise SbomError("SBOM_INVALID_PURL", purl)

        remainder = purl[4:]
        identity, _, _fragment = remainder.partition("#")
        main, has_query, query = identity.partition("?")
        if has_query and not query:
            raise SbomError("SBOM_INVALID_PURL", purl)
        if "/" not in main:
            raise SbomError("SBOM_INVALID_PURL", purl)

        purl_type, _, path = main.partition("/")
        purl_type = purl_type.lower()
        if not purl_type or not path or not _PURL_TYPE_RE.fullmatch(purl_type):
            raise SbomError("SBOM_INVALID_PURL", purl)
        if path.count("@") != 1:
            raise SbomError("SBOM_INVALID_PURL", purl)

        path_part, _, version = path.partition("@")
        segments = path_part.split("/")
        if not segments or not segments[-1]:
            raise SbomError("SBOM_INVALID_PURL", purl)

        name = segments[-1].lower()
        namespace = "/".join(
            segment.lower() for segment in segments[:-1] if segment != ""
        )
        version = _normalize_purl_version(version)
        if not version:
            raise SbomError("SBOM_INVALID_PURL", purl)

        qualifiers = []
        if has_query:
            for item in query.split("&"):
                if not item or "=" not in item:
                    raise SbomError("SBOM_INVALID_PURL", purl)
                key, value = item.split("=", 1)
                if not key:
                    raise SbomError("SBOM_INVALID_PURL", purl)
                qualifiers.append((key.lower(), value))
            qualifiers.sort(key=lambda item: (item[0], item[1]))

        normalized = "pkg:" + purl_type + "/"
        if namespace:
            normalized += namespace + "/"
        normalized += name + "@" + version
        if qualifiers:
            normalized += "?" + "&".join(
                key + "=" + value for key, value in qualifiers
            )
        return normalized

    def add_component(self, purl, scope="runtime"):
        self._validate_scope(scope)
        canonical = self.normalize_purl(purl)
        with self._lock:
            if canonical not in self._originals:
                if len(self._originals) >= self.max_nodes:
                    raise SbomError("SBOM_NODE_LIMIT", canonical)
                self._originals[canonical] = set()
                self._scopes[canonical] = set()
            self._originals[canonical].add(purl)
            self._scopes[canonical].add(scope)
            return canonical

    def _cycle_path_locked(self, parent, child):
        if parent == child:
            return [parent]

        previous = {child: None}
        queue = deque([child])
        while queue:
            node = queue.popleft()
            if node == parent:
                path = []
                current = parent
                while current is not None:
                    path.append(current)
                    if current == child:
                        break
                    current = previous.get(current)
                path.reverse()
                return path

            for nxt in sorted(self._outgoing.get(node, ())):
                if nxt not in previous:
                    previous[nxt] = node
                    queue.append(nxt)
        return None

    def add_edge(self, parent, child, scope="runtime"):
        self._validate_scope(scope)
        parent_purl = self.normalize_purl(parent)
        child_purl = self.normalize_purl(child)
        edge = (parent_purl, child_purl, scope)

        with self._lock:
            missing = []
            for purl in (parent_purl, child_purl):
                if purl not in self._originals and purl not in missing:
                    missing.append(purl)
            if len(self._originals) + len(missing) > self.max_nodes:
                raise SbomError("SBOM_NODE_LIMIT", missing)

            if edge in self._edges:
                return (parent_purl, child_purl)

            cycle_path = self._cycle_path_locked(parent_purl, child_purl)
            if cycle_path is not None:
                raise SbomError("SBOM_CYCLE_DETECTED", cycle_path)

            if len(self._edges) >= self.max_edges:
                raise SbomError("SBOM_EDGE_LIMIT", edge)

            for canonical, raw in (
                (parent_purl, parent),
                (child_purl, child),
            ):
                self._originals.setdefault(canonical, set()).add(raw)
                self._scopes.setdefault(canonical, set()).add(scope)

            self._edges[edge] = None
            self._outgoing.setdefault(parent_purl, set()).add(child_purl)
            self._incoming.setdefault(child_purl, set()).add(parent_purl)
            return (parent_purl, child_purl)

    @staticmethod
    def _compare_versions(left, right):
        for left_part, right_part in zip(left[:3], right[:3]):
            if left_part != right_part:
                return -1 if left_part < right_part else 1

        left_pre = left[3]
        right_pre = right[3]
        if not left_pre and not right_pre:
            return 0
        if not left_pre:
            return 1
        if not right_pre:
            return -1

        for left_part, right_part in zip(left_pre, right_pre):
            left_number = isinstance(left_part, int)
            right_number = isinstance(right_part, int)
            if left_number and right_number:
                if left_part != right_part:
                    return -1 if left_part < right_part else 1
            elif left_number:
                return -1
            elif right_number:
                return 1
            elif left_part != right_part:
                return -1 if left_part < right_part else 1

        if len(left_pre) == len(right_pre):
            return 0
        return -1 if len(left_pre) < len(right_pre) else 1

    def parse_version(self, version):
        if not isinstance(version, str):
            raise SbomError("SBOM_INVALID_VERSION", version)

        text = version
        if "+" in text:
            text, build = text.split("+", 1)
            build_parts = build.split(".")
            if not build or any(
                not part or not _PRERELEASE_PART_RE.fullmatch(part)
                for part in build_parts
            ):
                raise SbomError("SBOM_INVALID_VERSION", version)

        prerelease_text = None
        if "-" in text:
            text, prerelease_text = text.split("-", 1)

        core = text.split(".")
        if len(core) != 3 or any(not _is_numeric(part) for part in core):
            raise SbomError("SBOM_INVALID_VERSION", version)

        prerelease = ()
        if prerelease_text is not None:
            identifiers = []
            for part in prerelease_text.split("."):
                if not part or not _PRERELEASE_PART_RE.fullmatch(part):
                    raise SbomError("SBOM_INVALID_VERSION", version)
                if _is_numeric(part):
                    identifiers.append(int(part))
                else:
                    identifiers.append(part)
            prerelease = tuple(identifiers)

        return (int(core[0]), int(core[1]), int(core[2]), prerelease)

    def _parse_constraint(self, constraint):
        if not isinstance(constraint, str) or not constraint.strip():
            raise SbomError("SBOM_INVALID_CONSTRAINT", constraint)

        text = constraint.strip()
        if text == "*":
            return [("*",)]

        clauses = []
        for raw_clause in text.split(","):
            clause = "".join(raw_clause.split())
            if not clause:
                raise SbomError("SBOM_INVALID_CONSTRAINT", constraint)
            if clause == "*":
                clauses.append(("*",))
                continue

            wildcard = _WILDCARD_RE.fullmatch(clause)
            if wildcard:
                clauses.append((
                    "wildcard",
                    int(wildcard.group(1)),
                    int(wildcard.group(2)),
                ))
                continue

            operator = None
            bound_text = clause
            for candidate in (">=", "<=", "=", ">", "<"):
                if clause.startswith(candidate):
                    operator = candidate
                    bound_text = clause[len(candidate):]
                    break

            if operator is None:
                operator = "="
            if not bound_text:
                raise SbomError("SBOM_INVALID_CONSTRAINT", constraint)

            try:
                bound = self.parse_version(bound_text)
            except SbomError as exc:
                raise SbomError(
                    "SBOM_INVALID_CONSTRAINT", constraint
                ) from exc
            clauses.append((operator, bound))
        return clauses

    def _matches_clauses(self, parsed_version, clauses):
        for clause in clauses:
            operator = clause[0]
            if operator == "*":
                continue

            if operator == "wildcard":
                if parsed_version[0] != clause[1] or parsed_version[1] != clause[2]:
                    return False
                continue

            comparison = self._compare_versions(parsed_version, clause[1])
            matched = {
                ">=": comparison >= 0,
                ">": comparison > 0,
                "<=": comparison <= 0,
                "<": comparison < 0,
                "=": comparison == 0,
            }[operator]
            if not matched:
                return False
        return True

    def version_in_range(self, version, constraint):
        parsed_version = self.parse_version(version)
        clauses = self._parse_constraint(constraint)
        return self._matches_clauses(parsed_version, clauses)

    @staticmethod
    def _version_from_purl(purl):
        identity = purl.split("?", 1)[0]
        return identity.rsplit("@", 1)[1]

    def affected(self, advisory):
        if isinstance(advisory, dict):
            advisory_id = advisory.get("id")
            constraint = advisory.get("constraint")
        elif isinstance(advisory, str):
            advisory_id = None
            constraint = advisory
        else:
            raise SbomError("SBOM_INVALID_ADVISORY", advisory)

        clauses = self._parse_constraint(constraint)

        with self._lock:
            runtime_children = {}
            for parent, child, scope in self._edges:
                if scope == "runtime":
                    runtime_children.setdefault(parent, set()).add(child)

            predecessor = {}
            queue = deque()
            for root in self.roots():
                if "runtime" in self._scopes.get(root, set()):
                    predecessor[root] = None
                    queue.append(root)

            while queue:
                parent = queue.popleft()
                for child in sorted(runtime_children.get(parent, ())):
                    if child not in predecessor:
                        predecessor[child] = parent
                        queue.append(child)

            results = []
            for purl in sorted(predecessor):
                version = self._version_from_purl(purl)
                try:
                    parsed_version = self.parse_version(version)
                except SbomError as exc:
                    if exc.code == "SBOM_INVALID_VERSION":
                        continue
                    raise
                in_range = self._matches_clauses(parsed_version, clauses)
                if in_range:
                    path = []
                    current = purl
                    while current is not None:
                        path.append(current)
                        current = predecessor[current]
                    path.reverse()
                    results.append({
                        "purl": purl,
                        "version": version,
                        "path": path,
                        "advisory": advisory_id,
                    })
            results.sort(key=lambda item: tuple(item["path"]))
            return results

    def paths_to_root(self, component):
        canonical = self.normalize_purl(component)
        with self._lock:
            if canonical not in self._originals:
                return []

            paths = set()
            reverse_path = [canonical]
            stack = [(
                canonical,
                sorted(self._incoming.get(canonical, ())),
                0,
            )]
            while stack:
                node, parents, next_index = stack[-1]
                if next_index >= len(parents):
                    if not parents:
                        paths.add(tuple(reversed(reverse_path)))
                    stack.pop()
                    reverse_path.pop()
                    continue

                parent = parents[next_index]
                stack[-1] = (node, parents, next_index + 1)
                reverse_path.append(parent)
                stack.append((
                    parent,
                    sorted(self._incoming.get(parent, ())),
                    0,
                ))

            return [list(path) for path in sorted(paths)]

    def explain(self, component):
        canonical = self.normalize_purl(component)
        with self._lock:
            originals = self._originals.get(canonical)
            return sorted(originals) if originals else []

    def roots(self):
        with self._lock:
            return sorted(
                purl
                for purl in self._originals
                if not self._incoming.get(purl)
            )

    def components(self):
        with self._lock:
            return list(self._originals)

    def edges(self):
        with self._lock:
            return list(self._edges)

    def stats(self):
        with self._lock:
            return {
                "components": len(self._originals),
                "originals": sum(
                    len(originals) for originals in self._originals.values()
                ),
                "edges": len(self._edges),
            }

    def snapshot(self):
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "components": {
                    purl: sorted(self._scopes.get(purl, set()))
                    for purl in self._originals
                },
                "edges": [list(edge) for edge in self._edges],
                "originals": {
                    purl: sorted(values)
                    for purl, values in self._originals.items()
                },
            }

    @staticmethod
    def _coerce_snapshot_scopes(value):
        if value is None:
            return {SCOPES[0]}
        if isinstance(value, str):
            scopes = {value}
        elif isinstance(value, dict):
            raw_scopes = value.get("scopes", value.get("scope", ()))
            if isinstance(raw_scopes, str):
                scopes = {raw_scopes}
            else:
                scopes = set(raw_scopes)
        elif isinstance(value, (list, tuple, set, frozenset)):
            scopes = set(value)
        else:
            raise SbomError("SBOM_SNAPSHOT_INVALID", "invalid component scope")

        if not scopes:
            return {SCOPES[0]}
        for scope in scopes:
            if scope not in SCOPES:
                raise SbomError("SBOM_SNAPSHOT_INVALID", "invalid component scope")
        return scopes

    @staticmethod
    def _candidate_has_cycle(nodes, edges):
        outgoing = {purl: set() for purl in nodes}
        incoming = {purl: 0 for purl in nodes}
        for parent, child, _scope in edges:
            if child not in outgoing[parent]:
                outgoing[parent].add(child)
                incoming[child] += 1

        queue = deque(purl for purl in nodes if incoming[purl] == 0)
        visited = 0
        while queue:
            node = queue.popleft()
            visited += 1
            for child in outgoing[node]:
                incoming[child] -= 1
                if incoming[child] == 0:
                    queue.append(child)
        return visited != len(nodes)

    def restore(self, state):
        if not isinstance(state, dict):
            raise SbomError("SBOM_SNAPSHOT_INVALID", type(state).__name__)

        version = state.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != _SNAPSHOT_VERSION
        ):
            raise SbomError("SBOM_SNAPSHOT_INVALID", version)

        components = state.get("components")
        edges = state.get("edges")
        originals = state.get("originals")
        if not isinstance(components, dict):
            raise SbomError("SBOM_SNAPSHOT_INVALID", "components")
        if not isinstance(edges, list):
            raise SbomError("SBOM_SNAPSHOT_INVALID", "edges")
        if not isinstance(originals, dict):
            raise SbomError("SBOM_SNAPSHOT_INVALID", "originals")

        try:
            new_originals = {}
            new_scopes = {}

            for raw_purl, scope_value in components.items():
                if not isinstance(raw_purl, str):
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "component purl")
                canonical = self.normalize_purl(raw_purl)
                scopes = self._coerce_snapshot_scopes(scope_value)
                new_originals.setdefault(canonical, set())
                new_scopes.setdefault(canonical, set()).update(scopes)

            for raw_purl, values in originals.items():
                if not isinstance(raw_purl, str) or not isinstance(values, list):
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "originals")
                canonical = self.normalize_purl(raw_purl)
                if canonical not in new_scopes:
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "orphan originals")
                for value in values:
                    if not isinstance(value, str):
                        raise SbomError("SBOM_SNAPSHOT_INVALID", "original purl")
                    if self.normalize_purl(value) != canonical:
                        raise SbomError("SBOM_SNAPSHOT_INVALID", "original purl")
                    new_originals[canonical].add(value)

            new_edges = {}
            for edge in edges:
                if not isinstance(edge, (list, tuple)) or len(edge) != 3:
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "edge")
                parent, child, scope = edge
                if not isinstance(parent, str) or not isinstance(child, str):
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "edge purl")
                self._validate_scope(scope)
                parent_purl = self.normalize_purl(parent)
                child_purl = self.normalize_purl(child)
                if parent_purl not in new_scopes or child_purl not in new_scopes:
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "dangling edge")

                normalized_edge = (parent_purl, child_purl, scope)
                if normalized_edge in new_edges:
                    raise SbomError("SBOM_SNAPSHOT_INVALID", "duplicate edge")
                new_edges[normalized_edge] = None
                new_scopes[parent_purl].add(scope)
                new_scopes[child_purl].add(scope)

            for purl in new_scopes:
                originals = new_originals.setdefault(purl, set())
                if not originals:
                    originals.add(purl)

            if len(new_scopes) > self.max_nodes:
                raise SbomError("SBOM_SNAPSHOT_INVALID", "node limit")
            if len(new_edges) > self.max_edges:
                raise SbomError("SBOM_SNAPSHOT_INVALID", "edge limit")
            if self._candidate_has_cycle(new_scopes, new_edges):
                raise SbomError("SBOM_SNAPSHOT_INVALID", "cycle")

            new_outgoing = {}
            new_incoming = {}
            for parent, child, _scope in new_edges:
                new_outgoing.setdefault(parent, set()).add(child)
                new_incoming.setdefault(child, set()).add(parent)
        except SbomError as exc:
            if exc.code == "SBOM_SNAPSHOT_INVALID":
                raise
            raise SbomError("SBOM_SNAPSHOT_INVALID", exc.code) from exc
        except (TypeError, ValueError) as exc:
            raise SbomError("SBOM_SNAPSHOT_INVALID", type(exc).__name__) from exc

        with self._lock:
            self._originals = new_originals
            self._scopes = new_scopes
            self._edges = new_edges
            self._outgoing = new_outgoing
            self._incoming = new_incoming
