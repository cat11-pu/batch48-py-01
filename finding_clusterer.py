"""Cluster findings by root cause with manual merge and split traces."""

from __future__ import annotations

import threading

DEFAULT_MAX_FINDINGS = 200000
DEFAULT_MAX_MEMBERS = 4096


class ClusterError(ValueError):
    """Raised for every rejected finding or cluster operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FindingClusterer:
    """Group findings by root key and keep manual merge/split decisions."""

    def __init__(self, *, max_findings=DEFAULT_MAX_FINDINGS,
                 max_members=DEFAULT_MAX_MEMBERS):
        self.max_findings = max_findings
        self.max_members = max_members
        self._lock = threading.RLock()

    def add(self, finding):
        raise NotImplementedError

    def cluster(self):
        raise NotImplementedError

    def merge(self, cluster_a, cluster_b):
        raise NotImplementedError

    def split(self, cluster_id, finding_ids):
        raise NotImplementedError

    def members(self, cluster_id):
        raise NotImplementedError

    def explain(self, cluster_id):
        raise NotImplementedError

    def findings(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def reset_stats(self):
        raise NotImplementedError
