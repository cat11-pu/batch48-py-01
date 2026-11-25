"""Flow export guard for a NetFlow collector."""

from __future__ import annotations

import copy
import threading


class ExportGuardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class NetflowExportGuard:
    """Age flows, manage export templates and drain an export queue."""

    UINT64_MAX = (1 << 64) - 1

    def __init__(self, *, active_timeout: int = 30, interim_interval: int = 60,
                 max_queue: int = 512, max_flows: int = 10000,
                 sampling_rate: int = 1, clock=None) -> None:
        self.active_timeout = active_timeout
        self.interim_interval = interim_interval
        self.max_queue = max_queue
        self.max_flows = max_flows
        self.sampling_rate = sampling_rate
        self._clock = clock
        self._lock = threading.RLock()
        self._templates: dict[int, list[str]] = {}
        self._template_versions: dict[int, int] = {}
        self._flows: dict[str, dict] = {}
        self._sequence = 0
        self._queue: list[dict] = []
        self._dropped = 0
        self._stat_packets = 0
        self._stat_bytes = 0
        self._stat_packets_exported = 0

    def _now(self, now):
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        raise ExportGuardError("CLOCK_MISSING", "a timestamp is required")

    @staticmethod
    def _flow_key(src: str, dst: str, src_port: int, dst_port: int,
                  protocol: str) -> str:
        return f"{src}:{src_port}->{dst}:{dst_port}/{protocol}"

    @staticmethod
    def _flow_record(flow: dict) -> dict:
        return {
            "flow_key": flow["flow_key"],
            "packets": flow["packets"],
            "bytes": flow["bytes"],
            "first_seen": flow["first_seen"],
            "last_seen": flow["last_seen"],
        }

    def _estimated_bytes(self, total_bytes: int) -> int:
        scaled = total_bytes * self.sampling_rate
        if scaled > self.UINT64_MAX:
            return self.UINT64_MAX
        return scaled

    def _export_record(self, flow: dict) -> dict:
        record = self._flow_record(flow)
        record["estimated_bytes"] = self._estimated_bytes(flow["bytes"])
        return record

    def _enqueue(self, packet: dict) -> dict:
        """Append an export packet, dropping the oldest one when full."""
        if self.max_queue > 0 and len(self._queue) >= self.max_queue:
            self._queue.pop(0)
            self._dropped += 1
        if self.max_queue > 0:
            self._queue.append(packet)
        else:
            self._dropped += 1
        return packet

    def _next_template(self) -> tuple[int, int]:
        if not self._templates:
            raise ExportGuardError(
                "TEMPLATE_UNKNOWN", "no template registered")
        template_id = min(self._templates)
        return template_id, self._template_versions[template_id]

    def _build_packet(self, records: list[dict]) -> dict:
        template_id, template_version = self._next_template()
        self._sequence += 1
        return self._enqueue({
            "sequence": self._sequence,
            "template_id": template_id,
            "template_version": template_version,
            "record_count": len(records),
            "records": records,
        })

    def add_packet(self, *, src: str, dst: str, src_port: int, dst_port: int,
                   protocol: str, length: int, tcp_flags: int = 0,
                   now=None) -> dict:
        with self._lock:
            timestamp = self._now(now)
            key = self._flow_key(src, dst, src_port, dst_port, protocol)
            flow = self._flows.get(key)
            if flow is None:
                if len(self._flows) >= self.max_flows:
                    raise ExportGuardError(
                        "FLOW_FULL", f"flow table full at {self.max_flows}")
                flow = {
                    "flow_key": key,
                    "packets": 0,
                    "bytes": 0,
                    "first_seen": timestamp,
                    "last_seen": timestamp,
                    "last_interim": timestamp,
                }
                self._flows[key] = flow
            flow["packets"] += 1
            flow["bytes"] += length
            flow["last_seen"] = timestamp
            self._stat_packets += 1
            self._stat_bytes += length
            return self._flow_record(flow)

    def close_flow(self, flow_key: str, *, now=None) -> dict:
        with self._lock:
            timestamp = self._now(now)
            flow = self._flows.pop(flow_key, None)
            if flow is None:
                raise ExportGuardError(
                    "FLOW_UNKNOWN", f"unknown flow: {flow_key}")
            try:
                self._next_template()
            except ExportGuardError:
                self._flows[flow_key] = flow
                raise
            flow["last_seen"] = timestamp
            packet = self._build_packet([self._export_record(flow)])
            self._stat_packets_exported += flow["packets"]
            return packet

    def set_template(self, template_id: int, fields: list[str]) -> bool:
        if not fields:
            raise ExportGuardError("TEMPLATE_INVALID", "field list is empty")
        if len(set(fields)) != len(fields):
            raise ExportGuardError(
                "TEMPLATE_INVALID", "duplicate fields in template")
        with self._lock:
            current = self._templates.get(template_id)
            if current is not None and current == list(fields):
                return False
            self._templates[template_id] = list(fields)
            self._template_versions[template_id] = (
                self._template_versions.get(template_id, 0) + 1)
            return True

    def export_due(self, *, now=None) -> list[dict]:
        with self._lock:
            timestamp = self._now(now)
            # A missing template blocks every export but keeps flows intact.
            self._next_template()
            exported: list[dict] = []
            for key in list(self._flows):
                flow = self._flows[key]
                idle_for = timestamp - flow["last_seen"]
                if idle_for >= self.active_timeout:
                    packet = self._build_packet(
                        [self._export_record(flow)])
                    self._stat_packets_exported += flow["packets"]
                    del self._flows[key]
                    exported.append(packet)
                elif timestamp - flow["last_interim"] >= self.interim_interval:
                    packet = self._build_packet(
                        [self._export_record(flow)])
                    self._stat_packets_exported += flow["packets"]
                    flow["last_interim"] = timestamp
                    exported.append(packet)
            return exported

    def flush(self, *, now=None) -> list[dict]:
        with self._lock:
            timestamp = self._now(now)
            if not self._flows:
                return []
            self._next_template()
            exported: list[dict] = []
            for key in list(self._flows):
                flow = self._flows.pop(key)
                flow["last_seen"] = timestamp
                packet = self._build_packet([self._export_record(flow)])
                self._stat_packets_exported += flow["packets"]
                exported.append(packet)
            return exported

    def active_flows(self, *, now=None) -> list[dict]:
        with self._lock:
            return [self._flow_record(flow) for flow in self._flows.values()]

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def dropped_count(self) -> int:
        with self._lock:
            return self._dropped

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "sequence": self._sequence,
                "templates": copy.deepcopy(self._templates),
                "template_versions": copy.deepcopy(self._template_versions),
                "flows": copy.deepcopy(self._flows),
            }

    def restore(self, state: dict) -> None:
        restored = self._validate_state(state)
        with self._lock:
            self._sequence = restored["sequence"]
            self._templates = restored["templates"]
            self._template_versions = restored["template_versions"]
            self._flows = restored["flows"]
            self._queue = []
            self._dropped = 0
            self._stat_packets = 0
            self._stat_bytes = 0
            self._stat_packets_exported = 0

    @staticmethod
    def _is_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _validate_state(cls, state: dict) -> dict:
        if not isinstance(state, dict):
            raise ExportGuardError("STATE_CORRUPT", "state must be a mapping")
        required = {"sequence", "templates", "template_versions", "flows"}
        if set(state.keys()) != required:
            raise ExportGuardError(
                "STATE_CORRUPT", "state keys do not match snapshot layout")

        sequence = state["sequence"]
        if not cls._is_int(sequence) or sequence < 0:
            raise ExportGuardError("STATE_CORRUPT", "invalid sequence")

        templates = state["templates"]
        versions = state["template_versions"]
        if not isinstance(templates, dict) or not isinstance(versions, dict):
            raise ExportGuardError("STATE_CORRUPT", "invalid template tables")
        if set(templates.keys()) != set(versions.keys()):
            raise ExportGuardError(
                "STATE_CORRUPT", "template ids and versions disagree")
        new_templates: dict[int, list[str]] = {}
        new_versions: dict[int, int] = {}
        for template_id, fields in templates.items():
            if not cls._is_int(template_id):
                raise ExportGuardError("STATE_CORRUPT", "invalid template id")
            if not isinstance(fields, list) or not fields:
                raise ExportGuardError(
                    "STATE_CORRUPT", "invalid template fields")
            if any(not isinstance(field, str) for field in fields):
                raise ExportGuardError(
                    "STATE_CORRUPT", "template fields must be strings")
            if len(set(fields)) != len(fields):
                raise ExportGuardError(
                    "STATE_CORRUPT", "duplicate template fields")
            version = versions[template_id]
            if not cls._is_int(version) or version < 1:
                raise ExportGuardError(
                    "STATE_CORRUPT", "invalid template version")
            new_templates[template_id] = list(fields)
            new_versions[template_id] = version

        flows = state["flows"]
        if not isinstance(flows, dict):
            raise ExportGuardError("STATE_CORRUPT", "invalid flow table")
        new_flows: dict[str, dict] = {}
        flow_keys = ("flow_key", "packets", "bytes", "first_seen",
                     "last_seen", "last_interim")
        for key, flow in flows.items():
            if not isinstance(key, str) or not isinstance(flow, dict):
                raise ExportGuardError("STATE_CORRUPT", "invalid flow entry")
            if set(flow.keys()) != set(flow_keys):
                raise ExportGuardError(
                    "STATE_CORRUPT", "flow entry keys are malformed")
            if flow["flow_key"] != key:
                raise ExportGuardError(
                    "STATE_CORRUPT", "flow key mismatch")
            for field in ("packets", "bytes", "first_seen",
                          "last_seen", "last_interim"):
                if not cls._is_int(flow[field]):
                    raise ExportGuardError(
                        "STATE_CORRUPT", f"invalid flow field {field}")
            if flow["packets"] < 1 or flow["bytes"] < 0:
                raise ExportGuardError(
                    "STATE_CORRUPT", "flow counters are invalid")
            if not (flow["first_seen"] <= flow["last_interim"]
                    <= flow["last_seen"]):
                raise ExportGuardError(
                    "STATE_CORRUPT", "flow timestamps are invalid")
            new_flows[key] = {
                "flow_key": flow["flow_key"],
                "packets": flow["packets"],
                "bytes": flow["bytes"],
                "first_seen": flow["first_seen"],
                "last_seen": flow["last_seen"],
                "last_interim": flow["last_interim"],
            }

        return {
            "sequence": sequence,
            "templates": new_templates,
            "template_versions": new_versions,
            "flows": new_flows,
        }

    def stats(self) -> dict:
        with self._lock:
            return {
                "packets": self._stat_packets,
                "bytes": self._stat_bytes,
                "packets_exported": self._stat_packets_exported,
                "sequence": self._sequence,
                "dropped": self._dropped,
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._stat_packets = 0
            self._stat_bytes = 0
            self._stat_packets_exported = 0
            self._dropped = 0
