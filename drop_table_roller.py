"""Weighted loot table roller with guaranteed drops and pity counters."""

from __future__ import annotations

import random
import threading


class DropTableError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DropRoller:
    """Register loot tables, settle kills once and track pity per table."""

    def __init__(self, *, seed: int = 1337, max_rolls_per_kill: int = 6,
                 pity_threshold: int = 8) -> None:
        self.seed = seed
        self.max_rolls_per_kill = max_rolls_per_kill
        self.pity_threshold = pity_threshold
        self._rng = random.Random(seed)
        self._lock = threading.RLock()

    def RegisterTable(self, table_id, spec):
        raise NotImplementedError

    def Roll(self, table_id, kill_context, rng=None):
        raise NotImplementedError

    def Guarantee(self, table_id, key):
        raise NotImplementedError

    def Pity(self, table_id, key):
        raise NotImplementedError

    def Budget(self, table_id):
        raise NotImplementedError

    def Settled(self, table_id, kill_id):
        raise NotImplementedError

    def ResetPity(self, table_id, key):
        raise NotImplementedError

    def Snapshot(self):
        raise NotImplementedError

    def Restore(self, state):
        raise NotImplementedError

    def Stats(self):
        raise NotImplementedError
