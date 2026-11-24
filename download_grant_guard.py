"""Download grant guard: short-lived signed grants bound to a resource root."""

from __future__ import annotations

import threading
from dataclasses import dataclass

RESOURCE_ROOT = "/data"
ALLOWED_METHODS = ("GET", "HEAD")


class DownloadError(ValueError):
    """Raised when a download grant operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GrantDecision:
    allowed: bool
    reason: str
    expires_at: int | None


class DownloadGrantGuard:
    """Issue, authorize, consume and revoke download grants."""

    def __init__(self, *, max_grants: int = 1000) -> None:
        self.max_grants = max_grants
        self._lock = threading.RLock()

    def issue(self, resource_id: str, subject: str, ttl: int, now: int,
              *, grant_id: str | None = None) -> dict:
        raise NotImplementedError

    def authorize(self, grant: object, request_path: str, method: str,
                  now: int) -> GrantDecision:
        raise NotImplementedError

    def consume(self, grant_id: str, now: int) -> bool:
        raise NotImplementedError

    def revoke(self, resource_id: str, now: int = 0) -> int:
        raise NotImplementedError

    def purge(self, now: int) -> int:
        raise NotImplementedError

    def inspect(self, grant_id: str) -> dict:
        raise NotImplementedError

    def grants(self) -> list:
        raise NotImplementedError

    def watermark(self, resource_id: str) -> int | None:
        raise NotImplementedError

    def check_range(self, grant: object, header: str, size: int, now: int) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
