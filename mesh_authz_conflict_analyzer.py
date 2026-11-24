"""Static conflict diagnosis for service-mesh authorization policies."""

from __future__ import annotations

import threading

DEFAULT_MAX_POLICIES = 4096


class MeshAuthzError(ValueError):
    """Raised for every rejected policy set operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class MeshAuthzConflictAnalyzer:
    """Load policy revisions and diagnose shadowing and revision impact."""

    def __init__(self, *, max_policies=DEFAULT_MAX_POLICIES):
        self.max_policies = max_policies
        self._lock = threading.RLock()

    def load(self, revision, policies):
        raise NotImplementedError

    def revisions(self):
        raise NotImplementedError

    def policies(self, *, revision=None):
        raise NotImplementedError

    def analyze(self, *, revision=None):
        raise NotImplementedError

    def diff(self, from_revision, to_revision):
        raise NotImplementedError

    def explain(self, policy_id, *, revision=None):
        raise NotImplementedError

    def match(self, request, *, revision=None):
        raise NotImplementedError

    def expand_principal(self, spec):
        raise NotImplementedError

    def path_segments(self, path):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
