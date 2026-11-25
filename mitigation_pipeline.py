"""Damage mitigation pipeline with a fixed settlement order.

Layers are settled in a single fixed order: penetration, armor, resist,
multiplicative reduction, final cap and finally shield absorption.
"""

from __future__ import annotations

import math
import threading
from copy import deepcopy


LAYER_KINDS = ("armor", "penetration", "resist", "multiplier", "cap")
TRUE_DAMAGE = "true"


UNKNOWN_TARGET = "MITIGATION_UNKNOWN_TARGET"
UNKNOWN_LAYER = "MITIGATION_UNKNOWN_LAYER"
DUPLICATE_LAYER = "MITIGATION_DUPLICATE_LAYER"
INVALID_LAYER = "MITIGATION_INVALID_LAYER"
INVALID_AMOUNT = "MITIGATION_INVALID_AMOUNT"
SNAPSHOT_INVALID = "MITIGATION_SNAPSHOT_INVALID"
LAYER_LIMIT = "MITIGATION_LAYER_LIMIT_EXCEEDED"
SHIELD_LIMIT = "MITIGATION_SHIELD_LIMIT_EXCEEDED"
INVALID_SHIELD = "MITIGATION_INVALID_SHIELD"


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
        self._targets: dict = {}

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _require_amount(cls, amount: object) -> int:
        if not cls._is_int(amount) or amount < 0:
            raise MitigationError(INVALID_AMOUNT, {"amount": amount})
        return amount

    @staticmethod
    def _valid_percent(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
            and 0 <= value <= 1
        )

    @classmethod
    def _validate_layer(cls, layer: object) -> dict:
        if not isinstance(layer, dict):
            raise MitigationError(INVALID_LAYER, {"layer": layer})

        layer_id = layer.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id:
            raise MitigationError(INVALID_LAYER, {"layer": layer})

        kind = layer.get("kind")
        if kind not in LAYER_KINDS:
            raise MitigationError(INVALID_LAYER, {"layer_id": layer_id,
                                                  "kind": kind})

        if kind in ("armor", "penetration"):
            amount = layer.get("amount")
            if not cls._is_int(amount) or amount < 0:
                raise MitigationError(INVALID_LAYER,
                                      {"layer_id": layer_id, "amount": amount})
        elif kind == "resist":
            damage_type = layer.get("damage_type")
            if not isinstance(damage_type, str) or not damage_type:
                raise MitigationError(
                    INVALID_LAYER,
                    {"layer_id": layer_id, "damage_type": damage_type},
                )
            if not cls._valid_percent(layer.get("percent")):
                raise MitigationError(
                    INVALID_LAYER,
                    {"layer_id": layer_id, "percent": layer.get("percent")},
                )
        elif not cls._valid_percent(layer.get("percent")):
            raise MitigationError(
                INVALID_LAYER,
                {"layer_id": layer_id, "percent": layer.get("percent")},
            )

        return deepcopy(layer)

    def _require_target(self, target: str) -> dict:
        try:
            state = self._targets[target]
        except KeyError as exc:
            raise MitigationError(UNKNOWN_TARGET, {"target": target}) from exc
        return state

    def register_target(self, target: str, *, hp: int = 100) -> dict:
        if not self._is_int(hp) or hp < 0:
            raise MitigationError(INVALID_AMOUNT, {"hp": hp})
        with self._lock:
            self._targets[target] = {
                "hp": hp,
                "layers": [],
                "shields": {},
            }
            return {"target": target, "hp": hp}

    def set_hp(self, target: str, hp: int) -> dict:
        with self._lock:
            self._require_target(target)
            hp = self._require_amount(hp)
            self._targets[target]["hp"] = hp
            return {"target": target, "hp": hp}

    def add_layer(self, target: str, layer: dict) -> dict:
        with self._lock:
            state = self._require_target(target)
            normalized = self._validate_layer(layer)
            layer_id = normalized["layer_id"]

            if any(item["layer_id"] == layer_id for item in state["layers"]):
                raise MitigationError(DUPLICATE_LAYER,
                                      {"layer_id": layer_id})
            if len(state["layers"]) >= self.max_layers:
                raise MitigationError(
                    LAYER_LIMIT,
                    {"target": target, "max_layers": self.max_layers},
                )

            state["layers"].append(normalized)
            return deepcopy(normalized)

    def remove_layer(self, target: str, layer_id: str) -> dict:
        with self._lock:
            state = self._require_target(target)
            for index, layer in enumerate(state["layers"]):
                if layer["layer_id"] == layer_id:
                    state["layers"].pop(index)
                    return {
                        "removed": layer_id,
                        "layers": deepcopy(state["layers"]),
                    }
            raise MitigationError(UNKNOWN_LAYER, {"layer_id": layer_id})

    def layers(self, target: str) -> list:
        with self._lock:
            return deepcopy(self._require_target(target)["layers"])

    def add_shield(self, target: str, shield_type: str, amount: int) -> dict:
        with self._lock:
            state = self._require_target(target)
            if not isinstance(shield_type, str) or not shield_type:
                raise MitigationError(INVALID_SHIELD,
                                      {"shield_type": shield_type})
            amount = self._require_amount(amount)
            shields = state["shields"]

            if shield_type not in shields and len(shields) >= self.max_layers:
                raise MitigationError(
                    SHIELD_LIMIT,
                    {"target": target, "max_shields": self.max_layers},
                )

            shields[shield_type] = shields.get(shield_type, 0) + amount
            return {
                "shield_type": shield_type,
                "shield_remaining": shields[shield_type],
            }

    def shield_absorb(self, target: str, amount: int,
                      damage_type: str = "physical") -> int:
        with self._lock:
            state = self._require_target(target)
            amount = self._require_amount(amount)
            shields = state["shields"]
            available = shields.get(damage_type, 0)
            if available <= 0:
                return 0

            absorbed = min(available, amount)
            shields[damage_type] = available - absorbed
            return absorbed

    def resolve(self, target: str, incoming: int,
                damage_type: str = "physical") -> dict:
        with self._lock:
            state = self._require_target(target)
            incoming = self._require_amount(incoming)

            penetration_layers = []
            armor_layers = []
            resist_layers = []
            multiplier_layers = []
            cap_layers = []

            for layer in state["layers"]:
                kind = layer["kind"]
                if kind == "penetration":
                    penetration_layers.append(layer)
                elif kind == "armor":
                    armor_layers.append(layer)
                elif kind == "resist":
                    resist_layers.append(layer)
                elif kind == "multiplier":
                    multiplier_layers.append(layer)
                elif kind == "cap":
                    cap_layers.append(layer)

            layers_applied = []
            capped = False
            absorbed = 0
            shield_remaining = 0
            damage = incoming

            if damage_type == TRUE_DAMAGE:
                for layer in cap_layers:
                    layers_applied.append(layer["layer_id"])
                    floor_damage = math.floor(
                        incoming * (1 - layer["percent"])
                    )
                    if damage < floor_damage:
                        damage = floor_damage
                        capped = True
            else:
                penetration = 0
                for layer in penetration_layers:
                    layers_applied.append(layer["layer_id"])
                    penetration += layer["amount"]

                armor = 0
                for layer in armor_layers:
                    layers_applied.append(layer["layer_id"])
                    armor += layer["amount"]

                effective_armor = max(0, armor - penetration)
                damage = max(0, math.floor(damage - effective_armor))

                resist_percent = 0.0
                for layer in resist_layers:
                    if layer.get("damage_type") == damage_type:
                        layers_applied.append(layer["layer_id"])
                        resist_percent += layer["percent"]

                resist_percent = min(1.0, max(0.0, resist_percent))
                damage = max(
                    0,
                    math.floor(damage * (1 - resist_percent)),
                )

                for layer in multiplier_layers:
                    layers_applied.append(layer["layer_id"])
                    damage = max(
                        0,
                        math.floor(damage * (1 - layer["percent"])),
                    )

                for layer in cap_layers:
                    layers_applied.append(layer["layer_id"])
                    floor_damage = math.floor(
                        incoming * (1 - layer["percent"])
                    )
                    if damage < floor_damage:
                        damage = floor_damage
                        capped = True

                shields = state["shields"]
                available_shield = shields.get(damage_type, 0)
                absorbed = min(available_shield, damage)
                if available_shield > 0:
                    shields[damage_type] = available_shield - absorbed
                damage -= absorbed
                shield_remaining = shields.get(damage_type, 0)

            return {
                "incoming": incoming,
                "final": damage,
                "absorbed": absorbed,
                "shield_remaining": shield_remaining,
                "capped": capped,
                "layers_applied": layers_applied,
            }

    def effective_hp(self, target: str) -> int:
        with self._lock:
            state = self._require_target(target)
            return state["hp"] + sum(state["shields"].values())

    def snapshot(self) -> dict:
        with self._lock:
            return {"version": 1, "targets": deepcopy(self._targets)}

    def restore(self, state: dict) -> None:
        restored = self._parse_snapshot(state, self.max_layers)
        with self._lock:
            self._targets = restored

    @classmethod
    def _parse_snapshot(cls, state: object, max_layers: int) -> dict:
        if not isinstance(state, dict):
            raise MitigationError(SNAPSHOT_INVALID, {"reason": "not a dict"})

        version = state.get("version")
        if not cls._is_int(version) or version != 1:
            raise MitigationError(SNAPSHOT_INVALID, {"version": version})

        targets = state.get("targets")
        if not isinstance(targets, dict):
            raise MitigationError(SNAPSHOT_INVALID, {"targets": targets})

        restored_targets = {}
        for target, target_state in targets.items():
            if not isinstance(target_state, dict):
                raise MitigationError(SNAPSHOT_INVALID,
                                      {"target": target})

            hp = target_state.get("hp")
            if not cls._is_int(hp) or hp < 0:
                raise MitigationError(SNAPSHOT_INVALID,
                                      {"target": target, "hp": hp})

            raw_layers = target_state.get("layers")
            if not isinstance(raw_layers, list):
                raise MitigationError(SNAPSHOT_INVALID,
                                      {"target": target, "layers": raw_layers})

            layers = []
            layer_ids = set()
            for raw_layer in raw_layers:
                try:
                    layer = cls._validate_layer(raw_layer)
                except MitigationError as exc:
                    raise MitigationError(
                        SNAPSHOT_INVALID,
                        {"target": target, "layer": raw_layer},
                    ) from exc

                layer_id = layer["layer_id"]
                if layer_id in layer_ids:
                    raise MitigationError(
                        SNAPSHOT_INVALID,
                        {"target": target, "layer_id": layer_id},
                    )
                layer_ids.add(layer_id)
                layers.append(layer)

            if len(layers) > max_layers:
                raise MitigationError(
                    SNAPSHOT_INVALID,
                    {"target": target, "max_layers": max_layers},
                )

            raw_shields = target_state.get("shields")
            if not isinstance(raw_shields, dict):
                raise MitigationError(
                    SNAPSHOT_INVALID,
                    {"target": target, "shields": raw_shields},
                )

            shields = {}
            for shield_type, amount in raw_shields.items():
                if not isinstance(shield_type, str) or not shield_type:
                    raise MitigationError(
                        SNAPSHOT_INVALID,
                        {"target": target, "shield_type": shield_type},
                    )
                if not cls._is_int(amount) or amount < 0:
                    raise MitigationError(
                        SNAPSHOT_INVALID,
                        {"target": target, "amount": amount},
                    )
                shields[shield_type] = amount

            if len(shields) > max_layers:
                raise MitigationError(
                    SNAPSHOT_INVALID,
                    {"target": target, "max_shields": max_layers},
                )

            restored_targets[target] = {
                "hp": hp,
                "layers": layers,
                "shields": shields,
            }

        return restored_targets
