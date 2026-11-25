"""Root-cause fingerprint based false positive suppression registry."""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from collections.abc import Mapping, Sequence
from typing import Any


SCOPES = ("file", "rule", "global")
SCOPE_BREADTH = {"file": 0, "rule": 1, "global": 2}
_SNAPSHOT_VERSION = 1
_STAT_KEYS = (
    "active_entries",
    "suppressed_total",
    "expired_total",
    "filter_calls",
)


class SuppressorError(ValueError):
    """Raised for every rejected operation; ``code`` is a stable string."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _fail(code: str, detail: Any = None) -> SuppressorError:
    return SuppressorError(code, detail)


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_time(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if isinstance(value, float) and value != value:
        return False
    return True


def _require_time(value: Any, code: str):
    if not _is_time(value):
        raise _fail(code, value)
    return value


def _normalize_context_line(line: str) -> str:
    return " ".join(line.strip().split())


def _valid_finding(finding: Any) -> bool:
    if not isinstance(finding, dict):
        return False

    for field in ("rule_id", "path", "line", "context"):
        if field not in finding:
            return False

    rule_id = finding["rule_id"]
    path = finding["path"]
    line = finding["line"]
    context = finding["context"]

    if not isinstance(rule_id, str) or not rule_id:
        return False
    if not isinstance(path, str) or not path:
        return False
    if not _is_integer(line):
        return False
    if isinstance(context, (str, bytes, bytearray, memoryview)):
        return False
    if not isinstance(context, Sequence) or len(context) != 5:
        return False
    return all(isinstance(item, str) for item in context)


class FalsePositiveSuppressor:
    """Track suppression entries by root-cause fingerprint."""

    def __init__(self, *, max_entries=50000, max_reason_len=256):
        self.max_entries = max_entries
        self.max_reason_len = max_reason_len
        self._lock = threading.RLock()

        # fingerprint -> scope -> entry record
        self._entries: dict[str, dict[str, dict[str, Any]]] = {}
        # fingerprint -> cumulative audit information
        self._history: dict[str, dict[str, Any]] = {}
        self._entry_count = 0
        self._suppressed_total = 0
        self._expired_total = 0
        self._filter_calls = 0

    def fingerprint(self, finding) -> str:
        if not _valid_finding(finding):
            raise _fail("SUPPRESS_FINDING_INVALID")

        normalized_context = [
            _normalize_context_line(line) for line in finding["context"]
        ]
        payload = json.dumps(
            [finding["rule_id"], normalized_context],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def suppress(self, fingerprint, reason, until, scope="file") -> dict:
        if not isinstance(fingerprint, str) or not fingerprint:
            raise _fail("SUPPRESS_FINGERPRINT_INVALID", fingerprint)
        if not isinstance(scope, str) or scope not in SCOPE_BREADTH:
            raise _fail("SUPPRESS_SCOPE_INVALID", scope)
        if not isinstance(reason, str):
            raise _fail("SUPPRESS_REASON_INVALID", reason)
        if len(reason) > self.max_reason_len:
            raise _fail("SUPPRESS_REASON_TOO_LONG", reason)
        _require_time(until, "SUPPRESS_UNTIL_INVALID")

        with self._lock:
            scopes = self._entries.get(fingerprint)
            record = None if scopes is None else scopes.get(scope)

            if record is None and self._entry_count >= self.max_entries:
                raise _fail("SUPPRESS_CAPACITY_EXCEEDED", self.max_entries)

            history = self._history.setdefault(
                fingerprint,
                {"count": 0, "last_until": None, "scopes": []},
            )

            if record is None:
                if scopes is None:
                    scopes = {}
                    self._entries[fingerprint] = scopes
                record = {
                    "fingerprint": fingerprint,
                    "reason": reason,
                    "until": until,
                    "scope": scope,
                    "count": 1,
                }
                scopes[scope] = record
                self._entry_count += 1
                effective_until = until
            else:
                record["count"] += 1
                record["reason"] = reason
                effective_until = max(record["until"], until)
                record["until"] = effective_until

            history["count"] += 1
            previous_until = history["last_until"]
            if previous_until is None or effective_until > previous_until:
                history["last_until"] = effective_until
            if scope not in history["scopes"]:
                history["scopes"].append(scope)
                history["scopes"].sort()

            self._suppressed_total += 1
            return copy.deepcopy(record)

    def filter(self, findings, at):
        _require_time(at, "SUPPRESS_AT_INVALID")
        try:
            finding_list = list(findings)
        except TypeError as exc:
            raise _fail("SUPPRESS_FINDING_INVALID", "findings") from exc

        prepared = []
        for finding in finding_list:
            fingerprint = self.fingerprint(finding)
            prepared.append((finding, fingerprint))

        with self._lock:
            self._filter_calls += 1
            kept = []
            suppressed = []

            for finding, fingerprint in prepared:
                record = self._active_record_locked(fingerprint, at)
                if record is None:
                    kept.append(finding)
                else:
                    suppressed.append((finding, copy.deepcopy(record)))

            return kept, suppressed

    def expire(self, at) -> list[str]:
        _require_time(at, "SUPPRESS_AT_INVALID")

        reopened = []
        with self._lock:
            for fingerprint, scopes in list(self._entries.items()):
                for scope, record in list(scopes.items()):
                    if record["until"] <= at:
                        del scopes[scope]
                        self._entry_count -= 1
                        self._expired_total += 1

                if not scopes:
                    del self._entries[fingerprint]
                    reopened.append(fingerprint)

            reopened.sort()
            return reopened

    def diff(self, baseline, current, at) -> dict:
        _require_time(at, "SUPPRESS_AT_INVALID")
        try:
            baseline_list = list(baseline)
            current_list = list(current)
        except TypeError as exc:
            raise _fail("SUPPRESS_FINDING_INVALID", "findings") from exc

        baseline_items = [
            (finding, self.fingerprint(finding)) for finding in baseline_list
        ]
        current_items = [
            (finding, self.fingerprint(finding)) for finding in current_list
        ]

        with self._lock:
            baseline_groups = self._group_by_location(baseline_items)
            current_groups = self._group_by_location(current_items)

            unmatched_baseline = set(range(len(baseline_items)))
            unmatched_current = set(range(len(current_items)))
            changed = []

            keys = list(baseline_groups)
            keys.extend(
                key for key in current_groups if key not in baseline_groups
            )

            for path, line in keys:
                key = (path, line)
                baseline_indexes = baseline_groups.get(key, [])
                current_indexes = current_groups.get(key, [])

                same_baseline, same_current = self._match_same_fingerprints(
                    baseline_indexes,
                    current_indexes,
                    baseline_items,
                    current_items,
                )
                unmatched_baseline.difference_update(same_baseline)
                unmatched_current.difference_update(same_current)

                remaining_baseline = [
                    index
                    for index in baseline_indexes
                    if index in unmatched_baseline
                ]
                remaining_current = [
                    index
                    for index in current_indexes
                    if index in unmatched_current
                ]

                for baseline_index, current_index in zip(
                    remaining_baseline, remaining_current
                ):
                    from_fingerprint = baseline_items[baseline_index][1]
                    to_fingerprint = current_items[current_index][1]
                    changed.append(
                        {
                            "path": path,
                            "line": line,
                            "from": from_fingerprint,
                            "to": to_fingerprint,
                        }
                    )
                    unmatched_baseline.discard(baseline_index)
                    unmatched_current.discard(current_index)

            added = []
            for index, (finding, fingerprint) in enumerate(current_items):
                if index not in unmatched_current:
                    continue
                if self._active_record_locked(fingerprint, at) is None:
                    added.append(finding)

            removed = [
                baseline_items[index][0]
                for index in range(len(baseline_items))
                if index in unmatched_baseline
            ]

            return {
                "added": added,
                "removed": removed,
                "changed": changed,
            }

    def entries(self) -> list[dict]:
        with self._lock:
            result = []
            for scopes in self._entries.values():
                result.extend(
                    copy.deepcopy(record) for record in scopes.values()
                )
            return result

    def history(self, fingerprint) -> dict:
        with self._lock:
            history = self._history.get(fingerprint)
            if history is None:
                return {"count": 0, "last_until": None, "scopes": []}
            return copy.deepcopy(history)

    def is_suppressed(self, fingerprint, at) -> bool:
        if not isinstance(fingerprint, str) or not fingerprint:
            raise _fail("SUPPRESS_FINGERPRINT_INVALID", fingerprint)
        _require_time(at, "SUPPRESS_AT_INVALID")

        with self._lock:
            return self._active_record_locked(fingerprint, at) is not None

    def snapshot(self) -> dict:
        with self._lock:
            entries = []
            for scopes in self._entries.values():
                entries.extend(
                    copy.deepcopy(record) for record in scopes.values()
                )

            return {
                "version": _SNAPSHOT_VERSION,
                "entries": entries,
                "history": copy.deepcopy(self._history),
                "stats": self._stats_locked(),
            }

    def restore(self, state) -> None:
        parsed = self._parse_snapshot(state)
        with self._lock:
            self._entries = parsed["entries"]
            self._history = parsed["history"]
            self._entry_count = parsed["entry_count"]
            self._suppressed_total = parsed["suppressed_total"]
            self._expired_total = parsed["expired_total"]
            self._filter_calls = parsed["filter_calls"]

    def stats(self) -> dict:
        with self._lock:
            return self._stats_locked()

    def reset_stats(self) -> None:
        with self._lock:
            self._suppressed_total = 0
            self._expired_total = 0
            self._filter_calls = 0

    def _stats_locked(self) -> dict:
        return {
            "active_entries": self._entry_count,
            "suppressed_total": self._suppressed_total,
            "expired_total": self._expired_total,
            "filter_calls": self._filter_calls,
        }

    def _active_record_locked(self, fingerprint: str, at):
        scopes = self._entries.get(fingerprint)
        if not scopes:
            return None

        active = [
            record for record in scopes.values() if record["until"] > at
        ]
        if not active:
            return None
        return max(active, key=lambda record: SCOPE_BREADTH[record["scope"]])

    @staticmethod
    def _group_by_location(items):
        groups = {}
        for index, (finding, _fingerprint) in enumerate(items):
            key = (finding["path"], finding["line"])
            groups.setdefault(key, []).append(index)
        return groups

    @staticmethod
    def _match_same_fingerprints(
        baseline_indexes,
        current_indexes,
        baseline_items,
        current_items,
    ):
        baseline_by_fp = {}
        current_by_fp = {}

        for index in baseline_indexes:
            fingerprint = baseline_items[index][1]
            baseline_by_fp.setdefault(fingerprint, []).append(index)
        for index in current_indexes:
            fingerprint = current_items[index][1]
            current_by_fp.setdefault(fingerprint, []).append(index)

        matched_baseline = set()
        matched_current = set()
        for fingerprint, baseline_matches in baseline_by_fp.items():
            current_matches = current_by_fp.get(fingerprint)
            if not current_matches:
                continue
            common = min(len(baseline_matches), len(current_matches))
            for offset in range(common):
                matched_baseline.add(baseline_matches[offset])
                matched_current.add(current_matches[offset])

        return matched_baseline, matched_current

    @classmethod
    def _parse_snapshot(cls, state: Any) -> dict[str, Any]:
        try:
            data = copy.deepcopy(state)
        except Exception as exc:
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "state") from exc

        if not isinstance(data, Mapping):
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "state")
        version = data.get("version")
        if isinstance(version, bool) or version != _SNAPSHOT_VERSION:
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "version")

        for key in ("entries", "history", "stats"):
            if key not in data:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", key)

        raw_entries = data["entries"]
        raw_history = data["history"]
        raw_stats = data["stats"]

        if not isinstance(raw_entries, list):
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "entries")
        if not isinstance(raw_history, Mapping):
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "history")
        if not isinstance(raw_stats, Mapping):
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "stats")

        entries: dict[str, dict[str, dict[str, Any]]] = {}
        seen_entry_keys = set()
        for raw_entry in raw_entries:
            if not isinstance(raw_entry, Mapping):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "entry")

            fingerprint = raw_entry.get("fingerprint")
            reason = raw_entry.get("reason")
            until = raw_entry.get("until")
            scope = raw_entry.get("scope")
            count = raw_entry.get("count")

            if not isinstance(fingerprint, str) or not fingerprint:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "fingerprint")
            if not isinstance(reason, str):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "reason")
            if not _is_time(until):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "until")
            if not isinstance(scope, str) or scope not in SCOPE_BREADTH:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "scope")
            if not _is_integer(count) or count < 1:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "count")

            entry_key = (fingerprint, scope)
            if entry_key in seen_entry_keys:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "duplicate entry")
            seen_entry_keys.add(entry_key)

            entry = {
                "fingerprint": fingerprint,
                "reason": reason,
                "until": until,
                "scope": scope,
                "count": count,
            }
            entries.setdefault(fingerprint, {})[scope] = entry

        history: dict[str, dict[str, Any]] = {}
        for fingerprint, raw_item in raw_history.items():
            if not isinstance(fingerprint, str) or not fingerprint:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "history key")
            if not isinstance(raw_item, Mapping):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "history item")

            count = raw_item.get("count")
            last_until = raw_item.get("last_until")
            scopes = raw_item.get("scopes")

            if not _is_integer(count) or count < 0:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "history count")
            if last_until is not None and not _is_time(last_until):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "last_until")
            if not isinstance(scopes, list):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "scopes")
            if any(not isinstance(scope, str) or scope not in SCOPE_BREADTH
                   for scope in scopes):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "history scope")
            if len(set(scopes)) != len(scopes):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "duplicate scope")

            history[fingerprint] = {
                "count": count,
                "last_until": last_until,
                "scopes": sorted(scopes),
            }

        for fingerprint, scopes in entries.items():
            audit = history.get(fingerprint)
            if audit is None:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "missing history")
            if set(scopes).difference(audit["scopes"]):
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "entry scopes")
            entry_counts = sum(entry["count"] for entry in scopes.values())
            if audit["count"] < entry_counts:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", "history count")

        for key in _STAT_KEYS:
            if key not in raw_stats:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", f"stats.{key}")
            value = raw_stats[key]
            if not _is_integer(value) or value < 0:
                raise _fail("SUPPRESS_SNAPSHOT_INVALID", f"stats.{key}")

        entry_count = len(seen_entry_keys)
        if raw_stats["active_entries"] != entry_count:
            raise _fail("SUPPRESS_SNAPSHOT_INVALID", "active_entries")

        return {
            "entries": entries,
            "history": history,
            "entry_count": entry_count,
            "suppressed_total": raw_stats["suppressed_total"],
            "expired_total": raw_stats["expired_total"],
            "filter_calls": raw_stats["filter_calls"],
        }
