"""Detection rule compiler and per-record priority evaluator."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Pattern

MAX_RULES = 4096
MAX_DEPTH = 32

_MISSING = object()
_CMP_OPS = frozenset(("==", "!=", ">", ">=", "<", "<=", "~"))


class DetectionError(ValueError):
    """Refusal raised by the engine; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _CompileFailure(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _glob_pattern(pattern: str) -> str:
    pieces = ["\\A"]
    for char in pattern:
        if char == "*":
            pieces.append(".*")
        elif char == "?":
            pieces.append(".")
        else:
            pieces.append(re.escape(char))
    pieces.append("\\Z")
    return "".join(pieces)


@dataclass(frozen=True)
class _Cmp:
    field: str
    cmp: str
    value: object
    _pattern: Pattern[str] | None = field(
        init=False, repr=False, compare=False, default=None
    )

    def __post_init__(self) -> None:
        if self.cmp == "~":
            # DOTALL keeps '*' and '?' consistent even for embedded newlines.
            object.__setattr__(
                self, "_pattern", re.compile(_glob_pattern(self.value), re.DOTALL)
            )

    @property
    def depth(self) -> int:
        return 1

    @property
    def fields(self) -> tuple[str, ...]:
        return (self.field,)

    def matches(self, record: Mapping) -> bool:
        actual = record.get(self.field, _MISSING)
        if actual is _MISSING:
            return self.cmp == "!="

        if self.cmp == "==":
            return actual == self.value
        if self.cmp == "!=":
            return actual != self.value
        if self.cmp == "~":
            return (
                isinstance(actual, str)
                and self._pattern.fullmatch(actual) is not None
            )
        if not _is_number(actual):
            return False
        if self.cmp == ">":
            return actual > self.value
        if self.cmp == ">=":
            return actual >= self.value
        if self.cmp == "<":
            return actual < self.value
        return actual <= self.value

    def to_tree(self) -> dict:
        return {
            "op": "cmp",
            "field": self.field,
            "cmp": self.cmp,
            "value": self.value,
        }


@dataclass(frozen=True)
class _Bool:
    op: str
    children: tuple[Any, ...]
    depth: int = field(init=False)
    fields: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "depth", 1 + max(child.depth for child in self.children)
        )
        names: set[str] = set()
        for child in self.children:
            names.update(child.fields)
        object.__setattr__(self, "fields", tuple(sorted(names)))

    def matches(self, record: Mapping) -> bool:
        if self.op == "and":
            return all(child.matches(record) for child in self.children)
        return any(child.matches(record) for child in self.children)

    def to_tree(self) -> dict:
        return {
            "op": self.op,
            "children": [child.to_tree() for child in self.children],
        }


@dataclass(frozen=True)
class _Not:
    child: Any
    depth: int = field(init=False)
    fields: tuple[str, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "depth", 1 + self.child.depth)
        object.__setattr__(self, "fields", self.child.fields)

    def matches(self, record: Mapping) -> bool:
        return not self.child.matches(record)

    def to_tree(self) -> dict:
        return {"op": "not", "children": [self.child.to_tree()]}


@dataclass(frozen=True)
class _CompiledRule:
    rule_id: str
    priority: int
    depth: int
    tree: Any
    fields: tuple[str, ...]

    def signature(self) -> tuple:
        return (self.rule_id, self.priority, self.depth, self.fields, self.tree)


@dataclass(frozen=True)
class _Snapshot:
    version: int
    rules: tuple[_CompiledRule, ...]
    by_id: Mapping[str, _CompiledRule]


class _PredicateParser:
    def __init__(self, text: str, max_depth: int) -> None:
        self.text = text
        self.max_depth = max_depth
        self.pos = 0
        self.length = len(text)

    def parse(self) -> Any:
        node = self._parse_expression(0)
        self._skip_space()
        if self.pos != self.length:
            self._syntax("unexpected trailing text")
        if node.depth > self.max_depth:
            raise _CompileFailure(
                "rule.depth_exceeded",
                f"rule depth {node.depth} exceeds limit {self.max_depth} "
                f"in {self.text!r}",
            )
        return node

    def _skip_space(self) -> None:
        while self.pos < self.length and self.text[self.pos].isspace():
            self.pos += 1

    def _peek(self) -> str:
        if self.pos >= self.length:
            return ""
        return self.text[self.pos]

    def _syntax(self, message: str) -> None:
        start = max(0, self.pos - 12)
        end = min(self.length, self.pos + 12)
        fragment = self.text[start:end]
        raise _CompileFailure(
            "rule.syntax", f"{message} near {fragment!r} in {self.text!r}"
        )

    def _read_identifier(self) -> str | None:
        self._skip_space()
        start = self.pos
        while self.pos < self.length:
            char = self.text[self.pos]
            if char.isspace() or char in '(),=<>!~"':
                break
            self.pos += 1
        if self.pos == start:
            return None
        return self.text[start:self.pos]

    def _parse_expression(self, boolean_ancestors: int) -> Any:
        identifier = self._read_identifier()
        if identifier is None:
            self._syntax("expected a field or boolean operator")

        self._skip_space()
        if identifier in ("AND", "OR", "NOT") and self._peek() == "(":
            if boolean_ancestors >= self.max_depth - 1:
                raise _CompileFailure(
                    "rule.depth_exceeded",
                    f"rule depth exceeds limit {self.max_depth} "
                    f"in {self.text!r}",
                )
            return self._parse_boolean(identifier, boolean_ancestors + 1)

        self._skip_space()
        operator = self._read_operator()
        value = self._read_value()
        if operator == "~" and not isinstance(value, str):
            self._syntax("wildcard '~' requires a quoted string value")
        return _Cmp(identifier, operator, value)

    def _parse_boolean(self, operator: str, child_level: int) -> Any:
        self.pos += 1  # Opening parenthesis.
        children = []
        self._skip_space()
        if self._peek() == ")":
            self._syntax(f"{operator} requires at least one operand")

        while True:
            children.append(self._parse_expression(child_level))
            self._skip_space()
            char = self._peek()
            if char == ",":
                self.pos += 1
                continue
            if char == ")":
                self.pos += 1
                break
            self._syntax("expected ',' or ')'")

        if operator == "NOT":
            if len(children) != 1:
                self._syntax("NOT requires exactly one operand")
            return _Not(children[0])
        if len(children) < 2:
            self._syntax(f"{operator} requires at least two operands")
        return _Bool(operator.lower(), tuple(children))

    def _read_operator(self) -> str:
        if self.text.startswith("==", self.pos) or self.text.startswith(
            "!=", self.pos
        ):
            operator = self.text[self.pos:self.pos + 2]
            self.pos += 2
        elif self.text.startswith(">=", self.pos) or self.text.startswith(
            "<=", self.pos
        ):
            operator = self.text[self.pos:self.pos + 2]
            self.pos += 2
        elif self._peek() in (">", "<", "~"):
            operator = self._peek()
            self.pos += 1
        else:
            self._syntax("expected a comparison operator")
        if operator not in _CMP_OPS:
            self._syntax("expected a comparison operator")
        return operator

    def _read_value(self) -> object:
        self._skip_space()
        char = self._peek()
        if char == "":
            self._syntax("expected a value")
        if char == '"':
            return self._read_string()
        if char == "-" or char.isdigit():
            start = self.pos
            if char == "-":
                self.pos += 1
            digits = self.pos
            while self.pos < self.length and self.text[self.pos].isdigit():
                self.pos += 1
            if self.pos == digits:
                self._syntax("expected an integer value")
            if self.pos < self.length and not (
                self.text[self.pos].isspace() or self.text[self.pos] in ",)"
            ):
                self._syntax("invalid integer value")
            return int(self.text[start:self.pos])
        self._syntax("expected a quoted string or integer value")

    def _read_string(self) -> str:
        self.pos += 1  # Opening quote.
        chars: list[str] = []
        escapes = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
        while self.pos < self.length:
            char = self.text[self.pos]
            self.pos += 1
            if char == '"':
                return "".join(chars)
            if char == "\\":
                if self.pos >= self.length:
                    self._syntax("unterminated escape sequence")
                escaped = self.text[self.pos]
                self.pos += 1
                chars.append(escapes.get(escaped, escaped))
            else:
                chars.append(char)
        self._syntax("unterminated string value")


class DetectionRuleEngine:
    """Compile rule text into predicate trees and evaluate records."""

    MAX_RULES = MAX_RULES
    MAX_DEPTH = MAX_DEPTH

    def __init__(self, *, fields=(), default_priority: int = 100,
                 max_rules: int = MAX_RULES, max_depth: int = MAX_DEPTH) -> None:
        self._fields: set[str] = set()
        for name in ((fields,) if isinstance(fields, str) else fields):
            self._add_field(name)
        self._default_priority = default_priority
        self._max_rules = max_rules
        self._max_depth = max_depth
        self._lock = threading.RLock()
        self._snapshot = self._make_snapshot(0, ())
        self._compile_errors: list[dict] = []
        self._counts: dict[str, int] = {}
        self._counted: set[tuple[str, str, int]] = set()
        self._evaluations = 0
        self._hits = 0

    @staticmethod
    def _add_field_to(target: set[str], name: object) -> None:
        if not isinstance(name, str) or not name or any(c.isspace() for c in name):
            raise DetectionError("field.invalid", f"invalid field name: {name!r}")
        target.add(name)

    def _add_field(self, name: object) -> None:
        self._add_field_to(self._fields, name)

    def register_field(self, name: str) -> None:
        with self._lock:
            self._add_field(name)

    def known_fields(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._fields))

    def _make_snapshot(
        self, version: int, rules: list[_CompiledRule] | tuple
    ) -> _Snapshot:
        ordered = tuple(sorted(rules, key=lambda rule: (rule.priority, rule.rule_id)))
        return _Snapshot(version, ordered, {rule.rule_id: rule for rule in ordered})

    def _compile(self, rule_text: object) -> _CompiledRule:
        if not isinstance(rule_text, str):
            raise _CompileFailure(
                "rule.syntax", f"rule text must be a string: {rule_text!r}"
            )
        if "\n" in rule_text or "\r" in rule_text:
            raise _CompileFailure(
                "rule.syntax", f"rule must be a single line: {rule_text!r}"
            )

        match = re.match(
            r"^\s*RULE\s+(\S+)\s+(?:PRIORITY\s+(-?\d+)\s+)?WHEN\s+(.+?)\s*$",
            rule_text,
        )
        if match is None:
            raise _CompileFailure(
                "rule.syntax", f"invalid rule header: {rule_text!r}"
            )

        rule_id = match.group(1)
        priority = (
            int(match.group(2))
            if match.group(2) is not None
            else self._default_priority
        )
        predicate_text = match.group(3)
        tree = _PredicateParser(predicate_text, self._max_depth).parse()

        unknown = next(
            (name for name in tree.fields if name not in self._fields), None
        )
        if unknown is not None:
            raise _CompileFailure(
                "rule.unknown_field",
                f"unknown field {unknown!r} in rule {rule_id!r}: {rule_text!r}",
            )
        return _CompiledRule(rule_id, priority, tree.depth, tree, tree.fields)

    @staticmethod
    def _error_entry(index: int, failure: _CompileFailure, text: object) -> dict:
        return {
            "index": index,
            "code": failure.code,
            "detail": failure.detail,
            "text": text if isinstance(text, str) else repr(text),
        }

    def add_rule(self, rule_text: str) -> dict:
        with self._lock:
            try:
                rule = self._compile(rule_text)
            except _CompileFailure as failure:
                self._compile_errors = [
                    self._error_entry(0, failure, rule_text)
                ]
                return {
                    "accepted": False,
                    "code": failure.code,
                    "detail": failure.detail,
                }

            if rule.rule_id in self._snapshot.by_id:
                failure = _CompileFailure(
                    "rule.duplicate_id",
                    f"duplicate rule id {rule.rule_id!r} in {rule_text!r}",
                )
                self._compile_errors = [
                    self._error_entry(0, failure, rule_text)
                ]
                return {
                    "accepted": False,
                    "code": failure.code,
                    "detail": failure.detail,
                }

            if len(self._snapshot.rules) >= self._max_rules:
                failure = _CompileFailure(
                    "rule.limit",
                    f"rule limit {self._max_rules} reached; "
                    f"cannot add {rule.rule_id!r}",
                )
                self._compile_errors = [
                    self._error_entry(0, failure, rule_text)
                ]
                return {
                    "accepted": False,
                    "code": failure.code,
                    "detail": failure.detail,
                }

            rules = list(self._snapshot.rules)
            rules.append(rule)
            self._snapshot = self._make_snapshot(
                self._snapshot.version + 1, rules
            )
            self._compile_errors = []
            return {
                "accepted": True,
                "rule_id": rule.rule_id,
                "priority": rule.priority,
                "depth": rule.depth,
            }

    def remove_rule(self, rule_id: str) -> bool:
        with self._lock:
            if rule_id not in self._snapshot.by_id:
                return False
            rules = [
                rule for rule in self._snapshot.rules
                if rule.rule_id != rule_id
            ]
            self._snapshot = self._make_snapshot(
                self._snapshot.version + 1, rules
            )
            return True

    def reload(self, rule_texts) -> dict:
        with self._lock:
            previous_version = self._snapshot.version
            try:
                texts = list(rule_texts)
            except TypeError as exc:
                raise DetectionError(
                    "rule.syntax", "reload expects an iterable of rule texts"
                ) from exc

            errors: list[dict] = []
            candidates: list[tuple[int, _CompiledRule]] = []
            seen: set[str] = set()
            for index, text in enumerate(texts):
                try:
                    rule = self._compile(text)
                except _CompileFailure as failure:
                    errors.append(self._error_entry(index, failure, text))
                    continue
                if rule.rule_id in seen:
                    failure = _CompileFailure(
                        "rule.duplicate_id",
                        f"duplicate rule id {rule.rule_id!r} in {text!r}",
                    )
                    errors.append(self._error_entry(index, failure, text))
                    continue
                seen.add(rule.rule_id)
                candidates.append((index, rule))

            if len(candidates) > self._max_rules:
                for index, rule in candidates[self._max_rules:]:
                    failure = _CompileFailure(
                        "rule.limit",
                        f"reload contains {len(candidates)} rules, exceeding "
                        f"limit {self._max_rules}; existing rules kept "
                        f"(rule {rule.rule_id!r})",
                    )
                    errors.append(
                        self._error_entry(index, failure, texts[index])
                    )
                errors.sort(key=lambda entry: entry["index"])
                self._compile_errors = errors
                return {
                    "previous_version": previous_version,
                    "version": previous_version,
                    "accepted": 0,
                    "rejected": len(texts),
                }

            rules = [rule for _, rule in candidates]
            old_signature = tuple(
                rule.signature() for rule in self._snapshot.rules
            )
            new_snapshot = self._make_snapshot(previous_version, rules)
            new_signature = tuple(
                rule.signature() for rule in new_snapshot.rules
            )
            if new_signature != old_signature:
                new_snapshot = self._make_snapshot(
                    previous_version + 1, rules
                )
                self._snapshot = new_snapshot

            self._compile_errors = errors
            return {
                "previous_version": previous_version,
                "version": self._snapshot.version,
                "accepted": len(rules),
                "rejected": len(errors),
            }

    def version(self) -> int:
        with self._lock:
            return self._snapshot.version

    def rule_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._snapshot.by_id)

    def evaluate(self, record: dict) -> list[dict]:
        with self._lock:
            if not isinstance(record, Mapping):
                raise DetectionError(
                    "record.invalid", f"record must be a mapping: {record!r}"
                )
            record_id = record.get("record_id", _MISSING)
            if (
                record_id is _MISSING
                or not isinstance(record_id, str)
                or not record_id
            ):
                raise DetectionError(
                    "record.invalid", "record_id must be a non-empty string"
                )

            snapshot = self._snapshot
            version = snapshot.version
            hits: list[dict] = []
            for rule in snapshot.rules:
                if not rule.tree.matches(record):
                    continue
                hits.append({
                    "rule_id": rule.rule_id,
                    "version": version,
                    "priority": rule.priority,
                    "dedup_key": f"{record_id}:{rule.rule_id}@{version}",
                    "fields": list(rule.fields),
                })
                counted = (record_id, rule.rule_id, version)
                if counted not in self._counted:
                    self._counted.add(counted)
                    key = f"{rule.rule_id}@{version}"
                    self._counts[key] = self._counts.get(key, 0) + 1

            self._evaluations += 1
            self._hits += len(hits)
            return hits

    def explain(self, rule_id: str) -> dict:
        with self._lock:
            rule = self._snapshot.by_id.get(rule_id)
            if rule is None:
                raise DetectionError(
                    "rule.unknown", f"unknown rule id: {rule_id!r}"
                )
            return {
                "rule_id": rule.rule_id,
                "priority": rule.priority,
                "depth": rule.depth,
                "tree": rule.tree.to_tree(),
            }

    def compile_errors(self) -> list[dict]:
        with self._lock:
            return [dict(entry) for entry in self._compile_errors]

    def hit_counts(self) -> dict[str, int]:
        with self._lock:
            return {key: self._counts[key] for key in sorted(self._counts)}

    def stats(self) -> dict:
        with self._lock:
            return {
                "version": self._snapshot.version,
                "rules": len(self._snapshot.rules),
                "evaluations": self._evaluations,
                "hits": self._hits,
            }
