"""Revocation effectiveness barrier with lease draining and hard expiry."""

from __future__ import annotations

import copy
import threading
import uuid


SNAPSHOT_VERSION = 1
STAT_KEYS = ("issued", "leased", "released", "enforced", "rejected")


class RevocationError(ValueError):
    """Raised when a revocation barrier operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RevocationDrainBarrier:
    """Stop new credentials, drain in-flight leases and enforce a hard cutoff."""

    def __init__(self, *, lease_ttl: int = 60, hard_cap: int = 600,
                 clock=None) -> None:
        self.lease_ttl = lease_ttl
        self.hard_cap = hard_cap
        self._clock = clock
        self._lock = threading.RLock()
        self._tenants: dict[str, dict] = {}
        self._leases: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}
        self._request_index: dict[tuple, str] = {}
        self._stats = {key: 0 for key in STAT_KEYS}

    def _tenant(self, tenant: str) -> dict:
        try:
            return self._tenants[tenant]
        except KeyError:
            raise RevocationError(
                "REVOCATION_UNKNOWN_TENANT",
                {"tenant": tenant},
            ) from None

    @staticmethod
    def _active_leases(tenant: dict, leases: dict[str, dict]) -> list[dict]:
        return [lease for lease in leases.values()
                if lease["tenant"] == tenant["tenant"] and lease["active"]]

    @staticmethod
    def _remaining(tenant: dict, now: int) -> int:
        if tenant["state"] != "draining":
            return 0
        reference = now if tenant.get("last_now") is None \
            else max(now, tenant["last_now"])
        return max(0, tenant["drain_deadline"] - reference)

    @staticmethod
    def _tick(tenant: dict, now: int) -> None:
        if tenant.get("last_now") is None:
            tenant["last_now"] = now
        else:
            tenant["last_now"] = max(tenant["last_now"], now)

    def configure_tenant(self, tenant: str, drain_window: int) -> dict:
        if not isinstance(drain_window, int) or isinstance(drain_window, bool) \
                or drain_window <= 0:
            raise RevocationError(
                "REVOCATION_INVALID_WINDOW",
                {"tenant": tenant, "drain_window": drain_window},
            )
        with self._lock:
            existing = self._tenants.get(tenant)
            if existing is not None and existing["state"] != "open":
                raise RevocationError(
                    "REVOCATION_ACTIVE",
                    {"tenant": tenant, "state": existing["state"]},
                )
            effective = min(drain_window, self.hard_cap)
            record = {
                "tenant": tenant,
                "drain_window": effective,
                "state": "open",
                "generation": 0,
                "drain_started_at": None,
                "drain_deadline": None,
                "last_now": None,
            }
            self._tenants[tenant] = record
            return {
                "tenant": tenant,
                "drain_window": effective,
                "state": "open",
            }

    def begin_revocation(self, tenant: str, *, now: int) -> dict:
        with self._lock:
            record = self._tenant(tenant)
            self._tick(record, now)
            if record["state"] == "open":
                record["state"] = "draining"
                record["generation"] += 1
                record["drain_started_at"] = now
                record["drain_deadline"] = now + record["drain_window"]
            return {
                "tenant": tenant,
                "generation": record["generation"],
                "state": record["state"],
                "drain_deadline": record["drain_deadline"],
            }

    def issue_token(self, tenant: str, subject: str, *, now: int) -> dict:
        with self._lock:
            record = self._tenant(tenant)
            self._tick(record, now)
            if record["state"] != "open":
                self._stats["rejected"] += 1
                raise RevocationError(
                    "REVOCATION_ACTIVE",
                    {"tenant": tenant, "state": record["state"]},
                )
            token = {
                "token_id": uuid.uuid4().hex,
                "tenant": tenant,
                "subject": subject,
                "generation": record["generation"],
                "issued_at": now,
            }
            self._tokens[token["token_id"]] = token
            self._stats["issued"] += 1
            return dict(token)

    def acquire_lease(self, tenant: str, subject: str, request_id: str,
                      *, now: int) -> dict:
        with self._lock:
            record = self._tenant(tenant)
            self._tick(record, now)
            key = (tenant, request_id)
            existing_id = self._request_index.get(key)
            if existing_id is not None:
                existing = self._leases[existing_id]
                if existing["active"]:
                    return dict(existing)
            if record["state"] != "open":
                self._stats["rejected"] += 1
                raise RevocationError(
                    "REVOCATION_DRAINING",
                    {"tenant": tenant, "state": record["state"]},
                )
            lease = {
                "lease_id": uuid.uuid4().hex,
                "tenant": tenant,
                "subject": subject,
                "request_id": request_id,
                "generation": record["generation"],
                "issued_at": now,
                "expires_at": now + self.lease_ttl,
                "active": True,
            }
            self._leases[lease["lease_id"]] = lease
            self._request_index[key] = lease["lease_id"]
            self._stats["leased"] += 1
            return dict(lease)

    def release_lease(self, tenant: str, lease_id: str) -> bool:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease["tenant"] != tenant:
                raise RevocationError(
                    "REVOCATION_UNKNOWN_LEASE",
                    {"tenant": tenant, "lease_id": lease_id},
                )
            if not lease["active"]:
                return False
            lease["active"] = False
            self._request_index.pop((tenant, lease["request_id"]), None)
            self._stats["released"] += 1
            return True

    def drain_status(self, tenant: str, *, now: int) -> dict:
        with self._lock:
            record = self._tenant(tenant)
            self._tick(record, now)
            in_flight = len(self._active_leases(record, self._leases))
            return {
                "tenant": tenant,
                "state": record["state"],
                "in_flight": in_flight,
                "drain_deadline": record["drain_deadline"],
                "remaining": self._remaining(record, now),
            }

    def enforce(self, tenant: str, *, now: int) -> dict:
        with self._lock:
            record = self._tenant(tenant)
            self._tick(record, now)
            closed = 0
            if record["state"] == "draining":
                hard_cut = now >= record["drain_deadline"]
                for lease in self._active_leases(record, self._leases):
                    if hard_cut or now >= lease["expires_at"]:
                        lease["active"] = False
                        self._request_index.pop(
                            (tenant, lease["request_id"]), None)
                        closed += 1
                self._stats["enforced"] += closed
                if hard_cut:
                    record["state"] = "enforced"
            return {
                "tenant": tenant,
                "state": record["state"],
                "closed": closed,
            }

    def token_state(self, token_id: str, *, now: int) -> dict:
        with self._lock:
            token = self._tokens.get(token_id)
            if token is None:
                raise RevocationError(
                    "REVOCATION_UNKNOWN_TOKEN",
                    {"token_id": token_id},
                )
            record = self._tenants[token["tenant"]]
            self._tick(record, now)
            return {
                "token_id": token_id,
                "tenant": token["tenant"],
                "generation": token["generation"],
                "usable": record["state"] != "enforced",
                "state": record["state"],
                "now": now,
            }

    def lease_state(self, tenant: str, lease_id: str, *, now: int) -> dict:
        with self._lock:
            lease = self._leases.get(lease_id)
            if lease is None or lease["tenant"] != tenant:
                raise RevocationError(
                    "REVOCATION_UNKNOWN_LEASE",
                    {"tenant": tenant, "lease_id": lease_id},
                )
            self._tick(self._tenants[tenant], now)
            return {
                **dict(lease),
                "expired": now >= lease["expires_at"],
            }

    def generation(self, tenant: str) -> int:
        with self._lock:
            return self._tenant(tenant)["generation"]

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STAT_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "tenants": copy.deepcopy(self._tenants),
                "leases": copy.deepcopy(self._leases),
                "tokens": copy.deepcopy(self._tokens),
                "stats": dict(self._stats),
            }

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _validate_snapshot(self, state: object) -> None:
        if not isinstance(state, dict):
            raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                  {"reason": "not a mapping"})
        if state.get("version") != SNAPSHOT_VERSION:
            raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                  {"reason": "bad version"})
        tenants = state.get("tenants")
        leases = state.get("leases")
        stats = state.get("stats")
        if not isinstance(tenants, dict) or not isinstance(leases, dict) \
                or not isinstance(stats, dict):
            raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                  {"reason": "bad top-level structure"})
        required_tenant = ("tenant", "drain_window", "state", "generation",
                           "drain_started_at", "drain_deadline")
        for key, record in tenants.items():
            if not isinstance(record, dict) or \
                    any(field not in record for field in required_tenant) or \
                    record.get("tenant") != key or \
                    record.get("state") not in ("open", "draining", "enforced"):
                raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                      {"reason": "bad tenant record",
                                       "tenant": key})
        required_lease = ("lease_id", "tenant", "subject", "request_id",
                          "generation", "issued_at", "expires_at", "active")
        for lease_id, lease in leases.items():
            if not isinstance(lease, dict) or \
                    any(field not in lease for field in required_lease) or \
                    lease.get("lease_id") != lease_id or \
                    lease.get("tenant") not in tenants or \
                    not isinstance(lease.get("active"), bool) or \
                    not self._is_int(lease.get("expires_at")):
                raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                      {"reason": "bad lease record",
                                       "lease_id": lease_id})
        if any(key not in stats for key in STAT_KEYS) or \
                any(not self._is_int(stats.get(key)) or stats[key] < 0
                    for key in STAT_KEYS):
            raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                  {"reason": "bad stats"})

    def restore(self, state: dict) -> None:
        self._validate_snapshot(state)
        tenants = copy.deepcopy(state["tenants"])
        leases = copy.deepcopy(state["leases"])
        tokens = copy.deepcopy(state.get("tokens", {}))
        if not isinstance(tokens, dict):
            raise RevocationError("REVOCATION_SNAPSHOT_INVALID",
                                  {"reason": "bad tokens"})
        request_index: dict[tuple, str] = {}
        for lease in leases.values():
            if lease["active"]:
                request_index[(lease["tenant"], lease["request_id"])] = \
                    lease["lease_id"]
        stats = {key: int(state["stats"][key]) for key in STAT_KEYS}
        with self._lock:
            self._tenants = tenants
            self._leases = leases
            self._tokens = tokens
            self._request_index = request_index
            self._stats = stats
