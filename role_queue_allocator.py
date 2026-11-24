"""Role preference queue allocation with autofill protection."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "queued",
    "cancelled",
    "teams_formed",
    "autofilled",
    "swapped",
    "changed",
    "locked",
    "departed",
    "rejected",
)

DEFAULT_ROLES = ("tank", "healer", "dps")
DEFAULT_QUOTAS = {"tank": 1, "healer": 1, "dps": 3}


class RoleQueueError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RoleQueueAllocator:
    """Hold role preference tickets and assemble quota bound teams."""

    def __init__(self, *, roles=DEFAULT_ROLES, quotas=None, team_size: int = 5,
                 queue_limit: int = 200, protection_cap: int = 5, clock=None) -> None:
        self.roles = tuple(roles)
        self.quotas = dict(DEFAULT_QUOTAS if quotas is None else quotas)
        self.team_size = team_size
        self.queue_limit = queue_limit
        self.protection_cap = protection_cap
        self._clock = clock
        self._lock = threading.RLock()

    def enqueue(self, player: str, *, primary: str,
                secondary: str | None = None, at: int = 0) -> dict:
        raise NotImplementedError

    def cancel(self, player: str) -> dict:
        raise NotImplementedError

    def queue_state(self, now: int = 0) -> list:
        raise NotImplementedError

    def autofill_protection(self, player: str) -> dict:
        raise NotImplementedError

    def form_team(self, team_id: str, *, quotas=None) -> dict:
        raise NotImplementedError

    def team_state(self, team_id: str) -> dict:
        raise NotImplementedError

    def swap_role(self, team_id: str, player_a: str, player_b: str) -> dict:
        raise NotImplementedError

    def change_role(self, team_id: str, player: str, new_role: str) -> dict:
        raise NotImplementedError

    def leave_team(self, team_id: str, player: str) -> dict:
        raise NotImplementedError

    def lock_team(self, team_id: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
