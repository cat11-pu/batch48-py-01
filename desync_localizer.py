"""Frame desync localizer over reported per-peer entity digests."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "reports",
    "duplicates",
    "conflicts",
    "matched",
    "diverged",
    "insufficient",
    "rejected",
    "evidence_dropped",
    "quarantined",
)

PEER_STATES = ("active", "quarantined")


class DesyncError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DesyncLocalizer:
    """Compares peer frame digests and pinpoints the first divergent frame."""

    def __init__(self, *, peer_limit: int = 16, frame_limit: int = 1000,
                 evidence_limit: int = 256, clock=None) -> None:
        self.peer_limit = int(peer_limit)
        self.frame_limit = int(frame_limit)
        self.evidence_limit = int(evidence_limit)
        self._clock = clock
        self._lock = threading.RLock()

    def register_peer(self, peer_id: str, *, endpoint: str | None = None) -> dict:
        raise NotImplementedError

    def quarantine_peer(self, peer_id: str, *, reason: str = "") -> dict:
        raise NotImplementedError

    def peer_state(self, peer_id: str) -> str:
        raise NotImplementedError

    def report_frame(self, peer_id: str, *, match_id: str, frame: int,
                     digest, inputs=None) -> dict:
        raise NotImplementedError

    def compare(self, *, frame: int, left: str, right: str,
                match_id: str = "default") -> dict:
        raise NotImplementedError

    def first_divergence(self, *, left: str, right: str, start: int = 0,
                         end: int | None = None,
                         match_id: str = "default") -> dict:
        raise NotImplementedError

    def bisect_range(self, *, left: str, right: str, start: int,
                     end: int, match_id: str = "default") -> dict:
        raise NotImplementedError

    def evidence(self, *, match_id: str | None = None,
                 limit: int | None = None) -> dict:
        raise NotImplementedError

    def clear_match(self, match_id: str) -> int:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
