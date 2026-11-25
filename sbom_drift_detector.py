"""Detect component and dependency drift between two SBOM snapshots."""

from __future__ import annotations

import copy
import json
import re
import threading
from datetime import date, datetime, time, timezone

DEFAULT_MAX_COMPONENTS = 100000
SIDES = ("base", "current")
SCOPES = ("runtime", "development")
DIFF_CATEGORIES = (
    "added",
    "removed",
    "upgraded",
    "downgraded",
    "scope_changed",
    "unknown",
)
SNAPSHOT_VERSION = 1

_VERSION_RE = re.compile(r"^[vV]?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


class SbomDriftError(ValueError):
    """Raised for every rejected snapshot or comparison request."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _normalize_purl(value):
    if not isinstance(value, str):
        raise SbomDriftError("INVALID_PURL", "purl must be a string")

    raw = value.strip()
    if not raw or not raw[:4].lower() == "pkg:":
        raise SbomDriftError("INVALID_PURL", "purl must start with pkg:")
    if any(character.isspace() for character in raw):
        raise SbomDriftError("INVALID_PURL", "purl must not contain whitespace")

    without_fragment, marker, fragment = raw.partition("#")
    body, query_marker, query = without_fragment.partition("?")
    body = body.lower()
    if not body[4:]:
        raise SbomDriftError("INVALID_PURL", "purl must contain a package name")

    qualifiers = []
    if query_marker and query:
        for item in query.split("&"):
            if not item:
                continue
            key, separator, qualifier_value = item.partition("=")
            if not key:
                raise SbomDriftError("INVALID_PURL", "empty purl qualifier")
            qualifiers.append(
                (key.lower(), qualifier_value.lower(), bool(separator))
            )
        qualifiers.sort(key=lambda item: (item[0], item[1]))

    normalized = body
    if qualifiers:
        parts = []
        for key, qualifier_value, has_value in qualifiers:
            if has_value:
                parts.append(key + "=" + qualifier_value)
            else:
                parts.append(key)
        normalized += "?" + "&".join(parts)
    if marker:
        normalized += "#" + fragment.lower()
    return normalized


def _parse_version(value):
    if not isinstance(value, str):
        return None
    match = _VERSION_RE.match(value.strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())


def _parse_time(value, end_of_day=False):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.max if end_of_day else time.min)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        date_only = len(text) == 10 and text[4] == "-" and text[7] == "-"
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if date_only and end_of_day:
            parsed = datetime.combine(parsed.date(), time.max)
    else:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _validate_until(until):
    if until is None:
        return
    if not isinstance(until, (str, date, datetime)):
        raise SbomDriftError("INVALID_UNTIL", "until must be an ISO date or time")
    if _parse_time(until, end_of_day=True) is None:
        raise SbomDriftError("INVALID_UNTIL", "until must be an ISO date or time")


def _component_ref(value):
    if isinstance(value, str):
        return _normalize_purl(value)
    if isinstance(value, dict) and "purl" in value:
        return _normalize_purl(value["purl"])
    raise SbomDriftError("INVALID_PURL", "component must identify a purl")


def _normalize_component(value):
    if not isinstance(value, dict):
        raise SbomDriftError("INVALID_COMPONENT", "component must be a mapping")

    purl = _normalize_purl(value.get("purl"))

    version = value.get("version")
    if not isinstance(version, str) or not version.strip():
        raise SbomDriftError("INVALID_COMPONENT", "version must not be empty")

    scope = value.get("scope")
    if scope not in SCOPES:
        raise SbomDriftError("INVALID_SCOPE", "scope must be runtime or development")

    return {
        "purl": purl,
        "version": version.strip(),
        "scope": scope,
    }


def _resolve_edge_endpoint(purl, components):
    if purl in components:
        return purl
    if "?" in purl or "#" in purl:
        raise SbomDriftError("UNKNOWN_COMPONENT", purl)

    matches = [
        component_purl
        for component_purl in components
        if component_purl.partition("?")[0].partition("#")[0] == purl
    ]
    if len(matches) == 1:
        return matches[0]
    raise SbomDriftError("UNKNOWN_COMPONENT", purl)


def _scope_changed(base, current):
    return base["scope"] != current["scope"]


def _classify(base, current):
    if base is None:
        return "added"
    if current is None:
        return "removed"

    base_version = _parse_version(base["version"])
    current_version = _parse_version(current["version"])
    if base_version is None or current_version is None:
        return "unknown"
    if current_version < base_version:
        return "downgraded"
    if _scope_changed(base, current):
        return "scope_changed"
    if current_version > base_version:
        return "upgraded"
    return "unchanged"


class SbomDriftDetector:
    """Compare a baseline and a current component graph."""

    def __init__(self, *, max_components=DEFAULT_MAX_COMPONENTS):
        if (
            not isinstance(max_components, int)
            or isinstance(max_components, bool)
            or max_components < 0
        ):
            raise ValueError("max_components must be a non-negative integer")

        self.max_components = max_components
        self._lock = threading.RLock()
        self._components = {side: {} for side in SIDES}
        self._edges = {side: set() for side in SIDES}
        self._loaded = {side: False for side in SIDES}
        self._whitelist = {}
        self._diffs = 0
        self._last_diff_signature = None

    def _ensure_component_known(self, purl):
        if (
            purl not in self._components["base"]
            and purl not in self._components["current"]
        ):
            raise SbomDriftError("UNKNOWN_COMPONENT", purl)

    def _is_whitelisted(self, purl, at=None):
        entry = self._whitelist.get(purl)
        if entry is None:
            return False
        until = entry["until"]
        if until is None:
            return True
        deadline = _parse_time(until, end_of_day=True)
        if deadline is None:
            return False
        moment = _parse_time(at) if at is not None else datetime.now(timezone.utc)
        if moment is None:
            moment = datetime.now(timezone.utc)
        return moment <= deadline

    def load(self, side, components, edges):
        if side not in SIDES:
            raise SbomDriftError("INVALID_SIDE", side)

        if components is None:
            components = []
        if edges is None:
            edges = []

        try:
            raw_components = list(components)
        except TypeError:
            raise SbomDriftError("INVALID_COMPONENT", "components must be iterable")
        try:
            raw_edges = list(edges)
        except TypeError:
            raise SbomDriftError("INVALID_EDGE", "edges must be iterable")

        if len(raw_components) > self.max_components:
            raise SbomDriftError(
                "SBOM_CAPACITY_EXCEEDED",
                "%s has more than %d components"
                % (side, self.max_components),
            )

        with self._lock:
            normalized_components = {}
            for component in raw_components:
                normalized = _normalize_component(component)
                purl = normalized["purl"]
                if purl in normalized_components:
                    raise SbomDriftError("DUPLICATE_COMPONENT", purl)
                normalized_components[purl] = normalized

            if len(normalized_components) > self.max_components:
                raise SbomDriftError(
                    "SBOM_CAPACITY_EXCEEDED",
                    "%s has more than %d components"
                    % (side, self.max_components),
                )

            normalized_edges = set()
            for edge in raw_edges:
                if not isinstance(edge, dict):
                    raise SbomDriftError("INVALID_EDGE", "edge must be a mapping")
                if "from" not in edge or "to" not in edge:
                    raise SbomDriftError(
                        "UNKNOWN_COMPONENT", "edge endpoint is missing"
                    )

                source = _resolve_edge_endpoint(
                    _normalize_purl(edge["from"]), normalized_components
                )
                target = _resolve_edge_endpoint(
                    _normalize_purl(edge["to"]), normalized_components
                )
                normalized_edges.add((source, target))

            self._components[side] = normalized_components
            self._edges[side] = normalized_edges
            self._loaded[side] = True

        return {
            "side": side,
            "components": len(normalized_components),
            "edges": len(normalized_edges),
        }

    def diff(self, at=None):
        with self._lock:
            if not self._loaded["base"] or not self._loaded["current"]:
                raise SbomDriftError("SBOM_INCOMPLETE", "both sides must be loaded")

            report = {category: [] for category in DIFF_CATEGORIES}
            base_components = self._components["base"]
            current_components = self._components["current"]

            for purl in sorted(set(base_components) | set(current_components)):
                base = base_components.get(purl)
                current = current_components.get(purl)
                category = _classify(base, current)

                if category == "added":
                    entry = copy.deepcopy(current)
                elif category == "removed":
                    entry = copy.deepcopy(base)
                elif category in ("upgraded", "downgraded", "unknown"):
                    entry = {
                        "purl": purl,
                        "from": base["version"],
                        "to": current["version"],
                    }
                elif category == "scope_changed":
                    entry = {
                        "purl": purl,
                        "from": base["scope"],
                        "to": current["scope"],
                    }
                else:
                    continue

                if self._is_whitelisted(purl, at):
                    entry["whitelisted"] = True
                report[category].append(entry)

            for category in DIFF_CATEGORIES:
                report[category].sort(key=lambda item: item["purl"])

            signature = json.dumps(report, sort_keys=True, separators=(",", ":"))
            if signature != self._last_diff_signature:
                self._diffs += 1
                self._last_diff_signature = signature

            return copy.deepcopy(report)

    def explain(self, component):
        purl = _component_ref(component)
        with self._lock:
            base = self._components["base"].get(purl)
            current = self._components["current"].get(purl)
            if base is None and current is None:
                raise SbomDriftError("UNKNOWN_COMPONENT", purl)

            return {
                "purl": purl,
                "category": _classify(base, current),
                "from": None if base is None else base["version"],
                "to": None if current is None else current["version"],
                "scope_from": None if base is None else base["scope"],
                "scope_to": None if current is None else current["scope"],
            }

    def accept(self, component, until=None, note=None):
        purl = _component_ref(component)
        _validate_until(until)
        with self._lock:
            self._ensure_component_known(purl)
            entry = {
                "purl": purl,
                "until": copy.deepcopy(until),
                "note": copy.deepcopy(note),
            }
            self._whitelist[purl] = entry
            return copy.deepcopy(entry)

    def revoke(self, component):
        purl = _component_ref(component)
        with self._lock:
            if purl not in self._whitelist:
                return False
            del self._whitelist[purl]
            return True

    def whitelist(self):
        with self._lock:
            return [
                copy.deepcopy(self._whitelist[purl])
                for purl in sorted(self._whitelist)
            ]

    def components(self, side):
        if side not in SIDES:
            raise SbomDriftError("INVALID_SIDE", side)
        with self._lock:
            return [
                copy.deepcopy(self._components[side][purl])
                for purl in sorted(self._components[side])
            ]

    def snapshot(self):
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "loaded": dict(self._loaded),
                "components": {
                    side: [
                        copy.deepcopy(self._components[side][purl])
                        for purl in sorted(self._components[side])
                    ]
                    for side in SIDES
                },
                "edges": {
                    side: [list(edge) for edge in sorted(self._edges[side])]
                    for side in SIDES
                },
                "whitelist": [
                    copy.deepcopy(self._whitelist[purl])
                    for purl in sorted(self._whitelist)
                ],
                "diffs": self._diffs,
                "last_diff_signature": self._last_diff_signature,
            }

    def restore(self, state):
        try:
            candidate = copy.deepcopy(state)
        except Exception as exc:
            raise SbomDriftError("SBOM_SNAPSHOT_INVALID", str(exc))

        def invalid(detail):
            raise SbomDriftError("SBOM_SNAPSHOT_INVALID", detail)

        if not isinstance(candidate, dict):
            invalid("snapshot must be a mapping")
        if type(candidate.get("version")) is not int:
            invalid("snapshot version is missing")
        if candidate.get("version") != SNAPSHOT_VERSION:
            invalid("unsupported snapshot version")

        loaded = candidate.get("loaded")
        if not isinstance(loaded, dict) or set(loaded) != set(SIDES):
            invalid("snapshot loaded flags are invalid")
        if any(type(loaded[side]) is not bool for side in SIDES):
            invalid("snapshot loaded flags are invalid")

        raw_components = candidate.get("components")
        if not isinstance(raw_components, dict):
            invalid("snapshot components are invalid")

        new_components = {}
        for side in SIDES:
            items = raw_components.get(side)
            if not isinstance(items, list):
                invalid("snapshot %s components are invalid" % side)
            if len(items) > self.max_components:
                raise SbomDriftError(
                    "SBOM_CAPACITY_EXCEEDED",
                    "%s has more than %d components"
                    % (side, self.max_components),
                )
            side_components = {}
            for item in items:
                try:
                    normalized = _normalize_component(item)
                except SbomDriftError:
                    invalid("snapshot %s component is invalid" % side)
                purl = normalized["purl"]
                if purl in side_components:
                    invalid("snapshot contains duplicate components")
                side_components[purl] = normalized
            new_components[side] = side_components

        raw_edges = candidate.get("edges")
        if not isinstance(raw_edges, dict):
            invalid("snapshot edges are invalid")

        new_edges = {}
        for side in SIDES:
            items = raw_edges.get(side)
            if not isinstance(items, list):
                invalid("snapshot %s edges are invalid" % side)
            side_edges = set()
            for item in items:
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    invalid("snapshot edge is invalid")
                try:
                    source = _normalize_purl(item[0])
                    target = _normalize_purl(item[1])
                except SbomDriftError:
                    invalid("snapshot edge is invalid")
                if (
                    source not in new_components[side]
                    or target not in new_components[side]
                ):
                    invalid("snapshot edge references an unknown component")
                side_edges.add((source, target))
            new_edges[side] = side_edges

        raw_whitelist = candidate.get("whitelist")
        if not isinstance(raw_whitelist, list):
            invalid("snapshot whitelist is invalid")

        new_whitelist = {}
        for entry in raw_whitelist:
            if not isinstance(entry, dict):
                invalid("snapshot whitelist entry is invalid")
            try:
                purl = _normalize_purl(entry.get("purl"))
            except SbomDriftError:
                invalid("snapshot whitelist purl is invalid")
            if purl in new_whitelist:
                invalid("snapshot whitelist contains duplicates")
            until = entry.get("until")
            try:
                _validate_until(until)
            except SbomDriftError:
                invalid("snapshot whitelist expiry is invalid")
            new_whitelist[purl] = {
                "purl": purl,
                "until": copy.deepcopy(until),
                "note": copy.deepcopy(entry.get("note")),
            }

        diffs = candidate.get("diffs")
        if type(diffs) is not int or diffs < 0:
            invalid("snapshot diff counter is invalid")

        signature = candidate.get("last_diff_signature")
        if signature is not None and not isinstance(signature, str):
            invalid("snapshot diff signature is invalid")

        with self._lock:
            self._components = new_components
            self._edges = new_edges
            self._loaded = {side: loaded[side] for side in SIDES}
            self._whitelist = new_whitelist
            self._diffs = diffs
            self._last_diff_signature = signature

    def stats(self):
        with self._lock:
            all_edges = set()
            for side in SIDES:
                all_edges.update(self._edges[side])
            return {
                "base_components": len(self._components["base"]),
                "current_components": len(self._components["current"]),
                "edges": len(all_edges),
                "whitelisted": len(self._whitelist),
                "diffs": self._diffs,
            }

    def reset_stats(self):
        with self._lock:
            self._diffs = 0
            self._last_diff_signature = None
