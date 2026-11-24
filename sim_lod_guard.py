"""Server simulation LOD guard with distance tiers and per-tick budget."""

from __future__ import annotations

import copy
import threading

TIERS = ("each_tick", "every_2", "every_8", "sleep")
_TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}
_STAT_KEYS = ("ticks", "updates", "deferred", "tier_changes")

# A distance belongs to a tier while it stays strictly below the tier's
# upper bound; anything at or beyond the last bound stays asleep.
_UPPER_BOUNDS = (100.0, 300.0, 800.0)
_ENTRY_KEYS = ("tier", "distance", "region", "last_updated")


class SimError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SimLodGuard:
    """Assigns simulation tiers by distance and arbitrates the per-tick budget."""

    def __init__(self, *, budget: int = 8, hysteresis: float = 20.0) -> None:
        self._budget = budget
        self._hysteresis = hysteresis
        self._lock = threading.RLock()
        self._entities: dict[str, dict] = {}
        # Frame in which each entity last switched tier; used to collapse a
        # downgrade followed by an event-driven wake inside one frame.
        self._change_frames: dict[str, int] = {}
        self._frame = 0
        self._stats = {key: 0 for key in _STAT_KEYS}

    # ------------------------------------------------------------------
    # Registration and per-entity configuration

    def register_entity(self, entity_id: str, *, distance: float,
                        region: str = "A") -> dict:
        with self._lock:
            if entity_id in self._entities:
                raise SimError(
                    "SIM_DUPLICATE_ENTITY",
                    {"entity": entity_id},
                )
            tier = self._tier_for_distance(distance)
            self._entities[entity_id] = {
                "tier": tier,
                "distance": distance,
                "region": region,
                "last_updated": None,
            }
            return {"entity": entity_id, "tier": tier, "region": region}

    def update_distance(self, entity_id: str, distance: float) -> dict:
        with self._lock:
            entry = self._require_entity(entity_id)
            entry["distance"] = distance

            current_rank = _TIER_RANK[entry["tier"]]
            raw_rank = _TIER_RANK[self._tier_for_distance(distance)]
            target_rank = current_rank

            if raw_rank > current_rank:
                # Coarsening only after the distance clears the current
                # upper bound plus the hysteresis margin; jumps may span
                # several tiers at once.
                while (target_rank < len(TIERS) - 1
                       and distance > self._upper_bound(target_rank)
                       + self._hysteresis):
                    target_rank += 1
            elif raw_rank < current_rank:
                # Refining only after the distance drops below the current
                # lower bound minus the hysteresis margin.
                while (target_rank > 0
                       and distance < self._lower_bound(target_rank)
                       - self._hysteresis):
                    target_rank -= 1

            target_tier = TIERS[target_rank]
            changed = target_tier != entry["tier"]
            if changed:
                entry["tier"] = target_tier
                self._count_tier_change(entity_id)
            return {
                "entity": entity_id,
                "tier": entry["tier"],
                "changed": changed,
            }

    def set_tier(self, entity_id: str, tier: str) -> dict:
        with self._lock:
            entry = self._require_entity(entity_id)
            if tier not in _TIER_RANK:
                raise SimError(
                    "SIM_TIER_INVALID",
                    {"entity": entity_id, "tier": tier},
                )
            if tier != entry["tier"]:
                entry["tier"] = tier
                self._count_tier_change(entity_id)
            return {"entity": entity_id, "tier": tier}

    def move_region(self, entity_id: str, region: str) -> dict:
        with self._lock:
            entry = self._require_entity(entity_id)
            entry["region"] = region
            # Region transfers re-tier from the stored raw distance without
            # applying any hysteresis margin.
            tier = self._tier_for_distance(entry["distance"])
            if tier != entry["tier"]:
                entry["tier"] = tier
                self._count_tier_change(entity_id)
            return {
                "entity": entity_id,
                "region": region,
                "tier": entry["tier"],
            }

    def wake(self, entity_id: str, *, urgent: bool = True) -> dict:
        with self._lock:
            entry = self._require_entity(entity_id)
            if urgent and entry["tier"] == "sleep":
                # Emergency events bypass hysteresis and promote a sleeping
                # entity one notch so it becomes schedulable again.
                entry["tier"] = TIERS[_TIER_RANK["sleep"] - 1]
                if self._change_frames.get(entity_id) != self._frame:
                    # A downgrade followed by this event-driven upgrade in the
                    # same frame counts as a single tier change.
                    self._count_tier_change(entity_id)
                else:
                    self._change_frames[entity_id] = self._frame
            return {"entity": entity_id, "tier": entry["tier"]}

    # ------------------------------------------------------------------
    # Frame scheduling

    def tick(self) -> dict:
        with self._lock:
            self._frame += 1
            frame = self._frame
            self._stats["ticks"] += 1

            eligible = [
                (entity_id, entry)
                for entity_id, entry in self._entities.items()
                if self._is_due(entry["tier"], frame)
            ]
            eligible.sort(key=lambda item: self._schedule_key(item[0], item[1]))

            updated_ids = [entity_id for entity_id, _ in
                           eligible[:self._budget]]
            deferred_ids = [entity_id for entity_id, _ in
                            eligible[self._budget:]]

            for entity_id in updated_ids:
                self._entities[entity_id]["last_updated"] = frame
            self._stats["updates"] += len(updated_ids)
            self._stats["deferred"] += len(deferred_ids)
            return {
                "tick": frame,
                "updated": updated_ids,
                "deferred": deferred_ids,
            }

    # ------------------------------------------------------------------
    # Queries

    def tier_of(self, entity_id: str) -> str:
        with self._lock:
            return self._require_entity(entity_id)["tier"]

    def last_updated(self, entity_id: str) -> int | None:
        with self._lock:
            return self._require_entity(entity_id)["last_updated"]

    def entities(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._entities)

    def budget(self) -> int:
        with self._lock:
            return self._budget

    def stats(self) -> dict:
        with self._lock:
            return {key: self._stats[key] for key in _STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}

    # ------------------------------------------------------------------
    # Snapshot / restore

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": 1,
                "entities": self._entities,
                "frame": self._frame,
                "budget": self._budget,
                "hysteresis": self._hysteresis,
                "stats": self._stats,
            })

    def restore(self, state) -> None:
        with self._lock:
            entities, frame, budget, hysteresis, stats = \
                self._validate_snapshot(state)
            # Commit only after the whole snapshot validates, so a malformed
            # payload never leaves the guard in a partially updated state.
            self._entities = entities
            self._frame = frame
            self._budget = budget
            self._hysteresis = hysteresis
            self._stats = stats
            self._change_frames = {}

    # ------------------------------------------------------------------
    # Internal helpers (all invoked with the guard lock held)

    def _require_entity(self, entity_id: str) -> dict:
        entry = self._entities.get(entity_id)
        if entry is None:
            raise SimError(
                "SIM_UNKNOWN_ENTITY",
                {"entity": entity_id},
            )
        return entry

    @staticmethod
    def _tier_for_distance(distance: float) -> str:
        for index, upper_bound in enumerate(_UPPER_BOUNDS):
            if distance < upper_bound:
                return TIERS[index]
        return "sleep"

    @staticmethod
    def _upper_bound(rank: int) -> float:
        if rank >= len(_UPPER_BOUNDS):
            return float("inf")
        return _UPPER_BOUNDS[rank]

    @staticmethod
    def _lower_bound(rank: int) -> float:
        if rank <= 0:
            return float("-inf")
        return _UPPER_BOUNDS[rank - 1]

    @staticmethod
    def _is_due(tier: str, frame: int) -> bool:
        if tier == "each_tick":
            return True
        if tier == "every_2":
            return frame % 2 == 0
        if tier == "every_8":
            return frame % 8 == 0
        return False

    @staticmethod
    def _schedule_key(entity_id: str, entry: dict):
        last_updated = entry["last_updated"]
        # Entities that never ran are treated as the oldest possible update so
        # starvation resolves itself as soon as budget frees up.
        stale_rank = last_updated if last_updated is not None else -1
        return (_TIER_RANK[entry["tier"]], stale_rank, entity_id)

    def _count_tier_change(self, entity_id: str) -> None:
        self._stats["tier_changes"] += 1
        self._change_frames[entity_id] = self._frame

    def _validate_snapshot(self, state):
        invalid = SimError("SIM_SNAPSHOT_INVALID", None)
        if not isinstance(state, dict):
            raise invalid
        expected = {"version", "entities", "frame", "budget",
                    "hysteresis", "stats"}
        if set(state) != expected or state.get("version") != 1:
            raise invalid

        frame = state["frame"]
        if not self._is_int(frame) or frame < 0:
            raise invalid
        budget = state["budget"]
        if not self._is_int(budget) or budget < 1:
            raise invalid
        hysteresis = state["hysteresis"]
        if (isinstance(hysteresis, bool)
                or not isinstance(hysteresis, (int, float))
                or hysteresis < 0):
            raise invalid
        hysteresis = float(hysteresis)

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(_STAT_KEYS):
            raise invalid
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not self._is_int(value) or value < 0:
                raise invalid
            stats[key] = value

        raw_entities = state["entities"]
        if not isinstance(raw_entities, dict):
            raise invalid
        entities: dict[str, dict] = {}
        for entity_id, entry in raw_entities.items():
            if not isinstance(entity_id, str) or not isinstance(entry, dict):
                raise invalid
            if set(entry) != set(_ENTRY_KEYS):
                raise invalid
            tier = entry["tier"]
            if tier not in _TIER_RANK:
                raise invalid
            region = entry["region"]
            if not isinstance(region, str):
                raise invalid
            distance = entry["distance"]
            if isinstance(distance, bool) or not isinstance(
                    distance, (int, float)):
                raise invalid
            last_updated = entry["last_updated"]
            if last_updated is not None:
                if (not self._is_int(last_updated)
                        or last_updated < 1 or last_updated > frame):
                    raise invalid
            entities[entity_id] = {
                "tier": tier,
                "distance": distance,
                "region": region,
                "last_updated": last_updated,
            }

        return entities, frame, budget, hysteresis, stats

    @staticmethod
    def _is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)
