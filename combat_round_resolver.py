"""Round based combat resolution with deterministic seeded rolls."""

from __future__ import annotations

import threading

ACTION_KINDS = ("attack", "skill", "item", "move")
KIND_PRIORITY = {"skill": 30, "attack": 20, "item": 10, "move": 5}
CHECK_KINDS = ("hit", "dodge", "block", "crit")
LOG_FIELDS = ("round_no", "seed", "entries", "totals")


class CombatError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CombatRoundResolver:
    """Resolves one combat round at a time from ordered action submissions."""

    def __init__(self, *, seed: int = 0, max_rounds: int = 999) -> None:
        self.seed = int(seed)
        self.max_rounds = int(max_rounds)
        self._lock = threading.RLock()

    def define_unit(self, unit_id: str, *, hp: int, attack: int, defense: int,
                    dodge: float = 0.0, block: float = 0.0, crit: float = 0.0,
                    priority: int = 0, targets: int = 1) -> dict:
        raise NotImplementedError

    def remove_unit(self, unit_id: str) -> dict:
        raise NotImplementedError

    def resolve_round(self, actions, *, seed=None, round_no=None) -> dict:
        raise NotImplementedError

    def roll_check(self, attacker: str, defender: str, kind: str) -> dict:
        raise NotImplementedError

    def apply_order(self) -> list:
        raise NotImplementedError

    def replay(self, log: dict) -> dict:
        raise NotImplementedError

    def unit_state(self, unit_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
