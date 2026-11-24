"""Animation event track sampling, ordering and root motion extraction."""

from __future__ import annotations

import threading


class AnimationError(ValueError):
    """Raised for rejected clips, events and snapshots."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AnimationEventTrack:
    """Track animation clips, events and root motion in milliseconds."""

    MAX_EVENTS = 256

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def load_clip(self, clip: dict) -> dict:
        raise NotImplementedError

    def clip_info(self, clip: str) -> dict:
        raise NotImplementedError

    def list_clips(self) -> list:
        raise NotImplementedError

    def add_event(self, clip: str, event: dict) -> str:
        raise NotImplementedError

    def events(self, clip: str) -> list:
        raise NotImplementedError

    def events_between(self, clip: str, from_ms, to_ms) -> list:
        raise NotImplementedError

    def remove_event(self, clip: str, event_id: str) -> bool:
        raise NotImplementedError

    def clear_events(self, clip: str) -> int:
        raise NotImplementedError

    def event_count(self, clip: str) -> int:
        raise NotImplementedError

    def sample(self, clip: str, time_ms) -> dict:
        raise NotImplementedError

    def extract_root_motion(self, clip: str, from_ms, to_ms) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
