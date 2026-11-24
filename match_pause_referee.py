"""Pause referee with quotas, quorum voting and timeout recovery."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "teams",
    "proposals",
    "votes",
    "pauses",
    "resumes",
    "auto_resumes",
    "forced_resumes",
    "substitutions",
    "duplicates",
    "rejected",
)

PAUSE_STATES = ("running", "paused")


class PauseError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PauseReferee:
    """Referees ranked-match pause requests for both teams."""

    def __init__(self, *, quorum: int = 2, quota_per_match: int = 2,
                 max_duration_ms: int = 60000, max_players: int = 8,
                 clock=None) -> None:
        self.quorum = int(quorum)
        self.quota_per_match = int(quota_per_match)
        self.max_duration_ms = int(max_duration_ms)
        self.max_players = int(max_players)
        self._clock = clock
        self._lock = threading.RLock()

    def register_team(self, team: str, players) -> dict:
        raise NotImplementedError

    def roster(self, team: str) -> list:
        raise NotImplementedError

    def quota_of(self, team: str) -> int:
        raise NotImplementedError

    def request_pause(self, team: str, *, player: str,
                      duration: int | None = None, now=None) -> dict:
        raise NotImplementedError

    def vote_pause(self, proposal_id: str, *, player: str, now=None) -> dict:
        raise NotImplementedError

    def resume(self, proposal_id: str, *, player: str | None = None,
               now=None) -> dict:
        raise NotImplementedError

    def force_resume(self, *, reason: str = "", now=None) -> dict:
        raise NotImplementedError

    def request_substitution(self, team: str, *, out_player: str,
                             in_player: str, now=None) -> dict:
        raise NotImplementedError

    def state(self, *, now=None) -> dict:
        raise NotImplementedError

    def proposal_of(self, team: str) -> dict | None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
