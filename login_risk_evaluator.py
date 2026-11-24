"""Login risk scoring with weighted signals, thresholds and score decay."""

from __future__ import annotations

import threading


class LoginRiskError(ValueError):
    """Raised when a risk evaluation operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LoginRiskEvaluator:
    """Score logins from weighted signals and map scores onto decisions."""

    def __init__(self, *, weights=None, thresholds=None, half_life: int = 3600,
                 window: int = 600, signal_cap: int = 10, clock=None) -> None:
        self.half_life = half_life
        self.window = window
        self.signal_cap = signal_cap
        self._clock = clock
        self._lock = threading.RLock()

    def register_signal(self, account: str, key: str, value, *,
                        now: int = 0) -> dict:
        raise NotImplementedError

    def score_login(self, account: str, *, signals=None, now: int = 0) -> dict:
        raise NotImplementedError

    def set_list(self, account: str, kind) -> dict:
        raise NotImplementedError

    def decide(self, account: str, *, signals=None, now: int = 0) -> dict:
        raise NotImplementedError

    def explain(self, account: str, *, signals=None, now: int = 0) -> dict:
        raise NotImplementedError

    def record_outcome(self, account: str, outcome: str, *,
                       now: int = 0) -> dict:
        raise NotImplementedError

    def history_score(self, account: str, *, now: int = 0) -> float:
        raise NotImplementedError

    def decay(self, account: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
