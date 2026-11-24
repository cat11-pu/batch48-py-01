"""OAuth consent grant registry: incremental scopes, prompt suppression, revocation cascade."""

from __future__ import annotations

import copy
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any


SNAPSHOT_VERSION = 1
STAT_KEYS = (
    "versions",
    "suppressed",
    "prompted",
    "withdrawn",
    "revoked_tokens",
    "purged",
)
SNAPSHOT_KEYS = {"version", "records", "tokens", "deprecated", "counters", "stats"}
RECORD_KEYS = {
    "tenant",
    "user",
    "client",
    "scopes",
    "version",
    "granted_at",
    "expires_at",
}
TOKEN_KEYS = {"tenant", "user", "client", "scopes", "revoked"}
VERSION_COUNTER_KEYS = {"tenant", "user", "client", "version"}


class ConsentError(ValueError):
    """Raised when a consent operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GrantRecord:
    tenant: str
    user: str
    client: str
    scopes: tuple[str, ...]
    version: int
    granted_at: str
    expires_at: str | None


@dataclass(frozen=True)
class ConsentDecision:
    status: str
    record: GrantRecord | None
    missing_scopes: tuple[str, ...]
    rejected_scopes: tuple[str, ...]


Key = tuple[str, str, str]


def _invalid_request(detail: str) -> ConsentError:
    return ConsentError("CONSENT_INVALID_REQUEST", detail)


def _snapshot_invalid(detail: str) -> ConsentError:
    return ConsentError("CONSENT_SNAPSHOT_INVALID", detail)


def _require_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _invalid_request(f"{name} must be a non-empty string")
    return value


def _normalize_scopes(value: object, *, required: bool) -> tuple[str, ...]:
    if value is None:
        if required:
            raise _invalid_request("scopes must be a non-empty sequence of strings")
        return ()

    if isinstance(value, (str, bytes)):
        raise _invalid_request("scopes must be a sequence of strings")

    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise _invalid_request("scopes must be a sequence of strings") from exc

    scopes: set[str] = set()
    for scope in items:
        if not isinstance(scope, str) or not scope.strip():
            raise _invalid_request("every scope must be a non-empty string")
        scopes.add(scope)

    if required and not scopes:
        raise _invalid_request("at least one scope is required")

    return tuple(sorted(scopes))


def _parse_time(value: object) -> tuple[str, datetime]:
    if isinstance(value, datetime):
        moment = value
        stamp = moment.isoformat()
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        moment = datetime.fromtimestamp(value, tz=timezone.utc)
        stamp = moment.isoformat()
    elif isinstance(value, str) and value.strip():
        stamp = value
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise _invalid_request("now must be a valid ISO-8601 timestamp") from exc
    else:
        raise _invalid_request("now must be a valid timestamp")

    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    return stamp, moment.astimezone(timezone.utc)


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class ConsentGrantRegistry:
    """Keep per (tenant, user, client) consent records and derived token state."""

    def __init__(self, *, max_grants_per_tenant: int = 100,
                 default_ttl: int = 86400, clock=None) -> None:
        self.max_grants_per_tenant = max_grants_per_tenant
        self.default_ttl = default_ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._records: dict[Key, GrantRecord] = {}
        self._tokens: dict[str, dict[str, Any]] = {}
        self._deprecated: set[str] = set()
        self._version_numbers: dict[Key, int] = {}
        self._stats: dict[str, int] = {key: 0 for key in STAT_KEYS}

    def _resolve_now(self, now: object = None) -> tuple[str, datetime]:
        if now is not None:
            raw_now = now
        elif self._clock is None:
            raw_now = datetime.now(timezone.utc)
        elif callable(self._clock):
            raw_now = self._clock()
        elif hasattr(self._clock, "now") and callable(self._clock.now):
            raw_now = self._clock.now()
        else:
            raw_now = self._clock
        return _parse_time(raw_now)

    def _expires_at(self, moment: datetime) -> str | None:
        if self.default_ttl is None:
            return None
        try:
            ttl_seconds = float(self.default_ttl)
        except (TypeError, ValueError):
            return None
        return (moment + timedelta(seconds=ttl_seconds)).isoformat()

    def _is_expired(self, record: GrantRecord, moment: datetime) -> bool:
        if record.expires_at is None:
            return False
        try:
            _, expiry = _parse_time(record.expires_at)
        except ConsentError:
            return False
        return moment >= expiry

    @staticmethod
    def _key(tenant: str, user: str, client: str) -> Key:
        return tenant, user, client

    def _active_record(self, key: Key, moment: datetime) -> GrantRecord | None:
        record = self._records.get(key)
        if record is None or self._is_expired(record, moment):
            return None
        return record

    def _partition_deprecated(
        self, scopes: tuple[str, ...]
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        rejected = tuple(scope for scope in scopes if scope in self._deprecated)
        accepted = tuple(scope for scope in scopes if scope not in self._deprecated)
        return accepted, rejected

    def grant_consent(self, tenant: str | None = None, user: str | None = None,
                      client: str | None = None, scopes: object = None,
                      now: object = None) -> ConsentDecision:
        tenant = _require_identifier(tenant, "tenant")
        user = _require_identifier(user, "user")
        client = _require_identifier(client, "client")
        requested = _normalize_scopes(scopes, required=True)
        granted_at, moment = self._resolve_now(now)
        key = self._key(tenant, user, client)

        with self._lock:
            accepted, rejected = self._partition_deprecated(requested)
            existing = self._records.get(key)
            active = self._active_record(key, moment)

            if not accepted:
                if active is not None:
                    self._stats["suppressed"] += 1
                    return ConsentDecision("granted", active, (), rejected)
                return ConsentDecision("rejected", None, (), rejected)

            if active is not None:
                active_scopes = set(active.scopes)
                if all(scope in active_scopes for scope in accepted):
                    self._stats["suppressed"] += 1
                    return ConsentDecision("granted", active, (), rejected)

                merged = tuple(sorted(active_scopes.union(accepted)))
                previous_version = max(
                    active.version, self._version_numbers.get(key, active.version)
                )
                version = previous_version + 1
                record = GrantRecord(
                    tenant=tenant,
                    user=user,
                    client=client,
                    scopes=merged,
                    version=version,
                    granted_at=granted_at,
                    expires_at=self._expires_at(moment),
                )
                self._records[key] = record
                self._version_numbers[key] = version
                self._stats["versions"] += 1
                return ConsentDecision("granted", record, (), rejected)

            tenant_records = sum(
                1 for record_key in self._records if record_key[0] == tenant
            )
            if key not in self._records and tenant_records >= self.max_grants_per_tenant:
                detail = {
                    "tenant": tenant,
                    "limit": self.max_grants_per_tenant,
                    "active": tenant_records,
                }
                raise ConsentError("CONSENT_CAPACITY_EXCEEDED", detail)

            previous_version = self._version_numbers.get(
                key, existing.version if existing is not None else 0
            )
            version = previous_version + 1
            record = GrantRecord(
                tenant=tenant,
                user=user,
                client=client,
                scopes=accepted,
                version=version,
                granted_at=granted_at,
                expires_at=self._expires_at(moment),
            )
            self._records[key] = record
            self._version_numbers[key] = version
            self._stats["versions"] += 1
            return ConsentDecision("granted", record, (), rejected)

    def check_consent(self, tenant: str | None = None, user: str | None = None,
                      client: str | None = None, scopes: object = None,
                      now: object = None) -> ConsentDecision:
        tenant = _require_identifier(tenant, "tenant")
        user = _require_identifier(user, "user")
        client = _require_identifier(client, "client")
        requested = _normalize_scopes(scopes, required=True)
        _, moment = self._resolve_now(now)
        key = self._key(tenant, user, client)

        with self._lock:
            accepted, rejected = self._partition_deprecated(requested)
            active = self._active_record(key, moment)

            if active is None:
                self._stats["prompted"] += 1
                return ConsentDecision("prompt", None, accepted, rejected)

            if not accepted:
                self._stats["suppressed"] += 1
                return ConsentDecision("granted", active, (), rejected)

            active_scopes = set(active.scopes)
            missing = tuple(scope for scope in accepted if scope not in active_scopes)
            if missing:
                self._stats["prompted"] += 1
                return ConsentDecision("prompt", active, missing, rejected)

            self._stats["suppressed"] += 1
            return ConsentDecision("granted", active, (), rejected)

    def withdraw_consent(self, tenant: str | None = None, user: str | None = None,
                         client: str | None = None, scopes: object = None,
                         now: object = None) -> int:
        tenant = _require_identifier(tenant, "tenant")
        user = _require_identifier(user, "user")
        client = _require_identifier(client, "client")
        wanted = None if scopes is None else _normalize_scopes(scopes, required=True)
        self._resolve_now(now)
        key = self._key(tenant, user, client)

        with self._lock:
            revoked_tokens = 0
            for token in self._tokens.values():
                token_key = (token["tenant"], token["user"], token["client"])
                if token_key == key and not token["revoked"]:
                    token["revoked"] = True
                    revoked_tokens += 1

            self._stats["revoked_tokens"] += revoked_tokens

            record = self._records.get(key)
            if record is not None:
                if wanted is None:
                    self._records.pop(key, None)
                    self._stats["withdrawn"] += 1
                else:
                    wanted_scopes = set(wanted)
                    remaining = tuple(
                        scope for scope in record.scopes if scope not in wanted_scopes
                    )
                    if len(remaining) != len(record.scopes):
                        if remaining:
                            self._records[key] = replace(record, scopes=remaining)
                        else:
                            self._records.pop(key, None)
                        self._stats["withdrawn"] += 1

            return revoked_tokens

    def register_token(self, token_id: str | None = None, scopes: object = (), *,
                       tenant: str | None = None, user: str | None = None,
                       client: str | None = None, now: object = None) -> None:
        if not isinstance(token_id, str) or not token_id.strip():
            raise _invalid_request("token_id must be a non-empty string")
        token_scopes = _normalize_scopes(scopes, required=False)

        identities = (tenant, user, client)
        if any(value is not None for value in identities):
            tenant = _require_identifier(tenant, "tenant")
            user = _require_identifier(user, "user")
            client = _require_identifier(client, "client")

        with self._lock:
            self._tokens[token_id] = {
                "tenant": tenant,
                "user": user,
                "client": client,
                "scopes": token_scopes,
                "revoked": False,
            }

    def token_usable(self, token_id: str) -> bool:
        with self._lock:
            token = self._tokens.get(token_id)
            return token is not None and not token["revoked"]

    def mark_scope_deprecated(self, scope: str) -> None:
        scope = _require_identifier(scope, "scope")
        with self._lock:
            self._deprecated.add(scope)

    def purge_expired(self, now: object = None) -> int:
        _, moment = self._resolve_now(now)
        with self._lock:
            expired_keys = [
                key
                for key, record in self._records.items()
                if self._is_expired(record, moment)
            ]
            for key in expired_keys:
                self._records.pop(key, None)

            count = len(expired_keys)
            self._stats["purged"] += count
            return count

    def records_for(self, tenant: str) -> list[GrantRecord]:
        tenant = _require_identifier(tenant, "tenant")
        with self._lock:
            rows = [record for record in self._records.values()
                    if record.tenant == tenant]
            return sorted(rows, key=lambda record: (record.user, record.client))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "records": [
                    {
                        "tenant": record.tenant,
                        "user": record.user,
                        "client": record.client,
                        "scopes": list(record.scopes),
                        "version": record.version,
                        "granted_at": record.granted_at,
                        "expires_at": record.expires_at,
                    }
                    for record in sorted(
                        self._records.values(),
                        key=lambda item: (item.tenant, item.user, item.client),
                    )
                ],
                "tokens": {
                    token_id: {
                        "tenant": token["tenant"],
                        "user": token["user"],
                        "client": token["client"],
                        "scopes": list(token["scopes"]),
                        "revoked": token["revoked"],
                    }
                    for token_id, token in sorted(self._tokens.items())
                },
                "deprecated": sorted(self._deprecated),
                "counters": {
                    "version_numbers": [
                        {
                            "tenant": key[0],
                            "user": key[1],
                            "client": key[2],
                            "version": version,
                        }
                        for key, version in sorted(self._version_numbers.items())
                    ]
                },
                "stats": dict(self._stats),
            }

    @classmethod
    def _parse_snapshot(
        cls, state: object, max_grants_per_tenant: int
    ) -> tuple[
        dict[Key, GrantRecord],
        dict[str, dict[str, Any]],
        set[str],
        dict[Key, int],
        dict[str, int],
    ]:
        if not isinstance(state, dict):
            raise _snapshot_invalid("snapshot must be an object")
        if set(state) != SNAPSHOT_KEYS:
            raise _snapshot_invalid("snapshot keys are invalid")
        if state.get("version") != SNAPSHOT_VERSION:
            raise _snapshot_invalid("unsupported snapshot version")

        raw_records = state["records"]
        if not isinstance(raw_records, list):
            raise _snapshot_invalid("records must be a list")

        records: dict[Key, GrantRecord] = {}
        version_numbers: dict[Key, int] = {}
        tenant_counts: dict[str, int] = {}

        for raw_record in raw_records:
            if not isinstance(raw_record, dict) or set(raw_record) != RECORD_KEYS:
                raise _snapshot_invalid("record structure is invalid")

            tenant = raw_record["tenant"]
            user = raw_record["user"]
            client = raw_record["client"]
            if not all(isinstance(value, str) and value.strip()
                       for value in (tenant, user, client)):
                raise _snapshot_invalid("record identity is invalid")

            version = raw_record["version"]
            if not _positive_int(version):
                raise _snapshot_invalid("record version is invalid")

            raw_scopes = raw_record["scopes"]
            if not isinstance(raw_scopes, list) or not raw_scopes:
                raise _snapshot_invalid("record scopes must be a non-empty list")
            if not all(isinstance(scope, str) and scope.strip()
                       for scope in raw_scopes):
                raise _snapshot_invalid("record scopes are invalid")
            scopes = tuple(sorted(set(raw_scopes)))

            granted_at = raw_record["granted_at"]
            if not isinstance(granted_at, str) or not granted_at.strip():
                raise _snapshot_invalid("granted_at is invalid")
            try:
                _parse_time(granted_at)
            except ConsentError as exc:
                raise _snapshot_invalid("granted_at is invalid") from exc

            expires_at = raw_record["expires_at"]
            if expires_at is not None:
                if not isinstance(expires_at, str) or not expires_at.strip():
                    raise _snapshot_invalid("expires_at is invalid")
                try:
                    _parse_time(expires_at)
                except ConsentError as exc:
                    raise _snapshot_invalid("expires_at is invalid") from exc

            key = cls._key(tenant, user, client)
            if key in records:
                raise _snapshot_invalid("duplicate consent record")

            records[key] = GrantRecord(
                tenant=tenant,
                user=user,
                client=client,
                scopes=scopes,
                version=version,
                granted_at=granted_at,
                expires_at=expires_at,
            )
            tenant_counts[tenant] = tenant_counts.get(tenant, 0) + 1

        if any(count > max_grants_per_tenant for count in tenant_counts.values()):
            raise _snapshot_invalid("snapshot exceeds tenant capacity")

        raw_tokens = state["tokens"]
        if not isinstance(raw_tokens, dict):
            raise _snapshot_invalid("tokens must be an object")

        tokens: dict[str, dict[str, Any]] = {}
        for token_id, raw_token in raw_tokens.items():
            if not isinstance(token_id, str) or not token_id.strip():
                raise _snapshot_invalid("token id is invalid")
            if not isinstance(raw_token, dict) or set(raw_token) != TOKEN_KEYS:
                raise _snapshot_invalid("token structure is invalid")

            tenant = raw_token["tenant"]
            user = raw_token["user"]
            client = raw_token["client"]
            identity = (tenant, user, client)
            if any(value is not None for value in identity):
                if not all(isinstance(value, str) and value.strip()
                           for value in identity):
                    raise _snapshot_invalid("token identity is invalid")

            raw_scopes = raw_token["scopes"]
            if not isinstance(raw_scopes, list):
                raise _snapshot_invalid("token scopes must be a list")
            if not all(isinstance(scope, str) and scope.strip()
                       for scope in raw_scopes):
                    raise _snapshot_invalid("token scopes are invalid")

            revoked = raw_token["revoked"]
            if not isinstance(revoked, bool):
                raise _snapshot_invalid("token revoked flag is invalid")

            tokens[token_id] = {
                "tenant": tenant,
                "user": user,
                "client": client,
                "scopes": tuple(sorted(set(raw_scopes))),
                "revoked": revoked,
            }

        raw_deprecated = state["deprecated"]
        if not isinstance(raw_deprecated, list):
            raise _snapshot_invalid("deprecated must be a list")
        if not all(isinstance(scope, str) and scope.strip()
                   for scope in raw_deprecated):
            raise _snapshot_invalid("deprecated scopes are invalid")
        deprecated = set(raw_deprecated)

        raw_counters = state["counters"]
        if not isinstance(raw_counters, dict):
            raise _snapshot_invalid("counters must be an object")
        if set(raw_counters) - {"version_numbers"}:
            raise _snapshot_invalid("counter structure is invalid")

        raw_version_numbers = raw_counters.get("version_numbers", [])
        if not isinstance(raw_version_numbers, list):
            raise _snapshot_invalid("version counters must be a list")

        seen_version_counters: set[Key] = set()
        for raw_counter in raw_version_numbers:
            if not isinstance(raw_counter, dict) or set(raw_counter) != VERSION_COUNTER_KEYS:
                raise _snapshot_invalid("version counter structure is invalid")

            tenant = raw_counter["tenant"]
            user = raw_counter["user"]
            client = raw_counter["client"]
            if not all(isinstance(value, str) and value.strip()
                       for value in (tenant, user, client)):
                raise _snapshot_invalid("version counter identity is invalid")

            version = raw_counter["version"]
            if not _positive_int(version):
                raise _snapshot_invalid("version counter is invalid")

            key = cls._key(tenant, user, client)
            if key in seen_version_counters:
                raise _snapshot_invalid("duplicate version counter")
            seen_version_counters.add(key)
            if key in records and version < records[key].version:
                raise _snapshot_invalid("version counter is older than record")
            version_numbers[key] = version

        for key, record in records.items():
            version_numbers.setdefault(key, record.version)

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(STAT_KEYS):
            raise _snapshot_invalid("stats structure is invalid")
        if not all(_non_negative_int(raw_stats[key]) for key in STAT_KEYS):
            raise _snapshot_invalid("stats values must be non-negative integers")
        stats = {key: raw_stats[key] for key in STAT_KEYS}

        return records, tokens, deprecated, version_numbers, stats

    def restore(self, state: dict) -> None:
        try:
            detached_state = copy.deepcopy(state)
            parsed = self._parse_snapshot(
                detached_state, self.max_grants_per_tenant
            )
        except ConsentError:
            raise
        except Exception as exc:
            raise _snapshot_invalid("snapshot could not be decoded") from exc

        records, tokens, deprecated, version_numbers, stats = parsed
        with self._lock:
            self._records = records
            self._tokens = tokens
            self._deprecated = deprecated
            self._version_numbers = version_numbers
            self._stats = stats

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)
