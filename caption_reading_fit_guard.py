"""Reading speed budget, display width and line fitting for timed captions."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "cues_added",
    "cues_removed",
    "cues_split",
    "extended",
    "rejected",
    "cps_queries",
)


class CaptionError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CaptionReadingFitGuard:
    """Keep caption cues inside a reading speed budget and column layout."""

    def __init__(self, *, max_cps: float = 17.0, max_columns: int = 42,
                 max_lines: int = 2, min_duration_ms: int = 800,
                 max_duration_ms: int = 7000, clock=None) -> None:
        self.max_cps = max_cps
        self.max_columns = max_columns
        self.max_lines = max_lines
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self._clock = clock
        self._lock = threading.RLock()

    def add_cue(self, start_ms: int, end_ms: int, text: str, *,
                cue_id: str | None = None) -> dict:
        raise NotImplementedError

    def remove_cue(self, cue_id: str) -> None:
        raise NotImplementedError

    def cues(self) -> list:
        raise NotImplementedError

    def cps_of(self, text: str, start_ms: int, end_ms: int) -> float:
        raise NotImplementedError

    def display_width(self, text: str) -> int:
        raise NotImplementedError

    def wrap_text(self, text: str, columns: int) -> list:
        raise NotImplementedError

    def split_cue(self, cue_id: str) -> list:
        raise NotImplementedError

    def extend_durations(self) -> int:
        raise NotImplementedError

    def fit_all(self) -> dict:
        raise NotImplementedError

    def timeline_valid(self) -> bool:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
