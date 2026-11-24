"""Multipath subflow scheduler with DSS mapping dedup and backup promotion."""

from __future__ import annotations

import copy
import threading


MPTCP_INVALID_ARGUMENT = "MPTCP_INVALID_ARGUMENT"
MPTCP_DUPLICATE_SUBFLOW = "MPTCP_DUPLICATE_SUBFLOW"
MPTCP_UNKNOWN_SUBFLOW = "MPTCP_UNKNOWN_SUBFLOW"
MPTCP_SUBFLOW_LIMIT = "MPTCP_SUBFLOW_LIMIT"
MPTCP_INVALID_ROLE = "MPTCP_INVALID_ROLE"
MPTCP_BACKUP_NOT_ELIGIBLE = "MPTCP_BACKUP_NOT_ELIGIBLE"
MPTCP_HYSTERESIS_PENDING = "MPTCP_HYSTERESIS_PENDING"
MPTCP_NO_SUBFLOW = "MPTCP_NO_SUBFLOW"
MPTCP_CWND_EXHAUSTED = "MPTCP_CWND_EXHAUSTED"
MPTCP_INVALID_MAPPING = "MPTCP_INVALID_MAPPING"
MPTCP_MAPPING_LIMIT = "MPTCP_MAPPING_LIMIT"
MPTCP_SNAPSHOT_INVALID = "MPTCP_SNAPSHOT_INVALID"

_SNAPSHOT_VERSION = 1
_SNAPSHOT_KEYS = {"version", "subflows", "mappings", "ranges", "stats"}
_STAT_KEYS = {
    "subflows",
    "scheduled",
    "acked_bytes",
    "delivered_bytes",
    "mappings",
    "removed",
}
_COUNTER_KEYS = (
    "scheduled",
    "acked_bytes",
    "delivered_bytes",
    "mappings",
    "removed",
)
_SCHEDULABLE_STATES = {"active", "backup_active"}
_VALID_ROLES = {"primary", "backup"}
_VALID_STATES = {"active", "standby", "failed", "backup_active"}


class MptcpError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, float) and not isinstance(value, bool)
    )


class MptcpScheduler:
    """Track subflows, RTT estimates, DSS mappings and backup promotion."""

    def __init__(
        self,
        *,
        failure_threshold: int = 3,
        hysteresis_ms: int = 2000,
        max_subflows: int = 8,
        max_mappings: int = 4096,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.hysteresis_ms = hysteresis_ms
        self.max_subflows = max_subflows
        self.max_mappings = max_mappings
        self._lock = threading.RLock()
        self._subflows: dict[str, dict] = {}
        self._mappings: list[dict] = []
        self._ranges: list[list[int]] = []
        self._next_seq = 0
        self._counters = {key: 0 for key in _COUNTER_KEYS}

    @staticmethod
    def _error(code: str, detail: object = None) -> MptcpError:
        return MptcpError(code, detail)

    def _require_subflow_locked(self, sid: str) -> dict:
        subflow = self._subflows.get(sid)
        if subflow is None:
            raise self._error(MPTCP_UNKNOWN_SUBFLOW, sid)
        return subflow

    def _primary_subflow_locked(self) -> dict | None:
        for subflow in self._subflows.values():
            if subflow["role"] == "primary":
                return subflow
        return None

    @staticmethod
    def _merge_ranges(ranges: list[list[int]]) -> list[list[int]]:
        if not ranges:
            return []
        ordered = sorted(
            ([int(start), int(end)] for start, end in ranges),
            key=lambda item: (item[0], item[1]),
        )
        merged = [ordered[0][:]]
        for start, end in ordered[1:]:
            last = merged[-1]
            if start <= last[1]:
                if end > last[1]:
                    last[1] = end
            else:
                merged.append([start, end])
        return merged

    @staticmethod
    def _range_length(ranges: list[list[int]]) -> int:
        return sum(end - start for start, end in ranges)

    def add_subflow(
        self,
        sid: str,
        *,
        path_id: int | None = None,
        priority: int = 0,
    ) -> dict:
        with self._lock:
            if not isinstance(sid, str) or sid == "":
                raise self._error(
                    MPTCP_INVALID_ARGUMENT, "sid must be a non-empty string"
                )
            if sid in self._subflows:
                raise self._error(MPTCP_DUPLICATE_SUBFLOW, sid)
            if len(self._subflows) >= self.max_subflows:
                raise self._error(MPTCP_SUBFLOW_LIMIT, sid)

            is_primary = not self._subflows
            subflow = {
                "sid": sid,
                "path_id": path_id,
                "role": "primary" if is_primary else "backup",
                "state": "active" if is_primary else "standby",
                "priority": priority,
                "cwnd": 1000,
                "rtt_ms": 100,
                "loss": 0.0,
                "inflight": 0,
                "fails": 0,
                "healthy_since_ms": None,
                "enabled_at_ms": None,
                "seq_floor": 0,
                "pending": [],
            }
            self._subflows[sid] = subflow
            return {
                "sid": sid,
                "path_id": path_id,
                "role": subflow["role"],
                "state": subflow["state"],
            }

    def remove_subflow(self, sid: str) -> dict:
        with self._lock:
            subflow = self._require_subflow_locked(sid)
            inflight_released = subflow["inflight"]
            segments = sorted(
                ([seq, length] for seq, length in subflow["pending"]),
                key=lambda item: item[0],
            )

            del self._subflows[sid]
            self._counters["removed"] += 1

            survivors = list(self._subflows.values())
            for survivor in survivors:
                survivor["seq_floor"] = max(
                    survivor.get("seq_floor", 0), self._next_seq
                )

            for seq, length in segments:
                target = self._retransmission_target_locked(length)
                if target is None and survivors:
                    target = min(
                        survivors,
                        key=lambda item: (
                            item["state"] == "failed",
                            item["rtt_ms"],
                            item["sid"],
                        ),
                    )
                if target is not None:
                    target["inflight"] += length
                    target["pending"].append([seq, length])
                    target["pending"].sort(key=lambda item: item[0])
                    target["seq_floor"] = max(
                        target.get("seq_floor", 0), seq + length
                    )

            return {"sid": sid, "inflight_released": inflight_released}

    def _retransmission_target_locked(self, length: int) -> dict | None:
        candidates = [
            subflow
            for subflow in self._subflows.values()
            if subflow["state"] in _SCHEDULABLE_STATES
        ]
        fitting = [
            subflow
            for subflow in candidates
            if subflow["cwnd"] - subflow["inflight"] >= length
        ]
        pool = fitting or candidates
        if not pool:
            return None
        return min(pool, key=lambda item: (item["rtt_ms"], item["sid"]))

    def report_path(
        self,
        sid: str,
        *,
        ok: bool,
        rtt_ms: int = 0,
        loss: float = 0.0,
        now_ms: int = 0,
    ) -> dict:
        with self._lock:
            subflow = self._require_subflow_locked(sid)
            subflow["rtt_ms"] = rtt_ms
            subflow["loss"] = loss

            if ok:
                subflow["fails"] = 0
                if subflow["state"] == "failed":
                    subflow["state"] = "active"
                if (
                    subflow["role"] == "primary"
                    and subflow["healthy_since_ms"] is None
                ):
                    subflow["healthy_since_ms"] = now_ms
            else:
                subflow["fails"] += 1
                subflow["healthy_since_ms"] = None
                if subflow["fails"] >= self.failure_threshold:
                    subflow["state"] = "failed"

            return {
                "sid": sid,
                "rtt_ms": subflow["rtt_ms"],
                "loss": subflow["loss"],
                "fails": subflow["fails"],
                "state": subflow["state"],
            }

    def set_cwnd(self, sid: str, cwnd: int) -> dict:
        with self._lock:
            if not _is_int(cwnd) or cwnd <= 0:
                raise self._error(
                    MPTCP_INVALID_ARGUMENT, "cwnd must be a positive integer"
                )
            subflow = self._require_subflow_locked(sid)
            subflow["cwnd"] = cwnd
            return {"sid": sid, "cwnd": cwnd}

    def schedule(self, size: int, *, now_ms: int = 0) -> dict:
        with self._lock:
            if not _is_int(size) or size <= 0:
                raise self._error(
                    MPTCP_INVALID_ARGUMENT, "size must be a positive integer"
                )

            schedulable = [
                subflow
                for subflow in self._subflows.values()
                if subflow["state"] in _SCHEDULABLE_STATES
            ]
            if not schedulable:
                raise self._error(MPTCP_NO_SUBFLOW)

            fitting = [
                subflow
                for subflow in schedulable
                if subflow["cwnd"] - subflow["inflight"] >= size
            ]
            if not fitting:
                raise self._error(MPTCP_CWND_EXHAUSTED)

            selected = min(fitting, key=lambda item: (item["rtt_ms"], item["sid"]))
            selected["inflight"] += size
            seq = self._next_seq
            self._next_seq += size
            selected["pending"].append([seq, size])
            selected["seq_floor"] = self._next_seq
            self._counters["scheduled"] += 1

            return {
                "size": size,
                "sid": selected["sid"],
                "inflight": selected["inflight"],
            }

    def ack(self, subflow: str, length: int) -> dict:
        with self._lock:
            record = self._require_subflow_locked(subflow)
            if not _is_int(length) or length < 0:
                raise self._error(
                    MPTCP_INVALID_ARGUMENT, "length must be a non-negative integer"
                )

            previous_inflight = record["inflight"]
            acked = min(length, previous_inflight)
            remaining_ack = acked

            while record["pending"] and remaining_ack > 0:
                seq, pending_length = record["pending"][0]
                consumed = min(pending_length, remaining_ack)
                record["pending"][0][0] = seq + consumed
                record["pending"][0][1] = pending_length - consumed
                remaining_ack -= consumed
                if record["pending"][0][1] == 0:
                    record["pending"].pop(0)

            record["inflight"] = previous_inflight - acked
            self._counters["acked_bytes"] += acked
            return {
                "subflow": subflow,
                "inflight": record["inflight"],
                "acked": acked,
            }

    def dss_map(self, seq: int, length: int, subflow: str) -> dict:
        with self._lock:
            if not _is_int(seq):
                raise self._error(MPTCP_INVALID_MAPPING, "seq must be an integer")
            if not _is_int(length) or length <= 0:
                raise self._error(
                    MPTCP_INVALID_MAPPING, "length must be a positive integer"
                )

            self._require_subflow_locked(subflow)
            if len(self._mappings) >= self.max_mappings:
                raise self._error(MPTCP_MAPPING_LIMIT)

            end = seq + length
            before = self._range_length(self._ranges)
            merged = self._merge_ranges(self._ranges + [[seq, end]])
            delivered = self._range_length(merged) - before
            self._ranges = merged

            self._mappings.append({"seq": seq, "length": length, "subflow": subflow})
            self._counters["mappings"] += 1
            self._counters["delivered_bytes"] += delivered

            return {
                "seq": seq,
                "length": length,
                "subflow": subflow,
                "delivered": delivered,
                "ranges": [list(range_item) for range_item in self._ranges],
            }

    def enable_backup(self, sid: str, *, now_ms: int = 0) -> dict:
        with self._lock:
            subflow = self._require_subflow_locked(sid)
            if subflow["role"] == "primary":
                raise self._error(MPTCP_INVALID_ROLE, sid)
            if subflow["state"] != "standby":
                raise self._error(MPTCP_BACKUP_NOT_ELIGIBLE, sid)

            primary = self._primary_subflow_locked()
            if primary is None or primary["fails"] < self.failure_threshold:
                raise self._error(MPTCP_BACKUP_NOT_ELIGIBLE, sid)

            subflow["state"] = "backup_active"
            subflow["enabled_at_ms"] = now_ms
            return {"sid": sid, "state": subflow["state"]}

    def release_backup(self, sid: str, *, now_ms: int = 0) -> dict:
        with self._lock:
            subflow = self._require_subflow_locked(sid)
            if subflow["role"] == "primary":
                raise self._error(MPTCP_INVALID_ROLE, sid)
            if subflow["state"] == "standby":
                return {"sid": sid, "state": "standby"}

            primary = self._primary_subflow_locked()
            healthy_duration = None
            if primary is not None and primary["healthy_since_ms"] is not None:
                healthy_duration = now_ms - primary["healthy_since_ms"]

            if healthy_duration is None or healthy_duration < self.hysteresis_ms:
                raise self._error(MPTCP_HYSTERESIS_PENDING, sid)

            subflow["state"] = "standby"
            subflow["enabled_at_ms"] = None
            return {"sid": sid, "state": "standby"}

    def list_subflows(self) -> list:
        with self._lock:
            return [
                {
                    "sid": subflow["sid"],
                    "path_id": subflow["path_id"],
                    "role": subflow["role"],
                    "state": subflow["state"],
                    "cwnd": subflow["cwnd"],
                    "rtt_ms": subflow["rtt_ms"],
                    "inflight": subflow["inflight"],
                }
                for sid, subflow in sorted(
                    self._subflows.items(), key=lambda item: item[0]
                )
            ]

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": _SNAPSHOT_VERSION,
                "subflows": {
                    sid: {
                        "sid": subflow["sid"],
                        "path_id": subflow["path_id"],
                        "role": subflow["role"],
                        "state": subflow["state"],
                        "priority": subflow["priority"],
                        "cwnd": subflow["cwnd"],
                        "rtt_ms": subflow["rtt_ms"],
                        "loss": subflow["loss"],
                        "inflight": subflow["inflight"],
                        "fails": subflow["fails"],
                        "healthy_since_ms": subflow["healthy_since_ms"],
                        "enabled_at_ms": subflow["enabled_at_ms"],
                        "seq_floor": subflow["seq_floor"],
                        "pending": [
                            list(segment) for segment in subflow["pending"]
                        ],
                    }
                    for sid, subflow in self._subflows.items()
                },
                "mappings": [dict(mapping) for mapping in self._mappings],
                "ranges": [list(range_item) for range_item in self._ranges],
                "stats": self.stats(),
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                restored = self._build_restore_state(state)
            except MptcpError:
                raise
            except Exception as exc:
                raise self._error(MPTCP_SNAPSHOT_INVALID) from exc

            self._subflows = restored["subflows"]
            self._mappings = restored["mappings"]
            self._ranges = restored["ranges"]
            self._counters = restored["counters"]
            self._next_seq = restored["next_seq"]

    def _build_restore_state(self, state: object) -> dict:
        data = copy.deepcopy(state)
        if not isinstance(data, dict) or set(data.keys()) != _SNAPSHOT_KEYS:
            raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid snapshot keys")
        if not _is_int(data["version"]) or data["version"] != _SNAPSHOT_VERSION:
            raise self._error(
                MPTCP_SNAPSHOT_INVALID, "unsupported snapshot version"
            )

        raw_subflows = data["subflows"]
        if not isinstance(raw_subflows, dict):
            raise self._error(MPTCP_SNAPSHOT_INVALID, "subflows must be a mapping")
        if len(raw_subflows) > self.max_subflows:
            raise self._error(MPTCP_SNAPSHOT_INVALID, "too many subflows")

        subflows: dict[str, dict] = {}
        next_seq = 0
        primary_count = 0

        for key, raw in raw_subflows.items():
            if not isinstance(key, str) or key == "" or not isinstance(raw, dict):
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid subflow record"
                )
            sid = raw.get("sid", key)
            if sid != key or not isinstance(sid, str) or sid == "":
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "subflow sid mismatch"
                )

            path_id = raw.get("path_id")
            if path_id is not None and not _is_int(path_id):
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid path_id")

            role = raw.get("role")
            if role not in _VALID_ROLES:
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid role")
            if role == "primary":
                primary_count += 1

            state_name = raw.get("state")
            if state_name not in _VALID_STATES:
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid state")

            priority = raw.get("priority", 0)
            if not _is_int(priority):
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid priority")

            cwnd = raw.get("cwnd")
            if not _is_int(cwnd) or cwnd <= 0:
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid cwnd")

            rtt_ms = raw.get("rtt_ms")
            if not _is_number(rtt_ms):
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid rtt_ms")

            loss = raw.get("loss", 0.0)
            if not _is_number(loss):
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid loss")

            inflight = raw.get("inflight")
            if not _is_int(inflight) or inflight < 0:
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid inflight")

            fails = raw.get("fails", 0)
            if not _is_int(fails) or fails < 0:
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid fails")

            healthy_since_ms = raw.get("healthy_since_ms")
            if healthy_since_ms is not None and not _is_int(healthy_since_ms):
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid healthy timestamp"
                )

            enabled_at_ms = raw.get("enabled_at_ms")
            if enabled_at_ms is not None and not _is_int(enabled_at_ms):
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid enabled timestamp"
                )

            seq_floor = raw.get("seq_floor", 0)
            if not _is_int(seq_floor) or seq_floor < 0:
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid sequence floor"
                )

            raw_pending = raw.get("pending", [])
            if not isinstance(raw_pending, list):
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid pending segments"
                )

            pending: list[list[int]] = []
            for segment in raw_pending:
                if not isinstance(segment, list) or len(segment) != 2:
                    raise self._error(
                        MPTCP_SNAPSHOT_INVALID, "invalid pending segment"
                    )
                seq, length = segment
                if (
                    not _is_int(seq)
                    or seq < 0
                    or not _is_int(length)
                    or length <= 0
                ):
                    raise self._error(
                        MPTCP_SNAPSHOT_INVALID, "invalid pending segment"
                    )
                pending.append([seq, length])
                next_seq = max(next_seq, seq + length)

            pending.sort(key=lambda item: item[0])
            next_seq = max(next_seq, seq_floor)
            subflows[sid] = {
                "sid": sid,
                "path_id": path_id,
                "role": role,
                "state": state_name,
                "priority": priority,
                "cwnd": cwnd,
                "rtt_ms": rtt_ms,
                "loss": loss,
                "inflight": inflight,
                "fails": fails,
                "healthy_since_ms": healthy_since_ms,
                "enabled_at_ms": enabled_at_ms,
                "seq_floor": seq_floor,
                "pending": pending,
            }

        if primary_count > 1:
            raise self._error(
                MPTCP_SNAPSHOT_INVALID, "multiple primary subflows"
            )

        raw_mappings = data["mappings"]
        if not isinstance(raw_mappings, list):
            raise self._error(MPTCP_SNAPSHOT_INVALID, "mappings must be a list")
        if len(raw_mappings) > self.max_mappings:
            raise self._error(MPTCP_SNAPSHOT_INVALID, "too many mappings")

        mappings: list[dict] = []
        for mapping in raw_mappings:
            if not isinstance(mapping, dict):
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid mapping")
            seq = mapping.get("seq")
            length = mapping.get("length")
            subflow = mapping.get("subflow")
            if (
                not _is_int(seq)
                or not _is_int(length)
                or length <= 0
            ):
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid mapping sequence"
                )
            if not isinstance(subflow, str) or subflow == "":
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, "invalid mapping subflow"
                )
            mappings.append(
                {"seq": seq, "length": length, "subflow": subflow}
            )

        raw_ranges = data["ranges"]
        if not isinstance(raw_ranges, list):
            raise self._error(MPTCP_SNAPSHOT_INVALID, "ranges must be a list")

        ranges: list[list[int]] = []
        for range_item in raw_ranges:
            if not isinstance(range_item, list) or len(range_item) != 2:
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid range")
            start, end = range_item
            if (
                not _is_int(start)
                or not _is_int(end)
                or start >= end
            ):
                raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid range boundary")
            ranges.append([start, end])
        ranges = self._merge_ranges(ranges)

        raw_stats = data["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats.keys()) != _STAT_KEYS:
            raise self._error(MPTCP_SNAPSHOT_INVALID, "invalid stats")
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not _is_int(value) or value < 0:
                raise self._error(
                    MPTCP_SNAPSHOT_INVALID, f"invalid stat: {key}"
                )
        if raw_stats["subflows"] != len(subflows):
            raise self._error(
                MPTCP_SNAPSHOT_INVALID, "subflow stat mismatch"
            )

        counters = {key: raw_stats[key] for key in _COUNTER_KEYS}
        return {
            "subflows": subflows,
            "mappings": mappings,
            "ranges": ranges,
            "counters": counters,
            "next_seq": next_seq,
        }

    def stats(self) -> dict:
        with self._lock:
            return {
                "subflows": len(self._subflows),
                "scheduled": self._counters["scheduled"],
                "acked_bytes": self._counters["acked_bytes"],
                "delivered_bytes": self._counters["delivered_bytes"],
                "mappings": self._counters["mappings"],
                "removed": self._counters["removed"],
            }

    def reset_stats(self) -> None:
        with self._lock:
            for key in _COUNTER_KEYS:
                self._counters[key] = 0
