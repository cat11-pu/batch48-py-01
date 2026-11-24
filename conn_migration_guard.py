"""Connection migration epoch guard for multipath transport sessions."""

from __future__ import annotations

import threading


class MigrationError(ValueError):
    """Raised when a migration operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ConnMigrationGuard:
    """Track session paths, migration epochs and packet acceptance."""

    def __init__(self, *, max_paths_per_session: int = 3, grace_ms: int = 500,
                 cooldown_ms: int = 1000, max_migrations: int = 4,
                 reorder_window: int = 64, clock=None) -> None:
        self.max_paths_per_session = max_paths_per_session
        self.grace_ms = grace_ms
        self.cooldown_ms = cooldown_ms
        self.max_migrations = max_migrations
        self.reorder_window = reorder_window
        self._clock = clock
        self._lock = threading.RLock()

    def create_session(self, session: str, *, addr: str,
                       now: int = 0) -> dict:
        raise NotImplementedError

    def issue_ticket(self, session: str, *, ttl_ms: int = 2000,
                     now: int = 0) -> dict:
        raise NotImplementedError

    def probe_path(self, session: str, addr: str, *, ticket: str,
                   now: int = 0) -> dict:
        raise NotImplementedError

    def complete_migration(self, session: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def rollback(self, session: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def on_packet(self, path: str, epoch: int, seq: int, *, payload=None,
                  now: int = 0) -> dict:
        raise NotImplementedError

    def record_sample(self, path: str, rtt_ms: int, *, now: int = 0) -> dict:
        raise NotImplementedError

    def reset_estimates(self, session: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def path_state(self, session: str) -> dict:
        raise NotImplementedError

    def list_paths(self, session: str) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
