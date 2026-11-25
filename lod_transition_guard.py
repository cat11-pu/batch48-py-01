"""LOD selection with hysteresis, cross-fade and shadow offset."""

from __future__ import annotations

import copy
import math
from numbers import Real


STAT_KEYS = (
    "selections",
    "transitions",
    "instant_switches",
    "completed",
    "cancelled",
    "forced",
)

MAX_LEVELS = 64
MAX_BUDGET = 100_000


class LodError(ValueError):
    """Raised when an LOD decision or transition state is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LodTransitionGuard:
    """Pick LOD levels, cross-fade between them and track transitions."""

    def __init__(self, *, levels: int = 4, thresholds=(0.5, 0.2, 0.05),
                 hysteresis: float = 0.1, transition_frames: int = 4,
                 transition_budget: int = 2, shadow_offset: int = 1) -> None:
        if not self._is_int(levels) or not 1 <= levels <= MAX_LEVELS:
            raise LodError("LOD_CONFIG_INVALID", {"levels": levels})

        try:
            threshold_values = tuple(thresholds)
        except TypeError:
            raise LodError("LOD_CONFIG_INVALID",
                           {"thresholds": thresholds}) from None

        if len(threshold_values) != levels - 1:
            raise LodError("LOD_CONFIG_INVALID", {"thresholds": thresholds})
        if not all(
            self._is_number(value) and 0.0 < value <= 1.0
            for value in threshold_values
        ) or any(
            left <= right
            for left, right in zip(threshold_values, threshold_values[1:])
        ):
            raise LodError("LOD_CONFIG_INVALID", {"thresholds": thresholds})

        if (
            not self._is_number(hysteresis)
            or not 0.0 <= float(hysteresis) <= 1.0
        ):
            raise LodError("LOD_CONFIG_INVALID", {"hysteresis": hysteresis})
        if not self._is_int(transition_frames) or transition_frames < 1:
            raise LodError("LOD_CONFIG_INVALID",
                           {"transition_frames": transition_frames})
        if (
            not self._is_int(transition_budget)
            or not 0 <= transition_budget <= MAX_BUDGET
        ):
            raise LodError("LOD_CONFIG_INVALID",
                           {"transition_budget": transition_budget})
        if not self._is_int(shadow_offset) or shadow_offset < 0:
            raise LodError("LOD_CONFIG_INVALID",
                           {"shadow_offset": shadow_offset})

        self.levels = levels
        self.thresholds = tuple(float(value) for value in threshold_values)
        self.hysteresis = float(hysteresis)
        self.transition_frames = transition_frames
        self.transition_budget = transition_budget
        self.shadow_offset = shadow_offset

        self._entities: dict[object, dict] = {}
        self._budget = transition_budget
        self._stats = {key: 0 for key in STAT_KEYS}

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _is_number(value: object) -> bool:
        if not isinstance(value, Real) or isinstance(value, bool):
            return False
        try:
            return math.isfinite(float(value))
        except (OverflowError, TypeError, ValueError):
            return False

    def _valid_ratio(self, screen_ratio: object) -> float:
        if (
            not self._is_number(screen_ratio)
            or float(screen_ratio) < 0.0
            or float(screen_ratio) > 1.0
        ):
            raise LodError("LOD_RATIO_INVALID", screen_ratio)
        return float(screen_ratio)

    def _entity(self, entity: object) -> dict:
        try:
            return self._entities[entity]
        except (KeyError, TypeError):
            raise LodError("LOD_UNKNOWN_ENTITY", entity) from None

    def _raw_level(self, ratio: float) -> int:
        for index, threshold in enumerate(self.thresholds):
            if ratio >= threshold:
                return index
        return self.levels - 1

    def _next_level(self, current: int, ratio: float) -> int:
        target = self._raw_level(ratio)
        if target == current:
            return current
        if target > current:
            if (
                current < len(self.thresholds)
                and ratio < self.thresholds[current] - self.hysteresis
            ):
                return target
            return current
        if ratio >= self.thresholds[target] + self.hysteresis:
            return target
        return current

    def _cancel_transition(self, state: dict) -> None:
        transition = state["transition"]
        state["level"] = transition["from"]
        state["transition"] = None
        self._stats["cancelled"] += 1

    def _switch(self, state: dict, target: int, *, update_auto: bool) -> None:
        current = state["level"]
        if target == current:
            return

        transition = state["transition"]
        if transition is not None:
            if target == transition["from"]:
                if update_auto:
                    state["auto_level"] = target
                self._cancel_transition(state)
                return
            if transition["elapsed"] == 0:
                transition["to"] = target
                state["level"] = target
                if update_auto:
                    state["auto_level"] = target
                return

        if self._budget > 0:
            self._budget -= 1
            state["transition"] = {
                "from": current,
                "to": target,
                "elapsed": 0,
            }
            state["level"] = target
            self._stats["transitions"] += 1
        else:
            state["transition"] = None
            state["level"] = target
            self._stats["instant_switches"] += 1

        if update_auto:
            state["auto_level"] = target

    def _alpha(self, transition: dict) -> float:
        return transition["elapsed"] / self.transition_frames

    def _transition_info(self, state: dict) -> dict:
        transition = state["transition"]
        if transition is None:
            return {
                "phase": "stable",
                "level": state["level"],
                "from": None,
                "to": None,
                "alpha": 1.0,
                "forced": state["forced"],
            }
        return {
            "phase": "transition",
            "level": transition["to"],
            "from": transition["from"],
            "to": transition["to"],
            "alpha": self._alpha(transition),
            "forced": state["forced"],
        }

    def select_lod(self, entity: str, screen_ratio: float) -> int:
        ratio = self._valid_ratio(screen_ratio)
        try:
            state = self._entities[entity]
        except (KeyError, TypeError):
            level = self._raw_level(ratio)
            state = {
                "level": level,
                "auto_level": level,
                "transition": None,
                "forced": False,
                "forced_level": None,
                "last_ratio": ratio,
            }
            self._entities[entity] = state
            self._stats["selections"] += 1
            return level

        state["last_ratio"] = ratio
        if not state["forced"]:
            transition = state["transition"]
            base = transition["from"] if transition is not None else state["level"]
            target = self._next_level(base, ratio)
            self._switch(state, target, update_auto=True)

        self._stats["selections"] += 1
        return state["level"]

    def advance(self, entity: str, frames: int = 1) -> dict:
        if not self._is_int(frames) or frames < 1:
            raise LodError("LOD_FRAMES_INVALID", frames)
        state = self._entity(entity)
        transition = state["transition"]
        if transition is not None:
            transition["elapsed"] += frames
            if transition["elapsed"] >= self.transition_frames:
                state["level"] = transition["to"]
                state["transition"] = None
                self._stats["completed"] += 1
        return self._transition_info(state)

    def draw_state(self, entity: str) -> list[dict]:
        state = self._entity(entity)
        transition = state["transition"]
        if transition is None:
            level = state["level"]
            return [
                {"pass": "main", "level": level, "alpha": 1.0},
                {
                    "pass": "shadow",
                    "level": min(level + self.shadow_offset, self.levels - 1),
                    "alpha": 1.0,
                },
            ]

        alpha = self._alpha(transition)
        old_alpha = 1.0 - alpha
        old_level = transition["from"]
        new_level = transition["to"]
        return [
            {"pass": "main", "level": old_level, "alpha": old_alpha},
            {"pass": "main", "level": new_level, "alpha": alpha},
            {
                "pass": "shadow",
                "level": min(old_level + self.shadow_offset, self.levels - 1),
                "alpha": old_alpha,
            },
            {
                "pass": "shadow",
                "level": min(new_level + self.shadow_offset, self.levels - 1),
                "alpha": alpha,
            },
        ]

    def transition_of(self, entity: str) -> dict:
        return self._transition_info(self._entity(entity))

    def force_lod(self, entity: str, level: int) -> int:
        state = self._entity(entity)
        if (
            not self._is_int(level)
            or level < 0
            or level >= self.levels
        ):
            raise LodError("LOD_LEVEL_INVALID", level)

        state["forced"] = True
        state["forced_level"] = level
        self._stats["forced"] += 1
        self._switch(state, level, update_auto=False)
        return state["level"]

    def clear_force(self, entity: str) -> None:
        state = self._entity(entity)
        if not state["forced"]:
            raise LodError("LOD_NOT_FORCED", entity)

        state["forced"] = False
        state["forced_level"] = None
        ratio = state["last_ratio"]
        if ratio is None:
            target = state["auto_level"]
        else:
            target = self._next_level(state["auto_level"], ratio)
        state["auto_level"] = target
        self._switch(state, target, update_auto=False)

    def budget_left(self) -> int:
        return self._budget

    def reset_budget(self, amount: int | None = None) -> int:
        if amount is None:
            amount = self.transition_budget
        if (
            not self._is_int(amount)
            or amount < 0
            or amount > MAX_BUDGET
        ):
            raise LodError("LOD_BUDGET_INVALID", amount)
        self._budget = amount
        return self._budget

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "entities": copy.deepcopy(self._entities),
            "budget": self._budget,
            "stats": dict(self._stats),
        }

    def restore(self, state: dict) -> None:
        try:
            if not isinstance(state, dict):
                raise LodError("LOD_SNAPSHOT_INVALID", state)
            version = state.get("version")
            if not self._is_int(version) or version != 1:
                raise LodError("LOD_SNAPSHOT_INVALID", "version")

            raw_entities = state["entities"]
            raw_budget = state["budget"]
            raw_stats = state["stats"]
            if not isinstance(raw_entities, dict):
                raise LodError("LOD_SNAPSHOT_INVALID", "entities")
            if (
                not self._is_int(raw_budget)
                or not 0 <= raw_budget <= MAX_BUDGET
            ):
                raise LodError("LOD_SNAPSHOT_INVALID", "budget")
            if (
                not isinstance(raw_stats, dict)
                or set(raw_stats) != set(STAT_KEYS)
                or any(
                    not self._is_int(value) or value < 0
                    for value in raw_stats.values()
                )
            ):
                raise LodError("LOD_SNAPSHOT_INVALID", "stats")

            entities: dict[object, dict] = {}
            for name, raw in raw_entities.items():
                if not isinstance(raw, dict):
                    raise LodError("LOD_SNAPSHOT_INVALID", name)
                try:
                    level = raw["level"]
                    auto_level = raw["auto_level"]
                    transition = raw["transition"]
                    forced = raw["forced"]
                    forced_level = raw["forced_level"]
                    last_ratio = raw["last_ratio"]
                except (KeyError, TypeError):
                    raise LodError("LOD_SNAPSHOT_INVALID", name) from None

                if (
                    not self._is_int(level)
                    or not 0 <= level < self.levels
                    or not self._is_int(auto_level)
                    or not 0 <= auto_level < self.levels
                    or not isinstance(forced, bool)
                ):
                    raise LodError("LOD_SNAPSHOT_INVALID", name)

                if forced:
                    if (
                        not self._is_int(forced_level)
                        or not 0 <= forced_level < self.levels
                        or forced_level != level
                    ):
                        raise LodError("LOD_SNAPSHOT_INVALID", name)
                elif forced_level is not None:
                    raise LodError("LOD_SNAPSHOT_INVALID", name)

                if last_ratio is not None:
                    if (
                        not self._is_number(last_ratio)
                        or not 0.0 <= float(last_ratio) <= 1.0
                    ):
                        raise LodError("LOD_SNAPSHOT_INVALID", name)
                    last_ratio = float(last_ratio)

                if transition is not None:
                    if not isinstance(transition, dict):
                        raise LodError("LOD_SNAPSHOT_INVALID", name)
                    try:
                        source = transition["from"]
                        target = transition["to"]
                        elapsed = transition["elapsed"]
                    except (KeyError, TypeError):
                        raise LodError(
                            "LOD_SNAPSHOT_INVALID", name
                        ) from None
                    if (
                        not self._is_int(source)
                        or not 0 <= source < self.levels
                        or not self._is_int(target)
                        or not 0 <= target < self.levels
                        or source == target
                        or target != level
                        or not self._is_int(elapsed)
                        or not 0 <= elapsed < self.transition_frames
                    ):
                        raise LodError("LOD_SNAPSHOT_INVALID", name)
                    transition = {
                        "from": source,
                        "to": target,
                        "elapsed": elapsed,
                    }

                entities[name] = {
                    "level": level,
                    "auto_level": auto_level,
                    "transition": transition,
                    "forced": forced,
                    "forced_level": forced_level,
                    "last_ratio": last_ratio,
                }

            new_stats = {key: int(raw_stats[key]) for key in STAT_KEYS}
        except LodError:
            raise
        except Exception as exc:
            raise LodError("LOD_SNAPSHOT_INVALID", str(exc)) from None

        self._entities = entities
        self._budget = raw_budget
        self._stats = new_stats

    def stats(self) -> dict:
        return {key: self._stats[key] for key in STAT_KEYS}

    def reset_stats(self) -> None:
        self._stats = {key: 0 for key in STAT_KEYS}
