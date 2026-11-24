"""Detection rule compiler and per-record priority evaluator."""

from __future__ import annotations

import threading

MAX_RULES = 4096
MAX_DEPTH = 32


class DetectionError(ValueError):
    """Refusal raised by the engine; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DetectionRuleEngine:
    """Compile rule text into predicate trees and evaluate records."""

    MAX_RULES = MAX_RULES
    MAX_DEPTH = MAX_DEPTH

    def __init__(self, *, fields=(), default_priority: int = 100,
                 max_rules: int = MAX_RULES, max_depth: int = MAX_DEPTH) -> None:
        self._fields = tuple(sorted(set(fields)))
        self._default_priority = default_priority
        self._max_rules = max_rules
        self._max_depth = max_depth
        self._lock = threading.RLock()

    def register_field(self, name: str) -> None:
        raise NotImplementedError

    def known_fields(self) -> tuple[str, ...]:
        raise NotImplementedError

    def add_rule(self, rule_text: str) -> dict:
        raise NotImplementedError

    def remove_rule(self, rule_id: str) -> bool:
        raise NotImplementedError

    def reload(self, rule_texts) -> dict:
        raise NotImplementedError

    def version(self) -> int:
        raise NotImplementedError

    def rule_ids(self) -> list[str]:
        raise NotImplementedError

    def evaluate(self, record: dict) -> list[dict]:
        raise NotImplementedError

    def explain(self, rule_id: str) -> dict:
        raise NotImplementedError

    def compile_errors(self) -> list[dict]:
        raise NotImplementedError

    def hit_counts(self) -> dict[str, int]:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
