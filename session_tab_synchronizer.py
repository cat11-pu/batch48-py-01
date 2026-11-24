"""Session generation coordination across browser tabs of one login session."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class SessionTabError(ValueError):
    """Raised when a tab/session operation is refused; code is SESSION_ prefixed."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TabView:
    tab_id: str
    session_id: str
    generation: int
    capabilities: tuple
    last_seen: float
    valid: bool


@dataclass(frozen=True)
class RefreshClaim:
    session_id: str
    tab_id: str
    claim_id: str
    generation: int
    owner: bool
    status: str


class SessionTabSynchronizer:
    """Register tabs, heartbeat them and coordinate exactly one refresher."""

    def __init__(self, *, heartbeat_ttl: float = 30.0,
                 max_tabs_per_session: int = 8, clock=None) -> None:
        self.heartbeat_ttl = heartbeat_ttl
        self.max_tabs_per_session = max_tabs_per_session
        self._clock = clock
        self._lock = threading.RLock()

    def open_tab(self, session_id: str, tab_id: str, capabilities=(),
                 now=None) -> TabView:
        raise NotImplementedError

    def heartbeat(self, tab_id: str, now=None) -> dict:
        raise NotImplementedError

    def close_tab(self, tab_id: str, now=None) -> bool:
        raise NotImplementedError

    def claim_refresh(self, session_id: str, tab_id: str,
                      now=None) -> RefreshClaim:
        raise NotImplementedError

    def complete_refresh(self, session_id: str, claim_id: str, *, ok: bool,
                         reason: str = "", now=None) -> dict:
        raise NotImplementedError

    def await_refresh(self, session_id: str, tab_id: str, now=None) -> dict:
        raise NotImplementedError

    def publish_session(self, session_id: str, *, generation: int | None = None,
                        now=None) -> dict:
        raise NotImplementedError

    def broadcast_logout(self, session_id: str, *, reason: str = "admin_logout",
                         now=None) -> int:
        raise NotImplementedError

    def expire_tabs(self, now=None) -> int:
        raise NotImplementedError

    def tabs_for(self, session_id: str) -> tuple:
        raise NotImplementedError

    def session_info(self, session_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
