"""Round based combat resolution with deterministic seeded rolls."""

from __future__ import annotations

import copy
import math
import random
import threading

ACTION_KINDS = ("attack", "skill", "item", "move")
KIND_PRIORITY = {"skill": 30, "attack": 20, "item": 10, "move": 5}
CHECK_KINDS = ("hit", "dodge", "block", "crit")
LOG_FIELDS = ("round_no", "seed", "entries", "totals")

STAT_FIELDS = (
    "rounds",
    "actions",
    "hits",
    "dodges",
    "crits",
    "blocks",
    "damage",
)

_MISSING = object()


class CombatError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_real(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return isinstance(value, float) and math.isfinite(value)


def _as_int(value: object, *, minimum: int | None = None,
            maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise ValueError("not an integer")
    if isinstance(value, int):
        result = value
    elif isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError("not an integer")
        result = int(value)
    else:
        raise ValueError("not an integer")
    if minimum is not None and result < minimum:
        raise ValueError("below minimum")
    if maximum is not None and result > maximum:
        raise ValueError("above maximum")
    return result


def _as_chance(value: object) -> float:
    if isinstance(value, bool):
        raise ValueError("not a real number")
    if isinstance(value, int):
        if value < 0 or value > 1:
            raise ValueError("probability outside zero and one")
        return float(value)
    if not isinstance(value, float) or not math.isfinite(value):
        raise ValueError("not a real number")
    if value < 0.0 or value > 1.0:
        raise ValueError("probability outside zero and one")
    return value


def _normalise_rng_state(value: object) -> object:
    """Accept the tuple form emitted by random.Random and JSON list forms."""
    if isinstance(value, list):
        return tuple(_normalise_rng_state(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_normalise_rng_state(item) for item in value)
    return value


class CombatRoundResolver:
    """Resolves one combat round at a time from ordered action submissions."""

    def __init__(self, *, seed: int = 0, max_rounds: int = 999) -> None:
        try:
            resolved_seed = _as_int(seed)
            resolved_max_rounds = _as_int(max_rounds, minimum=1)
        except (TypeError, ValueError) as exc:
            raise CombatError("CR_INVALID_UNIT", "invalid resolver settings") from exc

        self.seed = resolved_seed
        self.max_rounds = resolved_max_rounds
        self._rng = random.Random(resolved_seed)
        self._units: dict[str, dict] = {}
        self._committed_round = 0
        self._stats = {name: 0 for name in STAT_FIELDS}
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Unit registration and inspection
    # ------------------------------------------------------------------
    def define_unit(self, unit_id: str, *, hp: int, attack: int, defense: int,
                    dodge: float = 0.0, block: float = 0.0, crit: float = 0.0,
                    priority: int = 0, targets: int = 1) -> dict:
        with self._lock:
            if not isinstance(unit_id, str) or not unit_id.strip():
                raise CombatError("CR_INVALID_UNIT", "unit id must not be empty")
            if unit_id in self._units:
                raise CombatError("CR_DUPLICATE_UNIT", unit_id)
            try:
                max_hp = _as_int(hp, minimum=1)
                attack_value = _as_int(attack, minimum=0)
                defense_value = _as_int(defense, minimum=0)
                dodge_value = _as_chance(dodge)
                block_value = _as_chance(block)
                crit_value = _as_chance(crit)
                priority_value = _as_int(priority)
                targets_value = _as_int(targets, minimum=1)
            except (TypeError, ValueError) as exc:
                raise CombatError("CR_INVALID_UNIT", unit_id) from exc

            self._units[unit_id] = {
                "unit": unit_id,
                "hp": max_hp,
                "max_hp": max_hp,
                "attack": attack_value,
                "defense": defense_value,
                "dodge": dodge_value,
                "block": block_value,
                "crit": crit_value,
                "priority": priority_value,
                "targets": targets_value,
            }
            return self.unit_state(unit_id)

    def remove_unit(self, unit_id: str) -> dict:
        with self._lock:
            if not isinstance(unit_id, str) or unit_id not in self._units:
                raise CombatError("CR_UNKNOWN_UNIT", unit_id)
            state = self.unit_state(unit_id)
            del self._units[unit_id]
            return state

    def unit_state(self, unit_id: str) -> dict:
        with self._lock:
            if not isinstance(unit_id, str) or unit_id not in self._units:
                raise CombatError("CR_UNKNOWN_UNIT", unit_id)
            unit = self._units[unit_id]
            return {
                "unit": unit["unit"],
                "hp": unit["hp"],
                "max_hp": unit["max_hp"],
                "attack": unit["attack"],
                "defense": unit["defense"],
                "dodge": unit["dodge"],
                "block": unit["block"],
                "crit": unit["crit"],
                "priority": unit["priority"],
                "targets": unit["targets"],
                "alive": unit["hp"] > 0,
            }

    def apply_order(self) -> list:
        return sorted(KIND_PRIORITY, key=lambda kind: KIND_PRIORITY[kind],
                      reverse=True)

    # ------------------------------------------------------------------
    # Individual rolls
    # ------------------------------------------------------------------
    def roll_check(self, attacker: str, defender: str, kind: str) -> dict:
        with self._lock:
            if kind not in CHECK_KINDS:
                raise CombatError("CR_INVALID_CHECK", kind)
            if not isinstance(attacker, str) or attacker not in self._units:
                raise CombatError("CR_UNKNOWN_UNIT", attacker)
            if not isinstance(defender, str) or defender not in self._units:
                raise CombatError("CR_UNKNOWN_UNIT", defender)

            if kind == "dodge":
                chance = self._units[defender]["dodge"]
            elif kind == "block":
                chance = self._units[defender]["block"]
            elif kind == "crit":
                chance = self._units[attacker]["crit"]
            else:  # "hit" has no accuracy stat; a plain hit check always lands.
                chance = 1.0

            roll = self._rng.random()
            return {
                "kind": kind,
                "chance": chance,
                "roll": roll,
                "success": roll < chance,
            }

    # ------------------------------------------------------------------
    # Round resolution
    # ------------------------------------------------------------------
    def resolve_round(self, actions, *, seed=None, round_no=None) -> dict:
        with self._lock:
            resolved_actions = self._prepare_actions(actions)
            if round_no is None:
                target_round = self._committed_round + 1
            else:
                try:
                    target_round = _as_int(round_no)
                except (TypeError, ValueError) as exc:
                    raise CombatError("CR_ROUND_GAP", round_no) from exc
                if target_round <= self._committed_round:
                    raise CombatError("CR_ROUND_COMMITTED", target_round)
                if target_round != self._committed_round + 1:
                    raise CombatError("CR_ROUND_GAP", target_round)

            if seed is None:
                effective_seed = self.seed
                rng = random.Random()
                rng.setstate(self._rng.getstate())
            else:
                try:
                    effective_seed = _as_int(seed)
                except (TypeError, ValueError) as exc:
                    raise CombatError("CR_INVALID_ACTION", "invalid seed") from exc
                rng = random.Random(effective_seed)

            # Work on copies so every failure leaves the resolver untouched.
            units = {unit_id: dict(unit) for unit_id, unit in self._units.items()}
            entries: list[dict] = []

            for action in resolved_actions:
                actor_id = action["actor"]
                actor = units[actor_id]
                priority = action["priority"]

                if actor["hp"] <= 0:
                    entries.append(self._dead_actor_entry(action, units))
                    continue

                target_ids = self._resolve_target_ids(action, actor, units)
                for target_id in target_ids:
                    defender = units[target_id]
                    if defender["hp"] <= 0:
                        entries.append(self._dead_target_entry(
                            action, priority, target_id, defender["hp"]
                        ))
                        continue
                    entries.append(self._resolve_attack(
                        action, priority, actor, defender, rng
                    ))

            totals = self._totals(entries)
            log = {
                "round_no": target_round,
                "seed": effective_seed,
                "entries": entries,
                "totals": totals,
            }

            self._units = units
            self._rng = rng
            self._committed_round = target_round
            self._stats["rounds"] += 1
            self._stats["actions"] += len(resolved_actions)
            for name in ("hits", "dodges", "crits", "blocks", "damage"):
                self._stats[name] += totals[name]
            return log

    def _prepare_actions(self, actions) -> list[dict]:
        if actions is None:
            raise CombatError("CR_EMPTY_ACTIONS", None)
        try:
            raw_actions = list(actions)
        except TypeError as exc:
            raise CombatError("CR_INVALID_ACTION", "actions must be iterable") from exc
        if not raw_actions:
            raise CombatError("CR_EMPTY_ACTIONS", None)

        prepared: list[dict] = []
        for index, raw in enumerate(raw_actions):
            if not isinstance(raw, dict):
                raise CombatError("CR_INVALID_ACTION", index)
            actor = raw.get("actor")
            if not isinstance(actor, str) or not actor.strip():
                raise CombatError("CR_INVALID_ACTION", "invalid actor")
            kind = raw.get("kind")
            if kind not in ACTION_KINDS:
                raise CombatError("CR_INVALID_ACTION", kind)
            if actor not in self._units:
                raise CombatError("CR_UNKNOWN_UNIT", actor)

            raw_priority = raw.get("priority", _MISSING)
            if raw_priority is _MISSING or raw_priority is None:
                priority = KIND_PRIORITY[kind]
            else:
                try:
                    priority = _as_int(raw_priority)
                except (TypeError, ValueError) as exc:
                    raise CombatError("CR_INVALID_ACTION", "invalid priority") from exc

            raw_power = raw.get("power", 0)
            try:
                power = _as_int(raw_power)
            except (TypeError, ValueError) as exc:
                raise CombatError("CR_INVALID_ACTION", "invalid power") from exc

            prepared.append({
                "actor": actor,
                "kind": kind,
                "priority": priority,
                "power": power,
                "target": raw.get("target", _MISSING),
                "targets": raw.get("targets", _MISSING),
                "index": index,
            })

        # Python's sort is stable, so equal priorities keep submission order.
        prepared.sort(key=lambda item: item["priority"], reverse=True)
        return prepared

    def _resolve_target_ids(self, action: dict, actor: dict,
                            units: dict[str, dict]) -> list[str]:
        actor_id = actor["unit"]
        raw_targets = action["targets"]
        raw_target = action["target"]

        explicit: list[str] | None = None
        count: int | None = None

        if raw_targets is not _MISSING and raw_targets is not None:
            if isinstance(raw_targets, (list, tuple)):
                explicit = list(raw_targets)
            elif isinstance(raw_targets, str):
                explicit = [raw_targets]
            else:
                try:
                    count = _as_int(raw_targets, minimum=1)
                except (TypeError, ValueError) as exc:
                    raise CombatError("CR_INVALID_ACTION", "invalid targets") from exc
        elif raw_target is not _MISSING and raw_target is not None:
            if isinstance(raw_target, (list, tuple)):
                explicit = list(raw_target)
            elif isinstance(raw_target, str):
                explicit = [raw_target]
            else:
                raise CombatError("CR_INVALID_ACTION", "invalid target")
        else:
            count = actor["targets"]

        if explicit is not None:
            for target_id in explicit:
                if not isinstance(target_id, str) or target_id not in units:
                    raise CombatError("CR_UNKNOWN_UNIT", target_id)

            # A unit with targets > 1 attacks its primary target and then the
            # next living units in registration order.  Explicit target lists
            # are always honoured exactly as submitted.
            single_target_key = (
                raw_targets is _MISSING
                and isinstance(raw_target, str)
            )
            if single_target_key and actor["targets"] > 1:
                for candidate in units:
                    if len(explicit) >= actor["targets"]:
                        break
                    if candidate == actor_id or candidate in explicit:
                        continue
                    if units[candidate]["hp"] > 0:
                        explicit.append(candidate)
            return explicit

        assert count is not None
        selected: list[str] = []
        if isinstance(raw_target, str) and raw_target in units:
            selected.append(raw_target)
        for candidate, unit in units.items():
            if len(selected) >= count:
                break
            if candidate == actor_id or candidate in selected:
                continue
            if unit["hp"] > 0:
                selected.append(candidate)
        if not selected:
            raise CombatError("CR_INVALID_ACTION", "no living target")
        return selected

    def _resolve_attack(self, action: dict, priority: int, actor: dict,
                        defender: dict, rng: random.Random) -> dict:
        dodge_roll = rng.random()
        crit_roll = rng.random()
        block_roll = rng.random()

        dodged = dodge_roll < defender["dodge"]
        crit_success = crit_roll < actor["crit"]
        block_success = block_roll < defender["block"]

        mitigation = defender["defense"] or actor["defense"]
        base = max(1, actor["attack"] + action["power"] - mitigation)
        if dodged:
            damage = 0
        else:
            damage = base * 2 if crit_success else base
            if block_success:
                damage = max(1, damage // 2)

        hit = not dodged
        crit = hit and crit_success
        blocked = hit and block_success
        hp_before = defender["hp"]
        hp_after = max(0, hp_before - damage)
        defender["hp"] = hp_after

        rolls = {
            "dodge": {
                "chance": defender["dodge"],
                "roll": dodge_roll,
                "success": dodged,
            },
            "crit": {
                "chance": actor["crit"],
                "roll": crit_roll,
                "success": crit_success,
            },
            "block": {
                "chance": defender["block"],
                "roll": block_roll,
                "success": block_success,
            },
        }
        return {
            "actor": actor["unit"],
            "kind": action["kind"],
            "target": defender["unit"],
            "priority": priority,
            "hit": hit,
            "dodged": dodged,
            "crit": crit,
            "blocked": blocked,
            "damage": damage,
            "hp_before": hp_before,
            "hp_after": hp_after,
            "power": action["power"],
            "attack": actor["attack"],
            "defense": defender["defense"],
            "actor_defense": actor["defense"],
            "rolls": rolls,
            "dodge_chance": defender["dodge"],
            "dodge_roll": dodge_roll,
            "dodge_success": dodged,
            "crit_chance": actor["crit"],
            "crit_roll": crit_roll,
            "crit_success": crit_success,
            "block_chance": defender["block"],
            "block_roll": block_roll,
            "block_success": block_success,
        }

    def _dead_actor_entry(self, action: dict, units: dict[str, dict]) -> dict:
        raw_target = action["target"]
        if isinstance(raw_target, (list, tuple)):
            target = raw_target[0] if raw_target else None
        elif isinstance(raw_target, str):
            target = raw_target
        else:
            target = None

        if not isinstance(target, str) or target not in units:
            candidates = [
                unit_id for unit_id in units if unit_id != action["actor"]
            ]
            target = candidates[0] if candidates else action["actor"]

        hp = units[target]["hp"]
        return {
            "actor": action["actor"],
            "kind": action["kind"],
            "target": target,
            "priority": action["priority"],
            "hit": False,
            "dodged": False,
            "crit": False,
            "blocked": False,
            "damage": 0,
            "hp_before": hp,
            "hp_after": hp,
            "status": "dead_actor",
        }

    def _dead_target_entry(self, action: dict, priority: int, target_id: str,
                           hp: int) -> dict:
        return {
            "actor": action["actor"],
            "kind": action["kind"],
            "target": target_id,
            "priority": priority,
            "hit": False,
            "dodged": False,
            "crit": False,
            "blocked": False,
            "damage": 0,
            "hp_before": hp,
            "hp_after": hp,
            "status": "dead_target",
        }

    @staticmethod
    def _totals(entries: list[dict]) -> dict:
        return {
            "damage": sum(int(entry["damage"]) for entry in entries),
            "hits": sum(1 for entry in entries if entry.get("hit") is True),
            "dodges": sum(1 for entry in entries if entry.get("dodged") is True),
            "crits": sum(1 for entry in entries if entry.get("crit") is True),
            "blocks": sum(1 for entry in entries if entry.get("blocked") is True),
        }

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------
    def replay(self, log: dict) -> dict:
        with self._lock:
            self._validate_log(log)
            return copy.deepcopy(log)

    def _validate_log(self, log: object) -> None:
        try:
            if not isinstance(log, dict):
                raise ValueError("log is not a mapping")
            if not all(field in log for field in LOG_FIELDS):
                raise ValueError("missing log field")
            _as_int(log["round_no"], minimum=1)
            _as_int(log["seed"])
            entries = log["entries"]
            totals = log["totals"]
            if not isinstance(entries, list) or not entries:
                raise ValueError("entries must be a non-empty list")
            if not isinstance(totals, dict):
                raise ValueError("totals must be a mapping")

            previous_hp: dict[str, int] = {}
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError("entry is not a mapping")
                self._validate_entry(entry, previous_hp)

            expected = self._totals(entries)
            if set(totals) != set(expected):
                raise ValueError("totals keys changed")
            for name, value in expected.items():
                if _as_int(totals[name], minimum=0) != value:
                    raise ValueError("totals do not match entries")
        except CombatError as exc:
            raise CombatError("CR_LOG_TAMPERED", str(exc)) from exc
        except (KeyError, TypeError, ValueError, AssertionError) as exc:
            raise CombatError("CR_LOG_TAMPERED", str(exc)) from exc

    def _validate_entry(self, entry: dict, previous_hp: dict[str, int]) -> None:
        required = (
            "actor",
            "kind",
            "target",
            "priority",
            "hit",
            "dodged",
            "crit",
            "blocked",
            "damage",
            "hp_before",
            "hp_after",
        )
        if not all(field in entry for field in required):
            raise ValueError("entry missing a required field")

        actor = entry["actor"]
        target = entry["target"]
        if not isinstance(actor, str) or not isinstance(target, str):
            raise ValueError("entry ids must be strings")
        if entry["kind"] not in ACTION_KINDS:
            raise ValueError("invalid entry kind")
        _as_int(entry["priority"])
        hp_before = _as_int(entry["hp_before"], minimum=0)
        hp_after = _as_int(entry["hp_after"], minimum=0)
        damage = _as_int(entry["damage"], minimum=0)

        if target in previous_hp and previous_hp[target] != hp_before:
            raise ValueError("hp_before does not follow previous hp_after")
        previous_hp[target] = hp_after

        status = entry.get("status")
        if status in ("dead_actor", "dead_target"):
            if any(entry.get(flag) is not False
                   for flag in ("hit", "dodged", "crit", "blocked")):
                raise ValueError("non-attack entry has combat flags")
            if damage != 0 or hp_before != hp_after:
                raise ValueError("non-attack entry changed hp")
            return
        if status is not None:
            raise ValueError("unknown entry status")

        if any(not isinstance(entry.get(flag), bool)
               for flag in ("hit", "dodged", "crit", "blocked")):
            raise ValueError("combat flags must be booleans")

        rolls = entry.get("rolls")
        if not isinstance(rolls, dict):
            raise ValueError("missing rolls")
        parsed: dict[str, tuple[float, float, bool]] = {}
        for kind in ("dodge", "crit", "block"):
            item = rolls.get(kind)
            if not isinstance(item, dict):
                raise ValueError("missing roll")
            chance = _as_chance(item.get("chance"))
            roll_value = item.get("roll")
            if isinstance(roll_value, bool) or not isinstance(
                    roll_value, (int, float)):
                raise ValueError("roll is not a number")
            if roll_value < 0 or roll_value >= 1:
                raise ValueError("roll outside zero and one")
            roll = float(roll_value)
            success = item.get("success")
            if not isinstance(success, bool) or success != (roll < chance):
                raise ValueError("roll success mismatch")
            parsed[kind] = (chance, roll, success)

        dodged = parsed["dodge"][2]
        crit = (not dodged) and parsed["crit"][2]
        blocked = (not dodged) and parsed["block"][2]
        if entry["dodged"] is not dodged:
            raise ValueError("dodge flag mismatch")
        if entry["crit"] is not crit:
            raise ValueError("crit flag mismatch")
        if entry["blocked"] is not blocked:
            raise ValueError("block flag mismatch")
        if entry["hit"] is not (not dodged):
            raise ValueError("hit flag mismatch")

        power = _as_int(entry.get("power"))
        attack = _as_int(entry.get("attack"), minimum=0)
        defense = _as_int(entry.get("defense"), minimum=0)
        actor_defense = _as_int(entry.get("actor_defense", 0), minimum=0)
        mitigation = defense or actor_defense
        expected_damage = max(1, attack + power - mitigation)
        if dodged:
            expected_damage = 0
        else:
            if crit:
                expected_damage *= 2
            if blocked:
                expected_damage = max(1, expected_damage // 2)
        if damage != expected_damage:
            raise ValueError("damage was tampered")
        if hp_after != max(0, hp_before - damage):
            raise ValueError("hp accounting was tampered")

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "rng_state": copy.deepcopy(self._rng.getstate()),
                "units": copy.deepcopy(self._units),
                "committed_round": self._committed_round,
                "round_no": self._committed_round,
                "seed": self.seed,
                "max_rounds": self.max_rounds,
                "stats": dict(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                if not isinstance(state, dict):
                    raise ValueError("snapshot is not a mapping")
                if "rng_state" not in state or "units" not in state:
                    raise ValueError("snapshot is missing its core state")

                rng_state = _normalise_rng_state(state["rng_state"])
                rng = random.Random()
                rng.setstate(rng_state)

                raw_units = state["units"]
                if not isinstance(raw_units, dict):
                    raise ValueError("units is not a mapping")
                units: dict[str, dict] = {}
                for unit_id, raw_unit in raw_units.items():
                    if not isinstance(unit_id, str) or not unit_id.strip():
                        raise ValueError("invalid unit id")
                    if not isinstance(raw_unit, dict):
                        raise ValueError("invalid unit")
                    max_hp = _as_int(raw_unit["max_hp"], minimum=1)
                    hp = _as_int(raw_unit["hp"], minimum=0, maximum=max_hp)
                    attack = _as_int(raw_unit["attack"], minimum=0)
                    defense = _as_int(raw_unit["defense"], minimum=0)
                    dodge = _as_chance(raw_unit["dodge"])
                    block = _as_chance(raw_unit["block"])
                    crit = _as_chance(raw_unit["crit"])
                    priority = _as_int(raw_unit["priority"])
                    targets = _as_int(raw_unit["targets"], minimum=1)
                    stored_id = raw_unit.get("unit", unit_id)
                    if stored_id != unit_id:
                        raise ValueError("unit id mismatch")
                    units[unit_id] = {
                        "unit": unit_id,
                        "hp": hp,
                        "max_hp": max_hp,
                        "attack": attack,
                        "defense": defense,
                        "dodge": dodge,
                        "block": block,
                        "crit": crit,
                        "priority": priority,
                        "targets": targets,
                    }

                round_key = (
                    "committed_round"
                    if "committed_round" in state
                    else "round_no"
                )
                if round_key not in state:
                    raise ValueError("snapshot is missing the committed round")
                committed_round = _as_int(state[round_key], minimum=0)
                seed = _as_int(state.get("seed", self.seed))
                max_rounds = _as_int(state.get("max_rounds", self.max_rounds),
                                     minimum=1)

                raw_stats = state.get("stats", {name: 0 for name in STAT_FIELDS})
                if not isinstance(raw_stats, dict):
                    raise ValueError("stats is not a mapping")
                stats = {}
                for name in STAT_FIELDS:
                    stats[name] = _as_int(raw_stats[name], minimum=0)
            except (KeyError, TypeError, ValueError) as exc:
                raise CombatError("CR_SNAPSHOT_INVALID", str(exc)) from exc

            # Only assign after every part of the snapshot has been validated.
            self._rng = rng
            self._units = units
            self._committed_round = committed_round
            self.seed = seed
            self.max_rounds = max_rounds
            self._stats = stats

    # ------------------------------------------------------------------
    # Statistics
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        with self._lock:
            return {name: int(self._stats[name]) for name in STAT_FIELDS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {name: 0 for name in STAT_FIELDS}
