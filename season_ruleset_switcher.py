"""Season ruleset switching with pinned in-flight matches and rollback."""

from __future__ import annotations

import hashlib
import json
import threading

STATS_KEYS = (
    "registered",
    "updated",
    "activated",
    "switched",
    "pinned",
    "finished",
    "rollbacks",
    "anomalies",
    "rejected",
)


class RulesetError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SeasonRulesetSwitcher:
    """Activate rulesets per season and pin one version per running match."""

    def __init__(self, *, max_rulesets: int = 8, clock=None) -> None:
        self.max_rulesets = max_rulesets
        self._clock = clock
        self._lock = threading.RLock()

    @staticmethod
    def canonical_digest(payload) -> str:
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def register_ruleset(self, name: str, *, season: str, payload) -> dict:
        raise NotImplementedError

    def ruleset_info(self, name: str, *, season: str, version: int | None = None) -> dict:
        raise NotImplementedError

    def activate(self, name: str, *, season: str) -> dict:
        raise NotImplementedError

    def active_for(self, season: str) -> dict:
        raise NotImplementedError

    def rollback(self, season: str) -> dict:
        raise NotImplementedError

    def create_room(self, room_id: str, *, season: str) -> dict:
        raise NotImplementedError

    def start_room(self, room_id: str) -> dict:
        raise NotImplementedError

    def pin_for_match(self, match_id: str, *, season: str) -> dict:
        raise NotImplementedError

    def resolve_for_match(self, match_id: str) -> dict:
        raise NotImplementedError

    def match_state(self, match_id: str) -> dict:
        raise NotImplementedError

    def finish_match(self, match_id: str, *, result: str) -> dict:
        raise NotImplementedError

    def audit_mismatch(self, match_id: str, expected_digest: str) -> dict:
        raise NotImplementedError

    def replay(self, match_id: str, recorded_digest: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
