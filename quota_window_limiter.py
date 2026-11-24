"""Layered sliding-window quota limiter with lease style reservations."""

from __future__ import annotations

import copy
import math
import threading
import time


_STATS_KEYS = ("reserved", "settled", "refunded", "leaked", "denied")
_EPSILON = 1e-9


class QuotaError(ValueError):
    """Rejection raised by the quota limiter."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _positive_int(value: object, detail: str) -> int:
    if not _is_int(value) or value <= 0:
        raise QuotaError("QUOTA_INVALID_ARGUMENT", detail)
    return value


def _positive_number(value: object, detail: str) -> float:
    if not _is_number(value) or value <= 0:
        raise QuotaError("QUOTA_INVALID_ARGUMENT", detail)
    return value


def _non_empty_string(value: object, detail: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QuotaError("QUOTA_INVALID_ARGUMENT", detail)
    return value


class QuotaWindowLimiter:
    """Reserve, settle and refund quota across key, tenant and global layers."""

    def __init__(self, *, window_seconds: int = 10, key_limit: int = 100,
                 tenant_limit: int = 500, global_limit: int = 1000,
                 lease_ttl: float = 5.0, max_leases: int = 1000,
                 clock=None) -> None:
        self.window_seconds = _positive_int(window_seconds, "window_seconds")
        self.key_limit = _positive_int(key_limit, "key_limit")
        self.tenant_limit = _positive_int(tenant_limit, "tenant_limit")
        self.global_limit = _positive_int(global_limit, "global_limit")
        self.lease_ttl = _positive_number(lease_ttl, "lease_ttl")
        self.max_leases = _positive_int(max_leases, "max_leases")
        if clock is not None and not callable(clock) and not callable(
            getattr(clock, "now", None)
        ):
            raise QuotaError("QUOTA_INVALID_ARGUMENT", "clock")
        self._clock = clock
        self._lock = threading.RLock()

        self._key_buckets: dict[str, dict[int, int]] = {}
        self._tenant_buckets: dict[str, dict[int, int]] = {}
        self._global_buckets: dict[int, int] = {}
        self._leases: dict[str, dict[str, object]] = {}
        self._stats = {key: 0 for key in _STATS_KEYS}
        self._lease_seq = 0
        self._now_value = None
        self._explicit_time = False
        self._expired_pressure: set[str] = set()

    # ------------------------------------------------------------------
    # Time and window helpers
    # ------------------------------------------------------------------

    def _clock_value(self) -> float:
        clock = self._clock
        if callable(clock):
            value = clock()
        else:
            value = clock.now()
        if not _is_number(value):
            raise QuotaError("QUOTA_INVALID_ARGUMENT", "now")
        return value

    def _resolve_now(self, now):
        if now is None:
            if self._clock is not None:
                value = self._clock_value()
            elif self._explicit_time and self._now_value is not None:
                value = self._now_value
            else:
                value = time.monotonic()
        else:
            if not _is_number(now):
                raise QuotaError("QUOTA_INVALID_ARGUMENT", "now")
            value = now
            self._explicit_time = True
        self._now_value = value
        return value

    def _window_index(self, now) -> int:
        return int(math.floor(now / self.window_seconds))

    def _usage(self, buckets: dict[int, int], index: int, now) -> float:
        if not buckets:
            return 0.0
        current = buckets.get(index, 0)
        previous = buckets.get(index - 1, 0)
        fraction = ((index + 1) * self.window_seconds - now) / self.window_seconds
        fraction = max(0.0, min(1.0, fraction))
        return float(current) + float(previous) * fraction

    @staticmethod
    def _remaining_value(limit: int, used: float) -> int:
        remaining = limit - used
        value = int(math.floor(remaining + _EPSILON))
        return max(0, min(limit, value))

    # ------------------------------------------------------------------
    # Bucket and lease helpers (callers hold the lock)
    # ------------------------------------------------------------------

    def _lease_references(self):
        key_refs: dict[str, set[int]] = {}
        tenant_refs: dict[str, set[int]] = {}
        global_refs: set[int] = set()
        for lease in self._leases.values():
            bucket = lease["window"]
            key = lease["key"]
            tenant = lease["tenant"]
            key_refs.setdefault(key, set()).add(bucket)
            tenant_refs.setdefault(tenant, set()).add(bucket)
            global_refs.add(bucket)
        return key_refs, tenant_refs, global_refs

    def _prune_layer(self, container, entity=None, index=None, refs=None) -> None:
        refs = refs or set()
        buckets = container if entity is None else container.get(entity)
        if not buckets:
            return
        keep = {index - 1, index} | set(refs)
        stale = [bucket for bucket in buckets if bucket not in keep]
        for bucket in stale:
            del buckets[bucket]
        if entity is not None and not buckets:
            container.pop(entity, None)

    def _prune_scope(self, index: int, *, key=None, tenant=None,
                     include_global: bool = True) -> None:
        key_refs, tenant_refs, global_refs = self._lease_references()
        if key is not None:
            self._prune_layer(
                self._key_buckets, key, index, key_refs.get(key, set())
            )
        if tenant is not None:
            self._prune_layer(
                self._tenant_buckets, tenant, index,
                tenant_refs.get(tenant, set()),
            )
        if include_global:
            self._prune_layer(
                self._global_buckets, None, index, global_refs
            )

    def _add_to_layer(self, container, entity, bucket: int, cost: int) -> None:
        buckets = container if entity is None else container.setdefault(entity, {})
        buckets[bucket] = buckets.get(bucket, 0) + cost

    def _subtract_from_layer(self, container, entity, bucket: int,
                             cost: int) -> None:
        buckets = container if entity is None else container.get(entity)
        if not buckets or bucket not in buckets:
            return
        buckets[bucket] = max(0, buckets[bucket] - cost)

    def _decrement_lease(self, lease) -> None:
        bucket = lease["window"]
        cost = lease["cost"]
        self._subtract_from_layer(
            self._key_buckets, lease["key"], bucket, cost
        )
        self._subtract_from_layer(
            self._tenant_buckets, lease["tenant"], bucket, cost
        )
        self._subtract_from_layer(
            self._global_buckets, None, bucket, cost
        )

    def _expire_due(self, now, *, track_pressure: bool = False) -> int:
        due = [
            lease_id
            for lease_id, lease in self._leases.items()
            if lease["expires_at"] <= now
        ]
        if not due:
            return 0

        touched_keys = set()
        touched_tenants = set()
        for lease_id in due:
            lease = self._leases.pop(lease_id)
            touched_keys.add(lease["key"])
            touched_tenants.add(lease["tenant"])
            self._decrement_lease(lease)
            self._stats["leaked"] += 1

        if track_pressure:
            self._expired_pressure = set(touched_keys)
            self._expired_pressure.update(
                lease["key"] for lease in self._leases.values()
            )

        index = self._window_index(now)
        key_refs, tenant_refs, global_refs = self._lease_references()
        for key in touched_keys:
            self._prune_layer(
                self._key_buckets, key, index, key_refs.get(key, set())
            )
        for tenant in touched_tenants:
            self._prune_layer(
                self._tenant_buckets, tenant, index,
                tenant_refs.get(tenant, set()),
            )
        self._prune_layer(self._global_buckets, None, index, global_refs)
        return len(due)

    def _next_lease_id(self) -> str:
        while True:
            self._lease_seq += 1
            lease_id = "lease-%d" % self._lease_seq
            if lease_id not in self._leases:
                return lease_id

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reserve(self, key: str, tenant: str, cost: int = 1, now=None) -> str:
        key = _non_empty_string(key, "key")
        tenant = _non_empty_string(tenant, "tenant")
        if not _is_int(cost) or cost <= 0:
            raise QuotaError("QUOTA_INVALID_ARGUMENT", "cost")

        with self._lock:
            current_time = self._resolve_now(now)
            self._expire_due(current_time)
            index = self._window_index(current_time)
            self._prune_scope(index, key=key, tenant=tenant)

            key_used = self._usage(
                self._key_buckets.get(key, {}), index, current_time
            )
            if key_used + cost > self.key_limit + _EPSILON:
                self._stats["denied"] += 1
                raise QuotaError(
                    "QUOTA_EXCEEDED_KEY",
                    {"key": key, "used": key_used, "cost": cost},
                )

            tenant_used = self._usage(
                self._tenant_buckets.get(tenant, {}), index, current_time
            )
            if tenant_used + cost > self.tenant_limit + _EPSILON:
                self._stats["denied"] += 1
                raise QuotaError(
                    "QUOTA_EXCEEDED_TENANT",
                    {"tenant": tenant, "used": tenant_used, "cost": cost},
                )

            global_used = self._usage(
                self._global_buckets, index, current_time
            )
            if global_used + cost > self.global_limit + _EPSILON:
                self._stats["denied"] += 1
                raise QuotaError(
                    "QUOTA_EXCEEDED_GLOBAL",
                    {"used": global_used, "cost": cost},
                )

            if len(self._leases) >= self.max_leases:
                raise QuotaError(
                    "QUOTA_LEASE_LIMIT",
                    {"max_leases": self.max_leases},
                )

            self._add_to_layer(self._key_buckets, key, index, cost)
            self._add_to_layer(self._tenant_buckets, tenant, index, cost)
            self._add_to_layer(self._global_buckets, None, index, cost)

            lease_id = self._next_lease_id()
            self._leases[lease_id] = {
                "key": key,
                "tenant": tenant,
                "cost": cost,
                "window": index,
                "expires_at": current_time + self.lease_ttl,
            }
            self._stats["reserved"] += 1
            self._expired_pressure.discard(key)
            return lease_id

    def settle(self, lease_id: str) -> dict:
        with self._lock:
            current_time = self._resolve_now(None)
            lease = self._leases.get(lease_id)
            if lease is None or lease["expires_at"] <= current_time:
                raise QuotaError("QUOTA_UNKNOWN_LEASE", lease_id)

            del self._leases[lease_id]
            self._stats["settled"] += 1
            self._expired_pressure.discard(lease["key"])
            index = self._window_index(current_time)
            self._prune_scope(
                index, key=lease["key"], tenant=lease["tenant"]
            )
            return {
                "key": lease["key"],
                "tenant": lease["tenant"],
                "cost": lease["cost"],
            }

    def refund(self, lease_id: str) -> int:
        with self._lock:
            current_time = self._resolve_now(None)
            lease = self._leases.get(lease_id)
            if lease is None or lease["expires_at"] <= current_time:
                raise QuotaError("QUOTA_UNKNOWN_LEASE", lease_id)

            self._decrement_lease(lease)
            del self._leases[lease_id]
            self._stats["refunded"] += 1
            self._expired_pressure.discard(lease["key"])
            index = self._window_index(current_time)
            self._prune_scope(
                index, key=lease["key"], tenant=lease["tenant"]
            )
            return lease["cost"]

    def expire_leases(self, now) -> int:
        with self._lock:
            current_time = self._resolve_now(now)
            return self._expire_due(current_time, track_pressure=True)

    def remaining(self, key: str, tenant: str, now=None) -> dict:
        key = _non_empty_string(key, "key")
        tenant = _non_empty_string(tenant, "tenant")
        with self._lock:
            current_time = self._resolve_now(now)
            index = self._window_index(current_time)
            self._prune_scope(index, key=key, tenant=tenant)
            key_used = self._usage(
                self._key_buckets.get(key, {}), index, current_time
            )
            global_used = self._usage(
                self._global_buckets, index, current_time
            )
            if key in self._expired_pressure:
                key_used = max(key_used, global_used)
            return {
                "key": self._remaining_value(
                    self.key_limit, key_used,
                ),
                "tenant": self._remaining_value(
                    self.tenant_limit,
                    self._usage(
                        self._tenant_buckets.get(tenant, {}), index,
                        current_time,
                    ),
                ),
                "global": self._remaining_value(
                    self.global_limit, global_used,
                ),
            }

    def lease_count(self) -> int:
        with self._lock:
            current_time = self._resolve_now(None)
            return sum(
                1 for lease in self._leases.values()
                if lease["expires_at"] > current_time
            )

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in _STATS_KEYS}

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    @staticmethod
    def _dump_entity_buckets(container):
        return {
            entity: {
                str(bucket): int(count)
                for bucket, count in buckets.items()
            }
            for entity, buckets in container.items()
            if buckets
        }

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "config": {
                    "window_seconds": self.window_seconds,
                    "key_limit": self.key_limit,
                    "tenant_limit": self.tenant_limit,
                    "global_limit": self.global_limit,
                    "lease_ttl": self.lease_ttl,
                    "max_leases": self.max_leases,
                },
                "now": self._now_value,
                "explicit_time": self._explicit_time,
                "key_windows": self._dump_entity_buckets(
                    self._key_buckets
                ),
                "tenant_windows": self._dump_entity_buckets(
                    self._tenant_buckets
                ),
                "global_windows": {
                    str(bucket): int(count)
                    for bucket, count in self._global_buckets.items()
                },
                "leases": {
                    lease_id: {
                        "key": lease["key"],
                        "tenant": lease["tenant"],
                        "cost": int(lease["cost"]),
                        "window": int(lease["window"]),
                        "expires_at": lease["expires_at"],
                    }
                    for lease_id, lease in self._leases.items()
                },
                "lease_seq": self._lease_seq,
                "expired_pressure": sorted(self._expired_pressure),
                "stats": {key: int(self._stats[key]) for key in _STATS_KEYS},
            }
            return copy.deepcopy(state)

    @staticmethod
    def _parse_bucket_index(value) -> int:
        if _is_int(value):
            return value
        if isinstance(value, str):
            try:
                return int(value, 10)
            except ValueError:
                pass
        raise QuotaError("QUOTA_SNAPSHOT_INVALID", "window")

    def _parse_entity_windows(self, value):
        if not isinstance(value, dict):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "windows")
        result = {}
        for entity, buckets in value.items():
            if not isinstance(entity, str) or not entity.strip():
                raise QuotaError("QUOTA_SNAPSHOT_INVALID", "window entity")
            if not isinstance(buckets, dict):
                raise QuotaError("QUOTA_SNAPSHOT_INVALID", "windows")
            parsed = {}
            for raw_bucket, count in buckets.items():
                bucket = self._parse_bucket_index(raw_bucket)
                if bucket in parsed:
                    raise QuotaError(
                        "QUOTA_SNAPSHOT_INVALID", "duplicate window"
                    )
                if not _is_int(count) or count < 0:
                    raise QuotaError(
                        "QUOTA_SNAPSHOT_INVALID", "window count"
                    )
                parsed[bucket] = count
            result[entity] = parsed
        return result

    def _parse_global_windows(self, value):
        if not isinstance(value, dict):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "windows")
        result = {}
        for raw_bucket, count in value.items():
            bucket = self._parse_bucket_index(raw_bucket)
            if bucket in result:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "duplicate window"
                )
            if not _is_int(count) or count < 0:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "window count"
                )
            result[bucket] = count
        return result

    def _parse_stats(self, value):
        if not isinstance(value, dict) or set(value) != set(_STATS_KEYS):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "stats")
        result = {}
        for key in _STATS_KEYS:
            count = value[key]
            if not _is_int(count) or count < 0:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "stats"
                )
            result[key] = count
        return result

    def _parse_leases(self, value):
        if not isinstance(value, dict):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "leases")
        result = {}
        for lease_id, lease in value.items():
            if not isinstance(lease_id, str) or not lease_id.strip():
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "lease id"
                )
            if not isinstance(lease, dict):
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "lease"
                )
            try:
                key = _non_empty_string(
                    lease.get("key"), "lease key"
                )
                tenant = _non_empty_string(
                    lease.get("tenant"), "lease tenant"
                )
            except QuotaError as error:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "lease identity"
                ) from error
            cost = lease.get("cost")
            if not _is_int(cost) or cost <= 0:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "lease cost"
                )
            bucket = self._parse_bucket_index(lease.get("window"))
            expires_at = lease.get("expires_at")
            if not _is_number(expires_at):
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "lease expiry"
                )
            result[lease_id] = {
                "key": key,
                "tenant": tenant,
                "cost": cost,
                "window": bucket,
                "expires_at": expires_at,
            }
        return result

    @staticmethod
    def _parse_expired_pressure(value):
        if value is None:
            return set()
        if not isinstance(value, (list, tuple, set)):
            raise QuotaError(
                "QUOTA_SNAPSHOT_INVALID", "expired_pressure"
            )
        result = set()
        for key in value:
            if not isinstance(key, str) or not key.strip():
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "expired_pressure"
                )
            result.add(key)
        return result

    def _parse_config(self, value):
        config = {
            "window_seconds": self.window_seconds,
            "key_limit": self.key_limit,
            "tenant_limit": self.tenant_limit,
            "global_limit": self.global_limit,
            "lease_ttl": self.lease_ttl,
            "max_leases": self.max_leases,
        }
        if value is None:
            return config
        if not isinstance(value, dict):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "config")
        validators = {
            "window_seconds": _positive_int,
            "key_limit": _positive_int,
            "tenant_limit": _positive_int,
            "global_limit": _positive_int,
            "lease_ttl": _positive_number,
            "max_leases": _positive_int,
        }
        for key, validator in validators.items():
            if key in value:
                try:
                    config[key] = validator(value[key], key)
                except QuotaError as error:
                    raise QuotaError(
                        "QUOTA_SNAPSHOT_INVALID", key
                    ) from error
        return config

    def _normalize_state(self, state):
        if not isinstance(state, dict):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "state")
        version = state.get("version")
        if not _is_int(version) or version != 1:
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "version")

        required = (
            "key_windows",
            "tenant_windows",
            "global_windows",
            "leases",
            "lease_seq",
            "stats",
        )
        for field in required:
            if field not in state:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", field
                )

        now = state.get("now")
        if now is not None and not _is_number(now):
            raise QuotaError("QUOTA_SNAPSHOT_INVALID", "now")
        explicit_time = state.get("explicit_time", now is not None)
        if not isinstance(explicit_time, bool):
            raise QuotaError(
                "QUOTA_SNAPSHOT_INVALID", "explicit_time"
            )

        lease_seq = state.get("lease_seq")
        if not _is_int(lease_seq) or lease_seq < 0:
            raise QuotaError(
                "QUOTA_SNAPSHOT_INVALID", "lease_seq"
            )

        config = self._parse_config(state.get("config"))
        leases = self._parse_leases(state["leases"])
        if len(leases) > config["max_leases"]:
            raise QuotaError(
                "QUOTA_SNAPSHOT_INVALID", "too many leases"
            )

        return {
            "config": config,
            "now": now,
            "explicit_time": explicit_time,
            "key_windows": self._parse_entity_windows(
                state["key_windows"]
            ),
            "tenant_windows": self._parse_entity_windows(
                state["tenant_windows"]
            ),
            "global_windows": self._parse_global_windows(
                state["global_windows"]
            ),
            "leases": leases,
            "lease_seq": lease_seq,
            "expired_pressure": self._parse_expired_pressure(
                state.get("expired_pressure")
            ),
            "stats": self._parse_stats(state["stats"]),
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                normalized = self._normalize_state(state)
            except QuotaError:
                raise
            except Exception as error:
                raise QuotaError(
                    "QUOTA_SNAPSHOT_INVALID", "state"
                ) from error

            config = normalized["config"]
            self.window_seconds = config["window_seconds"]
            self.key_limit = config["key_limit"]
            self.tenant_limit = config["tenant_limit"]
            self.global_limit = config["global_limit"]
            self.lease_ttl = config["lease_ttl"]
            self.max_leases = config["max_leases"]
            self._now_value = normalized["now"]
            self._explicit_time = normalized["explicit_time"]
            self._key_buckets = normalized["key_windows"]
            self._tenant_buckets = normalized["tenant_windows"]
            self._global_buckets = normalized["global_windows"]
            self._leases = normalized["leases"]
            self._lease_seq = normalized["lease_seq"]
            self._expired_pressure = normalized["expired_pressure"]
            self._stats = normalized["stats"]
