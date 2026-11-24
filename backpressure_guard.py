"""Relay backpressure guard with watermarks and deficit round robin."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict


BP_INVALID_ARGUMENT = "BP_INVALID_ARGUMENT"
BP_DUPLICATE_CONN = "BP_DUPLICATE_CONN"
BP_UNKNOWN_CONN = "BP_UNKNOWN_CONN"
BP_TENANT_QUOTA = "BP_TENANT_QUOTA"
BP_HARD_CAP = "BP_HARD_CAP"
BP_SNAPSHOT_INVALID = "BP_SNAPSHOT_INVALID"

_STATS_KEYS = (
    "accepted_bytes",
    "drained_bytes",
    "pauses",
    "resumes",
    "closed",
)


class BackpressureError(ValueError):
    """Rejection raised by the backpressure guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _ConnState:
    __slots__ = (
        "conn_id",
        "tenant",
        "pending",
        "paused",
        "paused_at",
        "pause_count",
        "resume_count",
        "deficit",
        "virtual_time",
        "served_bytes",
    )

    def __init__(self, conn_id: str, tenant: str) -> None:
        self.conn_id = conn_id
        self.tenant = tenant
        self.pending = 0
        self.paused = False
        self.paused_at: float | int | None = None
        self.pause_count = 0
        self.resume_count = 0
        self.deficit = 0
        self.virtual_time = 0
        self.served_bytes = 0


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value > 0
        and value == value
        and value not in (float("inf"), float("-inf"))
    )


def _is_time_value(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value == value
        and value not in (float("inf"), float("-inf"))
    )


class BackpressureGuard:
    """Track per-connection buffering, pause upstream reads and drain fairly."""

    def __init__(
        self,
        *,
        high_watermark: int = 8192,
        low_watermark: int = 2048,
        hard_cap: int = 65536,
        grace: float = 30.0,
        quantum: int = 1000,
        tenant_quota: int = 1 << 20,
        clock=None,
    ) -> None:
        if not _is_positive_int(high_watermark):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if not _is_non_negative_int(low_watermark):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if not _is_positive_int(hard_cap):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if not _is_positive_int(quantum):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if not _is_positive_int(tenant_quota):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if not _is_positive_number(grace):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if clock is not None and not callable(clock):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if low_watermark >= high_watermark:
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if high_watermark > hard_cap and low_watermark < hard_cap:
            raise BackpressureError(BP_INVALID_ARGUMENT)

        self.high_watermark = high_watermark
        self.low_watermark = low_watermark
        self.hard_cap = hard_cap
        self._hard_limit = max(hard_cap, high_watermark)
        self.grace = grace
        self.quantum = quantum
        self.tenant_quota = tenant_quota
        self._clock = clock
        self._lock = threading.RLock()
        self._conns: OrderedDict[str, _ConnState] = OrderedDict()
        self._tenant_usage: dict[str, int] = {}
        self._stats = {key: 0 for key in _STATS_KEYS}
        self._total_pending = 0
        self._global_virtual_time = 0
        self._schedule_cursor: str | None = None

    def register_conn(self, conn_id: str, tenant: str) -> dict:
        if not isinstance(conn_id, str) or not conn_id:
            raise BackpressureError(BP_INVALID_ARGUMENT)
        if not isinstance(tenant, str) or not tenant:
            raise BackpressureError(BP_INVALID_ARGUMENT)

        with self._lock:
            if conn_id in self._conns:
                raise BackpressureError(BP_DUPLICATE_CONN)

            conn = _ConnState(conn_id, tenant)
            self._conns[conn_id] = conn
            self._tenant_usage.setdefault(tenant, 0)
            return {
                "conn_id": conn_id,
                "tenant": tenant,
                "pending": 0,
                "paused": False,
            }

    def list_conns(self) -> list:
        with self._lock:
            return list(self._conns.keys())

    def on_read(self, conn_id: str, nbytes: int, now=None) -> bool:
        with self._lock:
            conn = self._get_conn_locked(conn_id)
            if not _is_positive_int(nbytes):
                raise BackpressureError(BP_INVALID_ARGUMENT)

            new_tenant_usage = self._tenant_usage.get(conn.tenant, 0) + nbytes
            if new_tenant_usage > self.tenant_quota:
                raise BackpressureError(BP_TENANT_QUOTA)

            current_time = self._resolve_time(now)
            new_pending = conn.pending + nbytes
            conn.pending = new_pending
            self._tenant_usage[conn.tenant] = new_tenant_usage
            self._total_pending += nbytes
            self._stats["accepted_bytes"] += nbytes

            if new_pending > self._hard_limit:
                self._close_by_id_locked(conn_id)
                raise BackpressureError(BP_HARD_CAP)

            if new_pending >= self.high_watermark and not conn.paused:
                conn.paused = True
                conn.paused_at = current_time
                conn.pause_count += 1
                self._stats["pauses"] += 1

            return conn.paused

    def drain(self, conn_id: str, nbytes: int, now=None) -> int:
        with self._lock:
            conn = self._get_conn_locked(conn_id)
            if not _is_non_negative_int(nbytes):
                raise BackpressureError(BP_INVALID_ARGUMENT)

            self._resolve_time(now)
            released = self._release_bytes_locked(conn, nbytes)
            return released

    def read_paused(self, conn_id: str) -> bool:
        with self._lock:
            return self._get_conn_locked(conn_id).paused

    def pending_bytes(self, conn_id: str) -> int:
        with self._lock:
            return self._get_conn_locked(conn_id).pending

    def schedule(self, budget_bytes: int, now=None) -> list:
        if not _is_non_negative_int(budget_bytes):
            raise BackpressureError(BP_INVALID_ARGUMENT)

        scheduled: list[tuple[str, int]] = []
        with self._lock:
            self._resolve_time(now)
            remaining = budget_bytes

            while remaining > 0 and self._total_pending > 0:
                conn_id = self._current_cursor_id_locked()
                if conn_id is None:
                    break

                conn = self._conns[conn_id]
                if conn.pending <= 0:
                    self._advance_cursor_locked(conn_id)
                    continue

                amount = min(conn.pending, self.quantum, remaining)
                released = self._release_bytes_locked(conn, amount)
                if released <= 0:
                    break

                scheduled.append((conn_id, released))
                remaining -= released
                self._advance_cursor_locked(conn_id)

            return scheduled

    def expire_slow(self, now) -> list:
        current_time = self._resolve_time(now)
        expired: list[str] = []

        with self._lock:
            for conn_id, conn in self._conns.items():
                if (
                    conn.paused
                    and conn.paused_at is not None
                    and current_time - conn.paused_at >= self.grace
                ):
                    expired.append(conn_id)

            for conn_id in expired:
                if conn_id in self._conns:
                    self._close_by_id_locked(conn_id)

        return expired

    def close_conn(self, conn_id: str) -> int:
        with self._lock:
            self._get_conn_locked(conn_id)
            return self._close_by_id_locked(conn_id)

    def tenant_usage(self, tenant: str) -> int:
        with self._lock:
            return self._tenant_usage.get(tenant, 0)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "stats": dict(self._stats),
                "global_virtual_time": self._global_virtual_time,
                "total_pending": self._total_pending,
                "schedule_cursor": self._schedule_cursor,
                "tenant_usage": dict(self._tenant_usage),
                "connections": [
                    {
                        "conn_id": conn.conn_id,
                        "tenant": conn.tenant,
                        "pending": conn.pending,
                        "paused": conn.paused,
                        "paused_at": conn.paused_at,
                        "pause_count": conn.pause_count,
                        "resume_count": conn.resume_count,
                        "deficit": conn.deficit,
                        "virtual_time": conn.virtual_time,
                        "served_bytes": conn.served_bytes,
                    }
                    for conn in self._conns.values()
                ],
            }

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise BackpressureError(BP_SNAPSHOT_INVALID)

        with self._lock:
            try:
                version = state["version"]
                if not isinstance(version, int) or isinstance(version, bool):
                    raise BackpressureError(BP_SNAPSHOT_INVALID)
                if version != 1:
                    raise BackpressureError(BP_SNAPSHOT_INVALID)

                raw_stats = state["stats"]
                if not isinstance(raw_stats, dict):
                    raise BackpressureError(BP_SNAPSHOT_INVALID)
                new_stats = {}
                for key in _STATS_KEYS:
                    value = raw_stats[key]
                    if not _is_non_negative_int(value):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    new_stats[key] = value

                raw_connections = state["connections"]
                if not isinstance(raw_connections, list):
                    raise BackpressureError(BP_SNAPSHOT_INVALID)

                new_conns: OrderedDict[str, _ConnState] = OrderedDict()
                new_tenant_usage: dict[str, int] = {}
                new_total_pending = 0

                for raw_conn in raw_connections:
                    if not isinstance(raw_conn, dict):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                    conn_id = raw_conn["conn_id"]
                    tenant = raw_conn["tenant"]
                    if not isinstance(conn_id, str) or not conn_id:
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if not isinstance(tenant, str) or not tenant:
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if conn_id in new_conns:
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                    pending = raw_conn["pending"]
                    if not _is_non_negative_int(pending):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                    paused = raw_conn["paused"]
                    if not isinstance(paused, bool):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                    paused_at = raw_conn["paused_at"]
                    if paused:
                        if not _is_time_value(paused_at):
                            raise BackpressureError(BP_SNAPSHOT_INVALID)
                    elif paused_at is not None and not _is_time_value(paused_at):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                    conn = _ConnState(conn_id, tenant)
                    conn.pending = pending
                    conn.paused = paused
                    conn.paused_at = paused_at

                    pause_count = raw_conn.get("pause_count", 0)
                    resume_count = raw_conn.get("resume_count", 0)
                    deficit = raw_conn.get("deficit", 0)
                    virtual_time = raw_conn.get("virtual_time", 0)
                    served_bytes = raw_conn.get("served_bytes", 0)

                    if not _is_non_negative_int(pause_count):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if not _is_non_negative_int(resume_count):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if not _is_non_negative_int(deficit):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if not _is_non_negative_int(virtual_time):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if not _is_non_negative_int(served_bytes):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                    conn.pause_count = pause_count
                    conn.resume_count = resume_count
                    conn.deficit = deficit
                    conn.virtual_time = virtual_time
                    conn.served_bytes = served_bytes

                    new_conns[conn_id] = conn
                    new_tenant_usage[tenant] = (
                        new_tenant_usage.get(tenant, 0) + pending
                    )
                    new_total_pending += pending

                cursor = state.get("schedule_cursor")
                if cursor is not None:
                    if not isinstance(cursor, str) or cursor not in new_conns:
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

                global_virtual_time = state.get("global_virtual_time", 0)
                if not _is_non_negative_int(global_virtual_time):
                    raise BackpressureError(BP_SNAPSHOT_INVALID)

                total_pending = state.get("total_pending", new_total_pending)
                if not _is_non_negative_int(total_pending):
                    raise BackpressureError(BP_SNAPSHOT_INVALID)
                if total_pending != new_total_pending:
                    raise BackpressureError(BP_SNAPSHOT_INVALID)

                tenant_usage = state.get("tenant_usage")
                if tenant_usage is not None:
                    if not isinstance(tenant_usage, dict):
                        raise BackpressureError(BP_SNAPSHOT_INVALID)
                    for tenant, usage in tenant_usage.items():
                        if not isinstance(tenant, str):
                            raise BackpressureError(BP_SNAPSHOT_INVALID)
                        if not _is_non_negative_int(usage):
                            raise BackpressureError(BP_SNAPSHOT_INVALID)
                    if tenant_usage != new_tenant_usage:
                        raise BackpressureError(BP_SNAPSHOT_INVALID)

            except BackpressureError:
                raise
            except (KeyError, TypeError, ValueError, AttributeError, IndexError) as exc:
                raise BackpressureError(BP_SNAPSHOT_INVALID) from exc

            self._conns = new_conns
            self._tenant_usage = new_tenant_usage
            self._stats = new_stats
            self._total_pending = new_total_pending
            self._schedule_cursor = cursor
            self._global_virtual_time = global_virtual_time

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def _get_conn_locked(self, conn_id: str) -> _ConnState:
        if not isinstance(conn_id, str) or not conn_id:
            raise BackpressureError(BP_INVALID_ARGUMENT)
        try:
            return self._conns[conn_id]
        except KeyError as exc:
            raise BackpressureError(BP_UNKNOWN_CONN) from exc

    def _resolve_time(self, now) -> int | float:
        if now is None:
            current = self._clock() if self._clock is not None else time.monotonic()
        else:
            current = now
        if not _is_time_value(current):
            raise BackpressureError(BP_INVALID_ARGUMENT)
        return current

    def _release_bytes_locked(self, conn: _ConnState, requested: int) -> int:
        released = min(requested, conn.pending)
        if released <= 0:
            return 0

        conn.pending -= released
        conn.served_bytes += released
        conn.virtual_time += released
        self._global_virtual_time += released
        self._total_pending -= released
        self._tenant_usage[conn.tenant] = (
            self._tenant_usage.get(conn.tenant, 0) - released
        )
        self._stats["drained_bytes"] += released

        if conn.paused and conn.pending <= self.low_watermark:
            conn.paused = False
            conn.paused_at = None
            conn.resume_count += 1
            self._stats["resumes"] += 1

        return released

    def _close_by_id_locked(self, conn_id: str) -> int:
        conn = self._conns[conn_id]
        pending = conn.pending
        tenant_usage = self._tenant_usage.get(conn.tenant, 0) - pending
        if tenant_usage:
            self._tenant_usage[conn.tenant] = tenant_usage
        else:
            self._tenant_usage.pop(conn.tenant, None)

        self._total_pending -= pending
        self._stats["closed"] += 1

        if self._schedule_cursor == conn_id:
            connection_ids = list(self._conns.keys())
            index = connection_ids.index(conn_id)
            if len(connection_ids) > 1:
                self._schedule_cursor = connection_ids[
                    (index + 1) % len(connection_ids)
                ]
            else:
                self._schedule_cursor = None

        del self._conns[conn_id]
        return pending

    def _current_cursor_id_locked(self) -> str | None:
        if not self._conns:
            return None
        if self._schedule_cursor in self._conns:
            return self._schedule_cursor
        self._schedule_cursor = next(iter(self._conns))
        return self._schedule_cursor

    def _advance_cursor_locked(self, conn_id: str) -> None:
        connection_ids = list(self._conns.keys())
        if not connection_ids:
            self._schedule_cursor = None
            return
        try:
            index = connection_ids.index(conn_id)
        except ValueError:
            self._schedule_cursor = connection_ids[0]
            return
        self._schedule_cursor = connection_ids[(index + 1) % len(connection_ids)]
