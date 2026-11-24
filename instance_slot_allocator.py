"""Dungeon instance slot allocation with party binding and reset anchors."""

from __future__ import annotations

import threading


class InstanceError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class InstanceSlotAllocator:
    """Allocate dungeon instances, bind parties and reclaim slots."""

    def __init__(self, *, reclaim_cooldown_ms: int = 60000,
                 reentry_cooldown_ms: int = 30000) -> None:
        self.reclaim_cooldown_ms = reclaim_cooldown_ms
        self.reentry_cooldown_ms = reentry_cooldown_ms
        self._lock = threading.RLock()

    def RegisterDungeon(self, dungeon_id, *, capacity, max_slots,
                        reset_every_ms, anchor_ms=0):
        raise NotImplementedError

    def Acquire(self, dungeon_id, party, now):
        raise NotImplementedError

    def Enter(self, instance_id, player, now):
        raise NotImplementedError

    def Leave(self, instance_id, player, now=None):
        raise NotImplementedError

    def Reset(self, instance_id):
        raise NotImplementedError

    def Sweep(self, now):
        raise NotImplementedError

    def State(self, instance_id):
        raise NotImplementedError

    def Cooldown(self, party, now=None):
        raise NotImplementedError

    def Snapshot(self):
        raise NotImplementedError

    def Restore(self, state):
        raise NotImplementedError

    def Stats(self):
        raise NotImplementedError
