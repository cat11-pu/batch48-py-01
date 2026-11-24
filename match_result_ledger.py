"""Match result ledger with idempotent settlement and reversal entries."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "accepted",
    "duplicates",
    "conflicts",
    "amended",
    "voided",
    "entries",
    "rejected",
)


class MatchLedgerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MatchResultLedger:
    """Append-only settlement ledger keyed by match identifier."""

    def __init__(self, *, season_starts=None, base_rating: int = 1000,
                 max_entries: int = 5000, clock=None) -> None:
        self.base_rating = base_rating
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.RLock()

    def resolve_season(self, ended_at: int) -> str:
        raise NotImplementedError

    def submit_result(self, match_id: str, *, ended_at: int, results: dict) -> dict:
        raise NotImplementedError

    def amend_result(self, match_id: str, *, results: dict,
                     reason: str = "") -> dict:
        raise NotImplementedError

    def void_match(self, match_id: str, *, reason: str = "") -> dict:
        raise NotImplementedError

    def rating_of(self, player: str, *, season: str) -> int:
        raise NotImplementedError

    def history_of(self, player: str, *, season: str | None = None) -> list:
        raise NotImplementedError

    def settlement_of(self, match_id: str) -> dict:
        raise NotImplementedError

    def season_matches(self, season: str) -> int:
        raise NotImplementedError

    def match_state(self, match_id: str) -> str:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
