"""Conservative candidate index for detection rules."""

from __future__ import annotations

import threading

DEFAULT_MAX_ENTRIES = 200000
DEFAULT_MAX_KEYWORDS = 16


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

    def register(self, rule_id, file_kinds, keywords, cost=1):
        raise NotImplementedError

    def candidates(self, path, head_bytes, whole_file=False):
        raise NotImplementedError

    def explain(self, rule_id, path, head_bytes, whole_file=False):
        raise NotImplementedError

    def audit(self, rule_id):
        raise NotImplementedError

    def remove(self, rule_id):
        raise NotImplementedError

    def rules(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def reset_stats(self):
        raise NotImplementedError
