"""Root-cause fingerprint based false positive suppression registry."""

import hashlib
import threading

SCOPES = ("file", "rule", "global")
SCOPE_BREADTH = {"file": 0, "rule": 1, "global": 2}


class SuppressorError(ValueError):
    """Raised for every rejected operation; ``code`` is a stable string."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class FalsePositiveSuppressor:
    """Track suppression entries by root-cause fingerprint."""

    def __init__(self, *, max_entries=50000, max_reason_len=256):
        self.max_entries = max_entries
        self.max_reason_len = max_reason_len
        self._lock = threading.RLock()

    def fingerprint(self, finding):
        raise NotImplementedError

    def suppress(self, fingerprint, reason, until, scope="file"):
        raise NotImplementedError

    def filter(self, findings, at):
        raise NotImplementedError

    def expire(self, at):
        raise NotImplementedError

    def diff(self, baseline, current, at):
        raise NotImplementedError

    def entries(self):
        raise NotImplementedError

    def history(self, fingerprint):
        raise NotImplementedError

    def is_suppressed(self, fingerprint, at):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError

    def reset_stats(self):
        raise NotImplementedError
