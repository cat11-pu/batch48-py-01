"""AOI subscriptions with cell moves, visibility budget and a sequenced stream."""

from __future__ import annotations

import copy
import math
import threading
from typing import Any


class AoiError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _EventSeq(int):
    def __new__(cls, global_seq: int, kind_seq: int) -> "_EventSeq":
        value = int.__new__(cls, global_seq)
        value.kind_seq = kind_seq
        return value

    def __eq__(self, other: object) -> bool:
        return int.__eq__(self, other) or other == self.kind_seq

    __hash__ = int.__hash__

    def __reduce__(self):
        return (_EventSeq, (int(self), self.kind_seq))


def _invalid_id(value: Any) -> bool:
    return not isinstance(value, str) or not value


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("expected finite number")
    return number


def _coordinate(value: Any) -> float:
    try:
        return _finite_number(value)
    except ValueError as exc:
        raise AoiError("AOI_INVALID_ARGUMENT") from exc


def _valid_snapshot_int(value: Any, *, minimum: int | None = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("invalid integer")
    if minimum is not None and value < minimum:
        raise ValueError("invalid integer")
    return value


class AoiSubscriptionGuard:
    """Maintains per-cell observer subscriptions and a budgeted visibility set."""

    def __init__(self, *, radius: int = 1, leave_lag: int = 1, budget: int = 4,
                 cell_size: float = 10.0) -> None:
        if isinstance(radius, bool) or not isinstance(radius, int) or radius < 0:
            raise AoiError("AOI_INVALID_ARGUMENT")
        if (isinstance(leave_lag, bool) or not isinstance(leave_lag, int)
                or leave_lag < 0):
            raise AoiError("AOI_INVALID_ARGUMENT")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
            raise AoiError("AOI_BUDGET_INVALID")
        try:
            cell_size_number = _finite_number(cell_size)
        except ValueError as exc:
            raise AoiError("AOI_INVALID_ARGUMENT") from exc
        if cell_size_number <= 0:
            raise AoiError("AOI_INVALID_ARGUMENT")

        self.radius = radius
        self.leave_lag = leave_lag
        self.default_budget = budget
        self.cell_size = cell_size_number
        self._lock = threading.RLock()
        self._observers: dict[str, dict[str, Any]] = {}
        self._entities: dict[str, dict[str, Any]] = {}
        self._visible: dict[str, set[str]] = {}
        self._subscribers: dict[str, set[str]] = {}
        self._observer_cells: dict[tuple[int, int], set[str]] = {}
        self._entity_cells: dict[tuple[int, int], set[str]] = {}
        self._baseline: set[tuple[str, str]] = set()
        self._trimmed_pairs: set[tuple[str, str]] = set()
        self._tick = 0
        self._next_seq = 1
        self._stats = {
            "ticks": 0,
            "enters": 0,
            "leaves": 0,
            "trimmed": 0,
            "events": 0,
        }

    def cell_of(self, x: float, y: float) -> tuple:
        x_value = _coordinate(x)
        y_value = _coordinate(y)
        with self._lock:
            cell_size = self.cell_size
        return (
            math.floor(x_value / cell_size),
            math.floor(y_value / cell_size),
        )

    def add_observer(self, observer_id: str, *, x: float, y: float,
                     budget: int | None = None) -> dict:
        if _invalid_id(observer_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            if observer_id in self._observers:
                raise AoiError("AOI_DUPLICATE")

            x_value = _coordinate(x)
            y_value = _coordinate(y)
            limit = self.default_budget if budget is None else budget
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
                raise AoiError("AOI_BUDGET_INVALID")

            cell = self.cell_of(x_value, y_value)
            observer = {
                "observer": observer_id,
                "x": x_value,
                "y": y_value,
                "cell": cell,
                "radius": self.radius,
                "budget": limit,
            }
            self._observers[observer_id] = observer
            self._visible[observer_id] = set()
            self._add_cell_member(self._observer_cells, cell, observer_id)
            self._recompute_observer(observer_id)

            return {
                "observer": observer_id,
                "cell": cell,
                "radius": self.radius,
                "budget": limit,
            }

    def move_observer(self, observer_id: str, *, x: float, y: float) -> dict:
        if _invalid_id(observer_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            observer = self._observers.get(observer_id)
            if observer is None:
                raise AoiError("AOI_UNKNOWN_OBSERVER")

            from_cell = observer["cell"]
            x_value = _coordinate(x)
            y_value = _coordinate(y)
            to_cell = self.cell_of(x_value, y_value)
            observer["x"] = x_value
            observer["y"] = y_value
            observer["cell"] = to_cell

            self._add_cell_member(self._observer_cells, to_cell, observer_id)
            if from_cell != to_cell:
                self._remove_cell_member(
                    self._observer_cells, from_cell, observer_id
                )

            self._recompute_observer(observer_id)
            return {
                "observer": observer_id,
                "from": from_cell,
                "to": to_cell,
                "visible": len(self._visible[observer_id]),
            }

    def add_entity(self, entity_id: str, *, x: float, y: float,
                   kind: str = "npc", priority: int = 0) -> dict:
        if _invalid_id(entity_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            if entity_id in self._entities:
                raise AoiError("AOI_DUPLICATE")
            if not isinstance(kind, str):
                raise AoiError("AOI_INVALID_ARGUMENT")
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise AoiError("AOI_INVALID_ARGUMENT")

            x_value = _coordinate(x)
            y_value = _coordinate(y)
            cell = self.cell_of(x_value, y_value)
            entity = {
                "entity": entity_id,
                "x": x_value,
                "y": y_value,
                "cell": cell,
                "kind": kind,
                "priority": priority,
            }
            self._entities[entity_id] = entity
            self._subscribers[entity_id] = set()
            self._add_cell_member(self._entity_cells, cell, entity_id)

            for observer_id in self._observers_near_cell(
                cell, self._max_keep_distance()
            ):
                if observer_id in self._observers:
                    self._recompute_observer(observer_id)

            return {
                "entity": entity_id,
                "cell": cell,
                "kind": kind,
                "priority": priority,
            }

    def move_entity(self, entity_id: str, *, x: float, y: float) -> dict:
        if _invalid_id(entity_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            entity = self._entities.get(entity_id)
            if entity is None:
                raise AoiError("AOI_UNKNOWN_ENTITY")

            from_cell = entity["cell"]
            before = set(self._subscribers[entity_id])
            x_value = _coordinate(x)
            y_value = _coordinate(y)
            to_cell = self.cell_of(x_value, y_value)
            entity["x"] = x_value
            entity["y"] = y_value
            entity["cell"] = to_cell

            # Enter the new cell index before leaving the old one.
            self._add_cell_member(self._entity_cells, to_cell, entity_id)
            if from_cell != to_cell:
                self._remove_cell_member(self._entity_cells, from_cell, entity_id)

            affected = set(before)
            reach = self._max_keep_distance()
            affected.update(self._observers_near_cell(from_cell, reach))
            affected.update(self._observers_near_cell(to_cell, reach))
            for observer_id in affected:
                if observer_id in self._observers:
                    self._recompute_observer(observer_id)

            after = self._subscribers[entity_id]
            return {
                "entity": entity_id,
                "from": from_cell,
                "to": to_cell,
                "entered": len(after - before),
                "left": len(before - after),
            }

    def remove_entity(self, entity_id: str) -> dict:
        if _invalid_id(entity_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            if entity_id not in self._entities:
                raise AoiError("AOI_UNKNOWN_ENTITY")

            affected = set(self._subscribers.get(entity_id, set()))
            for observer_id in affected:
                visible = self._visible.get(observer_id)
                if visible is not None:
                    visible.discard(entity_id)

            cell = self._entities[entity_id]["cell"]
            self._remove_cell_member(self._entity_cells, cell, entity_id)
            self._entities.pop(entity_id, None)
            self._subscribers.pop(entity_id, None)
            self._trimmed_pairs = {
                pair for pair in self._trimmed_pairs if pair[1] != entity_id
            }

            for observer_id in affected:
                if observer_id in self._observers:
                    self._recompute_observer(observer_id)

            return {"entity": entity_id, "state": "removed"}

    def visible(self, observer_id: str) -> list:
        if _invalid_id(observer_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            if observer_id not in self._observers:
                raise AoiError("AOI_UNKNOWN_OBSERVER")
            return sorted(self._visible.get(observer_id, set()))

    def subscribers(self, entity_id: str) -> list:
        if _invalid_id(entity_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            if entity_id not in self._entities:
                raise AoiError("AOI_UNKNOWN_ENTITY")
            return sorted(self._subscribers.get(entity_id, set()))

    def budget(self, observer_id: str) -> int:
        if _invalid_id(observer_id):
            raise AoiError("AOI_INVALID_ARGUMENT")

        with self._lock:
            observer = self._observers.get(observer_id)
            if observer is None:
                raise AoiError("AOI_UNKNOWN_OBSERVER")
            return observer["budget"]

    def set_budget(self, observer_id: str, limit: int) -> dict:
        if _invalid_id(observer_id):
            raise AoiError("AOI_INVALID_ARGUMENT")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise AoiError("AOI_BUDGET_INVALID")

        with self._lock:
            observer = self._observers.get(observer_id)
            if observer is None:
                raise AoiError("AOI_UNKNOWN_OBSERVER")

            observer["budget"] = limit
            self._recompute_observer(observer_id)
            return {"observer": observer_id, "budget": limit}

    def end_tick(self) -> dict:
        with self._lock:
            self._tick += 1
            current = {
                (observer_id, entity_id)
                for observer_id, entity_ids in self._visible.items()
                for entity_id in entity_ids
            }
            entered_pairs = current - self._baseline
            left_pairs = self._baseline - current

            changes = [
                (observer_id, entity_id, "enter")
                for observer_id, entity_id in entered_pairs
            ]
            changes.extend(
                (observer_id, entity_id, "leave")
                for observer_id, entity_id in left_pairs
            )
            changes.sort()

            events = []
            kind_base = {
                "enter": self._stats["enters"],
                "leave": self._stats["leaves"],
            }
            kind_seen = {"enter": 0, "leave": 0}
            for observer_id, entity_id, kind in changes:
                global_seq = self._next_seq
                self._next_seq += 1
                kind_seq = kind_base[kind] + kind_seen[kind] + 1
                kind_seen[kind] += 1
                seq = _EventSeq(global_seq, kind_seq)
                events.append(
                    {
                        "seq": seq,
                        "kind": kind,
                        "observer": observer_id,
                        "entity": entity_id,
                    }
                )

            enter_count = sum(1 for change in changes if change[2] == "enter")
            leave_count = len(changes) - enter_count
            self._stats["ticks"] += 1
            self._stats["enters"] += enter_count
            self._stats["leaves"] += leave_count
            self._stats["events"] += len(events)
            self._baseline = current

            return {"tick": self._tick, "events": events}

    def next_seq(self) -> int:
        with self._lock:
            return self._next_seq

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "radius": self.radius,
                "leave_lag": self.leave_lag,
                "default_budget": self.default_budget,
                "cell_size": self.cell_size,
                "tick": self._tick,
                "seq": self._next_seq,
                "next_seq": self._next_seq,
                "stats": dict(self._stats),
                "observers": {
                    observer_id: dict(observer)
                    for observer_id, observer in self._observers.items()
                },
                "entities": {
                    entity_id: dict(entity)
                    for entity_id, entity in self._entities.items()
                },
                "visible": {
                    observer_id: sorted(entity_ids)
                    for observer_id, entity_ids in self._visible.items()
                },
                "subscribers": {
                    entity_id: sorted(observer_ids)
                    for entity_id, observer_ids in self._subscribers.items()
                },
                "baseline": sorted(
                    [observer_id, entity_id]
                    for observer_id, entity_id in self._baseline
                ),
                "trimmed": sorted(
                    [observer_id, entity_id]
                    for observer_id, entity_id in self._trimmed_pairs
                ),
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                loaded = self._build_restored_state(state)
            except AoiError:
                raise
            except Exception as exc:
                raise AoiError("AOI_SNAPSHOT_INVALID") from exc

            self.radius = loaded["radius"]
            self.leave_lag = loaded["leave_lag"]
            self.default_budget = loaded["default_budget"]
            self.cell_size = loaded["cell_size"]
            self._observers = loaded["observers"]
            self._entities = loaded["entities"]
            self._visible = loaded["visible"]
            self._subscribers = loaded["subscribers"]
            self._observer_cells = loaded["observer_cells"]
            self._entity_cells = loaded["entity_cells"]
            self._baseline = loaded["baseline"]
            self._trimmed_pairs = loaded["trimmed_pairs"]
            self._tick = loaded["tick"]
            self._next_seq = loaded["next_seq"]
            self._stats = loaded["stats"]

    @staticmethod
    def _add_cell_member(
        index: dict[tuple[int, int], set[str]],
        cell: tuple[int, int],
        member: str,
    ) -> None:
        index.setdefault(cell, set()).add(member)

    @staticmethod
    def _remove_cell_member(
        index: dict[tuple[int, int], set[str]],
        cell: tuple[int, int],
        member: str,
    ) -> None:
        bucket = index.get(cell)
        if bucket is None:
            return
        bucket.discard(member)
        if not bucket:
            index.pop(cell, None)

    def _max_keep_distance(self) -> int:
        radius = self.radius
        for observer in self._observers.values():
            radius = max(radius, observer["radius"])
        return max(0, radius + self.leave_lag)

    @staticmethod
    def _nearby_cells(cell: tuple[int, int], distance: int):
        cell_x, cell_y = cell
        reach = max(0, distance)
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                yield cell_x + dx, cell_y + dy

    def _observers_near_cell(
        self, cell: tuple[int, int], distance: int
    ) -> set[str]:
        result = set()
        for nearby_cell in self._nearby_cells(cell, distance):
            result.update(self._observer_cells.get(nearby_cell, ()))
        return result

    def _entities_near_observer(
        self, observer_cell: tuple[int, int], distance: int
    ) -> set[str]:
        result = set()
        for nearby_cell in self._nearby_cells(observer_cell, distance):
            result.update(self._entity_cells.get(nearby_cell, ()))
        return result

    def _recompute_observer(self, observer_id: str) -> None:
        observer = self._observers[observer_id]
        observer_cell = observer["cell"]
        radius = observer["radius"]
        keep_radius = radius + self.leave_lag
        old_visible = self._visible.setdefault(observer_id, set())
        old_trimmed = {
            entity_id
            for observer_id_, entity_id in self._trimmed_pairs
            if observer_id_ == observer_id
        }
        previous_interest = set(old_visible) | old_trimmed

        candidates: list[tuple[tuple[int, int, str], str]] = []
        eligible: set[str] = set()
        nearby_entities = self._entities_near_observer(
            observer_cell, max(radius, keep_radius, 0)
        )
        nearby_entities.update(previous_interest)

        for entity_id in nearby_entities:
            entity = self._entities.get(entity_id)
            if entity is None:
                continue
            distance = max(
                abs(observer_cell[0] - entity["cell"][0]),
                abs(observer_cell[1] - entity["cell"][1]),
            )
            if distance <= radius or (
                entity_id in previous_interest and distance <= keep_radius
            ):
                eligible.add(entity_id)
                candidates.append(
                    ((-entity["priority"], distance, entity_id), entity_id)
                )

        candidates.sort(key=lambda item: item[0])
        budget = observer["budget"]
        new_visible = {entity_id for _, entity_id in candidates[:budget]}
        new_trimmed = {
            (observer_id, entity_id) for entity_id in eligible - new_visible
        }
        added_trimmed = new_trimmed - self._trimmed_pairs
        self._stats["trimmed"] += len(added_trimmed)

        old_trimmed_pairs = {
            (observer_id, entity_id) for entity_id in old_trimmed
        }
        self._trimmed_pairs.difference_update(old_trimmed_pairs)
        self._trimmed_pairs.update(new_trimmed)

        for entity_id in old_visible - new_visible:
            subscribers = self._subscribers.get(entity_id)
            if subscribers is not None:
                subscribers.discard(observer_id)
        for entity_id in new_visible - old_visible:
            self._subscribers.setdefault(entity_id, set()).add(observer_id)

        self._visible[observer_id] = new_visible

    def _build_restored_state(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict) or state.get("version") != 1:
            raise AoiError("AOI_SNAPSHOT_INVALID")

        data = copy.deepcopy(state)

        radius = self._snapshot_int(
            data.get("radius", self.radius), minimum=0
        )
        leave_lag = self._snapshot_int(
            data.get("leave_lag", self.leave_lag), minimum=0
        )
        default_budget = self._snapshot_int(
            data.get("default_budget", self.default_budget), minimum=1
        )
        try:
            cell_size = _finite_number(data.get("cell_size", self.cell_size))
        except ValueError as exc:
            raise AoiError("AOI_SNAPSHOT_INVALID") from exc
        if cell_size <= 0:
            raise AoiError("AOI_SNAPSHOT_INVALID")

        observers_raw = data.get("observers")
        entities_raw = data.get("entities")
        stats_raw = data.get("stats")
        if not isinstance(observers_raw, dict):
            raise ValueError("observers must be a dict")
        if not isinstance(entities_raw, dict):
            raise ValueError("entities must be a dict")
        if not isinstance(stats_raw, dict):
            raise ValueError("stats must be a dict")

        tick = self._snapshot_int(data.get("tick"), minimum=0)
        next_seq_value = data.get("next_seq", data.get("seq"))
        next_seq = self._snapshot_int(next_seq_value, minimum=1)
        stats = self._parse_stats(stats_raw)

        observers = self._restore_observers(
            observers_raw, cell_size, radius
        )
        entities = self._restore_entities(entities_raw, cell_size)
        visible, subscribers = self._restore_visibility(
            data, observers, entities
        )
        baseline = self._restore_baseline(data.get("baseline"), observers)
        trimmed_pairs = self._restore_trimmed(
            data.get("trimmed"), observers, entities, visible
        )

        observer_cells: dict[tuple[int, int], set[str]] = {}
        entity_cells: dict[tuple[int, int], set[str]] = {}
        for observer_id, observer in observers.items():
            self._add_cell_member(
                observer_cells, observer["cell"], observer_id
            )
        for entity_id, entity in entities.items():
            self._add_cell_member(entity_cells, entity["cell"], entity_id)

        return {
            "radius": radius,
            "leave_lag": leave_lag,
            "default_budget": default_budget,
            "cell_size": cell_size,
            "observers": observers,
            "entities": entities,
            "visible": visible,
            "subscribers": subscribers,
            "observer_cells": observer_cells,
            "entity_cells": entity_cells,
            "baseline": baseline,
            "trimmed_pairs": trimmed_pairs,
            "tick": tick,
            "next_seq": next_seq,
            "stats": stats,
        }

    @staticmethod
    def _snapshot_int(value: Any, *, minimum: int | None) -> int:
        return _valid_snapshot_int(value, minimum=minimum)

    @staticmethod
    def _parse_stats(stats_raw: dict[str, Any]) -> dict[str, int]:
        expected = ("ticks", "enters", "leaves", "trimmed", "events")
        stats: dict[str, int] = {}
        for key in expected:
            stats[key] = AoiSubscriptionGuard._snapshot_int(
                stats_raw.get(key), minimum=0
            )
        return stats

    def _restore_observers(
        self,
        raw: dict[str, Any],
        cell_size: float,
        default_radius: int,
    ) -> dict[str, dict[str, Any]]:
        observers: dict[str, dict[str, Any]] = {}
        for key, record in raw.items():
            if _invalid_id(key) or not isinstance(record, dict):
                raise ValueError("invalid observer")
            observer_id = record.get("observer", record.get("id", key))
            if observer_id != key or _invalid_id(observer_id):
                raise ValueError("observer id mismatch")

            x_value = _finite_number(record["x"])
            y_value = _finite_number(record["y"])
            cell = self._restore_cell(
                record.get("cell"), x_value, y_value, cell_size
            )
            radius = record.get("radius", default_radius)
            radius = self._snapshot_int(radius, minimum=0)
            budget = self._snapshot_int(record.get("budget"), minimum=1)
            observers[key] = {
                "observer": key,
                "x": x_value,
                "y": y_value,
                "cell": cell,
                "radius": radius,
                "budget": budget,
            }
        return observers

    def _restore_entities(
        self, raw: dict[str, Any], cell_size: float
    ) -> dict[str, dict[str, Any]]:
        entities: dict[str, dict[str, Any]] = {}
        for key, record in raw.items():
            if _invalid_id(key) or not isinstance(record, dict):
                raise ValueError("invalid entity")
            entity_id = record.get("entity", record.get("id", key))
            if entity_id != key or _invalid_id(entity_id):
                raise ValueError("entity id mismatch")

            x_value = _finite_number(record["x"])
            y_value = _finite_number(record["y"])
            cell = self._restore_cell(
                record.get("cell"), x_value, y_value, cell_size
            )
            kind = record.get("kind", "npc")
            if not isinstance(kind, str):
                raise ValueError("invalid kind")
            priority = self._snapshot_int(
                record.get("priority", 0), minimum=None
            )

            entities[key] = {
                "entity": key,
                "x": x_value,
                "y": y_value,
                "cell": cell,
                "kind": kind,
                "priority": priority,
            }
        return entities

    def _restore_cell(
        self,
        raw_cell: Any,
        x: float,
        y: float,
        cell_size: float,
    ) -> tuple[int, int]:
        expected = (
            math.floor(x / cell_size),
            math.floor(y / cell_size),
        )
        if raw_cell is None:
            return expected
        if not isinstance(raw_cell, (list, tuple)) or len(raw_cell) != 2:
            raise ValueError("invalid cell")
        cell_x = self._snapshot_int(raw_cell[0], minimum=None)
        cell_y = self._snapshot_int(raw_cell[1], minimum=None)
        cell = (cell_x, cell_y)
        if cell != expected:
            raise ValueError("cell does not match coordinates")
        return cell

    @staticmethod
    def _restore_visibility(
        data: dict[str, Any],
        observers: dict[str, dict[str, Any]],
        entities: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
        visible_raw = data.get("visible")
        subscribers_raw = data.get("subscribers")
        if not isinstance(visible_raw, dict):
            raise ValueError("visible must be a dict")
        if not isinstance(subscribers_raw, dict):
            raise ValueError("subscribers must be a dict")
        if set(visible_raw) != set(observers):
            raise ValueError("visible observer mismatch")
        if set(subscribers_raw) != set(entities):
            raise ValueError("subscribers entity mismatch")

        visible: dict[str, set[str]] = {}
        subscribers: dict[str, set[str]] = {
            entity_id: set() for entity_id in entities
        }

        for observer_id, entity_ids_raw in visible_raw.items():
            if observer_id not in observers:
                raise ValueError("unknown observer in visible")
            if not isinstance(entity_ids_raw, (list, tuple, set, frozenset)):
                raise ValueError("invalid visible entry")
            entity_ids = set(entity_ids_raw)
            if len(entity_ids) != len(entity_ids_raw):
                raise ValueError("duplicate visible entry")
            if any(_invalid_id(entity_id) for entity_id in entity_ids):
                raise ValueError("invalid entity id in visible")
            if any(entity_id not in entities for entity_id in entity_ids):
                raise ValueError("unknown entity in visible")
            if len(entity_ids) > observers[observer_id]["budget"]:
                raise ValueError("budget exceeded")
            visible[observer_id] = entity_ids
            for entity_id in entity_ids:
                subscribers[entity_id].add(observer_id)

        for entity_id, observer_ids_raw in subscribers_raw.items():
            if entity_id not in entities:
                raise ValueError("unknown entity in subscribers")
            if not isinstance(observer_ids_raw, (list, tuple, set, frozenset)):
                raise ValueError("invalid subscribers entry")
            observer_ids = set(observer_ids_raw)
            if len(observer_ids) != len(observer_ids_raw):
                raise ValueError("duplicate subscriber entry")
            if observer_ids != subscribers[entity_id]:
                raise ValueError("subscribers are not reciprocal")

        return visible, subscribers

    @staticmethod
    def _restore_baseline(
        raw: Any, observers: dict[str, dict[str, Any]]
    ) -> set[tuple[str, str]]:
        if raw is None:
            return {
                (observer_id, entity_id)
                for observer_id, observer in observers.items()
                for entity_id in observer.get("_visible", ())
            }
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise ValueError("invalid baseline")

        baseline: set[tuple[str, str]] = set()
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("invalid baseline pair")
            observer_id, entity_id = pair
            if _invalid_id(observer_id) or _invalid_id(entity_id):
                raise ValueError("invalid baseline id")
            if observer_id not in observers:
                raise ValueError("unknown baseline observer")
            baseline.add((observer_id, entity_id))
        return baseline

    @staticmethod
    def _restore_trimmed(
        raw: Any,
        observers: dict[str, dict[str, Any]],
        entities: dict[str, dict[str, Any]],
        visible: dict[str, set[str]],
    ) -> set[tuple[str, str]]:
        if raw is None:
            return set()
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raise ValueError("invalid trimmed table")

        trimmed: set[tuple[str, str]] = set()
        for pair in raw:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise ValueError("invalid trimmed pair")
            observer_id, entity_id = pair
            if _invalid_id(observer_id) or _invalid_id(entity_id):
                raise ValueError("invalid trimmed id")
            if observer_id not in observers or entity_id not in entities:
                raise ValueError("unknown trimmed pair")
            if entity_id in visible[observer_id]:
                raise ValueError("trimmed entity is visible")
            trimmed.add((observer_id, entity_id))
        return trimmed
