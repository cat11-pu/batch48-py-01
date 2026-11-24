"""Certificate transparency log entry merger with revocation matching."""

import threading


class CtError(ValueError):
    """Raised for every rejected ingestion or revocation operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CtLogMatcher:
    """Merge CT log entries by fingerprint and match revocation records."""

    def __init__(self, *, max_entries=100000):
        self.max_entries = max_entries
        self._lock = threading.RLock()

    def ingest(self, entries):
        raise NotImplementedError

    def dedup(self):
        raise NotImplementedError

    def match_revocation(self, cert, reason, at):
        raise NotImplementedError

    def cursor(self):
        raise NotImplementedError

    def advance_cursor(self, value):
        raise NotImplementedError

    def processed(self):
        raise NotImplementedError

    def lookup(self, fingerprint):
        raise NotImplementedError

    def entries(self):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError
