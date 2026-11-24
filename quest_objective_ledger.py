"""Quest objective progress ledger with party sharing and turn-in."""

from __future__ import annotations

import threading


class QuestLedgerError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ObjectiveLedger:
    """Track per-player quest objectives, sources and turn-in state."""

    def __init__(self) -> None:
        self._lock = threading.RLock()

    def DefineQuest(self, quest, objectives, *, turn_in=None, reward=None):
        raise NotImplementedError

    def Accept(self, player, quest):
        raise NotImplementedError

    def Record(self, player, quest, objective, delta, source):
        raise NotImplementedError

    def Progress(self, player, quest):
        raise NotImplementedError

    def TurnIn(self, player, quest, items):
        raise NotImplementedError

    def Abandon(self, player, quest):
        raise NotImplementedError

    def Sources(self, player, quest, objective):
        raise NotImplementedError

    def FormParty(self, party, players):
        raise NotImplementedError

    def Snapshot(self):
        raise NotImplementedError

    def Restore(self, state):
        raise NotImplementedError

    def Stats(self):
        raise NotImplementedError
