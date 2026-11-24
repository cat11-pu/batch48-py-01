"""Replay view projector with time-accurate visibility filtering."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "views",
    "entities",
    "vision_samples",
    "projected",
    "visible",
    "hidden",
    "cropped",
    "duplicates",
    "conflicts",
    "rejected",
)

EVENT_TYPES = ("move", "ability", "damage", "death", "spawn", "chat", "voice")
VIEW_KINDS = ("player", "observer")
SPEECH_TYPES = ("chat", "voice")
CATEGORIES = ("unit", "projectile", "structure")


class ReplayError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReplayViewProjector:
    """Projects a recorded replay into independently filtered viewer perspectives."""

    def __init__(self, *, view_limit: int = 8, entity_limit: int = 64,
                 event_limit: int = 2000, clock=None) -> None:
        self.view_limit = int(view_limit)
        self.entity_limit = int(entity_limit)
        self.event_limit = int(event_limit)
        self._clock = clock
        self._lock = threading.RLock()

    def register_view(self, view_id: str, *, kind: str = "player",
                      team: str | None = None, owner: str | None = None,
                      version: int = 1) -> dict:
        raise NotImplementedError

    def register_entity(self, entity_id: str, *, team: str,
                        category: str = "unit") -> dict:
        raise NotImplementedError

    def record_vision(self, view_id: str, *, at: int, visible) -> dict:
        raise NotImplementedError

    def project_event(self, view_id: str, event: dict) -> dict:
        raise NotImplementedError

    def project_range(self, view_id: str, *, start: int = 0,
                      end: int | None = None, limit: int | None = None) -> dict:
        raise NotImplementedError

    def seek(self, view_id: str, at: int) -> dict:
        raise NotImplementedError

    def verify_view(self, view_id: str) -> dict:
        raise NotImplementedError

    def event_count(self, view_id: str) -> int:
        raise NotImplementedError

    def content_of(self, view_id: str, seq: int) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
