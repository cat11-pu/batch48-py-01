"""Damage mitigation pipeline with a fixed settlement order.

Layers are settled in a single fixed order: penetration, armor, resist,
multiplicative reduction, final cap and finally shield absorption.
"""

from __future__ import annotations

import threading


LAYER_KINDS = ("armor", "penetration", "resist", "multiplier", "cap")
TRUE_DAMAGE = "true"


class MitigationError(ValueError):
    """Mitigation failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MitigationPipeline:
    """Settles a single incoming hit for one registered target."""

    def __init__(self, *, max_layers: int = 32) -> None:
        self.max_layers = int(max_layers)
        self._lock = threading.RLock()

    def register_target(self, target: str, *, hp: int = 100) -> dict:
        raise NotImplementedError

    def set_hp(self, target: str, hp: int) -> dict:
        raise NotImplementedError

    def add_layer(self, target: str, layer: dict) -> dict:
        raise NotImplementedError

    def remove_layer(self, target: str, layer_id: str) -> dict:
        raise NotImplementedError

    def layers(self, target: str) -> list:
        raise NotImplementedError

    def add_shield(self, target: str, shield_type: str, amount: int) -> dict:
        raise NotImplementedError

    def shield_absorb(self, target: str, amount: int,
                      damage_type: str = "physical") -> int:
        raise NotImplementedError

    def resolve(self, target: str, incoming: int,
                damage_type: str = "physical") -> dict:
        raise NotImplementedError

    def effective_hp(self, target: str) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
