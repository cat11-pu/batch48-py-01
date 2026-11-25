"""Transparent draw ordering with packed 64-bit sort keys."""

from __future__ import annotations

import copy
import math
from bisect import bisect_left


UINT64_MAX = (1 << 64) - 1
LAYER_SHIFT = 56
MATERIAL_SHIFT = 44
MATERIAL_BATCH_LIMIT = 1 << 12
SNAPSHOT_VERSION = 1
STAT_KEYS = ("frames", "reused_order", "reordered_objects", "degraded",
             "depth_updates")


class TransparentSortError(ValueError):
    """Raised when a transparent object or sort key is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_finite_number(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, ValueError):
        return False


def _snapshot_invalid(detail: object = None) -> TransparentSortError:
    return TransparentSortError("SORT_SNAPSHOT_INVALID", detail)


class TransparentSortGuard:
    """Build unique 64-bit keys and keep transparent draw order stable."""

    def __init__(self, *, depth_bits: int = 8, depth_min: float = 0.0,
                 depth_max: float = 1000.0, max_objects: int = 1024,
                 sort_budget: int = 64) -> None:
        if not _is_int(depth_bits) or not 1 <= depth_bits <= 43:
            raise TransparentSortError(
                "SORT_CONFIG_INVALID", {"depth_bits": depth_bits})
        if not _is_int(max_objects) or max_objects <= 0:
            raise TransparentSortError(
                "SORT_CONFIG_INVALID", {"max_objects": max_objects})
        if not _is_int(sort_budget) or sort_budget <= 0:
            raise TransparentSortError(
                "SORT_CONFIG_INVALID", {"sort_budget": sort_budget})
        for name, value in (("depth_min", depth_min),
                            ("depth_max", depth_max)):
            if not _is_finite_number(value):
                raise TransparentSortError(
                    "SORT_CONFIG_INVALID", {name: value})
        if depth_max <= depth_min:
            raise TransparentSortError(
                "SORT_CONFIG_INVALID",
                {"depth_min": depth_min, "depth_max": depth_max})

        sequence_bits = 44 - depth_bits
        sequence_capacity = 1 << sequence_bits
        if 2 * max_objects > sequence_capacity:
            raise TransparentSortError(
                "SORT_CONFIG_INVALID",
                {"depth_bits": depth_bits, "max_objects": max_objects})

        self.depth_bits = depth_bits
        self.depth_min = float(depth_min)
        self.depth_max = float(depth_max)
        self.max_objects = max_objects
        self.sort_budget = sort_budget

        self._sequence_bits = sequence_bits
        self._sequence_mask = sequence_capacity - 1
        self._max_bucket = (1 << depth_bits) - 1

        self._objects: dict[str, dict] = {}
        self._materials: dict[str, int] = {}
        self._next_material_batch = 0
        self._next_sequence = 0

        self._last_entries: list[dict] = []
        self._canonical_entries: list[dict] = []
        self._bucket_snapshot: dict[str, int] = {}
        self._has_frame = False
        self._stats = {key: 0 for key in STAT_KEYS}

    def add(self, object_id: str, *, layer: int = 0, material: str = "default",
            double_sided: bool = False, depth: float = 0.0) -> dict:
        if not isinstance(object_id, str) or not object_id:
            raise TransparentSortError(
                "SORT_OBJECT_INVALID", {"object_id": object_id})
        if not isinstance(material, str) or not material:
            raise TransparentSortError(
                "SORT_OBJECT_INVALID", {"material": material})
        if not _is_int(layer) or not 0 <= layer <= 255:
            raise TransparentSortError(
                "SORT_LAYER_INVALID", {"layer": layer})

        bucket = self._bucket_for_depth(depth)

        if object_id in self._objects:
            raise TransparentSortError(
                "SORT_DUPLICATE_OBJECT", {"object_id": object_id})
        if len(self._objects) >= self.max_objects:
            raise TransparentSortError(
                "SORT_LIMIT", {"max_objects": self.max_objects})

        material_batch = self._materials.get(material)
        if material_batch is None:
            if self._next_material_batch >= MATERIAL_BATCH_LIMIT:
                raise TransparentSortError(
                    "SORT_MATERIAL_LIMIT",
                    {"material_batch": self._next_material_batch})
            material_batch = self._next_material_batch
            self._materials[material] = material_batch
            self._next_material_batch += 1

        sequence = self._next_sequence
        keys = [self._pack_key(
            layer=layer, material_batch=material_batch, bucket=bucket,
            sequence=sequence, side=0)]
        if double_sided:
            keys.append(self._pack_key(
                layer=layer, material_batch=material_batch, bucket=bucket,
                sequence=sequence, side=1))

        record = {
            "object_id": object_id,
            "layer": layer,
            "material": material,
            "material_batch": material_batch,
            "double_sided": bool(double_sided),
            "depth": depth,
            "bucket": bucket,
            "stable_sequence": sequence,
            "keys": keys,
        }
        self._objects[object_id] = record
        self._next_sequence += 2
        return self._record_view(record)

    def remove(self, object_id: str) -> None:
        self._require_object(object_id)
        del self._objects[object_id]

    def update_depth(self, object_id: str, depth: float) -> int:
        record = self._require_object(object_id)
        bucket = self._bucket_for_depth(depth)
        record["depth"] = depth
        record["bucket"] = bucket
        record["keys"] = self._keys_for_record(record)
        self._stats["depth_updates"] += 1
        return bucket

    def bucket_of(self, object_id: str) -> int:
        return self._require_object(object_id)["bucket"]

    def key_for(self, object_id: str, *, side: int = 0) -> int:
        record = self._require_object(object_id)
        if not _is_int(side) or side not in (0, 1):
            raise TransparentSortError(
                "SORT_SIDE_INVALID", {"object_id": object_id, "side": side})
        if side == 1 and not record["double_sided"]:
            raise TransparentSortError(
                "SORT_SIDE_INVALID", {"object_id": object_id, "side": side})
        return record["keys"][side]

    def keys(self, object_id: str) -> list[int]:
        return list(self._require_object(object_id)["keys"])

    def object_count(self) -> int:
        return len(self._objects)

    def sort(self) -> list[dict]:
        self._stats["frames"] += 1
        changed_ids = self._changed_object_ids()

        if self._has_frame and not changed_ids:
            self._stats["reused_order"] += 1
            return [self._public_entry(entry)
                    for entry in self._last_entries]

        if not self._has_frame:
            canonical_entries = self._sorted_key_entries(self._objects)
            output_entries = canonical_entries
        elif len(changed_ids) > self.sort_budget:
            self._stats["degraded"] += 1
            output_entries = self._coarse_entries()
            canonical_entries = self._sorted_key_entries(self._objects)
        else:
            canonical_entries = self._advance_canonical_entries()
            output_entries = canonical_entries

        old_positions = {
            entry["sequence"] + entry["side"]: index
            for index, entry in enumerate(self._last_entries)
        }
        moved = 0
        for index, entry in enumerate(output_entries):
            draw_id = entry["sequence"] + entry["side"]
            if old_positions.get(draw_id) != index:
                moved += 1

        self._stats["reordered_objects"] += moved
        self._last_entries = output_entries
        self._canonical_entries = canonical_entries
        self._bucket_snapshot = {
            object_id: record["bucket"]
            for object_id, record in self._objects.items()
        }
        self._has_frame = True

        return [self._public_entry(entry) for entry in output_entries]

    def snapshot(self) -> dict:
        objects = {}
        for object_id, record in self._objects.items():
            batch = record["material_batch"]
            sequence = record["stable_sequence"]
            objects[object_id] = {
                "object_id": object_id,
                "layer": record["layer"],
                "material": record["material"],
                "material_batch": batch,
                "batch": batch,
                "double_sided": record["double_sided"],
                "depth": record["depth"],
                "bucket": record["bucket"],
                "stable_sequence": sequence,
                "sequence": sequence,
                "stable_id": sequence,
                "keys": list(record["keys"]),
            }

        last_order = [self._public_entry(entry)
                      for entry in self._last_entries]
        bucket_snapshot = dict(self._bucket_snapshot)
        state = {
            "version": SNAPSHOT_VERSION,
            "objects": objects,
            "materials": dict(self._materials),
            "counter": self._next_sequence,
            "last_order": last_order,
            "order": copy.deepcopy(last_order),
            "previous_order": copy.deepcopy(last_order),
            "bucket_snapshot": bucket_snapshot,
            "buckets": dict(bucket_snapshot),
            "has_frame": self._has_frame,
            "stats": self.stats(),
        }
        return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        try:
            parsed = self._parse_snapshot(state)
        except TransparentSortError as exc:
            if exc.code == "SORT_SNAPSHOT_INVALID":
                raise
            raise _snapshot_invalid(exc.code) from exc
        except Exception as exc:
            raise _snapshot_invalid(str(exc)) from exc

        canonical_entries = self._sorted_key_entries(parsed["objects"])
        max_sequence = max(
            (record["stable_sequence"]
             for record in parsed["objects"].values()),
            default=-2,
        )
        next_sequence = max(parsed["counter"], max_sequence + 2)
        if next_sequence % 2:
            next_sequence += 1

        self._objects = parsed["objects"]
        self._materials = parsed["materials"]
        self._next_material_batch = len(self._materials)
        self._next_sequence = next_sequence
        self._last_entries = parsed["last_entries"]
        self._bucket_snapshot = parsed["bucket_snapshot"]
        self._has_frame = parsed["has_frame"]
        self._stats = parsed["stats"]
        self._canonical_entries = canonical_entries

    def stats(self) -> dict:
        return {key: int(self._stats[key]) for key in STAT_KEYS}

    def reset_stats(self) -> None:
        for key in STAT_KEYS:
            self._stats[key] = 0

    def _require_object(self, object_id: str) -> dict:
        record = self._objects.get(object_id)
        if record is None:
            raise TransparentSortError(
                "SORT_UNKNOWN_OBJECT", {"object_id": object_id})
        return record

    def _bucket_for_depth(self, depth: float) -> int:
        if not _is_finite_number(depth):
            raise TransparentSortError(
                "SORT_DEPTH_OUT_OF_RANGE", {"depth": depth})
        if depth < self.depth_min or depth > self.depth_max:
            raise TransparentSortError(
                "SORT_DEPTH_OUT_OF_RANGE", {"depth": depth})

        scaled = (depth - self.depth_min) * self._max_bucket
        span = self.depth_max - self.depth_min
        try:
            bucket = int(math.floor(scaled / span))
        except OverflowError:
            bucket = self._max_bucket

        if bucket < 0:
            bucket = 0
        elif bucket > self._max_bucket:
            bucket = self._max_bucket
        return bucket

    def _pack_key(self, *, layer: int, material_batch: int, bucket: int,
                  sequence: int, side: int) -> int:
        low_part = sequence + side
        if not 0 <= low_part <= self._sequence_mask:
            raise TransparentSortError(
                "SORT_KEY_OVERFLOW",
                {"sequence": sequence, "side": side,
                 "sequence_bits": self._sequence_bits})

        inverted_bucket = self._max_bucket - bucket
        key = ((layer << LAYER_SHIFT)
               | (material_batch << MATERIAL_SHIFT)
               | (inverted_bucket << self._sequence_bits)
               | low_part)
        if not 0 <= key <= UINT64_MAX:
            raise TransparentSortError("SORT_KEY_OVERFLOW", {"key": key})
        return key

    def _keys_for_record(self, record: dict) -> list[int]:
        keys = [self._pack_key(
            layer=record["layer"],
            material_batch=record["material_batch"],
            bucket=record["bucket"],
            sequence=record["stable_sequence"],
            side=0)]
        if record["double_sided"]:
            keys.append(self._pack_key(
                layer=record["layer"],
                material_batch=record["material_batch"],
                bucket=record["bucket"],
                sequence=record["stable_sequence"],
                side=1))
        return keys

    def _record_view(self, record: dict) -> dict:
        return {
            "object_id": record["object_id"],
            "layer": record["layer"],
            "material": record["material"],
            "material_batch": record["material_batch"],
            "double_sided": record["double_sided"],
            "depth": record["depth"],
            "bucket": record["bucket"],
            "stable_sequence": record["stable_sequence"],
            "keys": list(record["keys"]),
        }

    @staticmethod
    def _make_entry(record: dict, side: int) -> dict:
        return {
            "object_id": record["object_id"],
            "side": side,
            "key": record["keys"][side],
            "sequence": record["stable_sequence"],
        }

    @staticmethod
    def _public_entry(entry: dict) -> dict:
        return {
            "object_id": entry["object_id"],
            "key": entry["key"],
            "side": entry["side"],
        }

    @classmethod
    def _sorted_key_entries(cls, objects: dict[str, dict]) -> list[dict]:
        entries = []
        seen_keys = set()
        for record in objects.values():
            for side in range(len(record["keys"])):
                entry = cls._make_entry(record, side)
                if entry["key"] in seen_keys:
                    raise TransparentSortError(
                        "SORT_KEY_COLLISION", {"key": entry["key"]})
                seen_keys.add(entry["key"])
                entries.append(entry)
        entries.sort(key=lambda entry: entry["key"])
        return entries

    def _coarse_entries(self) -> list[dict]:
        entries = []
        for record in self._objects.values():
            for side in range(len(record["keys"])):
                entries.append(self._make_entry(record, side))
        entries.sort(key=lambda entry: (
            self._objects[entry["object_id"]]["layer"],
            self._objects[entry["object_id"]]["material_batch"],
            -self._objects[entry["object_id"]]["bucket"],
            entry["object_id"],
            entry["side"],
        ))
        return entries

    def _changed_object_ids(self) -> set[str]:
        previous_side_zero = {
            entry["object_id"]: entry
            for entry in self._last_entries
            if entry["side"] == 0
        }
        changed = set()

        for object_id, record in self._objects.items():
            previous = previous_side_zero.get(object_id)
            if (previous is None
                    or previous.get("sequence") != record["stable_sequence"]
                    or previous.get("key") != record["keys"][0]):
                changed.add(object_id)

        for object_id in previous_side_zero:
            if object_id not in self._objects:
                changed.add(object_id)

        return changed

    def _advance_canonical_entries(self) -> list[dict]:
        base = []
        for entry in self._canonical_entries:
            record = self._objects.get(entry["object_id"])
            side = entry["side"]
            if record is None or side >= len(record["keys"]):
                continue
            if entry["sequence"] != record["stable_sequence"]:
                continue
            if entry["key"] != record["keys"][side]:
                continue
            base.append(dict(entry))

        base_keys = [entry["key"] for entry in base]
        represented_sequences = {
            entry["sequence"]
            for entry in base
            if entry["side"] == 0
        }

        for record in self._objects.values():
            sequence = record["stable_sequence"]
            if sequence in represented_sequences:
                continue
            group = [
                self._make_entry(record, side)
                for side in range(len(record["keys"]))
            ]
            insert_at = bisect_left(base_keys, group[0]["key"])
            base[insert_at:insert_at] = group
            base_keys[insert_at:insert_at] = [
                entry["key"] for entry in group]

        return base

    def _parse_snapshot(self, state: object) -> dict:
        try:
            data = copy.deepcopy(state)
        except Exception as exc:
            raise _snapshot_invalid("uncopyable state") from exc

        if not isinstance(data, dict):
            raise _snapshot_invalid("snapshot must be a dict")
        if type(data.get("version")) is not int or data.get("version") != 1:
            raise _snapshot_invalid("unsupported version")

        raw_materials = data.get("materials")
        if not isinstance(raw_materials, dict):
            raise _snapshot_invalid("materials must be a dict")
        materials: dict[str, int] = {}
        batches: set[int] = set()
        for material, batch in raw_materials.items():
            if not isinstance(material, str) or not material:
                raise _snapshot_invalid("invalid material name")
            if not _is_int(batch) or not 0 <= batch < MATERIAL_BATCH_LIMIT:
                raise _snapshot_invalid("invalid material batch")
            if batch in batches:
                raise _snapshot_invalid("duplicate material batch")
            materials[material] = batch
            batches.add(batch)
        if sorted(batches) != list(range(len(batches))):
            raise _snapshot_invalid("material batches must be contiguous")

        counter = data.get("counter")
        if not _is_int(counter) or counter < 0 or counter % 2:
            raise _snapshot_invalid("invalid stable counter")

        raw_stats = data.get("stats")
        if not isinstance(raw_stats, dict):
            raise _snapshot_invalid("stats must be a dict")
        if set(raw_stats) != set(STAT_KEYS):
            raise _snapshot_invalid("stats keys mismatch")
        stats = {}
        for key in STAT_KEYS:
            value = raw_stats[key]
            if not _is_int(value) or value < 0:
                raise _snapshot_invalid(f"invalid stat {key}")
            stats[key] = value

        raw_objects = data.get("objects")
        if not isinstance(raw_objects, dict):
            raise _snapshot_invalid("objects must be a dict")

        objects: dict[str, dict] = {}
        current_keys: set[int] = set()
        current_draw_ids: set[int] = set()
        for object_id, raw_record in raw_objects.items():
            if not isinstance(object_id, str) or not object_id:
                raise _snapshot_invalid("invalid object id")
            if not isinstance(raw_record, dict):
                raise _snapshot_invalid("object record must be a dict")

            supplied_id = raw_record.get(
                "object_id", raw_record.get("id", object_id))
            if supplied_id != object_id:
                raise _snapshot_invalid("object id mismatch")

            layer = raw_record.get("layer")
            if not _is_int(layer) or not 0 <= layer <= 255:
                raise _snapshot_invalid("invalid layer")

            material = raw_record.get("material")
            if not isinstance(material, str) or not material:
                raise _snapshot_invalid("invalid material")
            batch = raw_record.get(
                "material_batch", raw_record.get("batch"))
            if not _is_int(batch) or materials.get(material) != batch:
                raise _snapshot_invalid("material batch mismatch")

            depth = raw_record.get("depth")
            if (not _is_finite_number(depth)
                    or depth < self.depth_min
                    or depth > self.depth_max):
                raise _snapshot_invalid("invalid depth")

            bucket = raw_record.get("bucket")
            expected_bucket = self._bucket_for_depth(depth)
            if not _is_int(bucket) or bucket != expected_bucket:
                raise _snapshot_invalid("bucket mismatch")

            sequence = raw_record.get(
                "stable_sequence",
                raw_record.get("sequence", raw_record.get("stable_id")))
            if (not _is_int(sequence) or sequence < 0 or sequence % 2
                    or sequence > self._sequence_mask):
                raise _snapshot_invalid("invalid stable sequence")

            raw_keys = raw_record.get("keys")
            if not isinstance(raw_keys, list) or not raw_keys:
                raise _snapshot_invalid("keys must be a non-empty list")
            if len(raw_keys) > 2:
                raise _snapshot_invalid("too many draw keys")
            keys = []
            for key in raw_keys:
                if not _is_int(key) or not 0 <= key <= UINT64_MAX:
                    raise _snapshot_invalid("invalid packed key")
                keys.append(key)
            if len(set(keys)) != len(keys):
                raise _snapshot_invalid("duplicate keys for one object")

            double_sided = raw_record.get("double_sided", len(keys) == 2)
            if not isinstance(double_sided, bool):
                raise _snapshot_invalid("invalid double_sided flag")
            if len(keys) != (2 if double_sided else 1):
                raise _snapshot_invalid("key count mismatch")

            expected_keys = []
            for side in range(len(keys)):
                expected_key = self._pack_key(
                    layer=layer, material_batch=batch, bucket=bucket,
                    sequence=sequence, side=side)
                expected_keys.append(expected_key)
            if keys != expected_keys:
                raise _snapshot_invalid("packed keys do not match fields")

            for side, key in enumerate(keys):
                draw_id = sequence + side
                if key in current_keys or draw_id in current_draw_ids:
                    raise _snapshot_invalid("global key/draw collision")
                current_keys.add(key)
                current_draw_ids.add(draw_id)

            objects[object_id] = {
                "object_id": object_id,
                "layer": layer,
                "material": material,
                "material_batch": batch,
                "double_sided": double_sided,
                "depth": depth,
                "bucket": bucket,
                "stable_sequence": sequence,
                "keys": keys,
            }

        raw_order = None
        for order_key in ("last_order", "order", "previous_order"):
            if order_key in data:
                raw_order = data[order_key]
                break
        if not isinstance(raw_order, list):
            raise _snapshot_invalid("last order must be a list")

        last_entries = []
        order_keys: set[int] = set()
        order_draw_ids: set[int] = set()
        for raw_entry in raw_order:
            if not isinstance(raw_entry, dict):
                raise _snapshot_invalid("order entry must be a dict")
            object_id = raw_entry.get("object_id", raw_entry.get("id"))
            side = raw_entry.get("side", 0)
            key = raw_entry.get("key")
            if not isinstance(object_id, str) or not object_id:
                raise _snapshot_invalid("invalid order object id")
            if not _is_int(side) or side not in (0, 1):
                raise _snapshot_invalid("invalid order side")
            if not _is_int(key) or not 0 <= key <= UINT64_MAX:
                raise _snapshot_invalid("invalid order key")

            low_part = key & self._sequence_mask
            sequence = low_part - side
            if sequence < 0 or sequence % 2:
                raise _snapshot_invalid("order key sequence mismatch")
            supplied_sequence = raw_entry.get(
                "sequence",
                raw_entry.get("stable_sequence",
                              raw_entry.get("stable_id")))
            if (supplied_sequence is not None
                    and supplied_sequence != sequence):
                raise _snapshot_invalid("order sequence mismatch")

            record = objects.get(object_id)
            if record is not None:
                if (sequence != record["stable_sequence"]
                        or side >= len(record["keys"])
                        or key != record["keys"][side]):
                    raise _snapshot_invalid("order does not match objects")
            elif key in current_keys or low_part in current_draw_ids:
                raise _snapshot_invalid("stale order entry collides")

            if key in order_keys or low_part in order_draw_ids:
                raise _snapshot_invalid("duplicate order key/draw id")
            order_keys.add(key)
            order_draw_ids.add(low_part)
            last_entries.append({
                "object_id": object_id,
                "side": side,
                "key": key,
                "sequence": sequence,
            })

        raw_buckets = None
        for bucket_key in ("bucket_snapshot", "buckets"):
            if bucket_key in data:
                raw_buckets = data[bucket_key]
                break
        if not isinstance(raw_buckets, dict):
            raise _snapshot_invalid("bucket snapshot must be a dict")
        bucket_snapshot = {}
        for object_id, bucket in raw_buckets.items():
            if not isinstance(object_id, str) or not object_id:
                raise _snapshot_invalid("invalid bucket object id")
            if not _is_int(bucket) or not 0 <= bucket <= self._max_bucket:
                raise _snapshot_invalid("invalid bucket snapshot value")
            bucket_snapshot[object_id] = bucket

        has_frame = data.get("has_frame", True)
        if not isinstance(has_frame, bool):
            raise _snapshot_invalid("invalid has_frame flag")

        return {
            "objects": objects,
            "materials": materials,
            "counter": counter,
            "last_entries": last_entries,
            "bucket_snapshot": bucket_snapshot,
            "has_frame": has_frame,
            "stats": stats,
        }
