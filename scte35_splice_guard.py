"""SCTE-35 splice insert parsing and execution guard."""

from __future__ import annotations

import copy
import threading


class Scte35SpliceError(ValueError):
    """Rejection raised by the splice guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class Scte35SpliceGuard:
    """Normalise splice inserts, queue them and execute at keyframes."""

    def __init__(self, *, max_pending: int = 256, clock=None) -> None:
        self.max_pending = max_pending
        self._clock = clock
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}
        self._triggered: set[str] = set()
        self._executions: list[dict] = []
        self._stats = self._fresh_stats()

    @staticmethod
    def _fresh_stats() -> dict:
        return {
            "scheduled": 0,
            "triggered": 0,
            "cancelled": 0,
            "duplicate_rejected": 0,
            "queue_full_rejected": 0,
            "invalid_rejected": 0,
            "beyond_program_rejected": 0,
            "keyframes_settled": 0,
        }

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _invalid(detail: object = None) -> Scte35SpliceError:
        return Scte35SpliceError("SPLICE_INVALID", detail)

    def _parse_locked(self, command: dict, *,
                      program_end_ms: int | None) -> dict:
        if not isinstance(command, dict):
            raise self._invalid({"field": "command", "reason": "must_be_dict"})

        splice_id = command.get("splice_id")
        if not isinstance(splice_id, str) or not splice_id:
            raise self._invalid({
                "field": "splice_id",
                "reason": "must_be_non_empty_string",
            })

        splice_pts_ms = command.get("splice_pts_ms")
        if not self._is_int(splice_pts_ms) or splice_pts_ms < 0:
            raise self._invalid({
                "field": "splice_pts_ms",
                "reason": "must_be_non_negative_integer",
            })

        if "break_duration_ms" in command:
            requested_duration_ms = command["break_duration_ms"]
        else:
            requested_duration_ms = 0

        if (not self._is_int(requested_duration_ms)
                or requested_duration_ms < 0):
            raise self._invalid({
                "field": "break_duration_ms",
                "reason": "must_be_non_negative_integer",
            })

        if program_end_ms is not None:
            if not self._is_int(program_end_ms) or program_end_ms < 0:
                raise self._invalid({
                    "field": "program_end_ms",
                    "reason": "must_be_non_negative_integer",
                })
            if splice_pts_ms >= program_end_ms:
                raise Scte35SpliceError("SPLICE_BEYOND_PROGRAM", {
                    "splice_id": splice_id,
                    "splice_pts_ms": splice_pts_ms,
                    "program_end_ms": program_end_ms,
                })
            effective_duration_ms = min(
                requested_duration_ms,
                program_end_ms - splice_pts_ms,
            )
        else:
            effective_duration_ms = requested_duration_ms

        return {
            "splice_id": splice_id,
            "splice_pts_ms": splice_pts_ms,
            "requested_duration_ms": requested_duration_ms,
            "effective_duration_ms": effective_duration_ms,
            "end_pts_ms": splice_pts_ms + effective_duration_ms,
        }

    def parse_command(self, command: dict, *,
                      program_end_ms: int | None = None) -> dict:
        with self._lock:
            try:
                return self._parse_locked(
                    command, program_end_ms=program_end_ms)
            except Scte35SpliceError as exc:
                if exc.code == "SPLICE_INVALID":
                    self._stats["invalid_rejected"] += 1
                elif exc.code == "SPLICE_BEYOND_PROGRAM":
                    self._stats["beyond_program_rejected"] += 1
                raise

    def schedule(self, command: dict, *,
                 program_end_ms: int | None = None) -> dict:
        with self._lock:
            try:
                entry = self._parse_locked(
                    command, program_end_ms=program_end_ms)
            except Scte35SpliceError as exc:
                if exc.code == "SPLICE_INVALID":
                    self._stats["invalid_rejected"] += 1
                elif exc.code == "SPLICE_BEYOND_PROGRAM":
                    self._stats["beyond_program_rejected"] += 1
                raise

            splice_id = entry["splice_id"]
            if splice_id in self._pending or splice_id in self._triggered:
                self._stats["duplicate_rejected"] += 1
                raise Scte35SpliceError("SPLICE_DUPLICATE", {
                    "splice_id": splice_id,
                })

            if len(self._pending) >= self.max_pending:
                self._stats["queue_full_rejected"] += 1
                raise Scte35SpliceError("SPLICE_QUEUE_FULL", {
                    "max_pending": self.max_pending,
                    "pending": len(self._pending),
                })

            self._pending[splice_id] = copy.deepcopy(entry)
            self._stats["scheduled"] += 1
            return copy.deepcopy(entry)

    def on_keyframe(self, keyframe_pts_ms: int) -> list[dict]:
        with self._lock:
            if not self._is_int(keyframe_pts_ms):
                self._stats["invalid_rejected"] += 1
                raise self._invalid({
                    "field": "keyframe_pts_ms",
                    "reason": "must_be_integer",
                })

            due = [
                entry for entry in self._pending.values()
                if entry["splice_pts_ms"] <= keyframe_pts_ms
                and entry["splice_id"] not in self._triggered
            ]
            due.sort(key=lambda entry: entry["splice_pts_ms"])

            records: list[dict] = []
            for entry in due:
                splice_id = entry["splice_id"]
                self._pending.pop(splice_id, None)
                if splice_id in self._triggered:
                    continue

                record = {
                    "splice_id": splice_id,
                    "splice_pts_ms": entry["splice_pts_ms"],
                    "triggered_pts_ms": keyframe_pts_ms,
                    "offset_ms": keyframe_pts_ms - entry["splice_pts_ms"],
                    "effective_duration_ms": entry["effective_duration_ms"],
                    "end_pts_ms": entry["end_pts_ms"],
                }
                self._executions.append(record)
                self._triggered.add(splice_id)
                self._stats["triggered"] += 1
                records.append(copy.deepcopy(record))

            if records:
                self._stats["keyframes_settled"] += 1
            return records

    def cancel(self, splice_id: str) -> bool:
        with self._lock:
            if splice_id not in self._pending:
                return False
            del self._pending[splice_id]
            self._stats["cancelled"] += 1
            return True

    def pending(self) -> list[dict]:
        with self._lock:
            entries = list(self._pending.values())
            entries.sort(key=lambda entry: entry["splice_pts_ms"])
            return copy.deepcopy(entries)

    def triggered_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._triggered)

    def executions(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._executions)

    def is_triggered(self, splice_id: str) -> bool:
        with self._lock:
            return splice_id in self._triggered

    def snapshot(self) -> dict:
        with self._lock:
            entries = list(self._pending.values())
            entries.sort(key=lambda entry: entry["splice_pts_ms"])
            state = {
                "pending": entries,
                "triggered_ids": sorted(self._triggered),
                "executions": self._executions,
                "stats": self._stats,
            }
            return copy.deepcopy(state)

    def restore(self, state) -> None:
        with self._lock:
            pending, triggered, executions, stats = self._validate_state(state)
            self._pending = pending
            self._triggered = triggered
            self._executions = executions
            self._stats = stats

    @classmethod
    def _validate_state(cls, state: object):
        if not isinstance(state, dict):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "reason": "snapshot_must_be_dict",
            })

        pending_raw = state.get("pending")
        if not isinstance(pending_raw, list):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "pending",
                "reason": "must_be_list",
            })

        pending: dict[str, dict] = {}
        for item in pending_raw:
            entry = cls._validated_pending_entry(item)
            splice_id = entry["splice_id"]
            if splice_id in pending:
                raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                    "field": "pending",
                    "reason": "duplicate_splice_id",
                    "splice_id": splice_id,
                })
            pending[splice_id] = entry

        if "triggered_ids" in state:
            triggered_raw = state["triggered_ids"]
        else:
            triggered_raw = state.get("triggered")

        if isinstance(triggered_raw, (str, bytes)) or not isinstance(
                triggered_raw, (list, tuple, set, frozenset)):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "triggered_ids",
                "reason": "must_be_collection_of_strings",
            })

        triggered: set[str] = set()
        for splice_id in triggered_raw:
            if not isinstance(splice_id, str) or not splice_id:
                raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                    "field": "triggered_ids",
                    "reason": "must_be_non_empty_strings",
                })
            triggered.add(splice_id)

        executions_raw = state.get("executions")
        if not isinstance(executions_raw, list):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions",
                "reason": "must_be_list",
            })

        executions: list[dict] = []
        for item in executions_raw:
            executions.append(cls._validated_execution(item))

        stats_raw = state.get("stats")
        if not isinstance(stats_raw, dict):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "stats",
                "reason": "must_be_dict",
            })

        try:
            stats = copy.deepcopy(stats_raw)
        except Exception as exc:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "stats",
                "reason": "not_deep_copyable",
            }) from exc

        return pending, triggered, executions, stats

    @classmethod
    def _validated_pending_entry(cls, item: object) -> dict:
        required = (
            "splice_id",
            "splice_pts_ms",
            "requested_duration_ms",
            "effective_duration_ms",
            "end_pts_ms",
        )
        if not isinstance(item, dict) or any(
                key not in item for key in required):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "pending",
                "reason": "entry_missing_required_fields",
            })

        splice_id = item["splice_id"]
        splice_pts_ms = item["splice_pts_ms"]
        requested_duration_ms = item["requested_duration_ms"]
        effective_duration_ms = item["effective_duration_ms"]
        end_pts_ms = item["end_pts_ms"]

        if not isinstance(splice_id, str) or not splice_id:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "pending.splice_id",
                "reason": "must_be_non_empty_string",
            })
        if not cls._is_int(splice_pts_ms) or splice_pts_ms < 0:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "pending.splice_pts_ms",
                "reason": "must_be_non_negative_integer",
            })
        for field_name, value in (
            ("requested_duration_ms", requested_duration_ms),
            ("effective_duration_ms", effective_duration_ms),
        ):
            if not cls._is_int(value) or value < 0:
                raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                    "field": "pending." + field_name,
                    "reason": "must_be_non_negative_integer",
                })
        if not cls._is_int(end_pts_ms) or end_pts_ms < splice_pts_ms:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "pending.end_pts_ms",
                "reason": "must_be_at_least_splice_pts_ms",
            })

        return {key: copy.deepcopy(item[key]) for key in required}

    @classmethod
    def _validated_execution(cls, item: object) -> dict:
        required = (
            "splice_id",
            "splice_pts_ms",
            "triggered_pts_ms",
            "offset_ms",
            "effective_duration_ms",
            "end_pts_ms",
        )
        if not isinstance(item, dict) or any(
                key not in item for key in required):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions",
                "reason": "record_missing_required_fields",
            })

        splice_id = item["splice_id"]
        splice_pts_ms = item["splice_pts_ms"]
        triggered_pts_ms = item["triggered_pts_ms"]
        offset_ms = item["offset_ms"]
        effective_duration_ms = item["effective_duration_ms"]
        end_pts_ms = item["end_pts_ms"]

        if not isinstance(splice_id, str) or not splice_id:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions.splice_id",
                "reason": "must_be_non_empty_string",
            })
        if not cls._is_int(splice_pts_ms) or splice_pts_ms < 0:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions.splice_pts_ms",
                "reason": "must_be_non_negative_integer",
            })
        if not cls._is_int(triggered_pts_ms):
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions.triggered_pts_ms",
                "reason": "must_be_integer",
            })
        if not cls._is_int(offset_ms) or offset_ms < 0:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions.offset_ms",
                "reason": "must_be_non_negative_integer",
            })
        if not cls._is_int(effective_duration_ms) or effective_duration_ms < 0:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions.effective_duration_ms",
                "reason": "must_be_non_negative_integer",
            })
        if not cls._is_int(end_pts_ms) or end_pts_ms < splice_pts_ms:
            raise Scte35SpliceError("SPLICE_SNAPSHOT_INVALID", {
                "field": "executions.end_pts_ms",
                "reason": "must_be_at_least_splice_pts_ms",
            })

        return {key: copy.deepcopy(item[key]) for key in required}

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = self._fresh_stats()
