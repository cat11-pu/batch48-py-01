"""NPC perception, memory decay and alarm propagation guard."""

from __future__ import annotations

import threading


class PerceptionError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


LEVELS = ("patrol", "search", "alert", "combat")


class PerceptionGuard:
    """Track sight, hearing, memory decay and alarm chains per NPC."""

    def __init__(self, *, view_range: float = 15.0, view_angle_deg: float = 90.0,
                 memory_ms: int = 4000, alarm_ttl_ms: int = 6000) -> None:
        self.view_range = view_range
        self.view_angle_deg = view_angle_deg
        self.memory_ms = memory_ms
        self.alarm_ttl_ms = alarm_ttl_ms
        self._lock = threading.RLock()

    def Place(self, npc, position):
        raise NotImplementedError

    def SetStealth(self, target, factor):
        raise NotImplementedError

    def Observe(self, npc, target, position, now):
        raise NotImplementedError

    def Hear(self, npc, event):
        raise NotImplementedError

    def Alert(self, npc, level, now=None):
        raise NotImplementedError

    def Propagate(self, npc, radius, now=None):
        raise NotImplementedError

    def State(self, npc, now):
        raise NotImplementedError

    def Forget(self, npc):
        raise NotImplementedError

    def Snapshot(self):
        raise NotImplementedError

    def Restore(self, state):
        raise NotImplementedError

    def Stats(self):
        raise NotImplementedError
