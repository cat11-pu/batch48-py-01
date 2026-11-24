"""Per-connection incremental snapshot encoder with dirty masks and budget."""

from __future__ import annotations

import copy
import threading


STATS_KEYS = ("encoded", "keyframes", "fields", "events", "deferred", "acks")


class DeltaError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SnapshotDeltaEncoder:
    """Encodes per-connection deltas against the last acknowledged baseline."""

    def __init__(self, *, keyframe_interval: int = 8, keyframe_age: int = 32,
                 field_cost: int = 1, event_cost: int = 1) -> None:
        self.keyframe_interval = int(keyframe_interval)
        self.keyframe_age = int(keyframe_age)
        self.field_cost = int(field_cost)
        self.event_cost = int(event_cost)
        self._lock = threading.RLock()
        self._entities: dict[str, dict] = {}
        self._baselines: dict[str, dict] = {}
        self._dirty: dict[str, set[str]] = {}
        self._events: list[dict] = []
        self._stats = {key: 0 for key in STATS_KEYS}
        # A slice is retained by tick so ack() can apply exactly what was sent.
        self._slices: dict[int, dict] = {}
        self._last_acked_tick: int | None = None
        # Re-registering a removed entity must not let an old, unacknowledged
        # slice advance the new entity's baseline.
        self._epochs: dict[str, int] = {}
        self._next_epoch = 1

    @staticmethod
    def _unknown(entity_id: str) -> DeltaError:
        return DeltaError("DELTA_UNKNOWN_ENTITY", {"entity": entity_id})

    def _require_entity(self, entity_id: str) -> dict:
        try:
            return self._entities[entity_id]
        except KeyError:
            raise self._unknown(entity_id) from None

    def _field_names(self, entity_id: str) -> list[str]:
        return sorted(self._entities[entity_id]["fields"].keys())

    def _priority(self, entity_id: str) -> int:
        record = self._entities[entity_id]
        score = 50 if record["local"] else 0
        distance = record["distance"]
        if distance is not None:
            if distance < 100:
                score += 30
            elif distance < 400:
                score += 15
        if entity_id not in self._baselines:
            score += 20
        return score

    def _ordered_entities(self) -> list[str]:
        return sorted(
            self._entities,
            key=lambda entity_id: (-self._priority(entity_id), entity_id),
        )

    def _pending_fields(self, entity_id: str) -> list[str]:
        fields = self._entities[entity_id]["fields"]
        baseline = self._baselines.get(entity_id)
        if baseline is None:
            return sorted(fields.keys())
        dirty = self._dirty.get(entity_id, set())
        pending = []
        for name in sorted(dirty):
            if name not in fields:
                continue
            if name not in baseline or fields[name] != baseline[name]:
                pending.append(name)
        return pending

    def register_entity(self, entity_id: str, fields: dict, *,
                        local: bool = False) -> dict:
        with self._lock:
            if entity_id in self._entities:
                raise DeltaError(
                    "DELTA_DUPLICATE_ENTITY", {"entity": entity_id}
                )
            stored_fields = copy.deepcopy(dict(fields))
            self._entities[entity_id] = {
                "fields": stored_fields,
                "local": bool(local),
                "distance": None,
            }
            self._dirty[entity_id] = set(stored_fields)
            self._epochs[entity_id] = self._next_epoch
            self._next_epoch += 1
            self._events.append({"type": "create", "entity": entity_id})
            return {"entity": entity_id, "fields": copy.deepcopy(stored_fields)}

    def remove_entity(self, entity_id: str) -> dict:
        with self._lock:
            self._require_entity(entity_id)
            del self._entities[entity_id]
            self._baselines.pop(entity_id, None)
            self._dirty.pop(entity_id, None)
            self._epochs.pop(entity_id, None)
            self._events.append({"type": "remove", "entity": entity_id})
            return {"entity": entity_id}

    def update_field(self, entity_id: str, name: str, value: object) -> dict:
        with self._lock:
            record = self._require_entity(entity_id)
            if name not in record["fields"]:
                raise self._unknown(entity_id)
            record["fields"][name] = copy.deepcopy(value)
            self._dirty.setdefault(entity_id, set()).add(name)
            return {
                "entity": entity_id,
                "field": name,
                "value": copy.deepcopy(value),
            }

    def update_fields(self, entity_id: str, values: dict) -> dict:
        with self._lock:
            self._require_entity(entity_id)
            for name in sorted(values):
                self.update_field(entity_id, name, values[name])
            return {
                "entity": entity_id,
                "fields": copy.deepcopy(dict(values)),
            }

    def set_local_player(self, entity_id: str, *, local: bool = True) -> dict:
        with self._lock:
            record = self._require_entity(entity_id)
            record["local"] = bool(local)
            return {"entity": entity_id, "local": record["local"]}

    def set_distance(self, entity_id: str, distance: float) -> dict:
        with self._lock:
            record = self._require_entity(entity_id)
            record["distance"] = distance
            return {"entity": entity_id, "distance": distance}

    def encode(self, *, tick: int, budget: int) -> dict:
        with self._lock:
            keyframe = self._last_acked_tick is None
            if not keyframe:
                if (self.keyframe_interval > 0
                        and tick % self.keyframe_interval == 0):
                    keyframe = True
                elif (self._last_acked_tick is not None
                      and tick - self._last_acked_tick >= self.keyframe_age):
                    keyframe = True

            events = copy.deepcopy(self._events)
            self._events.clear()
            event_cost_total = len(events) * self.event_cost
            remaining = int(budget) - event_cost_total

            encoded_entities: list[dict] = []
            slice_entities: list[dict] = []
            deferred = 0
            field_count = 0

            for entity_id in self._ordered_entities():
                names = self._field_names(entity_id)
                if not keyframe:
                    names = self._pending_fields(entity_id)
                    if not names:
                        continue
                    cost = len(names) * self.field_cost
                    if cost > remaining:
                        deferred += 1
                        continue
                    remaining -= cost

                values = {
                    name: copy.deepcopy(
                        self._entities[entity_id]["fields"][name]
                    )
                    for name in names
                }
                encoded_entities.append(
                    {"entity": entity_id, "fields": values}
                )
                slice_entities.append({
                    "entity": entity_id,
                    "fields": copy.deepcopy(values),
                    "epoch": self._epochs.get(entity_id),
                })
                field_count += len(names)

            self._slices[tick] = {"entities": slice_entities}
            self._stats["encoded"] += 1
            if keyframe:
                self._stats["keyframes"] += 1
            self._stats["fields"] += field_count
            self._stats["events"] += len(events)
            self._stats["deferred"] += deferred

            return {
                "tick": tick,
                "keyframe": keyframe,
                "entities": encoded_entities,
                "events": copy.deepcopy(events),
                "used": event_cost_total + field_count * self.field_cost,
                "deferred": deferred,
            }

    def ack(self, *, tick: int) -> dict:
        with self._lock:
            if tick not in self._slices:
                raise DeltaError("DELTA_UNKNOWN_TICK", {"tick": tick})

            applied: list[str] = []
            for item in self._slices[tick]["entities"]:
                entity_id = item["entity"]
                record = self._entities.get(entity_id)
                if record is None:
                    continue
                if item.get("epoch") != self._epochs.get(entity_id):
                    continue

                fields = item["fields"]
                baseline = self._baselines.setdefault(entity_id, {})
                dirty = self._dirty.get(entity_id)
                for name, value in fields.items():
                    baseline[name] = copy.deepcopy(value)
                    if (dirty is not None and name in dirty
                            and name in record["fields"]
                            and record["fields"][name] == value):
                        dirty.discard(name)
                if dirty is not None and not dirty:
                    self._dirty.pop(entity_id, None)
                applied.append(entity_id)

            if self._last_acked_tick is None:
                self._last_acked_tick = tick
            else:
                self._last_acked_tick = max(self._last_acked_tick, tick)
            self._stats["acks"] += 1
            return {"tick": tick, "applied": applied}

    def baseline_of(self, entity_id: str) -> dict | None:
        with self._lock:
            baseline = self._baselines.get(entity_id)
            if baseline is None:
                return None
            return copy.deepcopy(baseline)

    def dirty_of(self, entity_id: str) -> list:
        with self._lock:
            record = self._entities.get(entity_id)
            if record is None:
                return []
            if entity_id not in self._baselines:
                return sorted(record["fields"].keys())
            dirty = self._dirty.get(entity_id, set())
            return sorted(name for name in dirty if name in record["fields"])

    def entities(self) -> dict:
        with self._lock:
            result = {}
            for entity_id in sorted(self._entities):
                record = self._entities[entity_id]
                result[entity_id] = {
                    "fields": copy.deepcopy(record["fields"]),
                    "local": record["local"],
                    "distance": record["distance"],
                }
            return result

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in STATS_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STATS_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "entities": copy.deepcopy(self._entities),
                "baselines": copy.deepcopy(self._baselines),
                "dirty": {
                    entity_id: sorted(names)
                    for entity_id, names in self._dirty.items()
                },
                "events": copy.deepcopy(self._events),
                "stats": {key: int(self._stats[key]) for key in STATS_KEYS},
                "slices": copy.deepcopy(self._slices),
                "last_acked_tick": self._last_acked_tick,
                "epochs": dict(self._epochs),
                "next_epoch": self._next_epoch,
                "config": {
                    "keyframe_interval": self.keyframe_interval,
                    "keyframe_age": self.keyframe_age,
                    "field_cost": self.field_cost,
                    "event_cost": self.event_cost,
                },
            }

    @staticmethod
    def _invalid(detail: object) -> DeltaError:
        return DeltaError("DELTA_SNAPSHOT_INVALID", detail)

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = self._parse_snapshot(state)
            except DeltaError:
                raise
            except Exception as exc:
                raise self._invalid(str(exc)) from None

            self._entities = parsed["entities"]
            self._baselines = parsed["baselines"]
            self._dirty = parsed["dirty"]
            self._events = parsed["events"]
            self._stats = parsed["stats"]
            self._slices = parsed["slices"]
            self._last_acked_tick = parsed["last_acked_tick"]
            self._epochs = parsed["epochs"]
            self._next_epoch = parsed["next_epoch"]
            config = parsed["config"]
            if config is not None:
                self.keyframe_interval = config["keyframe_interval"]
                self.keyframe_age = config["keyframe_age"]
                self.field_cost = config["field_cost"]
                self.event_cost = config["event_cost"]

    def _parse_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise self._invalid("snapshot must be an object")
        if state.get("version") != 1 or not self._is_int(state.get("version")):
            raise self._invalid("unsupported snapshot version")

        for required in ("entities", "baselines", "dirty", "events", "stats"):
            if required not in state:
                raise self._invalid(f"missing {required}")

        raw_entities = state["entities"]
        if not isinstance(raw_entities, dict):
            raise self._invalid("entities must be an object")
        entities: dict[str, dict] = {}
        for entity_id, raw_record in raw_entities.items():
            if not isinstance(entity_id, str):
                raise self._invalid("entity id must be a string")
            if not isinstance(raw_record, dict):
                raise self._invalid("entity record must be an object")
            if "fields" not in raw_record:
                raise self._invalid("entity fields are missing")
            raw_fields = raw_record["fields"]
            if not isinstance(raw_fields, dict):
                raise self._invalid("entity fields must be an object")
            for name in raw_fields:
                if not isinstance(name, str):
                    raise self._invalid("field name must be a string")
            local = raw_record.get("local")
            if not isinstance(local, bool):
                raise self._invalid("entity local flag must be a boolean")
            distance = raw_record.get("distance")
            if (distance is not None
                    and (isinstance(distance, bool)
                         or not isinstance(distance, (int, float)))):
                raise self._invalid("entity distance must be numeric or null")
            entities[entity_id] = {
                "fields": copy.deepcopy(raw_fields),
                "local": local,
                "distance": distance,
            }

        raw_baselines = state["baselines"]
        if not isinstance(raw_baselines, dict):
            raise self._invalid("baselines must be an object")
        baselines: dict[str, dict] = {}
        for entity_id, raw_baseline in raw_baselines.items():
            if entity_id not in entities:
                raise self._invalid("baseline refers to an unknown entity")
            if raw_baseline is None:
                continue
            if not isinstance(raw_baseline, dict):
                raise self._invalid("baseline must be an object or null")
            fields = entities[entity_id]["fields"]
            for name in raw_baseline:
                if not isinstance(name, str) or name not in fields:
                    raise self._invalid("baseline field is not registered")
            baselines[entity_id] = copy.deepcopy(raw_baseline)

        raw_dirty = state["dirty"]
        if not isinstance(raw_dirty, dict):
            raise self._invalid("dirty must be an object")
        dirty: dict[str, set[str]] = {}
        for entity_id, raw_names in raw_dirty.items():
            if entity_id not in entities:
                raise self._invalid("dirty mask refers to an unknown entity")
            if not isinstance(raw_names, (list, tuple, set, frozenset)):
                raise self._invalid("dirty mask must be a collection")
            names = set()
            for name in raw_names:
                if (not isinstance(name, str)
                        or name not in entities[entity_id]["fields"]):
                    raise self._invalid("dirty field is not registered")
                names.add(name)
            if names:
                dirty[entity_id] = names

        raw_events = state["events"]
        if not isinstance(raw_events, list):
            raise self._invalid("events must be a list")
        events = []
        for raw_event in raw_events:
            if (not isinstance(raw_event, dict)
                    or set(raw_event) != {"type", "entity"}):
                raise self._invalid("event must have type and entity")
            if raw_event["type"] not in ("create", "remove"):
                raise self._invalid("event type is invalid")
            if not isinstance(raw_event["entity"], str):
                raise self._invalid("event entity must be a string")
            events.append(copy.deepcopy(raw_event))

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict):
            raise self._invalid("stats must be an object")
        if set(raw_stats) != set(STATS_KEYS):
            raise self._invalid("stats keys are invalid")
        for key in STATS_KEYS:
            if not self._is_int(raw_stats[key]) or raw_stats[key] < 0:
                raise self._invalid("stats values must be non-negative integers")
        stats = {key: int(raw_stats[key]) for key in STATS_KEYS}

        raw_slices = state.get("slices", {})
        if not isinstance(raw_slices, dict):
            raise self._invalid("slices must be an object")
        slices: dict[int, dict] = {}
        for tick, raw_slice in raw_slices.items():
            if not self._is_int(tick):
                raise self._invalid("slice tick must be an integer")
            if not isinstance(raw_slice, dict):
                raise self._invalid("slice must be an object")
            raw_items = raw_slice.get("entities")
            if not isinstance(raw_items, list):
                raise self._invalid("slice entities must be a list")
            items = []
            for raw_item in raw_items:
                if not isinstance(raw_item, dict):
                    raise self._invalid("slice entity must be an object")
                entity_id = raw_item.get("entity")
                fields = raw_item.get("fields")
                if not isinstance(entity_id, str) or not isinstance(fields, dict):
                    raise self._invalid("slice entity fields are invalid")
                for name in fields:
                    if not isinstance(name, str):
                        raise self._invalid("slice field name must be a string")
                epoch = raw_item.get("epoch")
                if epoch is not None and not self._is_int(epoch):
                    raise self._invalid("slice epoch must be an integer")
                items.append({
                    "entity": entity_id,
                    "fields": copy.deepcopy(fields),
                    "epoch": epoch,
                })
            slices[tick] = {"entities": items}

        last_acked_tick = state.get("last_acked_tick")
        if last_acked_tick is not None and not self._is_int(last_acked_tick):
            raise self._invalid("last acknowledged tick must be an integer")

        raw_epochs = state.get("epochs", {})
        if not isinstance(raw_epochs, dict):
            raise self._invalid("epochs must be an object")
        epochs: dict[str, int] = {}
        for entity_id, epoch in raw_epochs.items():
            if entity_id not in entities or not self._is_int(epoch):
                raise self._invalid("epoch is invalid")
            epochs[entity_id] = epoch
        if entities and not epochs:
            # Simple external snapshots may omit this internal bookkeeping.
            epochs = {entity_id: 1 for entity_id in entities}

        next_epoch = state.get("next_epoch")
        if next_epoch is None:
            next_epoch = max(epochs.values(), default=0) + 1
        if not self._is_int(next_epoch) or next_epoch < 1:
            raise self._invalid("next epoch must be a positive integer")

        config = state.get("config")
        if config is not None:
            if not isinstance(config, dict):
                raise self._invalid("config must be an object")
            config_values = {}
            for key in ("keyframe_interval", "keyframe_age",
                        "field_cost", "event_cost"):
                value = config.get(key)
                if not self._is_int(value):
                    raise self._invalid("config values must be integers")
                config_values[key] = value
            config = config_values

        return {
            "entities": entities,
            "baselines": baselines,
            "dirty": dirty,
            "events": events,
            "stats": stats,
            "slices": slices,
            "last_acked_tick": last_acked_tick,
            "epochs": epochs,
            "next_epoch": next_epoch,
            "config": config,
        }
