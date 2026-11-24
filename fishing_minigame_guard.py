"""Fishing minigame casting, bite window and settlement guard."""

from __future__ import annotations

import threading


class FishingError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class FishingMinigameGuard:
    """Track one active fishing line per player and settle landings once."""

    def __init__(self, *, bite_window_ms: int = 1200, max_tension: int = 100,
                 line_durability: int = 3, tables=None, clock=None) -> None:
        self.bite_window_ms = bite_window_ms
        self.max_tension = max_tension
        self.line_durability = line_durability
        self.tables = dict(tables or {})
        self._clock = clock
        self._lock = threading.RLock()

    def Cast(self, player, spot, now):
        raise NotImplementedError

    def Bite(self, player, now):
        raise NotImplementedError

    def Reel(self, player, dt_ms):
        raise NotImplementedError

    def SetTension(self, player, delta):
        raise NotImplementedError

    def Land(self, player, now):
        raise NotImplementedError

    def Escaped(self, player):
        raise NotImplementedError

    def Release(self, player):
        raise NotImplementedError

    def State(self, player):
        raise NotImplementedError

    def SpeciesOdds(self, spot, now=None):
        raise NotImplementedError

    def Snapshot(self):
        raise NotImplementedError

    def Restore(self, state):
        raise NotImplementedError

    def Stats(self):
        raise NotImplementedError
