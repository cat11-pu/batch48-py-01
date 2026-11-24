"""Interprocedural taint summaries computed to a fixed point."""

from __future__ import annotations

import threading

DEFAULT_MAX_FUNCTIONS = 50000
DEFAULT_MAX_CALLEES = 4096
DEFAULT_ITERATION_LIMIT = 64


class TaintError(ValueError):
    """Raised for every rejected definition, lookup or state."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class TaintSummary:
    """Record per-function flow summaries and iterate callee graphs."""

    def __init__(self, *, max_functions=DEFAULT_MAX_FUNCTIONS,
                 max_callees=DEFAULT_MAX_CALLEES,
                 iteration_limit=DEFAULT_ITERATION_LIMIT):
        self.max_functions = max_functions
        self.max_callees = max_callees
        self.iteration_limit = iteration_limit
        self._lock = threading.RLock()

    def index_function(self, name, params, callees):
        raise NotImplementedError

    def summarize(self, name):
        raise NotImplementedError

    def analyze(self, entry):
        raise NotImplementedError

    def invalidate(self, names):
        raise NotImplementedError

    def dependencies(self, name):
        raise NotImplementedError

    def explain(self, name):
        raise NotImplementedError

    def functions(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def reset_stats(self):
        raise NotImplementedError
