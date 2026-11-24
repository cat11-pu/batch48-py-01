"""Incremental scan planning over a reverse dependency closure."""

from __future__ import annotations

import threading

DEFAULT_MAX_BATCH = 10000
DEFAULT_MAX_EDGES = 500000


class PlannerError(ValueError):
    """Raised for every rejected change, dependency or plan."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class IncrementalScanPlanner:
    """Track file digests, dependency edges and batched rescan plans."""

    def __init__(self, *, max_batch=DEFAULT_MAX_BATCH, max_edges=DEFAULT_MAX_EDGES):
        self.max_batch = max_batch
        self.max_edges = max_edges
        self._lock = threading.RLock()

    def update(self, path, digest):
        raise NotImplementedError

    def remove(self, path):
        raise NotImplementedError

    def add_dependency(self, referrer, dependency):
        raise NotImplementedError

    def plan(self, budget):
        raise NotImplementedError

    def commit(self, plan_id):
        raise NotImplementedError

    def pending(self):
        raise NotImplementedError

    def explain(self, path):
        raise NotImplementedError

    def files(self):
        raise NotImplementedError

    def dependencies(self, path):
        raise NotImplementedError

    def dependents(self, path):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def reset_stats(self):
        raise NotImplementedError
