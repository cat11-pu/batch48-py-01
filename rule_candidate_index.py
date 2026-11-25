"""Conservative candidate index for detection rules."""

from __future__ import annotations

from copy import deepcopy
import os
import threading

DEFAULT_MAX_ENTRIES = 200000
DEFAULT_MAX_KEYWORDS = 16
SNAPSHOT_VERSION = 1


class RuleIndexError(ValueError):
    """Raised for every rejected rule registration or lookup."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RuleCandidateIndex:
    """Keep rules addressable by file kind and header keywords."""

    def __init__(self, *, max_entries=DEFAULT_MAX_ENTRIES,
                 max_keywords=DEFAULT_MAX_KEYWORDS):
        self.max_entries = max_entries
        self.max_keywords = max_keywords
        self._lock = threading.RLock()
        self._rules: dict[str, dict] = {}
        self._audit: dict[str, dict] = {}
        self._stats = {"calls": 0, "served": 0, "pruned": 0}

    def register(self, rule_id, file_kinds, keywords, cost=1):
        normalized_id = self._validate_rule_id(rule_id)
        normalized_cost = self._validate_cost(cost)
        normalized_kinds = self._normalize_kinds(file_kinds)
        normalized_keywords = self._normalize_keywords(keywords)

        with self._lock:
            if normalized_id in self._rules:
                raise RuleIndexError(
                    "RULE_DUPLICATE",
                    f"rule {normalized_id!r} is already registered",
                )
            if len(self._rules) >= self.max_entries:
                raise RuleIndexError(
                    "RULE_CAPACITY_EXCEEDED",
                    f"candidate index is full at {self.max_entries} entries",
                )

            entry = {
                "rule_id": normalized_id,
                "file_kinds": normalized_kinds,
                "keywords": normalized_keywords,
                "cost": normalized_cost,
            }
            self._rules[normalized_id] = entry
            self._audit.setdefault(normalized_id, self._new_audit())
            return deepcopy(entry)

    def candidates(self, path, head_bytes, whole_file=False):
        extension = self._path_extension(path)
        sample = self._as_sample(head_bytes)

        with self._lock:
            self._stats["calls"] += 1
            ordered_rules = sorted(
                self._rules.values(),
                key=lambda entry: (entry["cost"], entry["rule_id"]),
            )
            result = []
            seen = set()

            for entry in ordered_rules:
                candidate, basis = self._evaluate(
                    entry, extension, sample, bool(whole_file)
                )
                audit = self._audit.setdefault(
                    entry["rule_id"], self._new_audit()
                )
                audit["last_basis"] = basis

                if candidate:
                    audit["hits"] += 1
                    self._stats["served"] += 1
                    if entry["rule_id"] not in seen:
                        seen.add(entry["rule_id"])
                        result.append(entry["rule_id"])
                else:
                    audit["pruned"] += 1
                    self._stats["pruned"] += 1

            return result

    def explain(self, rule_id, path, head_bytes, whole_file=False):
        normalized_id = self._validate_rule_id(rule_id)
        extension = self._path_extension(path)
        sample = self._as_sample(head_bytes)

        with self._lock:
            entry = self._rules.get(normalized_id)
            if entry is None:
                raise RuleIndexError(
                    "RULE_NOT_FOUND",
                    f"rule {normalized_id!r} is not active",
                )
            candidate, basis = self._evaluate(
                entry, extension, sample, bool(whole_file)
            )
            return {
                "rule_id": normalized_id,
                "candidate": candidate,
                "basis": basis,
            }

    def audit(self, rule_id):
        normalized_id = self._validate_rule_id(rule_id)
        with self._lock:
            record = self._audit.get(normalized_id)
            if record is None:
                raise RuleIndexError(
                    "RULE_NOT_FOUND",
                    f"rule {normalized_id!r} has no audit record",
                )
            return {
                "rule_id": normalized_id,
                "hits": record["hits"],
                "pruned": record["pruned"],
                "last_basis": record["last_basis"],
                "active": normalized_id in self._rules,
            }

    def remove(self, rule_id):
        normalized_id = self._validate_rule_id(rule_id)
        with self._lock:
            if normalized_id not in self._rules:
                return False
            del self._rules[normalized_id]
            self._audit.setdefault(normalized_id, self._new_audit())
            return True

    def rules(self):
        with self._lock:
            return deepcopy(list(self._rules.values()))

    def snapshot(self):
        with self._lock:
            return deepcopy(
                {
                    "version": SNAPSHOT_VERSION,
                    "rules": list(self._rules.values()),
                }
            )

    def restore(self, state):
        new_rules = self._parse_snapshot(state)
        with self._lock:
            self._rules = new_rules
            self._audit = {
                rule_id: self._new_audit() for rule_id in new_rules
            }
            self._stats = {"calls": 0, "served": 0, "pruned": 0}

    def stats(self):
        with self._lock:
            return {
                "rules": len(self._rules),
                "calls": self._stats["calls"],
                "served": self._stats["served"],
                "pruned": self._stats["pruned"],
            }

    def reset_stats(self):
        with self._lock:
            self._stats["calls"] = 0
            self._stats["served"] = 0
            self._stats["pruned"] = 0

    @staticmethod
    def _new_audit():
        return {"hits": 0, "pruned": 0, "last_basis": None}

    @staticmethod
    def _validate_rule_id(rule_id):
        if rule_id is None:
            raise RuleIndexError(
                "RULE_ID_INVALID", "rule_id must not be empty"
            )
        try:
            if len(rule_id) == 0:
                raise RuleIndexError(
                    "RULE_ID_INVALID", "rule_id must not be empty"
                )
        except TypeError:
            pass
        try:
            hash(rule_id)
        except TypeError as exc:
            raise RuleIndexError(
                "RULE_ID_INVALID", "rule_id must be hashable"
            ) from exc
        return rule_id

    @staticmethod
    def _validate_cost(cost):
        if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
            raise RuleIndexError(
                "RULE_COST_INVALID", "cost must be a positive integer"
            )
        return cost

    @classmethod
    def _normalize_kinds(cls, file_kinds):
        if isinstance(file_kinds, (str, bytes)):
            raise RuleIndexError(
                "RULE_KINDS_INVALID",
                "file_kinds must be an iterable of strings",
            )

        try:
            raw_kinds = list(file_kinds)
        except TypeError as exc:
            raise RuleIndexError(
                "RULE_KINDS_INVALID",
                "file_kinds must be an iterable of strings",
            ) from exc

        normalized = set()
        for kind in raw_kinds:
            if not isinstance(kind, str):
                raise RuleIndexError(
                    "RULE_KINDS_INVALID",
                    "every file kind must be a string",
                )
            kind = kind.lower()
            if not kind.startswith("."):
                kind = "." + kind
            normalized.add(kind)
        return sorted(normalized)

    def _normalize_keywords(self, keywords):
        if isinstance(keywords, (str, bytes)):
            raise RuleIndexError(
                "RULE_KEYWORDS_INVALID",
                "keywords must be an iterable of strings",
            )

        try:
            materialized = list(keywords)
        except TypeError as exc:
            raise RuleIndexError(
                "RULE_KEYWORDS_INVALID",
                "keywords must be an iterable of strings",
            ) from exc

        if len(materialized) > self.max_keywords:
            raise RuleIndexError("RULE_KEYWORDS_LIMIT")

        for keyword in materialized:
            if not isinstance(keyword, str):
                raise RuleIndexError(
                    "RULE_KEYWORDS_INVALID",
                    "every keyword must be a string",
                )
        return materialized

    @staticmethod
    def _path_extension(path):
        try:
            path_value = os.fspath(path)
        except TypeError as exc:
            raise RuleIndexError("PATH_INVALID", "path is invalid") from exc

        if isinstance(path_value, bytes):
            path_value = os.fsdecode(path_value)
        elif not isinstance(path_value, str):
            path_value = str(path_value)
        return os.path.splitext(path_value)[1].lower()

    @staticmethod
    def _as_sample(head_bytes):
        if head_bytes is None:
            return b""
        if isinstance(head_bytes, str):
            return head_bytes
        if isinstance(head_bytes, (bytes, bytearray, memoryview)):
            return bytes(head_bytes)
        raise RuleIndexError(
            "HEAD_BYTES_INVALID", "head_bytes must be bytes or a string"
        )

    def _evaluate(self, entry, extension, sample, whole_file):
        kinds = entry["file_kinds"]
        if kinds:
            if not extension:
                retained_basis = "retained"
            elif extension in kinds:
                retained_basis = "kind_matched"
            else:
                return False, "kind_mismatch"
        else:
            retained_basis = "no_kind_declared"

        keywords = entry["keywords"]
        if not keywords:
            return True, "no_keyword_declared"

        for keyword in keywords:
            needle = (
                keyword.encode("utf-8")
                if isinstance(sample, bytes)
                else keyword
            )
            if needle in sample:
                return True, "keyword_matched"

        if whole_file:
            return False, "keyword_miss"

        return True, retained_basis

    def _parse_snapshot(self, state):
        if not isinstance(state, dict):
            raise RuleIndexError("SNAPSHOT_INVALID", "snapshot must be a dict")
        version = state.get("version")
        if type(version) is not int or version != SNAPSHOT_VERSION:
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "unsupported snapshot version"
            )

        required = ("version", "rules")
        if any(field not in state for field in required):
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "snapshot is missing required fields"
            )

        try:
            snapshot = deepcopy(state)
            raw_rules = snapshot["rules"]
            if not isinstance(raw_rules, list):
                raise RuleIndexError(
                    "SNAPSHOT_INVALID", "rules must be a list"
                )
            if len(raw_rules) > self.max_entries:
                raise RuleIndexError(
                    "SNAPSHOT_INVALID", "snapshot exceeds entry limit"
                )

            new_rules = {}
            for raw_entry in raw_rules:
                entry = self._parse_rule_entry(raw_entry)
                rule_id = entry["rule_id"]
                if rule_id in new_rules:
                    raise RuleIndexError(
                        "SNAPSHOT_INVALID",
                        f"duplicate rule id {rule_id!r}",
                    )
                new_rules[rule_id] = entry

            return new_rules
        except RuleIndexError as exc:
            if exc.code == "SNAPSHOT_INVALID":
                raise
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "snapshot rule is invalid"
            ) from exc
        except Exception as exc:
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "snapshot structure is invalid"
            ) from exc

    def _parse_rule_entry(self, raw_entry):
        if not isinstance(raw_entry, dict):
            raise RuleIndexError("SNAPSHOT_INVALID", "rule must be a dict")

        required = ("rule_id", "file_kinds", "keywords", "cost")
        if any(field not in raw_entry for field in required):
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "rule is missing required fields"
            )

        rule_id = self._validate_rule_id(raw_entry["rule_id"])

        cost = raw_entry["cost"]
        if not self._is_positive_int(cost):
            raise RuleIndexError("SNAPSHOT_INVALID", "invalid rule cost")

        raw_kinds = raw_entry["file_kinds"]
        if not isinstance(raw_kinds, list):
            raise RuleIndexError("SNAPSHOT_INVALID", "file_kinds must be a list")
        kinds = self._normalize_kinds(raw_kinds)

        keywords = raw_entry["keywords"]
        if not isinstance(keywords, list) or any(
            not isinstance(keyword, str) for keyword in keywords
        ):
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "keywords must be a list of strings"
            )
        if len(keywords) > self.max_keywords:
            raise RuleIndexError(
                "SNAPSHOT_INVALID", "snapshot exceeds keyword limit"
            )

        return {
            "rule_id": rule_id,
            "file_kinds": kinds,
            "keywords": list(keywords),
            "cost": cost,
        }

    @staticmethod
    def _is_positive_int(value):
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
