"""SBOM dependency graph with purl normalisation and advisory propagation."""

import threading

SCOPES = ("runtime", "dev")


class SbomError(ValueError):
    """Raised for every rejected graph or advisory operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class SbomDependencyGraph:
    """Normalise purls, keep a scoped dependency graph and answer advisories."""

    def __init__(self, *, max_nodes=100000, max_edges=500000):
        self.max_nodes = max_nodes
        self.max_edges = max_edges
        self._lock = threading.RLock()

    def normalize_purl(self, purl):
        raise NotImplementedError

    def add_component(self, purl, scope="runtime"):
        raise NotImplementedError

    def add_edge(self, parent, child, scope="runtime"):
        raise NotImplementedError

    def affected(self, advisory):
        raise NotImplementedError

    def paths_to_root(self, component):
        raise NotImplementedError

    def explain(self, component):
        raise NotImplementedError

    def parse_version(self, version):
        raise NotImplementedError

    def version_in_range(self, version, constraint):
        raise NotImplementedError

    def roots(self):
        raise NotImplementedError

    def components(self):
        raise NotImplementedError

    def edges(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
