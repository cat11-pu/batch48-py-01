"""Account recovery codes: hashed batches, one-time redemption, epochs."""

from __future__ import annotations

import threading

DEFAULT_CODE_COUNT = 10
DEFAULT_CODE_LENGTH = 10
DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCK_WINDOW = 300
DEFAULT_HISTORY_LIMIT = 32


class RecoveryCodeError(ValueError):
    """Raised for every rejected recovery code operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class RecoveryCodeGuard:
    """Issue, redeem and invalidate one-time account recovery codes."""

    def __init__(self, *, code_count=DEFAULT_CODE_COUNT,
                 code_length=DEFAULT_CODE_LENGTH, code_ttl=None,
                 max_failures=DEFAULT_MAX_FAILURES,
                 lock_window=DEFAULT_LOCK_WINDOW,
                 history_limit=DEFAULT_HISTORY_LIMIT):
        self.code_count = code_count
        self.code_length = code_length
        self.code_ttl = code_ttl
        self.max_failures = max_failures
        self.lock_window = lock_window
        self.history_limit = history_limit
        self._lock = threading.RLock()

    def generate(self, account, *, now=0):
        raise NotImplementedError

    def regenerate(self, account, *, now=0):
        raise NotImplementedError

    def redeem(self, account, code, *, now=0):
        raise NotImplementedError

    def status(self, account, *, now=0):
        raise NotImplementedError

    def epoch_of(self, account):
        raise NotImplementedError

    def remaining(self, account):
        raise NotImplementedError

    def unlock(self, account):
        raise NotImplementedError

    def normalize(self, code):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
