"""L4 consistent-hash load balancer with backend drain and health tracking."""

from __future__ import annotations

import bisect
import copy
import hashlib
import threading


class LbError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_BACKEND_STATES = ("active", "draining", "down", "disabled")


def _stable_hash(text: str) -> int:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


class LbDrainGuard:
    """Keep a virtual-node hash ring, backend weights and drain progress."""

    def __init__(self, *, virtual_nodes: int = 160, failure_threshold: int = 3,
                 success_threshold: int = 2, max_connections: int = 1000) -> None:
        self.virtual_nodes = virtual_nodes
        self.failure_threshold = failure_threshold
        self.success_threshold = success_threshold
        self.max_connections = max_connections
        self._lock = threading.RLock()
        self._backends = {}
        self._ring = []
        self._connections = {}
        self._accepted = 0
        self._closed = 0
        self._forced = 0
        self._forced_log = []

    def _rebuild_ring(self) -> None:
        ring = []
        for name, record in self._backends.items():
            if record["state"] != "active" or record["weight"] <= 0:
                continue
            for index in range(record["weight"] * self.virtual_nodes):
                ring.append((_stable_hash("%s#%d" % (name, index)), name))
        ring.sort()
        self._ring = ring

    def _backend_view(self, record: dict) -> dict:
        return {
            "name": record["name"],
            "weight": record["weight"],
            "vnodes": record["weight"] * self.virtual_nodes,
            "state": record["state"],
        }

    def add_backend(self, name: str, *, weight: int = 1) -> dict:
        with self._lock:
            if name in self._backends:
                raise LbError("LB_BACKEND_EXISTS", name)
            if weight < 0:
                raise LbError("LB_INVALID_WEIGHT", weight)
            record = {
                "name": name,
                "weight": weight,
                "state": "active" if weight > 0 else "disabled",
                "failures": 0,
                "successes": 0,
                "deadline_ms": None,
            }
            self._backends[name] = record
            self._rebuild_ring()
            return self._backend_view(record)

    def remove_backend(self, name: str) -> dict:
        with self._lock:
            record = self._backends.get(name)
            if record is None:
                raise LbError("LB_UNKNOWN_BACKEND", name)
            if len(self._backends) == 1:
                raise LbError("LB_LAST_BACKEND", name)
            vnodes_removed = sum(1 for _, owner in self._ring if owner == name)
            del self._backends[name]
            self._rebuild_ring()
            return {"name": name, "vnodes_removed": vnodes_removed}

    def set_weight(self, name: str, weight: int) -> dict:
        with self._lock:
            record = self._backends.get(name)
            if record is None:
                raise LbError("LB_UNKNOWN_BACKEND", name)
            if weight < 0:
                raise LbError("LB_INVALID_WEIGHT", weight)
            record["weight"] = weight
            if weight == 0:
                if record["state"] != "draining":
                    record["state"] = "disabled"
            elif record["state"] == "disabled":
                record["state"] = "active"
                record["failures"] = 0
                record["successes"] = 0
            self._rebuild_ring()
            return self._backend_view(record)

    def drain(self, name: str, *, deadline_ms: int) -> dict:
        with self._lock:
            record = self._backends.get(name)
            if record is None:
                raise LbError("LB_UNKNOWN_BACKEND", name)
            if record["state"] == "draining":
                return {"name": name, "state": "draining",
                        "deadline_ms": record["deadline_ms"]}
            record["state"] = "draining"
            record["deadline_ms"] = deadline_ms
            record["failures"] = 0
            record["successes"] = 0
            self._rebuild_ring()
            return {"name": name, "state": "draining", "deadline_ms": deadline_ms}

    def route(self, key: str) -> str:
        with self._lock:
            if not self._ring:
                raise LbError("LB_NO_BACKEND", key)
            point = _stable_hash(key)
            index = bisect.bisect_left(self._ring, (point, ""))
            if index == len(self._ring):
                index = 0
            return self._ring[index][1]

    def accept_connection(self, backend: str, conn_id: str, *, now_ms: int) -> dict:
        with self._lock:
            record = self._backends.get(backend)
            if record is None:
                raise LbError("LB_UNKNOWN_BACKEND", backend)
            if conn_id in self._connections:
                raise LbError("LB_DUPLICATE_CONNECTION", conn_id)
            if record["state"] == "draining":
                raise LbError("LB_BACKEND_DRAINING", backend)
            if len(self._connections) >= self.max_connections:
                raise LbError("LB_CONNECTION_LIMIT", conn_id)
            self._connections[conn_id] = {
                "conn_id": conn_id,
                "backend": backend,
                "opened_ms": now_ms,
            }
            self._accepted += 1
            return {"conn_id": conn_id, "backend": backend, "opened_ms": now_ms}

    def close_connection(self, conn_id: str, *, now_ms: int) -> dict:
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None:
                raise LbError("LB_UNKNOWN_CONNECTION", conn_id)
            del self._connections[conn_id]
            record = self._backends.get(conn["backend"])
            forced = (
                record is not None
                and record["state"] == "draining"
                and record["deadline_ms"] is not None
                and now_ms >= record["deadline_ms"]
            )
            if forced:
                self._forced += 1
                self._forced_log.append({
                    "conn_id": conn_id,
                    "backend": conn["backend"],
                    "closed_ms": now_ms,
                })
            else:
                self._closed += 1
            return {"conn_id": conn_id, "closed_ms": now_ms, "forced": forced}

    def expire_connections(self, now_ms: int) -> dict:
        with self._lock:
            closed = 0
            forced = 0
            for conn_id, conn in list(self._connections.items()):
                record = self._backends.get(conn["backend"])
                if (
                    record is None
                    or record["state"] != "draining"
                    or record["deadline_ms"] is None
                    or now_ms < record["deadline_ms"]
                ):
                    continue
                del self._connections[conn_id]
                closed += 1
                forced += 1
                self._forced += 1
                self._forced_log.append({
                    "conn_id": conn_id,
                    "backend": conn["backend"],
                    "closed_ms": now_ms,
                })
            return {"closed": closed, "forced": forced}

    def report_health(self, name: str, ok: bool) -> dict:
        with self._lock:
            record = self._backends.get(name)
            if record is None:
                raise LbError("LB_UNKNOWN_BACKEND", name)
            changed = False
            if ok:
                record["successes"] += 1
                record["failures"] = 0
                if (record["state"] == "down"
                        and record["successes"] >= self.success_threshold):
                    record["state"] = "active"
                    record["failures"] = 0
                    record["successes"] = 0
                    changed = True
                    self._rebuild_ring()
            else:
                record["failures"] += 1
                record["successes"] = 0
                if (record["state"] == "active"
                        and record["failures"] >= self.failure_threshold):
                    record["state"] = "down"
                    record["failures"] = 0
                    record["successes"] = 0
                    changed = True
                    self._rebuild_ring()
            return {
                "name": name,
                "state": record["state"],
                "failures": record["failures"],
                "successes": record["successes"],
                "changed": changed,
            }

    def list_backends(self) -> list:
        with self._lock:
            return [self._backend_view(self._backends[name])
                    for name in sorted(self._backends)]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "backends": copy.deepcopy(self._backends),
                "ring": copy.deepcopy(self._ring),
                "connections": copy.deepcopy(self._connections),
                "stats": {
                    "accepted": self._accepted,
                    "closed": self._closed,
                    "forced": self._forced,
                },
            }

    @staticmethod
    def _validate_snapshot(state: dict) -> None:
        def invalid(detail=None):
            raise LbError("LB_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict) or state.get("version") != 1:
            invalid("version")
        for key in ("backends", "ring", "connections", "stats"):
            if key not in state:
                invalid(key)
        backends = state["backends"]
        ring = state["ring"]
        connections = state["connections"]
        stats = state["stats"]
        if not isinstance(backends, dict):
            invalid("backends")
        for name, record in backends.items():
            if not isinstance(name, str) or not isinstance(record, dict):
                invalid("backends")
            if record.get("name") != name:
                invalid("backends")
            if (not isinstance(record.get("weight"), int)
                    or isinstance(record.get("weight"), bool)
                    or record["weight"] < 0):
                invalid("backends")
            if record.get("state") not in _BACKEND_STATES:
                invalid("backends")
            for counter in ("failures", "successes"):
                if (not isinstance(record.get(counter), int)
                        or isinstance(record.get(counter), bool)
                        or record[counter] < 0):
                    invalid("backends")
            deadline = record.get("deadline_ms")
            if deadline is not None and (not isinstance(deadline, int)
                                         or isinstance(deadline, bool)):
                invalid("backends")
        if not isinstance(ring, list):
            invalid("ring")
        for entry in ring:
            if (not isinstance(entry, (list, tuple)) or len(entry) != 2
                    or not isinstance(entry[0], int)
                    or isinstance(entry[0], bool)
                    or not isinstance(entry[1], str)
                    or entry[1] not in backends):
                invalid("ring")
        if not isinstance(connections, dict):
            invalid("connections")
        for conn_id, conn in connections.items():
            if not isinstance(conn_id, str) or not isinstance(conn, dict):
                invalid("connections")
            if conn.get("conn_id") != conn_id:
                invalid("connections")
            if not isinstance(conn.get("backend"), str):
                invalid("connections")
            if (not isinstance(conn.get("opened_ms"), int)
                    or isinstance(conn.get("opened_ms"), bool)):
                invalid("connections")
        if not isinstance(stats, dict):
            invalid("stats")
        for counter in ("accepted", "closed", "forced"):
            if (not isinstance(stats.get(counter), int)
                    or isinstance(stats.get(counter), bool)
                    or stats[counter] < 0):
                invalid("stats")

    def restore(self, state: dict) -> None:
        with self._lock:
            self._validate_snapshot(state)
            backends = copy.deepcopy(state["backends"])
            ring = sorted((entry[0], entry[1]) for entry in state["ring"])
            connections = copy.deepcopy(state["connections"])
            stats = state["stats"]
            self._backends = backends
            self._ring = ring
            self._connections = connections
            self._accepted = stats["accepted"]
            self._closed = stats["closed"]
            self._forced = stats["forced"]
            self._forced_log = []

    def stats(self) -> dict:
        with self._lock:
            gauges = {"active": 0, "draining": 0, "down": 0, "disabled": 0}
            for record in self._backends.values():
                gauges[record["state"]] += 1
            return {
                "backends": len(self._backends),
                "active": gauges["active"],
                "draining": gauges["draining"],
                "down": gauges["down"],
                "disabled": gauges["disabled"],
                "connections": len(self._connections),
                "accepted": self._accepted,
                "closed": self._closed,
                "forced": self._forced,
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._accepted = 0
            self._closed = 0
            self._forced = 0
