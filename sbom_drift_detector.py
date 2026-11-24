"""Detect component and dependency drift between two SBOM snapshots."""

from __future__ import annotations

import threading

DEFAULT_MAX_COMPONENTS = 100000
SIDES = ("base", "current")
SCOPES = ("runtime", "development")


class SbomDriftError(ValueError):
    """Raised for every rejected snapshot or comparison request."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SbomDriftDetector:
    """Compare a baseline and a current component graph."""

    def __init__(self, *, max_components=DEFAULT_MAX_COMPONENTS):
        self.max_components = max_components
        self._lock = threading.RLock()

    def load(self, side, components, edges):
        raise NotImplementedError

    def diff(self, at=None):
        raise NotImplementedError

    def explain(self, component):
        raise NotImplementedError

    def accept(self, component, until=None, note=None):
        raise NotImplementedError

    def revoke(self, component):
        raise NotImplementedError

    def whitelist(self):
        raise NotImplementedError

    def components(self, side):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def reset_stats(self):
        raise NotImplementedError
