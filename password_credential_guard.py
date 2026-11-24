"""Password credential storage: parameter upgrades, pepper rotation, locks."""

from __future__ import annotations

import threading

DEFAULT_MAX_FAILURES = 5
DEFAULT_LOCK_WINDOW = 300
DEFAULT_HISTORY_LIMIT = 32


class PasswordGuardError(ValueError):
    """Raised for every rejected password operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class PasswordCredentialGuard:
    """Store password digests with versioned parameters and peppers."""

    def __init__(self, *, max_failures=DEFAULT_MAX_FAILURES,
                 lock_window=DEFAULT_LOCK_WINDOW,
                 history_limit=DEFAULT_HISTORY_LIMIT):
        self.max_failures = max_failures
        self.lock_window = lock_window
        self.history_limit = history_limit
        self._lock = threading.RLock()

    def policy(self):
        raise NotImplementedError

    def set_policy(self, *, algorithm=None, iterations=None,
                   min_length=None, max_length=None):
        raise NotImplementedError

    def rotate_pepper(self, pepper_id, secret):
        raise NotImplementedError

    def revoke_pepper(self, pepper_id):
        raise NotImplementedError

    def active_pepper(self):
        raise NotImplementedError

    def register(self, account, password, *, now=0.0):
        raise NotImplementedError

    def verify(self, account, password, *, now=0.0):
        raise NotImplementedError

    def change_password(self, account, old_password, new_password, *, now=0.0):
        raise NotImplementedError

    def params(self, account):
        raise NotImplementedError

    def lock_state(self, account, *, now=0.0):
        raise NotImplementedError

    def unlock(self, account):
        raise NotImplementedError

    def history(self, account):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
