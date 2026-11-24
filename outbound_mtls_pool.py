"""Outbound mTLS connection pool with tenant, SNI and certificate isolation."""

from __future__ import annotations

import copy
import threading
from typing import Any


class MtlsPoolError(ValueError):
    """Raised when an outbound mTLS pool operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class OutboundMtlsPool:
    """Track connection identities, per-target caps and draining connections."""

    def __init__(self, *, target_limit: int = 4, queue_timeout: int = 500,
                 clock=None) -> None:
        if not isinstance(target_limit, int) or isinstance(target_limit, bool) \
                or target_limit <= 0:
            raise MtlsPoolError("MTLS_INVALID_LIMIT", {"limit": target_limit})

        self.target_limit = target_limit
        self.queue_timeout = queue_timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._identities: dict[str, dict[str, Any]] = {}
        self._peers: dict[str, dict[str, Any]] = {}
        self._target_limits: dict[str, int] = {}
        self._connections: dict[str, dict[str, Any]] = {}
        self._leases: dict[str, dict[str, Any]] = {}
        self._waiters: dict[str, list[dict[str, Any]]] = {}
        self._stats = self._fresh_stats()
        self._sequences = {"conn": 0, "lease": 0, "ticket": 0}

    @staticmethod
    def _fresh_stats() -> dict[str, int]:
        return {
            "created": 0,
            "reused": 0,
            "acquired": 0,
            "released": 0,
            "release_repeated": 0,
            "closed": 0,
            "promoted": 0,
            "expired_waiters": 0,
            "expired": 0,
            "saturated": 0,
            "subject_rejected": 0,
            "rejected": 0,
            "failed": 0,
            "rotated": 0,
            "drained": 0,
        }

    def _now(self, now: int | None) -> int:
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        import time

        return time.monotonic_ns() // 1_000_000

    def _next_id(self, kind: str, prefix: str) -> str:
        self._sequences[kind] += 1
        return f"{prefix}-{self._sequences[kind]}"

    @staticmethod
    def _connection_key(conn: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            conn["tenant"],
            conn["target"],
            conn["sni"],
            conn["fingerprint"],
        )

    def _limit_for(self, target: str) -> int:
        return self._target_limits.get(target, self.target_limit)

    def _target_connection_count(self, target: str) -> int:
        return sum(
            conn["target"] == target and conn["state"] != "closed"
            for conn in self._connections.values()
        )

    def _find_reusable_connection(
        self, key: tuple[str, str, str, str]
    ) -> dict[str, Any] | None:
        for conn in self._connections.values():
            if conn["state"] not in ("idle", "active"):
                continue
            if self._connection_key(conn) == key:
                return conn
        return None

    def _make_lease_view(
        self, lease: dict[str, Any], conn: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "queued": False,
            "reused": bool(lease["reused"]),
            "lease_id": lease["lease_id"],
            "conn_id": conn["conn_id"],
            "tenant": conn["tenant"],
            "target": conn["target"],
            "sni": conn["sni"],
            "identity_id": conn["identity_id"],
            "fingerprint": conn["fingerprint"],
            "in_flight": conn["in_flight"],
        }

    def _checkout(
        self,
        conn: dict[str, Any],
        *,
        now: int,
        reused: bool,
        close: bool = False,
    ) -> dict[str, Any]:
        lease_id = self._next_id("lease", "lease")
        lease = {
            "lease_id": lease_id,
            "conn_id": conn["conn_id"],
            "state": "active",
            "reused": reused,
            "close_requested": close,
            "created_at": now,
            "released_at": None,
        }
        self._leases[lease_id] = lease
        conn["in_flight"] += 1
        conn["last_used_at"] = now
        if conn["state"] == "idle":
            conn["state"] = "active"
        if close:
            conn["close_requested"] = True
        self._stats["acquired"] += 1
        if reused:
            self._stats["reused"] += 1
        return self._make_lease_view(lease, conn)

    def _create_connection(
        self,
        *,
        tenant: str,
        target: str,
        sni: str,
        identity_id: str,
        fingerprint: str,
        now: int,
    ) -> dict[str, Any]:
        conn_id = self._next_id("conn", "conn")
        conn = {
            "conn_id": conn_id,
            "tenant": tenant,
            "target": target,
            "sni": sni,
            "identity_id": identity_id,
            "fingerprint": fingerprint,
            "state": "active",
            "in_flight": 0,
            "created_at": now,
            "last_used_at": now,
            "closed_at": None,
            "drain_deadline": None,
            "close_requested": False,
            "close_reason": None,
        }
        self._connections[conn_id] = conn
        self._stats["created"] += 1
        return conn

    def _close_connection(
        self, conn: dict[str, Any], *, now: int, reason: str
    ) -> bool:
        if conn["state"] == "closed":
            return False
        conn["state"] = "closed"
        conn["closed_at"] = now
        conn["close_reason"] = reason
        self._stats["closed"] += 1
        return True

    def _expire_target_waiters(self, target: str, now: int) -> list[dict[str, Any]]:
        queue = self._waiters.get(target)
        if not queue:
            return []
        expired = [item for item in queue if item["deadline"] <= now]
        if expired:
            self._waiters[target] = [
                item for item in queue if item["deadline"] > now
            ]
            self._stats["expired_waiters"] += len(expired)
            self._stats["expired"] += len(expired)
        return expired

    def _promote_next_waiter(
        self, target: str, now: int
    ) -> dict[str, Any] | None:
        self._expire_target_waiters(target, now)
        queue = self._waiters.get(target)
        limit = self._limit_for(target)

        while queue:
            waiter = queue[0]
            identity = self._identities.get(waiter["identity_id"])
            peer = self._peers.get(target)

            if (
                identity is None
                or identity["tenant"] != waiter["tenant"]
                or now >= identity["not_after"]
                or peer is None
                or peer["subject"] != waiter["expected_subject"]
            ):
                queue.pop(0)
                continue

            fingerprint = identity["fingerprint"]
            key = (waiter["tenant"], target, waiter["sni"], fingerprint)
            conn = self._find_reusable_connection(key)
            if conn is not None:
                queue.pop(0)
                self._stats["promoted"] += 1
                view = self._checkout(conn, now=now, reused=True)
                view["ticket"] = waiter["ticket"]
                return view

            if self._target_connection_count(target) >= limit:
                return None

            queue.pop(0)
            conn = self._create_connection(
                tenant=waiter["tenant"],
                target=target,
                sni=waiter["sni"],
                identity_id=waiter["identity_id"],
                fingerprint=fingerprint,
                now=now,
            )
            self._stats["promoted"] += 1
            view = self._checkout(conn, now=now, reused=False)
            view["ticket"] = waiter["ticket"]
            return view

        return None

    def register_identity(self, identity_id: str, *, tenant: str,
                          fingerprint: str, not_after: int) -> dict:
        with self._lock:
            if identity_id in self._identities:
                raise MtlsPoolError(
                    "MTLS_IDENTITY_CONFLICT",
                    {"identity_id": identity_id},
                )
            identity = {
                "identity_id": identity_id,
                "tenant": tenant,
                "fingerprint": fingerprint,
                "not_after": not_after,
                "previous_fingerprint": None,
                "rotated_at": None,
                "drain_deadline": None,
            }
            self._identities[identity_id] = identity
            return {
                "identity_id": identity_id,
                "tenant": tenant,
                "fingerprint": fingerprint,
                "not_after": not_after,
            }

    def register_peer(self, target: str, *, subject: str) -> dict:
        with self._lock:
            peer = {"target": target, "subject": subject}
            self._peers[target] = peer
            return dict(peer)

    def set_target_limit(self, target: str, limit: int) -> int:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise MtlsPoolError("MTLS_INVALID_LIMIT", {"target": target, "limit": limit})
        with self._lock:
            self._target_limits[target] = limit
            return limit

    def acquire(self, tenant: str, target: str, *, sni: str,
                identity_id: str, expected_subject: str, now: int,
                wait_ms: int = 0) -> dict:
        now = self._now(now)
        if wait_ms is None:
            wait_ms = self.queue_timeout

        with self._lock:
            self._expire_target_waiters(target, now)

            identity = self._identities.get(identity_id)
            if identity is None:
                raise MtlsPoolError(
                    "MTLS_UNKNOWN_IDENTITY",
                    {"identity_id": identity_id, "tenant": tenant},
                )
            if identity["tenant"] != tenant:
                raise MtlsPoolError(
                    "MTLS_IDENTITY_TENANT_MISMATCH",
                    {
                        "identity_id": identity_id,
                        "tenant": tenant,
                        "identity_tenant": identity["tenant"],
                    },
                )
            if now >= identity["not_after"]:
                raise MtlsPoolError(
                    "MTLS_IDENTITY_EXPIRED",
                    {
                        "identity_id": identity_id,
                        "now": now,
                        "not_after": identity["not_after"],
                    },
                )

            peer = self._peers.get(target)
            if peer is None:
                raise MtlsPoolError("MTLS_UNKNOWN_PEER", {"target": target})
            if peer["subject"] != expected_subject:
                self._stats["subject_rejected"] += 1
                self._stats["rejected"] += 1
                raise MtlsPoolError(
                    "MTLS_PEER_SUBJECT_MISMATCH",
                    {
                        "target": target,
                        "expected_subject": expected_subject,
                        "registered_subject": peer["subject"],
                    },
                )

            fingerprint = identity["fingerprint"]
            key = (tenant, target, sni, fingerprint)
            existing = self._find_reusable_connection(key)
            if existing is not None:
                return self._checkout(existing, now=now, reused=True)

            limit = self._limit_for(target)
            if self._target_connection_count(target) >= limit:
                if wait_ms and wait_ms > 0:
                    ticket = self._next_id("ticket", "ticket")
                    deadline = now + wait_ms
                    waiter = {
                        "ticket": ticket,
                        "tenant": tenant,
                        "target": target,
                        "sni": sni,
                        "identity_id": identity_id,
                        "expected_subject": expected_subject,
                        "fingerprint": fingerprint,
                        "wait_ms": wait_ms,
                        "enqueued_at": now,
                        "deadline": deadline,
                    }
                    queue = self._waiters.setdefault(target, [])
                    queue.append(waiter)
                    return {
                        "queued": True,
                        "reused": False,
                        "ticket": ticket,
                        "ticket_id": ticket,
                        "position": len(queue),
                        "deadline": deadline,
                        "wait_ms": wait_ms,
                        "tenant": tenant,
                        "target": target,
                        "sni": sni,
                        "identity_id": identity_id,
                        "fingerprint": fingerprint,
                    }

                self._stats["saturated"] += 1
                self._stats["rejected"] += 1
                raise MtlsPoolError(
                    "MTLS_TARGET_SATURATED",
                    {
                        "target": target,
                        "limit": limit,
                        "connections": self._target_connection_count(target),
                    },
                )

            conn = self._create_connection(
                tenant=tenant,
                target=target,
                sni=sni,
                identity_id=identity_id,
                fingerprint=fingerprint,
                now=now,
            )
            return self._checkout(conn, now=now, reused=False)

    def release(self, lease_id: str, *, now: int, close: bool = False) -> dict:
        now = self._now(now)
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise MtlsPoolError("MTLS_UNKNOWN_LEASE", {"lease_id": lease_id})

            conn = self._connections.get(lease["conn_id"])
            if lease["state"] != "active":
                self._stats["release_repeated"] += 1
                return {
                    "released": False,
                    "lease_id": lease_id,
                    "conn_id": lease["conn_id"],
                    "promoted": None,
                }

            lease["state"] = "released"
            lease["released_at"] = now
            lease["close_requested"] = bool(close or lease["close_requested"])
            self._stats["released"] += 1

            closed_now = False
            if conn is not None and conn["state"] != "closed":
                conn["in_flight"] = max(0, conn["in_flight"] - 1)
                if close:
                    conn["close_requested"] = True

                if conn["in_flight"] == 0:
                    if close or conn["state"] == "draining":
                        closed_now = self._close_connection(
                            conn, now=now, reason="release_close"
                        )
                    elif conn["state"] == "active":
                        conn["state"] = "idle"
                elif close or conn["state"] == "draining":
                    conn["state"] = "draining"

            promoted = None
            if closed_now and conn is not None:
                promoted = self._promote_next_waiter(conn["target"], now)

            return {
                "released": True,
                "lease_id": lease_id,
                "conn_id": lease["conn_id"],
                "closed": closed_now,
                "promoted": promoted,
            }

    def mark_failed(self, lease_id: str, *, now: int) -> dict:
        now = self._now(now)
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None:
                raise MtlsPoolError("MTLS_UNKNOWN_LEASE", {"lease_id": lease_id})
            if lease["state"] != "active":
                return {
                "marked_failed": False,
                "marked": False,
                "failed": False,
                "retried": False,
                "requeued": False,
                "reenqueued": False,
                "redelivered": False,
                    "lease_id": lease_id,
                    "conn_id": lease["conn_id"],
                    "promoted": None,
                }

            lease["state"] = "failed"
            lease["failed_at"] = now
            self._stats["failed"] += 1

            conn = self._connections.get(lease["conn_id"])
            closed_now = False
            if conn is not None and conn["state"] != "closed":
                conn["in_flight"] = max(0, conn["in_flight"] - 1)
                conn["state"] = "draining"
                conn["close_reason"] = "failed"
                if conn["in_flight"] == 0:
                    closed_now = self._close_connection(
                        conn, now=now, reason="failed"
                    )

            promoted = None
            if closed_now and conn is not None:
                promoted = self._promote_next_waiter(conn["target"], now)

            return {
                "marked_failed": True,
                "marked": True,
                "failed": True,
                "retried": False,
                "requeued": False,
                "reenqueued": False,
                "redelivered": False,
                "retry": False,
                "lease_id": lease_id,
                "conn_id": lease["conn_id"],
                "closed": closed_now,
                "promoted": promoted,
            }

    def rotate_identity(self, identity_id: str, new_fingerprint: str, *,
                        now: int, overlap: int = 0) -> dict:
        now = self._now(now)
        with self._lock:
            identity = self._identities.get(identity_id)
            if identity is None:
                raise MtlsPoolError(
                    "MTLS_UNKNOWN_IDENTITY",
                    {"identity_id": identity_id},
                )

            previous_fingerprint = identity["fingerprint"]
            drain_deadline = now + overlap
            identity["fingerprint"] = new_fingerprint
            identity["previous_fingerprint"] = previous_fingerprint
            identity["rotated_at"] = now
            identity["drain_deadline"] = drain_deadline
            self._stats["rotated"] += 1

            drained = 0
            closed_idle = 0
            for conn in self._connections.values():
                if conn["state"] == "closed":
                    continue
                if conn["identity_id"] != identity_id:
                    continue
                if conn["fingerprint"] == new_fingerprint:
                    continue
                conn["drain_deadline"] = drain_deadline
                drained += 1
                if conn["in_flight"] == 0:
                    self._close_connection(
                        conn, now=now, reason="identity_rotation"
                    )
                    closed_idle += 1
                else:
                    conn["state"] = "draining"

            return {
                "identity_id": identity_id,
                "tenant": identity["tenant"],
                "fingerprint": new_fingerprint,
                "new_fingerprint": new_fingerprint,
                "previous_fingerprint": previous_fingerprint,
                "drain_deadline": drain_deadline,
                "overlap": overlap,
                "drained_connections": drained,
                "closed_idle_connections": closed_idle,
            }

    def drain_target(self, target: str, *, now: int) -> int:
        now = self._now(now)
        with self._lock:
            closed = 0
            for conn in self._connections.values():
                if conn["target"] != target or conn["state"] == "closed":
                    continue
                if conn["in_flight"] == 0:
                    if self._close_connection(conn, now=now, reason="target_drain"):
                        closed += 1
                else:
                    conn["state"] = "draining"
                    conn["drain_started_at"] = now
            self._stats["drained"] += closed
            return closed

    def waiters(self, target: str) -> int:
        with self._lock:
            return len(self._waiters.get(target, ()))

    def expire_waiters(self, now: int) -> int:
        now = self._now(now)
        with self._lock:
            expired_count = 0
            for target in list(self._waiters):
                expired = self._expire_target_waiters(target, now)
                expired_count += len(expired)
                if not self._waiters[target]:
                    del self._waiters[target]
            return expired_count

    def connection_state(self, conn_id: str) -> dict:
        with self._lock:
            conn = self._connections.get(conn_id)
            if conn is None:
                raise MtlsPoolError(
                    "MTLS_UNKNOWN_CONNECTION", {"conn_id": conn_id}
                )
            return copy.deepcopy(conn)

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = self._fresh_stats()

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "target_limit": self.target_limit,
                "queue_timeout": self.queue_timeout,
                "target_limits": copy.deepcopy(self._target_limits),
                "config": {
                    "target_limit": self.target_limit,
                    "queue_timeout": self.queue_timeout,
                    "target_limits": copy.deepcopy(self._target_limits),
                },
                "identities": copy.deepcopy(self._identities),
                "peers": copy.deepcopy(self._peers),
                "connections": copy.deepcopy(self._connections),
                "leases": copy.deepcopy(self._leases),
                "waiters": copy.deepcopy(self._waiters),
                "stats": copy.deepcopy(self._stats),
                "sequences": copy.deepcopy(self._sequences),
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        try:
            restored = self._parse_snapshot(state)
        except MtlsPoolError:
            raise
        except Exception as exc:
            raise MtlsPoolError(
                "MTLS_SNAPSHOT_INVALID", {"reason": str(exc)}
            ) from exc

        with self._lock:
            self.target_limit = restored["target_limit"]
            self.queue_timeout = restored["queue_timeout"]
            self._target_limits = restored["target_limits"]
            self._identities = restored["identities"]
            self._peers = restored["peers"]
            self._connections = restored["connections"]
            self._leases = restored["leases"]
            self._waiters = restored["waiters"]
            self._stats = restored["stats"]
            self._sequences = restored["sequences"]

    @classmethod
    def _parse_snapshot(cls, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "snapshot must be a dict")

        version = state.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "unsupported version")

        connections = state.get("connections")
        stats = state.get("stats")
        if not isinstance(connections, dict):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "connections must be a dict")
        if not isinstance(stats, dict):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "stats must be a dict")

        data = copy.deepcopy(state)
        connections = data["connections"]

        config = data.get("config", {})
        if not isinstance(config, dict):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "config must be a dict")
        target_limit = data.get(
            "target_limit", config.get("target_limit", 4)
        )
        queue_timeout = data.get(
            "queue_timeout", config.get("queue_timeout", 500)
        )
        target_limits = data.get(
            "target_limits", config.get("target_limits", {})
        )
        if not cls._positive_int(target_limit):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid target_limit")
        if not isinstance(queue_timeout, int) or isinstance(queue_timeout, bool):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid queue_timeout")
        if not isinstance(target_limits, dict) or any(
            not cls._positive_int(value) for value in target_limits.values()
        ):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid target_limits")

        identities = data.get("identities", {})
        peers = data.get("peers", {})
        leases = data.get("leases", {})
        waiters = data.get("waiters", {})
        if not all(isinstance(item, dict) for item in identities.values()) \
                if isinstance(identities, dict) else False:
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid identities")
        if not isinstance(identities, dict):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "identities must be a dict")
        if not isinstance(peers, dict) or not all(
            isinstance(item, dict) for item in peers.values()
        ):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "peers must be a dict")
        if not isinstance(leases, dict) or not all(
            isinstance(item, dict) for item in leases.values()
        ):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "leases must be a dict")
        if not isinstance(waiters, dict) or not all(
            isinstance(queue, list) for queue in waiters.values()
        ):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "waiters must be a dict of lists")

        normalized_connections: dict[str, dict[str, Any]] = {}
        for conn_id, raw_conn in connections.items():
            if not isinstance(conn_id, str) or not isinstance(raw_conn, dict):
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid connection")
            conn = copy.deepcopy(raw_conn)
            conn["conn_id"] = conn_id
            for field in ("tenant", "target", "sni", "fingerprint"):
                if not isinstance(conn.get(field), str):
                    raise MtlsPoolError(
                        "MTLS_SNAPSHOT_INVALID", f"connection missing {field}"
                    )
            conn.setdefault("identity_id", "")
            if not isinstance(conn["identity_id"], str):
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid identity_id")
            if conn.get("state") not in ("idle", "active", "draining", "closed"):
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid connection state")
            in_flight = conn.get("in_flight", 0)
            if not isinstance(in_flight, int) or isinstance(in_flight, bool) \
                    or in_flight < 0:
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid in_flight")
            conn["in_flight"] = in_flight
            conn.setdefault("created_at", None)
            conn.setdefault("last_used_at", None)
            conn.setdefault("closed_at", None)
            conn.setdefault("drain_deadline", None)
            conn.setdefault("close_requested", False)
            conn.setdefault("close_reason", None)
            if conn["state"] == "active" and in_flight == 0:
                conn["state"] = "idle"
            normalized_connections[conn_id] = conn

        normalized_leases: dict[str, dict[str, Any]] = {}
        active_counts = {conn_id: 0 for conn_id in normalized_connections}
        for lease_id, raw_lease in leases.items():
            if not isinstance(lease_id, str) or not isinstance(raw_lease, dict):
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid lease")
            lease = copy.deepcopy(raw_lease)
            lease["lease_id"] = lease_id
            conn_id = lease.get("conn_id")
            if conn_id not in normalized_connections:
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "lease missing connection")
            if lease.get("state") not in ("active", "released", "failed"):
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid lease state")
            lease.setdefault("reused", False)
            lease.setdefault("created_at", None)
            lease.setdefault("released_at", None)
            lease.setdefault("close_requested", False)
            if lease["state"] == "active":
                active_counts[conn_id] += 1
            normalized_leases[lease_id] = lease

        for conn_id, conn in normalized_connections.items():
            conn["in_flight"] = active_counts[conn_id]
            if conn["state"] != "closed":
                if conn["in_flight"] > 0:
                    conn["state"] = "draining" if conn["state"] == "draining" else "active"
                elif conn["state"] == "active":
                    conn["state"] = "idle"

        for target, queue in waiters.items():
            if not isinstance(target, str):
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid waiter target")
            for waiter in queue:
                if not isinstance(waiter, dict):
                    raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid waiter")
                for field in ("ticket", "tenant", "sni", "identity_id", "deadline"):
                    if field not in waiter:
                        raise MtlsPoolError(
                            "MTLS_SNAPSHOT_INVALID", f"waiter missing {field}"
                        )

        normalized_stats = cls._fresh_stats()
        for key, value in stats.items():
            if isinstance(value, int) and not isinstance(value, bool):
                normalized_stats[key] = value

        sequences = data.get("sequences", {})
        if not isinstance(sequences, dict):
            raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid sequences")
        normalized_sequences = {"conn": 0, "lease": 0, "ticket": 0}
        prefixes = {"conn": "conn-", "lease": "lease-", "ticket": "ticket-"}
        for kind, current in normalized_sequences.items():
            value = sequences.get(kind, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise MtlsPoolError("MTLS_SNAPSHOT_INVALID", "invalid sequence")
            normalized_sequences[kind] = max(current, value)
            prefix = prefixes[kind]
            for item_id in (
                normalized_connections if kind == "conn"
                else normalized_leases if kind == "lease"
                else {w.get("ticket") for q in waiters.values() for w in q}
            ):
                candidate = item_id[len(prefix):] if isinstance(item_id, str) \
                    and item_id.startswith(prefix) else ""
                if candidate.isdigit():
                    normalized_sequences[kind] = max(
                        normalized_sequences[kind], int(candidate)
                    )

        return {
            "target_limit": target_limit,
            "queue_timeout": queue_timeout,
            "target_limits": dict(target_limits),
            "identities": identities,
            "peers": peers,
            "connections": normalized_connections,
            "leases": normalized_leases,
            "waiters": waiters,
            "stats": normalized_stats,
            "sequences": normalized_sequences,
        }

    @staticmethod
    def _positive_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0
