"""Instance slot and draw batch guard for instanced rendering."""

from __future__ import annotations

import copy
import threading
from typing import Any


_SNAPSHOT_VERSION = 1
_STAT_KEYS = (
    "created",
    "deleted",
    "committed_ranges",
    "commits",
    "compactions",
    "moved_slots",
)


class InstanceBatchError(ValueError):
    """Raised when the instance slot or batch bookkeeping is violated."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class InstanceBatchGuard:
    """Track instance slots, dirty attribute ranges and draw batches."""

    def __init__(self, *, max_slots: int = 256, max_batches: int = 64,
                 merge_gap: int = 0) -> None:
        if not _is_positive_int(max_slots):
            raise InstanceBatchError(
                "BATCH_CONFIG_INVALID", {"max_slots": max_slots}
            )
        if not _is_positive_int(max_batches):
            raise InstanceBatchError(
                "BATCH_CONFIG_INVALID", {"max_batches": max_batches}
            )
        if not _is_non_negative_int(merge_gap):
            raise InstanceBatchError(
                "BATCH_CONFIG_INVALID", {"merge_gap": merge_gap}
            )

        self.max_slots = max_slots
        self.max_batches = max_batches
        self.merge_gap = merge_gap
        self._lock = threading.RLock()
        self._slots: list[dict[str, Any] | None] = [None] * max_slots
        self._id_to_slot: dict[Any, int] = {}
        self._dirty: set[int] = set()
        self._stats = {key: 0 for key in _STAT_KEYS}

    def create(self, instance_id: str, *, mesh: str, material: str,
               attrs: dict | None = None) -> dict:
        with self._lock:
            if instance_id in self._id_to_slot:
                raise InstanceBatchError(
                    "DUPLICATE_INSTANCE", {"instance_id": instance_id}
                )

            if attrs is not None and not isinstance(attrs, dict):
                raise InstanceBatchError("ATTR_INVALID", {"attrs": attrs})
            if not isinstance(mesh, str) or mesh == "":
                raise InstanceBatchError("ATTR_INVALID", {"mesh": mesh})
            if not isinstance(material, str) or material == "":
                raise InstanceBatchError(
                    "ATTR_INVALID", {"material": material}
                )

            slot = self._lowest_free_slot()
            if slot is None:
                raise InstanceBatchError(
                    "SLOT_LIMIT", {"max_slots": self.max_slots}
                )

            record = {
                "instance_id": instance_id,
                "mesh": mesh,
                "material": material,
                "attrs": {} if attrs is None else copy.deepcopy(attrs),
            }
            candidate_slots = self._slots.copy()
            candidate_slots[slot] = record
            if len(self._build_batches(candidate_slots)) > self.max_batches:
                raise InstanceBatchError(
                    "BATCH_LIMIT", {"max_batches": self.max_batches}
                )

            self._slots[slot] = record
            self._id_to_slot[instance_id] = slot
            self._dirty.discard(slot)
            self._dirty.add(slot)
            self._stats["created"] += 1

            return {"instance_id": instance_id, "slot": slot}

    def update(self, instance_id: str, attrs: dict) -> int:
        with self._lock:
            slot = self._required_slot(instance_id)
            if not isinstance(attrs, dict):
                raise InstanceBatchError("ATTR_INVALID", {"attrs": attrs})

            record = self._slots[slot]
            assert record is not None
            for key, value in attrs.items():
                record["attrs"][key] = copy.deepcopy(value)
            self._dirty.add(slot)
            return slot

    def delete(self, instance_id: str) -> int:
        with self._lock:
            slot = self._required_slot(instance_id)
            del self._id_to_slot[instance_id]
            self._slots[slot] = None
            self._dirty.discard(slot)
            self._stats["deleted"] += 1
            return slot

    def slot_of(self, instance_id: str) -> int:
        with self._lock:
            return self._required_slot(instance_id)

    def instance_at(self, slot: int) -> str | None:
        with self._lock:
            if not _is_non_negative_int(slot) or slot >= self.max_slots:
                return None
            record = self._slots[slot]
            return None if record is None else record["instance_id"]

    def dirty_ranges(self) -> list[tuple[int, int]]:
        with self._lock:
            return self._merge_dirty_ranges(self._dirty)

    def commit(self) -> list[tuple[int, int]]:
        with self._lock:
            ranges = self._merge_dirty_ranges(self._dirty)
            self._dirty.clear()
            self._stats["commits"] += 1
            self._stats["committed_ranges"] += len(ranges)
            return ranges

    def batches(self) -> list[dict]:
        with self._lock:
            return self._build_batches(self._slots)

    def fragmentation(self) -> float:
        with self._lock:
            used_slots = [
                slot
                for slot, record in enumerate(self._slots)
                if record is not None
            ]
            if not used_slots:
                return 0.0

            span = max(used_slots) + 1
            return (span - len(used_slots)) / span

    def compact(self) -> dict:
        with self._lock:
            occupied = [
                (slot, record)
                for slot, record in enumerate(self._slots)
                if record is not None
            ]

            compacted_slots: list[dict[str, Any] | None] = [
                None
            ] * self.max_slots
            compacted_ids: dict[Any, int] = {}
            moved = 0

            for new_slot, (old_slot, record) in enumerate(occupied):
                assert record is not None
                compacted_slots[new_slot] = record
                compacted_ids[record["instance_id"]] = new_slot
                if old_slot != new_slot:
                    moved += 1

            count = len(occupied)
            self._slots = compacted_slots
            self._id_to_slot = compacted_ids
            self._dirty = set(range(count))
            self._stats["compactions"] += 1
            self._stats["moved_slots"] += moved

            return {"moved": moved, "slots": count}

    def snapshot(self) -> dict:
        with self._lock:
            slots = {}
            for slot, record in enumerate(self._slots):
                if record is None:
                    continue
                slots[slot] = {
                    "instance_id": record["instance_id"],
                    "mesh": record["mesh"],
                    "material": record["material"],
                    "attrs": copy.deepcopy(record["attrs"]),
                    "dirty": slot in self._dirty,
                }

            return {
                "version": _SNAPSHOT_VERSION,
                "slots": slots,
                "stats": {key: self._stats[key] for key in _STAT_KEYS},
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            slots, ids_to_slots, dirty, stats = self._parse_snapshot(state)
            self._slots = slots
            self._id_to_slot = ids_to_slots
            self._dirty = dirty
            self._stats = stats

    def stats(self) -> dict:
        with self._lock:
            return {key: self._stats[key] for key in _STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}

    def _required_slot(self, instance_id: str) -> int:
        try:
            return self._id_to_slot[instance_id]
        except (KeyError, TypeError):
            raise InstanceBatchError(
                "UNKNOWN_INSTANCE", {"instance_id": instance_id}
            ) from None

    def _lowest_free_slot(self) -> int | None:
        for slot, record in enumerate(self._slots):
            if record is None:
                return slot
        return None

    def _merge_dirty_ranges(
        self, dirty_slots: set[int]
    ) -> list[tuple[int, int]]:
        if not dirty_slots:
            return []

        ordered = sorted(dirty_slots)
        ranges: list[tuple[int, int]] = []
        start = previous = ordered[0]

        for slot in ordered[1:]:
            gap = slot - previous - 1
            if gap <= self.merge_gap:
                previous = slot
                continue

            ranges.append((start, previous + 1))
            start = previous = slot

        ranges.append((start, previous + 1))
        return ranges

    @staticmethod
    def _build_batches(
        slots: list[dict[str, Any] | None],
    ) -> list[dict]:
        batches: list[dict] = []
        start: int | None = None
        previous_slot: int | None = None
        mesh: str | None = None
        material: str | None = None

        for slot, record in enumerate(slots):
            if record is None:
                continue

            if start is None or previous_slot is None:
                start = slot
                previous_slot = slot
                mesh = record["mesh"]
                material = record["material"]
                continue

            contiguous = slot == previous_slot + 1
            same_batch = (
                contiguous
                and record["mesh"] == mesh
                and record["material"] == material
            )
            if same_batch:
                previous_slot = slot
                continue

            batches.append(
                {
                    "mesh": mesh,
                    "material": material,
                    "start": start,
                    "end": previous_slot + 1,
                }
            )
            start = slot
            previous_slot = slot
            mesh = record["mesh"]
            material = record["material"]

        if start is not None and previous_slot is not None:
            batches.append(
                {
                    "mesh": mesh,
                    "material": material,
                    "start": start,
                    "end": previous_slot + 1,
                }
            )

        return batches

    def _parse_snapshot(
        self, state: Any
    ) -> tuple[
        list[dict[str, Any] | None],
        dict[Any, int],
        set[int],
        dict[str, int],
    ]:
        if not isinstance(state, dict):
            raise InstanceBatchError(
                "SNAPSHOT_INVALID", "state must be a dict"
            )

        version = state.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != _SNAPSHOT_VERSION
        ):
            raise InstanceBatchError(
                "SNAPSHOT_INVALID", {"version": version}
            )

        raw_slots = state.get("slots")
        if not isinstance(raw_slots, dict):
            raise InstanceBatchError(
                "SNAPSHOT_INVALID", "slots must be a dict"
            )

        raw_stats = state.get("stats")
        if not isinstance(raw_stats, dict):
            raise InstanceBatchError(
                "SNAPSHOT_INVALID", "stats must be a dict"
            )

        stats: dict[str, int] = {}
        for key in _STAT_KEYS:
            value = raw_stats.get(key)
            if not _is_non_negative_int(value):
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID",
                    {"stats_key": key, "value": value},
                )
            stats[key] = value

        slots: list[dict[str, Any] | None] = [None] * self.max_slots
        ids_to_slots: dict[Any, int] = {}
        dirty: set[int] = set()
        required_entry_keys = {
            "instance_id",
            "mesh",
            "material",
            "attrs",
            "dirty",
        }

        for raw_slot, entry in raw_slots.items():
            slot = _coerce_slot_key(raw_slot)
            if (
                slot is None
                or not 0 <= slot < self.max_slots
                or slots[slot] is not None
            ):
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"slot": raw_slot}
                )

            if not isinstance(entry, dict):
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"slot_entry": raw_slot}
                )
            if not required_entry_keys.issubset(entry):
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"slot": slot, "entry": "incomplete"}
                )

            instance_id = entry["instance_id"]
            try:
                hash(instance_id)
            except TypeError:
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"instance_id": "unhashable"}
                ) from None
            if instance_id in ids_to_slots:
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"instance_id": instance_id}
                )

            mesh = entry["mesh"]
            material = entry["material"]
            attrs = entry["attrs"]
            is_dirty = entry["dirty"]
            if not isinstance(mesh, str) or mesh == "":
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"mesh": mesh}
                )
            if not isinstance(material, str) or material == "":
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"material": material}
                )
            if not isinstance(attrs, dict):
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"attrs": "must be dict"}
                )
            if not isinstance(is_dirty, bool):
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"dirty": is_dirty}
                )

            try:
                copied_attrs = copy.deepcopy(attrs)
            except Exception as exc:
                raise InstanceBatchError(
                    "SNAPSHOT_INVALID", {"attrs": "not copyable"}
                ) from exc

            slots[slot] = {
                "instance_id": instance_id,
                "mesh": mesh,
                "material": material,
                "attrs": copied_attrs,
            }
            ids_to_slots[instance_id] = slot
            if is_dirty:
                dirty.add(slot)

        if len(self._build_batches(slots)) > self.max_batches:
            raise InstanceBatchError(
                "SNAPSHOT_INVALID", {"max_batches": self.max_batches}
            )

        return slots, ids_to_slots, dirty, stats


def _is_positive_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value > 0
    )


def _is_non_negative_int(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )


def _coerce_slot_key(key: Any) -> int | None:
    if isinstance(key, int) and not isinstance(key, bool):
        return key
    if isinstance(key, str):
        try:
            if key != str(int(key)):
                return None
            return int(key)
        except ValueError:
            return None
    return None
