"""Multi-track timeline placement and render plan composition."""

from __future__ import annotations

import threading


class TimelineError(ValueError):
    """Rejection raised by the timeline compositor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TimelineCompositor:
    """Place clips on ordered tracks and build frame-accurate render plans."""

    def __init__(self, *, frame_rate: int = 25) -> None:
        self.frame_rate = frame_rate
        self._lock = threading.RLock()

    def add_track(self, name: str, *, order: int) -> dict:
        raise NotImplementedError

    def remove_track(self, name: str) -> None:
        raise NotImplementedError

    def tracks(self) -> list[dict]:
        raise NotImplementedError

    def add_clip(self, track: str, *, start_frame: int, duration_frames: int,
                 source_in_frame: int = 0, transition_in_frames: int = 0,
                 transition_out_frames: int = 0,
                 clip_id: str | None = None) -> dict:
        raise NotImplementedError

    def trim_clip(self, clip_id: str, *, duration_frames: int) -> dict:
        raise NotImplementedError

    def move_clip(self, clip_id: str, *, start_frame: int) -> dict:
        raise NotImplementedError

    def remove_clip(self, clip_id: str) -> None:
        raise NotImplementedError

    def effective_transition(self, clip_id: str, side: str) -> int:
        raise NotImplementedError

    def render_plan(self, *, start_frame: int, end_frame: int) -> dict:
        raise NotImplementedError

    def version(self) -> int:
        raise NotImplementedError

    def clips(self, track: str | None = None) -> list[dict]:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
