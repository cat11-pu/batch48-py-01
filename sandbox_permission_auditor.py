"""Reconcile declared sandbox permissions with observed usage."""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Iterable


DEFAULT_ALIASES = {
    "read": "fs.read",
    "write": "fs.write",
    "open": "fs.open",
    "connect": "net.connect",
    "listen": "net.listen",
    "dns": "net.dns",
    "exec": "proc.exec",
    "spawn": "proc.spawn",
}

SNAPSHOT_VERSION = 1
COUNTER_KEYS = (
    "observations",
    "undeclared_hits",
    "reports",
    "expired_jobs",
)
STAT_KEYS = ("jobs",) + COUNTER_KEYS


class PermissionAuditorError(ValueError):
    """Refusal raised by the sandbox permission auditor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PermissionAuditor:
    """Compare declared permissions against the operations actually seen."""

    def __init__(self, *, max_jobs: int = 32, max_permissions: int = 64,
                 aliases=None, clock=None) -> None:
        self.max_jobs = max_jobs
        self.max_permissions = max_permissions
        self.aliases = dict(DEFAULT_ALIASES if aliases is None else aliases)
        self._alias_lookup = self._build_alias_lookup(self.aliases)
        self._clock = clock
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._stats = {key: 0 for key in COUNTER_KEYS}

    @staticmethod
    def _build_alias_lookup(aliases: dict) -> dict[str, str]:
        lookup = {}
        for source, target in aliases.items():
            if isinstance(source, str) and isinstance(target, str):
                lookup[source.lower()] = target.lower()
        return lookup

    @staticmethod
    def _invalid(code: str, detail: object = None) -> PermissionAuditorError:
        return PermissionAuditorError(code, detail)

    @staticmethod
    def _valid_job_id(job_id: object) -> bool:
        return isinstance(job_id, str) and bool(job_id.strip())

    @staticmethod
    def _valid_count(count: object) -> bool:
        return isinstance(count, int) and not isinstance(count, bool) and count > 0

    @staticmethod
    def _valid_timestamp(value: object) -> bool:
        return value is None or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        )

    @staticmethod
    def _valid_ttl(ttl: object) -> bool:
        return (
            ttl is None
            or (
                isinstance(ttl, (int, float))
                and not isinstance(ttl, bool)
                and ttl > 0
            )
        )

    def _normalize_permission(self, permission: object) -> str:
        if not isinstance(permission, str):
            raise self._invalid(
                "PERMISSION_NAME_INVALID", {"permission": permission}
            )

        name = permission.lower()
        seen_aliases: set[str] = set()
        while name in self._alias_lookup and name not in seen_aliases:
            seen_aliases.add(name)
            target = self._alias_lookup[name]
            if not isinstance(target, str):
                raise self._invalid(
                    "PERMISSION_NAME_INVALID", {"permission": permission}
                )
            name = target.lower()

        if name in self._alias_lookup:
            raise self._invalid(
                "PERMISSION_NAME_INVALID",
                {"permission": permission, "alias": name},
            )

        segments = name.split(".")
        if len(segments) < 2 or any(segment == "" for segment in segments):
            raise self._invalid(
                "PERMISSION_NAME_INVALID", {"permission": permission}
            )
        return name

    def _current_time(self, now: object = None):
        if now is not None:
            return now
        if self._clock is None:
            return time.time()
        if callable(self._clock):
            return self._clock()
        clock_now = getattr(self._clock, "now", None)
        if callable(clock_now):
            return clock_now()
        return self._clock

    def _get_record(self, job_id: object) -> dict:
        if not self._valid_job_id(job_id):
            raise self._invalid("PERMISSION_JOB_INVALID", {"job_id": job_id})
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise self._invalid(
                "PERMISSION_UNKNOWN_JOB", {"job_id": job_id}
            ) from exc

    def _ensure_active(self, record: dict, now: object = None) -> None:
        job_id = record["job_id"]
        if record.get("expired") is True:
            raise self._invalid(
                "PERMISSION_DECLARATION_EXPIRED",
                {"job_id": job_id, "expired_at": record.get("expired_at")},
            )

        expires_at = record.get("expires_at")
        if expires_at is None:
            return

        if now is None and self._clock is None:
            return

        current = self._current_time(now)
        try:
            expired = current >= expires_at
        except TypeError as exc:
            raise self._invalid(
                "PERMISSION_DECLARATION_EXPIRED",
                {"job_id": job_id, "now": current, "expires_at": expires_at},
            ) from exc

        if expired:
            raise self._invalid(
                "PERMISSION_DECLARATION_EXPIRED",
                {"job_id": job_id, "now": current, "expires_at": expires_at},
            )

    def declare(self, job_id: str, permissions, ttl: int | None = None,
                now: int | None = None) -> dict:
        with self._lock:
            if not self._valid_job_id(job_id):
                raise self._invalid(
                    "PERMISSION_JOB_INVALID", {"job_id": job_id}
                )

            if (
                not isinstance(permissions, Iterable)
                or isinstance(permissions, (str, bytes))
            ):
                raise self._invalid(
                    "PERMISSION_DECLARE_INVALID",
                    {"job_id": job_id, "permissions": permissions},
                )

            try:
                raw_permissions = list(permissions)
            except TypeError as exc:
                raise self._invalid(
                    "PERMISSION_DECLARE_INVALID",
                    {"job_id": job_id, "permissions": permissions},
                ) from exc

            if not raw_permissions:
                raise self._invalid(
                    "PERMISSION_DECLARE_INVALID", {"job_id": job_id}
                )

            declared: list[str] = []
            seen: set[str] = set()
            for permission in raw_permissions:
                canonical = self._normalize_permission(permission)
                if canonical not in seen:
                    seen.add(canonical)
                    declared.append(canonical)
            declared.sort()

            if not self._valid_ttl(ttl):
                raise self._invalid(
                    "PERMISSION_TTL_INVALID", {"job_id": job_id, "ttl": ttl}
                )

            if job_id in self._jobs:
                raise self._invalid(
                    "PERMISSION_JOB_DUPLICATE", {"job_id": job_id}
                )

            if len(self._jobs) >= self.max_jobs:
                raise self._invalid(
                    "PERMISSION_JOB_LIMIT",
                    {"job_id": job_id, "max_jobs": self.max_jobs},
                )

            if len(declared) > self.max_permissions:
                raise self._invalid(
                    "PERMISSION_DECLARE_LIMIT",
                    {
                        "job_id": job_id,
                        "count": len(declared),
                        "max_permissions": self.max_permissions,
                    },
                )

            declared_at = self._current_time(now)
            expires_at = None if ttl is None else declared_at + ttl
            baseline = {
                "job_id": job_id,
                "version": 1,
                "counts": {permission: 0 for permission in declared},
                "captured_at": declared_at,
            }
            self._jobs[job_id] = {
                "job_id": job_id,
                "declared": declared,
                "version": 1,
                "declared_at": declared_at,
                "ttl": ttl,
                "expires_at": expires_at,
                "expired": False,
                "expired_at": None,
                "observations": {},
                "first_seen": {},
                "baseline_version": 1,
                "baseline": baseline,
            }
            return {
                "job_id": job_id,
                "declared": list(declared),
                "version": 1,
                "declared_at": declared_at,
                "ttl": ttl,
                "expires_at": expires_at,
            }

    def observe(self, job_id: str, permission: str, count: int = 1,
                now: int | None = None) -> dict:
        with self._lock:
            record = self._get_record(job_id)

            if not self._valid_count(count):
                raise self._invalid(
                    "PERMISSION_COUNT_INVALID", {"count": count}
                )

            canonical = self._normalize_permission(permission)
            observed_at = self._current_time(now)
            self._ensure_active(record, observed_at)

            observations = record["observations"]
            first_seen = record["first_seen"]
            total = observations.get(canonical, 0) + count
            observations[canonical] = total

            if canonical not in first_seen:
                first_seen[canonical] = observed_at
            else:
                first_seen[canonical] = min(first_seen[canonical], observed_at)

            declared = canonical in set(record["declared"])
            self._stats["observations"] += 1
            if not declared:
                self._stats["undeclared_hits"] += 1

            return {
                "job_id": job_id,
                "permission": canonical,
                "count": count,
                "total": total,
                "first_seen_at": first_seen[canonical],
                "declared": declared,
            }

    def report(self, job_id: str) -> dict:
        with self._lock:
            record = self._get_record(job_id)
            self._ensure_active(record)

            declared = list(record["declared"])
            declared_set = set(declared)
            observed = {
                permission: record["observations"][permission]
                for permission in sorted(record["observations"])
            }
            first_seen = {
                permission: record["first_seen"][permission]
                for permission in sorted(record["first_seen"])
            }
            undeclared = sorted(
                permission
                for permission in observed
                if permission not in declared_set
            )
            unused = [
                permission
                for permission in declared
                if permission not in observed
            ]
            violations = [
                {"permission": permission, "count": observed[permission]}
                for permission in undeclared
            ]

            self._stats["reports"] += 1
            return {
                "job_id": job_id,
                "declared": declared,
                "observed": observed,
                "undeclared": undeclared,
                "unused": unused,
                "violations": violations,
                "first_seen": first_seen,
                "total_observations": sum(observed.values()),
                "compliant": not violations,
                "version": record["version"],
                "baseline_version": record["baseline_version"],
            }

    def expire(self, now) -> int:
        with self._lock:
            expired_count = 0
            for record in self._jobs.values():
                if record.get("expired") is True:
                    continue
                expires_at = record.get("expires_at")
                if expires_at is None:
                    continue
                if now >= expires_at:
                    record["expired"] = True
                    record["expired_at"] = now
                    expired_count += 1
                    self._stats["expired_jobs"] += 1
            return expired_count

    def baseline(self, job_id: str) -> dict:
        with self._lock:
            record = self._get_record(job_id)
            self._ensure_active(record)
            return copy.deepcopy(record["baseline"])

    def get(self, job_id: str) -> dict:
        with self._lock:
            record = self._get_record(job_id)
            return copy.deepcopy(record)

    def jobs(self) -> list:
        with self._lock:
            return list(self._jobs)

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(
                {
                    "version": SNAPSHOT_VERSION,
                    "jobs": self._jobs,
                    "stats": self.stats(),
                }
            )

    def _validate_snapshot_stats(self, stats: object) -> dict:
        if not isinstance(stats, dict):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"field": "stats"}
            )

        validated = {}
        for key in STAT_KEYS:
            value = stats.get(key)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID",
                    {"field": f"stats.{key}", "value": value},
                )
            validated[key] = value
        return validated

    def _validate_snapshot_job(self, job_id: str, data: object) -> dict:
        if not self._valid_job_id(job_id) or not isinstance(data, dict):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )
        if "job_id" in data and data["job_id"] != job_id:
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )

        raw_declared = data.get("declared")
        if not isinstance(raw_declared, list):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID",
                {"job_id": job_id, "field": "declared"},
            )

        declared: list[str] = []
        declared_seen: set[str] = set()
        for permission in raw_declared:
            try:
                canonical = self._normalize_permission(permission)
            except PermissionAuditorError as exc:
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID",
                    {"job_id": job_id, "field": "declared"},
                ) from exc
            if canonical not in declared_seen:
                declared_seen.add(canonical)
                declared.append(canonical)
        declared.sort()

        raw_observations = data.get("observations", {})
        raw_first_seen = data.get("first_seen", {})
        if not isinstance(raw_observations, dict) or not isinstance(
            raw_first_seen, dict
        ):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )

        observations: dict[str, int] = {}
        for permission, value in raw_observations.items():
            try:
                canonical = self._normalize_permission(permission)
            except PermissionAuditorError as exc:
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID",
                    {"job_id": job_id, "field": "observations"},
                ) from exc
            if not self._valid_count(value):
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID",
                    {"job_id": job_id, "permission": canonical},
                )
            observations[canonical] = observations.get(canonical, 0) + value

        first_seen: dict[str, object] = {}
        for permission, value in raw_first_seen.items():
            try:
                canonical = self._normalize_permission(permission)
            except PermissionAuditorError as exc:
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID",
                    {"job_id": job_id, "field": "first_seen"},
                ) from exc
            if not self._valid_timestamp(value) or value is None:
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID",
                    {"job_id": job_id, "permission": canonical},
                )
            if canonical in first_seen:
                first_seen[canonical] = min(first_seen[canonical], value)
            else:
                first_seen[canonical] = value

        if set(observations) != set(first_seen):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )

        version = data.get("version", 1)
        baseline_version = data.get("baseline_version", version)
        if not self._valid_count(version) or not self._valid_count(
            baseline_version
        ):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )

        declared_at = data.get("declared_at")
        if not self._valid_timestamp(declared_at) or declared_at is None:
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID",
                {"job_id": job_id, "field": "declared_at"},
            )

        ttl = data.get("ttl")
        if not self._valid_ttl(ttl):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID",
                {"job_id": job_id, "field": "ttl"},
            )

        expires_at = data.get("expires_at")
        if not self._valid_timestamp(expires_at):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID",
                {"job_id": job_id, "field": "expires_at"},
            )
        if ttl is None:
            expires_at = None
        else:
            expires_at = declared_at + ttl

        expired = data.get("expired", False)
        if not isinstance(expired, bool):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )

        expired_at = data.get("expired_at")
        if not self._valid_timestamp(expired_at):
            raise self._invalid(
                "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
            )

        baseline_data = data.get("baseline")
        captured_at = declared_at
        if isinstance(baseline_data, dict):
            baseline_captured_at = baseline_data.get(
                "captured_at", declared_at
            )
            if (
                not self._valid_timestamp(baseline_captured_at)
                or baseline_captured_at is None
            ):
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID", {"job_id": job_id}
                )
            captured_at = baseline_captured_at

        baseline = {
            "job_id": job_id,
            "version": baseline_version,
            "counts": {permission: 0 for permission in declared},
            "captured_at": captured_at,
        }

        return {
            "job_id": job_id,
            "declared": declared,
            "version": version,
            "declared_at": declared_at,
            "ttl": ttl,
            "expires_at": expires_at,
            "expired": expired,
            "expired_at": expired_at,
            "observations": observations,
            "first_seen": first_seen,
            "baseline_version": baseline_version,
            "baseline": baseline,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            version = state.get("version") if isinstance(state, dict) else None
            if (
                not isinstance(state, dict)
                or not isinstance(version, int)
                or isinstance(version, bool)
                or version != SNAPSHOT_VERSION
            ):
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID", {"field": "version"}
                )

            jobs = state.get("jobs")
            if not isinstance(jobs, dict):
                raise self._invalid(
                    "PERMISSION_SNAPSHOT_INVALID", {"field": "jobs"}
                )

            restored_stats = self._validate_snapshot_stats(state.get("stats"))
            restored_jobs = {}
            for job_id, job_state in jobs.items():
                restored_jobs[job_id] = self._validate_snapshot_job(
                    job_id, job_state
                )

            self._jobs = restored_jobs
            self._stats = {
                key: restored_stats[key] for key in COUNTER_KEYS
            }

    def stats(self) -> dict:
        with self._lock:
            return {"jobs": len(self._jobs), **dict(self._stats)}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in COUNTER_KEYS}
