"""Squad order queue with formation slots and morale state.

Orders are appended, never replaced. Each pop hands one queued order to one
idle unit; reports release the unit again.
"""

from __future__ import annotations

import copy
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
        self._squads: dict = {}
        self._next_order = 1

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _squad(self, squad: str) -> dict:
        try:
            return self._squads[squad]
        except (KeyError, TypeError):
            raise SquadError("SQUAD_UNKNOWN_SQUAD", {"squad": squad})

    @staticmethod
    def _order_copy(order: dict, *, status: str | None = None) -> dict:
        copied = {
            "order_id": order["order_id"],
            "kind": order["kind"],
            "targets": list(order["targets"]),
            "priority": order["priority"],
        }
        if status is not None:
            copied["status"] = status
        return copied

    @staticmethod
    def _sort_queue(squad: dict) -> None:
        # Front-inserted retreats stay ahead of normal orders.  Within each
        # group the documented priority/arrival ordering is preserved.
        squad["queue"].sort(
            key=lambda order: (
                0 if order.get("front") else 1,
                -order["priority"],
                order["seq"],
            )
        )

    def _idle_unit(self, squad: dict):
        for slot in squad["slot_names"]:
            unit = squad["slots"][slot]
            if unit is None:
                continue
            record = squad["units"][unit]
            if record["alive"] and not record["busy"]:
                return unit, record
        return None, None

    # ------------------------------------------------------------------
    # Formation and lifecycle
    # ------------------------------------------------------------------

    def form(self, squad: str, members, formation: str = "line") -> dict:
        with self._lock:
            try:
                exists = squad in self._squads
            except TypeError:
                raise SquadError("SQUAD_ALREADY_FORMED", {"squad": squad})
            if exists:
                raise SquadError("SQUAD_ALREADY_FORMED", {"squad": squad})

            try:
                slot_names = tuple(self.formations[formation])
            except (KeyError, TypeError):
                raise SquadError(
                    "SQUAD_UNKNOWN_FORMATION", {"formation": formation}
                )

            if members is None or isinstance(members, (str, bytes)):
                raise SquadError(
                    "SQUAD_INVALID_MEMBERS", {"members": members}
                )
            try:
                member_list = list(members)
            except TypeError:
                raise SquadError(
                    "SQUAD_INVALID_MEMBERS", {"members": members}
                )
            if not member_list:
                raise SquadError("SQUAD_INVALID_MEMBERS", {"members": members})

            seen = set()
            for member in member_list:
                try:
                    duplicate = member in seen
                except TypeError:
                    raise SquadError(
                        "SQUAD_INVALID_MEMBERS", {"members": members}
                    )
                if duplicate:
                    raise SquadError(
                        "SQUAD_DUPLICATE_MEMBER", {"member": member}
                    )
                seen.add(member)

            if len(member_list) > len(slot_names):
                raise SquadError(
                    "SQUAD_FORMATION_FULL",
                    {
                        "formation": formation,
                        "capacity": len(slot_names),
                        "members": len(member_list),
                    },
                )

            slots = {slot: None for slot in slot_names}
            units = {}
            for slot, member in zip(slot_names, member_list):
                slots[slot] = member
                units[member] = {
                    "slot": slot,
                    "alive": True,
                    "busy": False,
                    "order": None,
                }

            self._squads[squad] = {
                "formation": formation,
                "slot_names": slot_names,
                "slots": slots,
                "units": units,
                "queue": [],
                "morale": max(self.morale_cap, 0),
                "collapsed": self.morale_cap <= 0,
            }
            return {
                "squad": squad,
                "formation": formation,
                "slots": dict(slots),
            }

    def disband(self, squad: str) -> dict:
        with self._lock:
            self._squad(squad)
            del self._squads[squad]
            return {"squad": squad, "status": "disbanded"}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def issue(self, squad: str, order: str, targets=None,
              *, priority: int = 0) -> dict:
        with self._lock:
            state = self._squad(squad)

            if order not in ORDER_KINDS:
                raise SquadError("SQUAD_INVALID_ORDER", {"order": order})

            try:
                priority = int(priority)
            except (TypeError, ValueError, OverflowError):
                raise SquadError(
                    "SQUAD_INVALID_ORDER", {"priority": priority}
                )

            if targets is None:
                target_list = []
            else:
                if isinstance(targets, str):
                    target_list = [targets]
                else:
                    try:
                        target_list = list(targets)
                    except TypeError:
                        raise SquadError(
                            "SQUAD_INVALID_ORDER", {"targets": targets}
                        )
                if not target_list:
                    raise SquadError(
                        "SQUAD_INVALID_ORDER", {"targets": targets}
                    )

            for target in target_list:
                try:
                    unit = state["units"].get(target)
                except TypeError:
                    raise SquadError(
                        "SQUAD_UNKNOWN_UNIT", {"unit": target}
                    )
                if unit is None or not unit["alive"]:
                    raise SquadError(
                        "SQUAD_UNKNOWN_UNIT", {"unit": target}
                    )

            if state["collapsed"] and order == "attack":
                raise SquadError(
                    "SQUAD_MORALE_BROKEN",
                    {"squad": squad, "morale": state["morale"]},
                )

            if order == "move":
                moving = set()
                for queued in state["queue"]:
                    if queued["kind"] == "move":
                        moving.update(queued["targets"])
                if any(target in moving for target in target_list):
                    raise SquadError(
                        "SQUAD_DUPLICATE_ORDER",
                        {"squad": squad, "targets": target_list},
                    )

            sequence = self._next_order
            record = {
                "order_id": f"ord-{sequence}",
                "kind": order,
                "targets": list(target_list),
                "priority": priority,
                "seq": sequence,
                "front": bool(state["collapsed"] and order == "retreat"),
            }
            self._next_order += 1
            state["queue"].append(record)
            self._sort_queue(state)
            return {
                "order_id": record["order_id"],
                "kind": record["kind"],
                "targets": list(record["targets"]),
                "priority": record["priority"],
                "status": "pending",
            }

    def next_order(self, squad: str) -> dict | None:
        with self._lock:
            state = self._squad(squad)
            if not state["queue"]:
                return None

            unit, record = self._idle_unit(state)
            if unit is None:
                return None

            order = state["queue"].pop(0)
            record["busy"] = True
            record["order"] = dict(order)
            return {
                "order_id": order["order_id"],
                "kind": order["kind"],
                "targets": list(order["targets"]),
                "priority": order["priority"],
                "unit": unit,
            }

    def report(self, squad: str, unit: str, status: str) -> dict:
        with self._lock:
            state = self._squad(squad)
            try:
                record = state["units"].get(unit)
            except TypeError:
                record = None
            if record is None:
                raise SquadError("SQUAD_UNKNOWN_UNIT", {"unit": unit})
            if status not in ("done", "dead"):
                raise SquadError(
                    "SQUAD_INVALID_ORDER", {"status": status}
                )

            record["busy"] = False
            record["order"] = None
            if status == "dead":
                record["alive"] = False

            return {
                "squad": squad,
                "unit": unit,
                "status": status,
                "alive": record["alive"],
                "busy": False,
            }

    def morale(self, squad: str, delta: int) -> dict:
        with self._lock:
            state = self._squad(squad)
            try:
                delta = int(delta)
            except (TypeError, ValueError, OverflowError):
                raise SquadError(
                    "SQUAD_INVALID_ORDER", {"delta": delta}
                )

            cap = max(self.morale_cap, 0)
            value = max(0, min(cap, state["morale"] + delta))
            state["morale"] = value
            state["collapsed"] = value == 0
            return {
                "squad": squad,
                "morale": value,
                "collapsed": state["collapsed"],
            }

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def slots(self, squad: str) -> dict:
        with self._lock:
            state = self._squad(squad)
            return {
                slot: state["slots"][slot]
                for slot in state["slot_names"]
            }

    def queue(self, squad: str) -> list:
        with self._lock:
            state = self._squad(squad)
            return [
                self._order_copy(order, status="pending")
                for order in state["queue"]
            ]

    def roster(self, squad: str) -> dict:
        with self._lock:
            state = self._squad(squad)
            return {
                unit: {
                    "slot": record["slot"],
                    "alive": record["alive"],
                    "busy": record["busy"],
                }
                for unit, record in state["units"].items()
            }

    def idle_units(self, squad: str) -> list:
        with self._lock:
            state = self._squad(squad)
            result = []
            for slot in state["slot_names"]:
                unit = state["slots"][slot]
                if unit is None:
                    continue
                record = state["units"][unit]
                if record["alive"] and not record["busy"]:
                    result.append(unit)
            return result

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            squads = {}
            for name, state in self._squads.items():
                squads[name] = {
                    "formation": state["formation"],
                    "slot_names": list(state["slot_names"]),
                    "slots": dict(state["slots"]),
                    "units": {
                        unit: {
                            "slot": record["slot"],
                            "alive": record["alive"],
                            "busy": record["busy"],
                            "order": copy.deepcopy(record["order"]),
                        }
                        for unit, record in state["units"].items()
                    },
                    "queue": [copy.deepcopy(order) for order in state["queue"]],
                    "morale": state["morale"],
                    "collapsed": state["collapsed"],
                }
            return {
                "version": 1,
                "next_order": self._next_order,
                "next_order_id": self._next_order,
                "next_order_seq": self._next_order,
                "squads": squads,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                squads, next_order = self._parse_snapshot(state)
            except SquadError:
                raise
            except Exception as exc:
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID", {"error": repr(exc)}
                ) from exc
            self._squads = squads
            self._next_order = next_order

    def _parse_snapshot(self, state):
        if not isinstance(state, dict):
            raise SquadError("SQUAD_SNAPSHOT_INVALID", {"state": state})

        version = state.get("version")
        if (not isinstance(version, int) or isinstance(version, bool)
                or version != 1):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID", {"version": version}
            )

        raw_squads = state.get("squads")
        if not isinstance(raw_squads, dict):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID", {"squads": raw_squads}
            )

        sequence_values = [
            state[key]
            for key in (
                "next_order",
                "next_order_id",
                "next_order_seq",
                "order_seq",
                "next_id",
            )
            if key in state
        ]
        if not sequence_values:
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID", {"next_order": None}
            )
        first_sequence = sequence_values[0]
        if (not isinstance(first_sequence, int)
                or isinstance(first_sequence, bool)
                or first_sequence < 1):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"next_order": first_sequence},
            )
        if any(value != first_sequence for value in sequence_values[1:]):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"next_order": sequence_values},
            )
        next_order = first_sequence

        squads = {}
        used_order_ids = set()
        max_sequence = 0

        for name, raw in raw_squads.items():
            if not isinstance(raw, dict):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID", {"squad": name}
                )

            formation = raw.get("formation")
            if not isinstance(formation, str):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "formation": formation},
                )

            if "slot_names" in raw:
                raw_slots = raw["slot_names"]
                if not isinstance(raw_slots, (list, tuple)):
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "slot_names": raw_slots},
                    )
                slot_names = tuple(raw_slots)
            else:
                try:
                    slot_names = tuple(self.formations[formation])
                except (KeyError, TypeError):
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "formation": formation},
                    )

            try:
                slot_set = set(slot_names)
            except TypeError:
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "slot_names": slot_names},
                )
            if len(slot_set) != len(slot_names):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "slot_names": slot_names},
                )
            for slot in slot_names:
                if not isinstance(slot, str):
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "slot": slot},
                    )

            raw_slots = raw.get("slots")
            if not isinstance(raw_slots, dict):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "slots": raw_slots},
                )
            if set(raw_slots) != slot_set:
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "slots": raw_slots},
                )
            slots = {slot: raw_slots[slot] for slot in slot_names}

            raw_units = raw.get("units")
            if not isinstance(raw_units, dict):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "units": raw_units},
                )

            occupied = {}
            for slot in slot_names:
                unit = slots[slot]
                if unit is None:
                    continue
                try:
                    duplicate = unit in occupied
                except TypeError:
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit},
                    )
                if duplicate:
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit},
                    )
                occupied[unit] = slot

            units = {}
            for unit, unit_state in raw_units.items():
                if not isinstance(unit_state, dict):
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit},
                    )
                slot = unit_state.get("slot")
                alive = unit_state.get("alive")
                busy = unit_state.get("busy")
                if slot not in slot_set:
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit, "slot": slot},
                    )
                if not isinstance(alive, bool) or not isinstance(busy, bool):
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit},
                    )
                try:
                    slot_owner = occupied.get(unit)
                except TypeError:
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit},
                    )
                if slots[slot] != unit or slot_owner != slot:
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"squad": name, "unit": unit, "slot": slot},
                    )

                active = unit_state.get("order")
                if active is not None:
                    active = self._parse_order(
                        active, name, units_for_targets=raw_units
                    )
                    if not busy:
                        raise SquadError(
                            "SQUAD_SNAPSHOT_INVALID",
                            {"squad": name, "unit": unit},
                        )
                    if active["order_id"] in used_order_ids:
                        raise SquadError(
                            "SQUAD_SNAPSHOT_INVALID",
                            {"order_id": active["order_id"]},
                        )
                    used_order_ids.add(active["order_id"])
                    max_sequence = max(max_sequence, active["seq"])

                units[unit] = {
                    "slot": slot,
                    "alive": alive,
                    "busy": busy,
                    "order": active,
                }

            if set(units) != set(occupied):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "units": raw_units},
                )

            raw_queue = raw.get("queue")
            if not isinstance(raw_queue, list):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "queue": raw_queue},
                )
            queue = []
            for index, raw_order in enumerate(raw_queue):
                order = self._parse_order(
                    raw_order,
                    name,
                    units_for_targets=raw_units,
                    fallback_seq=index + 1,
                )
                if raw_order.get("status", "pending") != "pending":
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"order": order["order_id"]},
                    )
                if order["order_id"] in used_order_ids:
                    raise SquadError(
                        "SQUAD_SNAPSHOT_INVALID",
                        {"order_id": order["order_id"]},
                    )
                used_order_ids.add(order["order_id"])
                max_sequence = max(max_sequence, order["seq"])
                queue.append(order)

            morale = raw.get("morale")
            if (not isinstance(morale, int) or isinstance(morale, bool)
                    or morale < 0 or morale > max(self.morale_cap, 0)):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "morale": morale},
                )
            collapsed = raw.get("collapsed")
            if not isinstance(collapsed, bool):
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"squad": name, "collapsed": collapsed},
                )

            squads[name] = {
                "formation": formation,
                "slot_names": slot_names,
                "slots": slots,
                "units": units,
                "queue": queue,
                "morale": morale,
                "collapsed": morale == 0,
            }

        if next_order <= max_sequence:
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"next_order": next_order, "last_order": max_sequence},
            )

        return squads, next_order

    @staticmethod
    def _parse_order(raw, squad_name, *, units_for_targets,
                     fallback_seq=None) -> dict:
        if not isinstance(raw, dict):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"squad": squad_name, "order": raw},
            )

        order_id = raw.get("order_id")
        kind = raw.get("kind")
        targets = raw.get("targets")
        priority = raw.get("priority")
        sequence = raw.get("seq", fallback_seq)
        front = raw.get("front", False)

        if not isinstance(order_id, str) or not order_id:
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID", {"order_id": order_id}
            )
        if kind not in ORDER_KINDS:
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"order_id": order_id, "kind": kind},
            )
        if not isinstance(targets, (list, tuple)):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"order_id": order_id, "targets": targets},
            )
        for target in targets:
            try:
                known = target in units_for_targets
            except TypeError:
                known = False
            if not known:
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"order_id": order_id, "target": target},
                )
        if not isinstance(priority, int) or isinstance(priority, bool):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"order_id": order_id, "priority": priority},
            )
        if (not isinstance(sequence, int) or isinstance(sequence, bool)
                or sequence < 1):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"order_id": order_id, "seq": sequence},
            )
        if order_id.startswith("ord-"):
            try:
                numeric_id = int(order_id[4:])
            except ValueError:
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"order_id": order_id},
                )
            if numeric_id != sequence:
                raise SquadError(
                    "SQUAD_SNAPSHOT_INVALID",
                    {"order_id": order_id, "seq": sequence},
                )
        if not isinstance(front, bool):
            raise SquadError(
                "SQUAD_SNAPSHOT_INVALID",
                {"order_id": order_id, "front": front},
            )

        return {
            "order_id": order_id,
            "kind": kind,
            "targets": list(targets),
            "priority": priority,
            "seq": sequence,
            "front": front,
        }
