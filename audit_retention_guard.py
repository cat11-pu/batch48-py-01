"""Audit retention guard: retention policies, legal holds and purge receipts."""

from __future__ import annotations

import copy
import threading


DAY = 86400
SNAPSHOT_VERSION = 1


def _valid_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_timestamp(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class RetentionError(ValueError):
    """Rejection raised by the retention guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RetentionGuard:
    """Enforce retention periods, legal holds and auditable destruction."""

    def __init__(self, *, max_records: int = 500, max_holds: int = 32,
                 clock=None) -> None:
        self.max_records = max_records
        self.max_holds = max_holds
        self._clock = clock
        self._lock = threading.RLock()
        self._policies: dict[tuple[str, str], dict] = {}
        self._records: dict[tuple[str, str], dict] = {}
        self._holds: dict[str, dict] = {}
        self._receipts: dict[str, dict] = {}
        self._next_record = 1
        self._next_hold = 1
        self._next_receipt = 1
        self._purged = 0
        self._last_purge_at: int | None = None

    @staticmethod
    def _policy_copy(policy: dict) -> dict:
        return {
            "tenant": policy["tenant"],
            "category": policy["category"],
            "days": policy["days"],
            "version": policy["version"],
        }

    @staticmethod
    def _hold_copy(hold: dict) -> dict:
        result = {
            "hold_id": hold["hold_id"],
            "tenant": hold["tenant"],
            "scope": hold["scope"],
            "until": hold["until"],
            "reason": hold["reason"],
            "status": hold["status"],
        }
        if "released_at" in hold:
            result["released_at"] = hold["released_at"]
        return result

    @staticmethod
    def _receipt_copy(receipt: dict) -> dict:
        return {
            "receipt_id": receipt["receipt_id"],
            "at": receipt["at"],
            "count": receipt["count"],
            "removed": list(receipt["removed"]),
            "blocked": list(receipt["blocked"]),
            "blocked_count": receipt["blocked_count"],
            "tenants": dict(receipt["tenants"]),
        }

    def _now(self) -> int:
        if self._clock is None:
            return 0
        try:
            now = self._clock()
        except Exception as exc:
            raise RetentionError("RETENTION_TIMESTAMP_INVALID", exc) from exc
        if not _valid_timestamp(now):
            raise RetentionError("RETENTION_TIMESTAMP_INVALID", now)
        return now

    def _tenant_exists(self, tenant: str) -> bool:
        return any(key[0] == tenant for key in self._policies)

    def _expiry(self, record: dict) -> int:
        policy = self._policies.get((record["tenant"], record["category"]))
        if policy is None:
            raise RetentionError(
                "RETENTION_POLICY_MISSING",
                (record["tenant"], record["category"]),
            )
        return record["at"] + policy["days"] * DAY

    def _record_copy(self, record: dict) -> dict:
        return {
            "id": record["id"],
            "tenant": record["tenant"],
            "category": record["category"],
            "at": record["at"],
            "expires": self._expiry(record),
        }

    @staticmethod
    def _sequence_key(identifier: str, prefix: str) -> tuple[int, str]:
        marker = prefix + "-"
        if identifier.startswith(marker):
            suffix = identifier[len(marker):]
            if suffix.isdigit():
                return int(suffix), identifier
        return 2**63 - 1, identifier

    def _hold_is_active_for(self, hold: dict, tenant: str, category: str,
                            now: int) -> bool:
        if hold["status"] != "active" or now >= hold["until"]:
            return False
        if hold["tenant"] != tenant:
            return False
        return hold["scope"] == "tenant" or hold["scope"] == f"category:{category}"

    def set_policy(self, tenant: str, category: str, days: int) -> dict:
        if not _valid_text(tenant) or not _valid_text(category):
            raise RetentionError(
                "RETENTION_POLICY_INVALID", (tenant, category, days)
            )
        if (not isinstance(days, int) or isinstance(days, bool)
                or not 1 <= days <= 3650):
            raise RetentionError(
                "RETENTION_POLICY_INVALID", (tenant, category, days)
            )
        with self._lock:
            key = (tenant, category)
            previous = self._policies.get(key)
            version = 1 if previous is None else previous["version"] + 1
            policy = {
                "tenant": tenant,
                "category": category,
                "days": days,
                "version": version,
            }
            self._policies[key] = policy
            return self._policy_copy(policy)

    def policy_for(self, tenant: str, category: str) -> dict:
        with self._lock:
            if _valid_text(tenant) and not self._tenant_exists(tenant):
                raise RetentionError("RETENTION_UNKNOWN_TENANT", tenant)
            policy = self._policies.get((tenant, category))
            if policy is None:
                raise RetentionError(
                    "RETENTION_POLICY_MISSING", (tenant, category)
                )
            return self._policy_copy(policy)

    def add_record(self, tenant: str, category: str, at: int,
                   record_id: str | None = None) -> dict:
        if not _valid_text(tenant) or not _valid_text(category):
            raise RetentionError("RETENTION_POLICY_MISSING", (tenant, category))
        if not _valid_timestamp(at):
            raise RetentionError("RETENTION_TIMESTAMP_INVALID", at)
        if record_id is not None and not _valid_text(record_id):
            raise RetentionError("RETENTION_RECORD_INVALID", record_id)

        with self._lock:
            if not self._tenant_exists(tenant):
                raise RetentionError("RETENTION_UNKNOWN_TENANT", tenant)
            policy = self._policies.get((tenant, category))
            if policy is None:
                raise RetentionError(
                    "RETENTION_POLICY_MISSING", (tenant, category)
                )
            if record_id is not None and (tenant, record_id) in self._records:
                raise RetentionError("RETENTION_DUPLICATE_RECORD", record_id)
            if len(self._records) >= self.max_records:
                raise RetentionError("RETENTION_RECORD_LIMIT", self.max_records)

            if record_id is None:
                while True:
                    candidate = f"record-{self._next_record}"
                    self._next_record += 1
                    if (tenant, candidate) not in self._records:
                        record_id = candidate
                        break

            record = {
                "id": record_id,
                "tenant": tenant,
                "category": category,
                "at": at,
            }
            self._records[(tenant, record_id)] = record
            return self._record_copy(record)

    def hold(self, tenant: str, scope: str, until: int, reason: str) -> dict:
        if not _valid_timestamp(until):
            raise RetentionError("RETENTION_TIMESTAMP_INVALID", until)
        if not _valid_text(reason) or not isinstance(scope, str):
            raise RetentionError(
                "RETENTION_HOLD_INVALID", (tenant, scope, until, reason)
            )

        with self._lock:
            if not _valid_text(tenant) or not self._tenant_exists(tenant):
                raise RetentionError("RETENTION_UNKNOWN_TENANT", tenant)
            if scope == "tenant":
                pass
            elif scope.startswith("category:"):
                category = scope[len("category:"):]
                if (not _valid_text(category)
                        or (tenant, category) not in self._policies):
                    raise RetentionError("RETENTION_HOLD_INVALID", scope)
            else:
                raise RetentionError("RETENTION_HOLD_INVALID", scope)
            now = self._now()
            if until <= now:
                raise RetentionError(
                    "RETENTION_HOLD_INVALID", (until, now)
                )
            for hold in self._holds.values():
                if (hold["status"] == "active"
                        and hold["tenant"] == tenant
                        and hold["scope"] == scope
                        and hold["until"] == until
                        and hold["reason"] == reason):
                    return self._hold_copy(hold)
            active = sum(
                1 for hold in self._holds.values()
                if hold["status"] == "active"
            )
            if active >= self.max_holds:
                raise RetentionError("RETENTION_HOLD_LIMIT", self.max_holds)

            hold_id = f"hold-{self._next_hold}"
            self._next_hold += 1
            hold = {
                "hold_id": hold_id,
                "tenant": tenant,
                "scope": scope,
                "until": until,
                "reason": reason,
                "status": "active",
            }
            self._holds[hold_id] = hold
            return self._hold_copy(hold)

    def release(self, hold_id: str, now: int | None = None) -> dict:
        with self._lock:
            hold = self._holds.get(hold_id) if isinstance(hold_id, str) else None
            if hold is None:
                raise RetentionError("RETENTION_HOLD_UNKNOWN", hold_id)
            if hold["status"] == "released":
                raise RetentionError("RETENTION_HOLD_CLOSED", hold_id)
            if now is None:
                now = self._now()
            elif not _valid_timestamp(now):
                raise RetentionError("RETENTION_TIMESTAMP_INVALID", now)
            hold["status"] = "released"
            hold["released_at"] = now
            return {
                "hold_id": hold_id,
                "status": "released",
                "released_at": now,
            }

    def hold_status(self, hold_id: str) -> dict:
        with self._lock:
            hold = self._holds.get(hold_id) if isinstance(hold_id, str) else None
            if hold is None:
                raise RetentionError("RETENTION_HOLD_UNKNOWN", hold_id)
            return self._hold_copy(hold)

    def purge(self, now: int) -> dict:
        if not _valid_timestamp(now):
            raise RetentionError("RETENTION_TIMESTAMP_INVALID", now)

        with self._lock:
            if self._last_purge_at is not None and now < self._last_purge_at:
                raise RetentionError(
                    "RETENTION_CLOCK_ROLLBACK", (now, self._last_purge_at)
                )

            records = list(self._records.values())
            held: dict[tuple[str, str], bool] = {}
            due: dict[tuple[str, str], bool] = {}
            segments: dict[str, list[dict]] = {}
            for record in records:
                key = (record["tenant"], record["id"])
                due[key] = now >= self._expiry(record)
                held[key] = any(
                    self._hold_is_active_for(
                        hold, record["tenant"], record["category"], now
                    )
                    for hold in self._holds.values()
                )
                segments.setdefault(record["id"], []).append(record)

            removed_keys: set[tuple[str, str]] = set()
            blocked_keys: set[tuple[str, str]] = set()
            for segment in segments.values():
                segment_keys = [
                    (record["tenant"], record["id"]) for record in segment
                ]
                if (all(due[key] for key in segment_keys)
                        and not any(held[key] for key in segment_keys)):
                    removed_keys.update(segment_keys)
                else:
                    blocked_keys.update(
                        key for key in segment_keys if due[key]
                    )

            ordered = sorted(
                records,
                key=lambda row: (row["at"], row["id"], row["tenant"]),
            )
            removed_rows = [
                row for row in ordered
                if (row["tenant"], row["id"]) in removed_keys
            ]
            blocked_rows = [
                row for row in ordered
                if (row["tenant"], row["id"]) in blocked_keys
            ]
            tenants: dict[str, int] = {}
            for row in removed_rows:
                tenants[row["tenant"]] = tenants.get(row["tenant"], 0) + 1

            receipt_id = f"purge-{self._next_receipt}"
            self._next_receipt += 1
            receipt = {
                "receipt_id": receipt_id,
                "at": now,
                "count": len(removed_rows),
                "removed": [row["id"] for row in removed_rows],
                "blocked": [row["id"] for row in blocked_rows],
                "blocked_count": len(blocked_rows),
                "tenants": tenants,
            }
            for row in removed_rows:
                del self._records[(row["tenant"], row["id"])]
            self._purged += len(removed_rows)
            self._receipts[receipt_id] = receipt
            self._last_purge_at = now
            return self._receipt_copy(receipt)

    def query(self, tenant: str, start: int, end: int) -> list:
        if not _valid_text(tenant):
            raise RetentionError("RETENTION_UNKNOWN_TENANT", tenant)
        if (not _valid_timestamp(start) or not _valid_timestamp(end)
                or start > end):
            raise RetentionError(
                "RETENTION_WINDOW_INVALID", (start, end)
            )
        with self._lock:
            if not self._tenant_exists(tenant):
                raise RetentionError("RETENTION_UNKNOWN_TENANT", tenant)
            rows = [
                record for record in self._records.values()
                if record["tenant"] == tenant
                and start <= record["at"] <= end
            ]
            rows.sort(key=lambda row: (row["at"], row["id"]))
            return [self._record_copy(row) for row in rows]

    def describe_receipt(self, receipt_id: str) -> dict:
        with self._lock:
            receipt = (
                self._receipts.get(receipt_id)
                if isinstance(receipt_id, str) else None
            )
            if receipt is None:
                raise RetentionError("RETENTION_RECEIPT_UNKNOWN", receipt_id)
            return self._receipt_copy(receipt)

    def snapshot(self) -> dict:
        with self._lock:
            policies = [
                self._policy_copy(policy)
                for _, policy in sorted(self._policies.items())
            ]
            records = [
                self._record_copy(record)
                for _, record in sorted(
                    self._records.items(),
                    key=lambda item: (
                        item[1]["at"], item[1]["id"], item[1]["tenant"]
                    ),
                )
            ]
            holds = [
                self._hold_copy(hold)
                for _, hold in sorted(
                    self._holds.items(),
                    key=lambda item: self._sequence_key(item[0], "hold"),
                )
            ]
            receipts = [
                self._receipt_copy(receipt)
                for _, receipt in sorted(
                    self._receipts.items(),
                    key=lambda item: self._sequence_key(item[0], "purge"),
                )
            ]
            return copy.deepcopy({
                "version": SNAPSHOT_VERSION,
                "policies": policies,
                "records": records,
                "holds": holds,
                "receipts": receipts,
                "counters": {
                    "record": self._next_record,
                    "hold": self._next_hold,
                    "receipt": self._next_receipt,
                    "purged": self._purged,
                },
                "last_purge_at": self._last_purge_at,
            })

    def restore(self, state: dict) -> None:
        try:
            restored = self._validated_state(state)
        except RetentionError as exc:
            if exc.code == "RETENTION_SNAPSHOT_INVALID":
                raise
            raise RetentionError("RETENTION_SNAPSHOT_INVALID", exc.detail) from exc
        except Exception as exc:
            raise RetentionError("RETENTION_SNAPSHOT_INVALID", exc) from exc

        with self._lock:
            self._policies = restored["policies"]
            self._records = restored["records"]
            self._holds = restored["holds"]
            self._receipts = restored["receipts"]
            self._next_record = restored["next_record"]
            self._next_hold = restored["next_hold"]
            self._next_receipt = restored["next_receipt"]
            self._purged = restored["purged"]
            self._last_purge_at = restored["last_purge_at"]

    def _validated_state(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise RetentionError("RETENTION_SNAPSHOT_INVALID", state)
        required = {
            "version", "policies", "records", "holds", "receipts",
            "counters", "last_purge_at",
        }
        if not required.issubset(state):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "missing fields"
            )
        if (not isinstance(state["version"], int)
                or isinstance(state["version"], bool)
                or state["version"] != SNAPSHOT_VERSION):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", state["version"]
            )
        if not isinstance(state["policies"], list):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "policies"
            )

        policies: dict[tuple[str, str], dict] = {}
        for raw in state["policies"]:
            if not isinstance(raw, dict):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "policy"
                )
            tenant = raw.get("tenant")
            category = raw.get("category")
            days = raw.get("days")
            version = raw.get("version")
            if (not _valid_text(tenant) or not _valid_text(category)
                    or not isinstance(days, int) or isinstance(days, bool)
                    or not 1 <= days <= 3650
                    or not isinstance(version, int)
                    or isinstance(version, bool) or version < 1):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "policy"
                )
            key = (tenant, category)
            if key in policies:
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "duplicate policy"
                )
            policies[key] = {
                "tenant": tenant,
                "category": category,
                "days": days,
                "version": version,
            }

        if not isinstance(state["records"], list):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "records"
            )
        if len(state["records"]) > self.max_records:
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "record limit"
            )
        records: dict[tuple[str, str], dict] = {}
        for raw in state["records"]:
            if not isinstance(raw, dict):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "record"
                )
            record_id = raw.get("id")
            tenant = raw.get("tenant")
            category = raw.get("category")
            at = raw.get("at")
            if (not _valid_text(record_id) or not _valid_text(tenant)
                    or not _valid_text(category)
                    or not _valid_timestamp(at)
                    or (tenant, category) not in policies):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "record"
                )
            key = (tenant, record_id)
            if key in records:
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "duplicate record"
                )
            records[key] = {
                "id": record_id,
                "tenant": tenant,
                "category": category,
                "at": at,
            }

        if not isinstance(state["holds"], list):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "holds"
            )
        holds: dict[str, dict] = {}
        active_holds = 0
        for raw in state["holds"]:
            if not isinstance(raw, dict):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "hold"
                )
            hold_id = raw.get("hold_id")
            tenant = raw.get("tenant")
            scope = raw.get("scope")
            until = raw.get("until")
            reason = raw.get("reason")
            status = raw.get("status")
            if (not _valid_text(hold_id) or not _valid_text(tenant)
                    or not _valid_text(scope) or not _valid_timestamp(until)
                    or not _valid_text(reason)
                    or status not in ("active", "released")
                    or not self._restored_tenant_exists(policies, tenant)):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "hold"
                )
            if scope == "tenant":
                pass
            elif scope.startswith("category:"):
                category = scope[len("category:"):]
                if ((tenant, category) not in policies):
                    raise RetentionError(
                        "RETENTION_SNAPSHOT_INVALID", "hold scope"
                    )
            else:
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "hold scope"
                )
            hold = {
                "hold_id": hold_id,
                "tenant": tenant,
                "scope": scope,
                "until": until,
                "reason": reason,
                "status": status,
            }
            if status == "released":
                released_at = raw.get("released_at")
                if not _valid_timestamp(released_at):
                    raise RetentionError(
                        "RETENTION_SNAPSHOT_INVALID", "released_at"
                    )
                hold["released_at"] = released_at
            else:
                active_holds += 1
            if hold_id in holds:
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "duplicate hold"
                )
            holds[hold_id] = hold
        if active_holds > self.max_holds:
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "hold limit"
            )

        if not isinstance(state["receipts"], list):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "receipts"
            )
        receipts: dict[str, dict] = {}
        for raw in state["receipts"]:
            if not isinstance(raw, dict):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "receipt"
                )
            receipt_id = raw.get("receipt_id")
            at = raw.get("at")
            removed = raw.get("removed")
            blocked = raw.get("blocked")
            count = raw.get("count")
            blocked_count = raw.get("blocked_count")
            tenants = raw.get("tenants")
            if (not _valid_text(receipt_id) or not _valid_timestamp(at)
                    or not isinstance(removed, list)
                    or not all(_valid_text(item) for item in removed)
                    or not isinstance(blocked, list)
                    or not all(_valid_text(item) for item in blocked)
                    or not isinstance(count, int) or isinstance(count, bool)
                    or count < 0 or count != len(removed)
                    or not isinstance(blocked_count, int)
                    or isinstance(blocked_count, bool)
                    or blocked_count < 0
                    or blocked_count != len(blocked)
                    or not isinstance(tenants, dict)
                    or not all(
                        _valid_text(tenant)
                        and isinstance(number, int)
                        and not isinstance(number, bool)
                        and number > 0
                        for tenant, number in tenants.items()
                    )
                    or sum(tenants.values()) != count
                    or receipt_id in receipts):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "receipt"
                )
            receipts[receipt_id] = {
                "receipt_id": receipt_id,
                "at": at,
                "count": count,
                "removed": list(removed),
                "blocked": list(blocked),
                "blocked_count": blocked_count,
                "tenants": dict(tenants),
            }

        counters = state["counters"]
        if not isinstance(counters, dict):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "counters"
            )
        counter_names = ("record", "hold", "receipt", "purged")
        if any(
            name not in counters
            or not isinstance(counters[name], int)
            or isinstance(counters[name], bool)
            or counters[name] < 0
            for name in counter_names
        ):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "counters"
            )
        if (counters["record"] < 1 or counters["hold"] < 1
                or counters["receipt"] < 1):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "counters"
            )
        if counters["purged"] != sum(
            receipt["count"] for receipt in receipts.values()
        ):
            raise RetentionError(
                "RETENTION_SNAPSHOT_INVALID", "purged counter"
            )

        last_purge_at = state["last_purge_at"]
        if last_purge_at is not None:
            if not _valid_timestamp(last_purge_at):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "last_purge_at"
                )
            if any(receipt["at"] > last_purge_at
                   for receipt in receipts.values()):
                raise RetentionError(
                    "RETENTION_SNAPSHOT_INVALID", "last_purge_at"
                )

        return {
            "policies": policies,
            "records": records,
            "holds": holds,
            "receipts": receipts,
            "next_record": counters["record"],
            "next_hold": counters["hold"],
            "next_receipt": counters["receipt"],
            "purged": counters["purged"],
            "last_purge_at": last_purge_at,
        }

    @staticmethod
    def _restored_tenant_exists(
        policies: dict[tuple[str, str], dict], tenant: str
    ) -> bool:
        return any(key[0] == tenant for key in policies)

    def stats(self) -> dict:
        with self._lock:
            return {
                "records": len(self._records),
                "holds": len(self._holds),
                "holds_active": sum(
                    1 for hold in self._holds.values()
                    if hold["status"] == "active"
                ),
                "purged": self._purged,
                "receipts": len(self._receipts),
            }
