"""Occluder proxy building, scoring and budgeted selection."""

from __future__ import annotations


class OccluderError(ValueError):
    """Raised when a proxy, camera or snapshot request is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class OccluderProxyGuard:
    """Build box/hull occluder proxies, score them and budget the set."""

    def __init__(self, *, max_proxies: int = 16, tri_budget: int = 400,
                 min_coverage: float = 0.002) -> None:
        self.max_proxies = max_proxies
        self.tri_budget = tri_budget
        self.min_coverage = min_coverage

    def build(self, mesh: dict) -> dict:
        raise NotImplementedError

    def proxy_of(self, mesh_id: str) -> dict:
        raise NotImplementedError

    def invalidate(self, mesh_id: str, *, bounds=None) -> None:
        raise NotImplementedError

    def rebuild(self, mesh_id: str) -> dict:
        raise NotImplementedError

    def rebuild_pending(self) -> int:
        raise NotImplementedError

    def score(self, mesh_id: str, camera: dict) -> float:
        raise NotImplementedError

    def coverage(self, mesh_id: str) -> float:
        raise NotImplementedError

    def occluders(self, camera: dict, limit: int = 8) -> list[str]:
        raise NotImplementedError

    def last_frame(self) -> dict:
        raise NotImplementedError

    def begin_frame(self) -> None:
        raise NotImplementedError

    def budget(self) -> dict:
        raise NotImplementedError

    def remove(self, mesh_id: str) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
