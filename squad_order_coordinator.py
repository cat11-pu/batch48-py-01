"""Squad order queue with formation slots and morale state.

Orders are appended, never replaced. Each pop hands one queued order to one
idle unit; reports release the unit again.
"""

from __future__ import annotations

import threading


DEFAULT_FORMATIONS = {
    "line": ("front", "middle", "back"),
    "wedge": ("tip", "left", "right", "rear"),
}

ORDER_KINDS = ("move", "attack", "hold", "retreat")


class SquadError(ValueError):
    """Squad failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SquadCoordinator:
    """Form squads, queue orders and track per-unit assignment state."""

    def __init__(self, *, formations=None, morale_cap: int = 100) -> None:
        self.formations = dict(formations or DEFAULT_FORMATIONS)
        self.morale_cap = int(morale_cap)
        self._lock = threading.RLock()

    def form(self, squad: str, members, formation: str = "line") -> dict:
        raise NotImplementedError

    def disband(self, squad: str) -> dict:
        raise NotImplementedError

    def issue(self, squad: str, order: str, targets=None,
              *, priority: int = 0) -> dict:
        raise NotImplementedError

    def next_order(self, squad: str) -> dict | None:
        raise NotImplementedError

    def report(self, squad: str, unit: str, status: str) -> dict:
        raise NotImplementedError

    def morale(self, squad: str, delta: int) -> dict:
        raise NotImplementedError

    def slots(self, squad: str) -> dict:
        raise NotImplementedError

    def queue(self, squad: str) -> list:
        raise NotImplementedError

    def roster(self, squad: str) -> dict:
        raise NotImplementedError

    def idle_units(self, squad: str) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
