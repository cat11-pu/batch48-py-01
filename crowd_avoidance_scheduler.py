"""Frame scheduled crowd avoidance with neighbor budget and stuck recovery."""

from __future__ import annotations

import heapq
import hashlib
import math
import threading


class CrowdError(ValueError):
    """Domain failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_real_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _point(value: object, code: str = "AV_INVALID_POSITION") -> tuple[float, float]:
    if not isinstance(value, (tuple, list)) or len(value) != 2:
        raise CrowdError(code, {"value": value})
    x, y = value
    if not _is_real_number(x) or not _is_real_number(y):
        raise CrowdError(code, {"value": value})
    return float(x), float(y)


def _optional_point(value: object) -> tuple[float, float] | None:
    if value is None:
        return None
    return _point(value)


def _non_negative_int(value: object, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CrowdError(code, {"value": value})
    return value


class AvoidanceScheduler:
    """Advances registered units one frame at a time under a neighbor budget."""

    _STUCK_WINDOW = 5

    def __init__(self, *, neighbor_budget: int = 32, cell_size: float = 2.0,
                 stuck_epsilon: float = 0.01) -> None:
        if (isinstance(neighbor_budget, bool)
                or not isinstance(neighbor_budget, int)
                or neighbor_budget < 0):
            raise CrowdError("AV_INVALID_BUDGET",
                             {"neighbor_budget": neighbor_budget})
        if not _is_real_number(cell_size) or float(cell_size) <= 0.0:
            raise CrowdError("AV_INVALID_CELL_SIZE", {"cell_size": cell_size})
        if not _is_real_number(stuck_epsilon) or float(stuck_epsilon) < 0.0:
            raise CrowdError("AV_INVALID_CONFIG",
                             {"stuck_epsilon": stuck_epsilon})

        self.neighbor_budget = neighbor_budget
        self.cell_size = float(cell_size)
        self.stuck_epsilon = float(stuck_epsilon)
        self._lock = threading.RLock()
        self._units: dict[object, dict] = {}
        self._frame = 0
        self._grid: dict[tuple[int, int], list[object]] = {}
        self._stats = {
            "ticks": 0,
            "pairs": 0,
            "separations": 0,
            "unsticks": 0,
            "registrations": 0,
        }

    def register(self, unit: str, *, radius: float = 0.5, priority: int = 0,
                 position=(0.0, 0.0), goal=None, max_speed: float = 1.0) -> dict:
        with self._lock:
            try:
                hash(unit)
            except TypeError as exc:
                raise CrowdError("AV_INVALID_UNIT", {"unit": unit}) from exc

            if unit in self._units:
                raise CrowdError("AV_DUPLICATE_UNIT", {"unit": unit})

            if not _is_real_number(radius) or float(radius) <= 0.0:
                raise CrowdError("AV_INVALID_UNIT", {"radius": radius})
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise CrowdError("AV_INVALID_UNIT", {"priority": priority})
            if not _is_real_number(max_speed) or float(max_speed) < 0.0:
                raise CrowdError("AV_INVALID_UNIT", {"max_speed": max_speed})

            pos = _point(position)
            normalized_goal = _optional_point(goal)
            record = {
                "unit": unit,
                "radius": float(radius),
                "priority": priority,
                "position": pos,
                "goal": normalized_goal,
                "max_speed": float(max_speed),
                "history": [],
                "escape_direction": None,
            }
            self._units[unit] = record
            self._stats["registrations"] += 1
            return self._public_state(record)

    def unregister(self, unit: str) -> dict:
        with self._lock:
            removed = self._units.pop(unit, None) is not None
            return {"unit": unit, "removed": removed}

    def set_goal(self, unit: str, goal) -> dict:
        with self._lock:
            record = self._require_unit(unit)
            record["goal"] = _optional_point(goal)
            record["escape_direction"] = None
            return self._public_state(record)

    def tick(self, *, dt_ms: int = 16, budget=None, units=None) -> dict:
        if not _is_real_number(dt_ms) or float(dt_ms) <= 0.0:
            raise CrowdError("AV_INVALID_DT", {"dt_ms": dt_ms})
        if budget is None:
            budget = self.neighbor_budget
        budget = _non_negative_int(budget, "AV_INVALID_BUDGET")

        with self._lock:
            active_ids = self._active_ids(units)
            before = {unit: self._units[unit]["position"] for unit in active_ids}

            self._frame += 1
            self._stats["ticks"] += 1
            dt_seconds = float(dt_ms) / 1000.0

            for unit in active_ids:
                self._move_toward_goal(self._units[unit], dt_seconds)

            pairs = self._select_pairs(active_ids, budget)
            pairs_evaluated = len(pairs)
            separations = 0
            for left, right in pairs:
                if self._separate_pair(self._units[left], self._units[right]):
                    separations += 1

            stuck_records = []
            for unit in active_ids:
                record = self._units[unit]
                provisional = self._delta(before[unit], record["position"])
                if (record["goal"] is not None
                        and not self._at_goal(record)
                        and self._would_be_stuck(record, provisional)):
                    stuck_records.append(record)

            for record in stuck_records:
                if record["escape_direction"] is None:
                    record["escape_direction"] = \
                        self._coordinated_escape_direction(record)

            for record in stuck_records:
                self._apply_unstick(record, dt_seconds)

            unsticks = len(stuck_records)
            for unit in active_ids:
                record = self._units[unit]
                final_delta = self._delta(before[unit], record["position"])
                record["history"].append(final_delta)
                if self._at_goal(record):
                    record["escape_direction"] = None

            self._stats["pairs"] += pairs_evaluated
            self._stats["separations"] += separations
            self._stats["unsticks"] += unsticks

            moved = {
                unit: list(self._delta(before[unit],
                                      self._units[unit]["position"]))
                for unit in active_ids
            }
            return {
                "frame": self._frame,
                "moved": moved,
                "pairs_evaluated": pairs_evaluated,
            }

    def neighbors(self, unit: str, radius: float) -> list:
        with self._lock:
            self._require_unit(unit)
            if not _is_real_number(radius) or float(radius) < 0.0:
                raise CrowdError("AV_INVALID_RADIUS", {"radius": radius})

            radius_value = float(radius)
            origin = self._units[unit]["position"]
            radius_squared = radius_value * radius_value
            found = []
            for other_id, record in self._units.items():
                if other_id == unit:
                    continue
                distance_squared = self._distance_squared(
                    origin, record["position"]
                )
                if distance_squared <= radius_squared:
                    found.append((math.sqrt(max(0.0, distance_squared)),
                                 other_id))
            found.sort(key=lambda item: (item[0], self._identity_key(item[1])))
            return [other_id for _, other_id in found]

    def stuck(self, unit: str, window: int) -> bool:
        with self._lock:
            record = self._require_unit(unit)
            window = _non_negative_int(window, "AV_INVALID_WINDOW")
            if window <= 0:
                raise CrowdError("AV_INVALID_WINDOW", {"window": window})

            history = record["history"]
            if len(history) < window:
                return False

            net_x = 0.0
            net_y = 0.0
            for dx, dy in history[-window:]:
                net_x += dx
                net_y += dy
            return math.hypot(net_x, net_y) < self.stuck_epsilon

    def position(self, unit: str) -> tuple:
        with self._lock:
            return self._require_unit(unit)["position"]

    def state(self, unit: str) -> dict:
        with self._lock:
            return self._public_state(self._require_unit(unit))

    def snapshot(self) -> dict:
        with self._lock:
            units = []
            for record in self._units.values():
                history = [list(item) for item in record["history"]]
                units.append({
                    "unit": record["unit"],
                    "radius": record["radius"],
                    "priority": record["priority"],
                    "position": list(record["position"]),
                    "goal": (None if record["goal"] is None
                             else list(record["goal"])),
                    "max_speed": record["max_speed"],
                    "history": history,
                    "stuck_window": [list(item) for item in history],
                    "escape_direction": (
                        None if record["escape_direction"] is None
                        else list(record["escape_direction"])
                    ),
                })
            return {
                "frame": self._frame,
                "units": units,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            frame, units = self._parse_snapshot(state)
            self._frame = frame
            self._units = units

    def stats(self) -> dict:
        with self._lock:
            return {
                "ticks": self._stats["ticks"],
                "pairs": self._stats["pairs"],
                "separations": self._stats["separations"],
                "unsticks": self._stats["unsticks"],
                "registrations": self._stats["registrations"],
            }

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0

    def _require_unit(self, unit: object) -> dict:
        try:
            return self._units[unit]
        except (KeyError, TypeError):
            raise CrowdError("AV_UNKNOWN_UNIT", {"unit": unit}) from None

    def _active_ids(self, units) -> list:
        if units is None:
            return list(self._units.keys())
        if isinstance(units, (str, bytes)) or not hasattr(units, "__iter__"):
            raise CrowdError("AV_INVALID_UNITS", {"units": units})

        active = []
        seen = set()
        for unit in units:
            self._require_unit(unit)
            if unit not in seen:
                seen.add(unit)
                active.append(unit)
        return active

    @staticmethod
    def _public_state(record: dict) -> dict:
        goal = record["goal"]
        return {
            "unit": record["unit"],
            "radius": record["radius"],
            "priority": record["priority"],
            "position": record["position"],
            "goal": None if goal is None else goal,
            "max_speed": record["max_speed"],
        }

    @staticmethod
    def _distance_squared(left: tuple[float, float],
                          right: tuple[float, float]) -> float:
        dx = right[0] - left[0]
        dy = right[1] - left[1]
        return dx * dx + dy * dy

    @staticmethod
    def _delta(start: tuple[float, float],
               end: tuple[float, float]) -> tuple[float, float]:
        return end[0] - start[0], end[1] - start[1]

    def _move_toward_goal(self, record: dict, dt_seconds: float) -> None:
        goal = record["goal"]
        if goal is None:
            return

        x, y = record["position"]
        gx, gy = goal
        dx = gx - x
        dy = gy - y
        distance = math.hypot(dx, dy)
        maximum = record["max_speed"] * dt_seconds
        if distance == 0.0:
            record["position"] = (gx, gy)
            return
        if distance <= maximum:
            record["position"] = (gx, gy)
            return

        scale = maximum / distance
        record["position"] = (x + dx * scale, y + dy * scale)

    def _at_goal(self, record: dict) -> bool:
        goal = record["goal"]
        if goal is None:
            return False
        return self._distance_squared(record["position"], goal) <= 1e-14

    def _select_pairs(self, active_ids: list, budget: int
                      ) -> list[tuple[object, object]]:
        if budget == 0 or len(active_ids) < 2:
            return []

        active_set = set(active_ids)
        self._build_grid(active_set)
        max_radius = max(self._units[unit]["radius"] for unit in active_ids)
        candidates = {}

        for unit in active_ids:
            record = self._units[unit]
            query_radius = record["radius"] + max_radius
            for other_id in self._nearby_ids(record["position"], query_radius):
                if other_id == unit or other_id not in active_set:
                    continue

                pair = self._canonical_pair(unit, other_id)
                if pair in candidates:
                    continue

                left, right = pair
                left_record = self._units[left]
                right_record = self._units[right]
                distance_squared = self._distance_squared(
                    left_record["position"], right_record["position"]
                )
                if distance_squared > query_radius * query_radius:
                    continue

                contact_radius = left_record["radius"] + right_record["radius"]
                candidates[pair] = self._pair_rank(
                    left_record, right_record, distance_squared, contact_radius
                )

        candidate_items = list(candidates.items())
        sort_key = self._pair_sort_key
        if budget >= len(candidate_items):
            ranked = sorted(candidate_items, key=sort_key)
        else:
            ranked = heapq.nsmallest(budget, candidate_items, key=sort_key)
            ranked.sort(key=sort_key)
        return [pair for pair, _rank in ranked]

    def _pair_sort_key(self, item: tuple[tuple[object, object], tuple]):
        pair, rank = item
        high, low, overlapping, penetration = rank
        return (
            -high,
            -low,
            -overlapping,
            -penetration,
            self._identity_key(pair[0]),
            self._identity_key(pair[1]),
        )

    def _build_grid(self, active_set: set) -> None:
        grid: dict[tuple[int, int], list[object]] = {}
        for unit in active_set:
            x, y = self._units[unit]["position"]
            key = (math.floor(x / self.cell_size),
                   math.floor(y / self.cell_size))
            grid.setdefault(key, []).append(unit)
        self._grid = grid

    def _nearby_ids(self, position: tuple[float, float], radius: float):
        cell_size = self.cell_size
        min_x = math.floor((position[0] - radius) / cell_size)
        max_x = math.floor((position[0] + radius) / cell_size)
        min_y = math.floor((position[1] - radius) / cell_size)
        max_y = math.floor((position[1] + radius) / cell_size)

        cell_count = (max_x - min_x + 1) * (max_y - min_y + 1)
        if cell_count > max(1, len(self._units) * 4):
            return list(self._units.keys())

        result = []
        for ix in range(min_x, max_x + 1):
            for iy in range(min_y, max_y + 1):
                result.extend(self._grid.get((ix, iy), ()))
        return result

    @classmethod
    def _canonical_pair(cls, left: object, right: object
                        ) -> tuple[object, object]:
        try:
            return (left, right) if left <= right else (right, left)
        except TypeError:
            left_key = cls._identity_key(left)
            right_key = cls._identity_key(right)
            return (left, right) if left_key <= right_key else (right, left)

    @staticmethod
    def _identity_key(unit: object) -> tuple:
        if unit is None:
            return (0, "")
        if isinstance(unit, str):
            return (1, unit)
        if isinstance(unit, bool):
            return (3, int(unit), "")
        if isinstance(unit, (int, float)):
            return (2, float(unit), "")
        return (9, type(unit).__name__, str(unit))

    def _pair_rank(self, left: dict, right: dict, distance_squared: float,
                   contact_radius: float) -> tuple[int, int, int, float]:
        left_priority = left["priority"]
        right_priority = right["priority"]
        high = max(left_priority, right_priority)
        low = min(left_priority, right_priority)
        overlapping = distance_squared <= contact_radius * contact_radius
        distance = math.sqrt(max(0.0, distance_squared))
        penetration = max(0.0, contact_radius - distance)
        return high, low, 1 if overlapping else 0, penetration

    def _separate_pair(self, left: dict, right: dict) -> bool:
        lp = left["position"]
        rp = right["position"]
        dx = rp[0] - lp[0]
        dy = rp[1] - lp[1]
        distance = math.hypot(dx, dy)
        minimum_distance = left["radius"] + right["radius"]
        if distance >= minimum_distance:
            return False

        if distance > 0.0:
            ux = dx / distance
            uy = dy / distance
        else:
            ux, uy = self._deterministic_direction(
                left["unit"], right["unit"], lp
            )

        overlap = minimum_distance - distance
        if left["priority"] == right["priority"]:
            self._move_position(left, -ux * overlap / 2.0,
                                -uy * overlap / 2.0)
            self._move_position(right, ux * overlap / 2.0,
                                uy * overlap / 2.0)
            return True

        if left["priority"] > right["priority"]:
            low = right
            move_x = ux * overlap
            move_y = uy * overlap
        else:
            low = left
            move_x = -ux * overlap
            move_y = -uy * overlap

        self._move_position(low, move_x, move_y)
        return True

    @staticmethod
    def _move_position(record: dict, dx: float, dy: float) -> None:
        x, y = record["position"]
        record["position"] = (x + dx, y + dy)

    def _deterministic_direction(self, first: object, second: object,
                                 position: tuple[float, float]
                                 ) -> tuple[float, float]:
        pair = self._canonical_pair(first, second)
        seed = "|".join((
            "crowd-contact-v1",
            repr(pair[0]),
            repr(pair[1]),
            f"{position[0]:.6f}",
            f"{position[1]:.6f}",
        )).encode("utf-8")
        digest = hashlib.sha256(seed).digest()
        angle = (int.from_bytes(digest[:8], "big") / float(1 << 64)) * math.tau
        return math.cos(angle), math.sin(angle)

    def _would_be_stuck(self, record: dict,
                        provisional_delta: tuple[float, float]) -> bool:
        window = self._STUCK_WINDOW
        history = record["history"]
        if len(history) + 1 < window:
            return False

        net_x, net_y = provisional_delta
        for dx, dy in history[-(window - 1):]:
            net_x += dx
            net_y += dy
        return math.hypot(net_x, net_y) < self.stuck_epsilon

    def _apply_unstick(self, record: dict, dt_seconds: float) -> None:
        step = max(record["max_speed"] * dt_seconds, self.stuck_epsilon)
        direction = record["escape_direction"]
        if direction is None:
            direction = self._fixed_escape_direction(record)
            record["escape_direction"] = direction
        dx, dy = direction
        self._move_position(record, dx * step, dy * step)

    def _coordinated_escape_direction(
            self, record: dict) -> tuple[float, float]:
        partner = self._nearest_blocking_unit(record)
        if partner is None:
            return self._fixed_escape_direction(record)

        pair = self._canonical_pair(record["unit"], partner)
        first_position = self._units[pair[0]]["position"]
        second_position = self._units[pair[1]]["position"]
        dx = second_position[0] - first_position[0]
        dy = second_position[1] - first_position[1]
        distance = math.hypot(dx, dy)
        if distance > 0.0:
            ux, uy = dx / distance, dy / distance
        else:
            ux, uy = self._deterministic_direction(
                pair[0], pair[1], first_position
            )

        perpendicular = (-uy, ux)
        if record["unit"] == pair[0]:
            return perpendicular
        return -perpendicular[0], -perpendicular[1]

    def _nearest_blocking_unit(self, record: dict):
        origin = record["position"]
        candidates = []
        for other_id, other in self._units.items():
            if other_id == record["unit"]:
                continue
            distance = math.sqrt(
                max(0.0, self._distance_squared(origin, other["position"]))
            )
            contact_radius = record["radius"] + other["radius"]
            if distance > contact_radius + 1e-7:
                continue
            candidates.append((distance, self._identity_key(other_id),
                               other_id))
        if not candidates:
            return None
        candidates.sort()
        return candidates[0][2]

    def _fixed_escape_direction(self, record: dict) -> tuple[float, float]:
        goal = record["goal"]
        position = record["position"]
        if goal is not None:
            dx = goal[0] - position[0]
            dy = goal[1] - position[1]
            length = math.hypot(dx, dy)
            if length > 0.0:
                perpendicular = (-dy / length, dx / length)
            else:
                perpendicular = (1.0, 0.0)
        else:
            perpendicular = (1.0, 0.0)

        seed = "|".join((
            "crowd-unstick-v1",
            repr(record["unit"]),
            f"{position[0]:.6f}",
            f"{position[1]:.6f}",
        )).encode("utf-8")
        sign = 1.0 if hashlib.sha256(seed).digest()[0] & 1 else -1.0
        return perpendicular[0] * sign, perpendicular[1] * sign

    def _parse_snapshot(self, state: object) -> tuple[int, dict]:
        if not isinstance(state, dict):
            raise CrowdError("AV_SNAPSHOT_INVALID",
                             {"type": type(state).__name__})
        if "frame" not in state or "units" not in state:
            raise CrowdError("AV_SNAPSHOT_INVALID",
                             {"keys": sorted(map(str, state.keys()))})

        frame = state["frame"]
        if isinstance(frame, bool) or not isinstance(frame, int) or frame < 0:
            raise CrowdError("AV_SNAPSHOT_INVALID", {"frame": frame})

        raw_units = state["units"]
        if isinstance(raw_units, dict):
            entries = []
            for key, raw in raw_units.items():
                if isinstance(raw, dict) and "unit" not in raw:
                    raw = dict(raw)
                    raw["unit"] = key
                entries.append(raw)
        elif isinstance(raw_units, list):
            entries = raw_units
        else:
            raise CrowdError("AV_SNAPSHOT_INVALID", {"units": raw_units})

        units = {}
        for raw in entries:
            parsed = self._parse_snapshot_unit(raw)
            unit = parsed["unit"]
            if unit in units:
                raise CrowdError("AV_SNAPSHOT_INVALID", {"unit": unit})
            units[unit] = parsed
        return frame, units

    def _parse_snapshot_unit(self, raw: object) -> dict:
        if not isinstance(raw, dict):
            raise CrowdError("AV_SNAPSHOT_INVALID", {"unit": raw})

        unit = raw.get("unit")
        if unit is None:
            raise CrowdError("AV_SNAPSHOT_INVALID", {"unit": raw})
        try:
            hash(unit)
        except TypeError as exc:
            raise CrowdError("AV_SNAPSHOT_INVALID", {"unit": unit}) from exc

        radius = raw.get("radius", 0.5)
        if not _is_real_number(radius) or float(radius) <= 0.0:
            raise CrowdError("AV_SNAPSHOT_INVALID", {"radius": radius})

        priority = raw.get("priority", 0)
        if isinstance(priority, bool) or not isinstance(priority, int):
            raise CrowdError("AV_SNAPSHOT_INVALID", {"priority": priority})

        position = _point(raw.get("position", raw.get("pos")),
                         "AV_SNAPSHOT_INVALID")
        goal_value = raw.get("goal")
        goal = (None if goal_value is None
                else _point(goal_value, "AV_SNAPSHOT_INVALID"))

        max_speed = raw.get("max_speed", 1.0)
        if not _is_real_number(max_speed) or float(max_speed) < 0.0:
            raise CrowdError("AV_SNAPSHOT_INVALID",
                             {"max_speed": max_speed})

        history_value = raw.get(
            "history",
            raw.get("stuck_window", raw.get("stuck_windows", ())),
        )
        history = self._parse_history(history_value)

        escape_direction = raw.get("escape_direction")
        if escape_direction is not None:
            escape_direction = _point(escape_direction,
                                      "AV_SNAPSHOT_INVALID")

        return {
            "unit": unit,
            "radius": float(radius),
            "priority": priority,
            "position": position,
            "goal": goal,
            "max_speed": float(max_speed),
            "history": history,
            "escape_direction": escape_direction,
        }

    @staticmethod
    def _parse_history(value: object) -> list[tuple[float, float]]:
        if isinstance(value, (str, bytes)) or not isinstance(
                value, (list, tuple)):
            raise CrowdError("AV_SNAPSHOT_INVALID", {"history": value})

        history = []
        for item in value:
            if isinstance(item, dict):
                if "displacement" in item:
                    history.append(_point(item["displacement"],
                                          "AV_SNAPSHOT_INVALID"))
                else:
                    history.append(_point(
                        (item.get("dx", 0.0), item.get("dy", 0.0)),
                        "AV_SNAPSHOT_INVALID",
                    ))
            else:
                history.append(_point(item, "AV_SNAPSHOT_INVALID"))
        return history
