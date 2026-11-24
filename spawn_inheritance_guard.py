"""Spawn inheritance guard: env allowlist, descriptor plan, cwd and umask."""

from __future__ import annotations

import threading


class SpawnGuardError(ValueError):
    """Rejection raised while composing or reviewing a spawn plan."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class InheritanceGuard:
    """Compose the execution environment handed to a child process."""

    def __init__(self, *, max_env: int = 64, max_env_value: int = 4096,
                 max_fds: int = 32, cwd_root: str = "/srv/sandbox",
                 default_umask: int = 0o077, clock=None) -> None:
        self.max_env = max_env
        self.max_env_value = max_env_value
        self.max_fds = max_fds
        self.cwd_root = cwd_root
        self.default_umask = default_umask
        self._clock = clock
        self._lock = threading.RLock()

    def build_env(self, base: dict, allowlist, overrides: dict | None = None) -> dict:
        raise NotImplementedError

    def filter_fds(self, inherited, keep) -> list:
        raise NotImplementedError

    def prepare_cwd(self, path: str, root: str | None = None) -> str:
        raise NotImplementedError

    def apply_umask(self, mask: int) -> int:
        raise NotImplementedError

    def pending_umask(self) -> int:
        raise NotImplementedError

    def plan(self, spec: dict) -> dict:
        raise NotImplementedError

    def verify(self, plan: dict) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
