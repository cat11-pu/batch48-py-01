"""Three color traffic policer with single rate and dual rate token buckets."""

from __future__ import annotations

import threading

MODES = ("single", "dual")
COLORS = ("green", "yellow", "red")

STATS_KEYS = (
    "policies_declared",
    "policies_upgraded",
    "flows_attached",
    "flows_detached",
    "packets_classified",
    "green",
    "yellow",
    "red",
    "rejected",
)


class PolicerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TrafficPolicerGuard:
    """Meter each flow with committed and peak token buckets."""

    def __init__(self, *, max_flows: int = 16, clock=None) -> None:
        self.max_flows = max_flows
        self._clock = clock
        self._lock = threading.RLock()

    def declare_policy(self, name: str, *, mode: str = "single",
                       committed_rate: int = 1000, peak_rate: int = None,
                       burst: int = 1500, peak_burst: int = None,
                       version: int = 1) -> dict:
        raise NotImplementedError

    def policy_info(self, name: str) -> dict:
        raise NotImplementedError

    def upgrade_policy(self, name: str, *, mode: str = None,
                       committed_rate: int = None, peak_rate: int = None,
                       burst: int = None, peak_burst: int = None) -> dict:
        raise NotImplementedError

    def set_mode(self, name: str, mode: str) -> dict:
        raise NotImplementedError

    def attach(self, flow: str, policy: str) -> dict:
        raise NotImplementedError

    def detach(self, flow: str) -> dict:
        raise NotImplementedError

    def flow_state(self, flow: str, *, now_ms: int = 0) -> dict:
        raise NotImplementedError

    def classify(self, flow: str, size: int, *, now_ms: int = 0,
                 color: str = None) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
