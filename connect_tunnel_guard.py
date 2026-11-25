"""HTTP CONNECT tunnel admission and byte metering guard."""

from __future__ import annotations

import copy
import ipaddress
import threading


class TunnelError(ValueError):
    """Raised when a tunnel operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_SNAPSHOT_VERSION = 1


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _non_negative_int(value: object) -> bool:
    return _is_int(value) and value >= 0


def _positive_int(value: object) -> bool:
    return _is_int(value) and value > 0


def _blocked_ip(ip) -> bool:
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _normalise_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _deny_host_match(host: str, patterns) -> str | None:
    normalised_host = _normalise_host(host)
    for pattern in patterns:
        candidate = _normalise_host(str(pattern))
        if normalised_host == candidate or normalised_host.endswith("." + candidate):
            return candidate
    return None


class ConnectTunnelGuard:
    """Admit CONNECT targets and meter per-tunnel byte usage."""

    def __init__(self, *, default_ports=(443,), max_tunnels_per_tenant: int = 4,
                 max_total_tunnels: int = 64, queue_timeout_ms: int = 500,
                 idle_timeout_ms: int = 30000, resolver=None,
                 clock=None) -> None:
        self.default_ports = tuple(default_ports)
        self.max_tunnels_per_tenant = max_tunnels_per_tenant
        self.max_total_tunnels = max_total_tunnels
        self.queue_timeout_ms = queue_timeout_ms
        self.idle_timeout_ms = idle_timeout_ms
        self._resolver = resolver
        self._clock = clock
        self._lock = threading.RLock()
        self._policies: dict[str, dict] = {}
        self._tunnels: dict[str, dict] = {}
        self._queues: dict[str, list[dict]] = {}
        self._tenant_rejected: dict[str, int] = {}
        self._stats = {
            "opened": 0,
            "closed": 0,
            "rejected": 0,
            "revalidations": 0,
        }
        self._seq = 0
        self._queue_seq = 0

    def _current_now(self, now):
        if now is not None:
            return now
        if callable(self._clock):
            return self._clock()
        if self._clock is not None and hasattr(self._clock, "now"):
            return self._clock.now()
        return 0

    @staticmethod
    def _parse_authority(authority: object) -> tuple[str, int]:
        if not isinstance(authority, str):
            raise TunnelError("TUNNEL_AUTHORITY_INVALID", {"authority": authority})

        text = authority.strip()
        if not text or any(ch.isspace() for ch in text):
            raise TunnelError("TUNNEL_AUTHORITY_INVALID", {"authority": authority})

        if text.startswith("["):
            end = text.find("]")
            if end <= 1 or text.count("[") != 1 or text.count("]") != 1:
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority})
            host = text[1:end]
            remainder = text[end + 1:]
            if not remainder.startswith(":"):
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority})
            port_text = remainder[1:]
            try:
                parsed_ip = ipaddress.ip_address(host)
            except ValueError as exc:
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority}) from exc
            if parsed_ip.version != 6:
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority})
            host = parsed_ip.compressed
        else:
            if ":" not in text:
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority})
            host, port_text = text.rsplit(":", 1)
            if not host or ":" in host:
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority})
            if any(separator in host for separator in "@/?#\\"):
                raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                                  {"authority": authority})
            try:
                parsed_ip = ipaddress.ip_address(host)
            except ValueError:
                parsed_ip = None
            if parsed_ip is not None:
                host = parsed_ip.compressed

        if not host:
            raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                              {"authority": authority})
        if (not isinstance(port_text, str) or not port_text
                or not port_text.isascii() or not port_text.isdigit()):
            raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                              {"authority": authority})

        port = int(port_text)
        if not 1 <= port <= 65535:
            raise TunnelError("TUNNEL_AUTHORITY_INVALID",
                              {"authority": authority})

        if not text.startswith("["):
            host = _normalise_host(host)
        else:
            host = host.lower()
        return host, port

    @staticmethod
    def _normalise_ports(ports: object) -> list[int]:
        if isinstance(ports, (str, bytes)) or not isinstance(
                ports, (list, tuple, set, frozenset)):
            raise TunnelError("TUNNEL_POLICY_INVALID", {"field": "ports"})
        normalised = []
        for port in ports:
            if not _positive_int(port) or port > 65535:
                raise TunnelError("TUNNEL_POLICY_INVALID", {"field": "ports"})
            normalised.append(port)
        if not normalised:
            raise TunnelError("TUNNEL_POLICY_INVALID", {"field": "ports"})
        return sorted(set(normalised))

    def _default_normalised_ports(self) -> list[int]:
        return self._normalise_ports(self.default_ports)

    def _normalise_policy(self, tenant: object, policy: object,
                          version: int) -> dict:
        if not isinstance(policy, dict) or not policy:
            raise TunnelError("TUNNEL_POLICY_INVALID")

        if "ports" in policy:
            ports = self._normalise_ports(policy["ports"])
        else:
            ports = self._default_normalised_ports()

        if "deny_private" in policy:
            deny_private = policy["deny_private"]
            if not isinstance(deny_private, bool):
                raise TunnelError("TUNNEL_POLICY_INVALID",
                                  {"field": "deny_private"})
        else:
            deny_private = True

        if "deny_hosts" in policy:
            raw_hosts = policy["deny_hosts"]
            if isinstance(raw_hosts, (str, bytes)) or not isinstance(
                    raw_hosts, (list, tuple, set, frozenset)):
                raise TunnelError("TUNNEL_POLICY_INVALID",
                                  {"field": "deny_hosts"})
            deny_hosts = []
            seen = set()
            for item in raw_hosts:
                if not isinstance(item, str):
                    raise TunnelError("TUNNEL_POLICY_INVALID",
                                      {"field": "deny_hosts"})
                host = _normalise_host(item)
                if not host or any(ch.isspace() for ch in host):
                    raise TunnelError("TUNNEL_POLICY_INVALID",
                                      {"field": "deny_hosts"})
                if host not in seen:
                    seen.add(host)
                    deny_hosts.append(host)
        else:
            deny_hosts = []

        if "max_tunnels" in policy:
            max_tunnels = policy["max_tunnels"]
            if not _positive_int(max_tunnels):
                raise TunnelError("TUNNEL_POLICY_INVALID",
                                  {"field": "max_tunnels"})
        else:
            max_tunnels = self.max_tunnels_per_tenant

        if "max_bytes" in policy:
            max_bytes = policy["max_bytes"]
            if not _non_negative_int(max_bytes):
                raise TunnelError("TUNNEL_POLICY_INVALID",
                                  {"field": "max_bytes"})
        else:
            max_bytes = 0

        return {
            "tenant": tenant,
            "version": version,
            "ports": ports,
            "deny_private": deny_private,
            "deny_hosts": deny_hosts,
            "max_tunnels": max_tunnels,
            "max_bytes": max_bytes,
        }

    def _default_policy(self, tenant: object) -> dict:
        return {
            "tenant": tenant,
            "version": 0,
            "ports": self._default_normalised_ports(),
            "deny_private": True,
            "deny_hosts": [],
            "max_tunnels": self.max_tunnels_per_tenant,
            "max_bytes": 0,
        }

    def _policy_locked(self, tenant: object) -> dict:
        return self._policies.get(tenant) or self._default_policy(tenant)

    def _count_rejection_locked(self, tenant: object, amount: int = 1) -> None:
        self._stats["rejected"] += amount
        if tenant is not None:
            self._tenant_rejected[tenant] = (
                self._tenant_rejected.get(tenant, 0) + amount
            )

    def _check_ip_allowed(self, value: object, policy: dict,
                          resolved: list | None = None) -> list:
        try:
            ip = value if isinstance(
                value, (ipaddress.IPv4Address, ipaddress.IPv6Address)
            ) \
                else ipaddress.ip_address(value)
        except ValueError as exc:
            raise TunnelError("TUNNEL_RESOLVE_FAILED",
                              {"address": value}) from exc

        address_text = ip.compressed
        if policy.get("deny_private", True) and _blocked_ip(ip):
            raise TunnelError(
                "TUNNEL_PRIVATE_TARGET",
                {"address": address_text, "resolved": resolved or [address_text]},
            )
        return [address_text]

    def _resolve_target_locked(self, host: str, policy: dict) -> list[str]:
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            literal = None

        if literal is not None:
            return self._check_ip_allowed(literal, policy, [literal.compressed])

        if self._resolver is None:
            raise TunnelError("TUNNEL_RESOLVE_FAILED", {"host": host})

        try:
            results = self._resolver(host)
        except Exception as exc:
            raise TunnelError("TUNNEL_RESOLVE_FAILED",
                              {"host": host, "error": repr(exc)}) from exc

        if isinstance(results, (str, bytes)):
            results = [results]
        try:
            raw_addresses = list(results or [])
        except TypeError as exc:
            raise TunnelError("TUNNEL_RESOLVE_FAILED",
                              {"host": host}) from exc
        if not raw_addresses:
            raise TunnelError("TUNNEL_RESOLVE_FAILED", {"host": host})

        resolved = []
        for item in raw_addresses:
            address = str(item).strip()
            if address.startswith("[") and address.endswith("]"):
                address = address[1:-1]
            try:
                ip = ipaddress.ip_address(address)
            except ValueError as exc:
                raise TunnelError("TUNNEL_RESOLVE_FAILED",
                                  {"host": host, "address": address}) from exc
            resolved.append(ip.compressed)

        for address in resolved:
            self._check_ip_allowed(address, policy, resolved)
        return resolved

    def _admit_target_locked(self, host: str, port: int,
                             policy: dict) -> list[str]:
        if port not in policy["ports"]:
            raise TunnelError("TUNNEL_PORT_DENIED",
                              {"host": host, "port": port,
                               "ports": list(policy["ports"])})

        denied_pattern = _deny_host_match(host, policy["deny_hosts"])
        if denied_pattern is not None:
            raise TunnelError("TUNNEL_TARGET_DENIED",
                              {"host": host, "rule": denied_pattern})

        return self._resolve_target_locked(host, policy)

    def _stored_target_allowed_locked(self, record: dict,
                                      policy: dict) -> None:
        host = record["host"]
        port = record["port"]
        if port not in policy["ports"]:
            raise TunnelError("TUNNEL_PORT_DENIED", {"host": host, "port": port})

        denied_pattern = _deny_host_match(host, policy["deny_hosts"])
        if denied_pattern is not None:
            raise TunnelError("TUNNEL_TARGET_DENIED",
                              {"host": host, "rule": denied_pattern})

        for address in record.get("resolved", []):
            self._check_ip_allowed(address, policy, list(record.get("resolved", [])))

    def _active_tenant_count_locked(self, tenant: object) -> int:
        return sum(
            1 for record in self._tunnels.values()
            if record["tenant"] == tenant and record["state"] == "open"
        )

    def _active_total_count_locked(self) -> int:
        return sum(1 for record in self._tunnels.values()
                   if record["state"] == "open")

    def _expire_queue_locked(self, tenant: object, now: int) -> list[dict]:
        queue = self._queues.get(tenant, [])
        if not queue:
            return []

        active = []
        expired = []
        for request in queue:
            if now >= request["expires_at"]:
                expired.append(request)
            else:
                active.append(request)

        if expired:
            self._queues[tenant] = active
            self._count_rejection_locked(tenant, len(expired))
        return expired

    @staticmethod
    def _expired_view(request: dict) -> dict:
        return {
            "request_id": request["request_id"],
            "tenant": request["tenant"],
            "status": "rejected",
            "code": "TUNNEL_QUEUE_TIMEOUT",
            "reason": "queue_timeout",
            "queued_at": request["queued_at"],
            "expires_at": request["expires_at"],
        }

    def _new_tunnel_id_locked(self) -> str:
        self._seq += 1
        return f"tn-{self._seq:04d}"

    def _new_request_id_locked(self) -> str:
        self._queue_seq += 1
        return f"q-{self._queue_seq:04d}"

    def _close_locked(self, record: dict, reason: str, now: int) -> bool:
        if record["state"] != "open":
            return False
        record["state"] = "closed"
        record["reason"] = reason or "client_closed"
        record["closed_at"] = now
        self._stats["closed"] += 1
        return True

    @staticmethod
    def _tunnel_view(record: dict) -> dict:
        view = copy.deepcopy(record)
        view.setdefault("total", view.get("up", 0) + view.get("down", 0))
        return view

    def _queue_request_locked(self, tenant: object, host: str, port: int,
                              resolved: list[str], policy_version: int,
                              now: int) -> dict:
        request_id = self._new_request_id_locked()
        request = {
            "request_id": request_id,
            "tenant": tenant,
            "status": "queued",
            "host": host,
            "port": port,
            "resolved": list(resolved),
            "policy_version": policy_version,
            "queued_at": now,
            "expires_at": now + self.queue_timeout_ms,
        }
        self._queues.setdefault(tenant, []).append(request)
        return copy.deepcopy(request)

    def set_policy(self, tenant: str, policy: dict) -> dict:
        with self._lock:
            previous_version = self._policies.get(tenant, {}).get("version", 0)
            normalised = self._normalise_policy(
                tenant, policy, previous_version + 1
            )
            self._policies[tenant] = normalised
            return copy.deepcopy(normalised)

    def policy_of(self, tenant: str) -> dict:
        with self._lock:
            if tenant not in self._policies:
                raise TunnelError("TUNNEL_UNKNOWN_TENANT", {"tenant": tenant})
            return copy.deepcopy(self._policies[tenant])

    def open_tunnel(self, authority: str, *, tenant: str,
                    now: int = 0) -> dict:
        with self._lock:
            current = self._current_now(now)
            host, port = self._parse_authority(authority)
            policy = self._policy_locked(tenant)

            try:
                resolved = self._admit_target_locked(host, port, policy)
            except TunnelError:
                self._count_rejection_locked(tenant)
                raise

            expired = self._expire_queue_locked(tenant, current)
            active_tenant = self._active_tenant_count_locked(tenant)
            tenant_limit = policy["max_tunnels"]
            queue = self._queues.get(tenant, [])

            if active_tenant >= tenant_limit or queue:
                request = self._queue_request_locked(
                    tenant, host, port, resolved, policy["version"], current
                )
                return {
                    "status": "queued",
                    "request_id": request["request_id"],
                    "tenant": tenant,
                    "host": host,
                    "port": port,
                    "resolved": list(resolved),
                    "policy_version": policy["version"],
                    "queued_at": current,
                    "expires_at": request["expires_at"],
                    "state": "queued",
                }

            if self._active_total_count_locked() >= self.max_total_tunnels:
                self._count_rejection_locked(tenant)
                raise TunnelError(
                    "TUNNEL_TOTAL_LIMIT",
                    {"max_total_tunnels": self.max_total_tunnels},
                )

            tunnel_id = self._new_tunnel_id_locked()
            record = {
                "tunnel_id": tunnel_id,
                "tenant": tenant,
                "host": host,
                "port": port,
                "resolved": list(resolved),
                "policy_version": policy["version"],
                "state": "open",
                "opened_at": current,
                "up": 0,
                "down": 0,
                "total": 0,
                "last_meter_at": current,
                "cycles": 0,
                "reason": None,
                "closed_at": None,
                "last_revalidated_at": None,
            }
            self._tunnels[tunnel_id] = record
            self._stats["opened"] += 1
            return self._tunnel_view(record)

    def admit_queued(self, tenant: str, *, now: int = 0) -> dict:
        with self._lock:
            current = self._current_now(now)
            rejected = [
                self._expired_view(request)
                for request in self._expire_queue_locked(tenant, current)
            ]
            queue = self._queues.get(tenant, [])
            admitted = []
            remaining = []

            for request in queue:
                if request["expires_at"] <= current:
                    self._count_rejection_locked(tenant)
                    rejected.append(self._expired_view(request))
                    continue

                policy = self._policy_locked(tenant)
                try:
                    self._stored_target_allowed_locked(request, policy)
                except TunnelError as exc:
                    self._count_rejection_locked(tenant)
                    rejected.append({
                        "request_id": request["request_id"],
                        "tenant": tenant,
                        "status": "rejected",
                        "code": exc.code,
                        "reason": exc.code.lower().removeprefix("tunnel_"),
                        "detail": exc.detail,
                    })
                    continue

                active_tenant = self._active_tenant_count_locked(tenant)
                tenant_slots = max(0, policy["max_tunnels"] - active_tenant)
                total_slots = max(
                    0,
                    self.max_total_tunnels - self._active_total_count_locked(),
                )
                if tenant_slots == 0 or total_slots == 0:
                    remaining.append(request)
                    continue

                tunnel_id = self._new_tunnel_id_locked()
                record = {
                    "tunnel_id": tunnel_id,
                    "tenant": tenant,
                    "host": request["host"],
                    "port": request["port"],
                    "resolved": list(request.get("resolved", [])),
                    "policy_version": policy["version"],
                    "state": "open",
                    "opened_at": current,
                    "up": 0,
                    "down": 0,
                    "total": 0,
                    "last_meter_at": current,
                    "cycles": 0,
                    "reason": None,
                    "closed_at": None,
                    "last_revalidated_at": None,
                    "request_id": request["request_id"],
                }
                self._tunnels[tunnel_id] = record
                self._stats["opened"] += 1
                view = self._tunnel_view(record)
                view["request_id"] = request["request_id"]
                admitted.append(view)

            self._queues[tenant] = remaining
            return {
                "tenant": tenant,
                "admitted": admitted,
                "rejected": rejected,
            }

    def meter(self, tunnel_id: str, up: int, down: int, *,
              now: int = 0) -> dict:
        with self._lock:
            current = self._current_now(now)
            record = self._tunnels.get(tunnel_id)
            if record is None:
                raise TunnelError("TUNNEL_UNKNOWN", {"tunnel_id": tunnel_id})
            if record["state"] != "open":
                raise TunnelError(
                    "TUNNEL_CLOSED",
                    {"tunnel_id": tunnel_id, "reason": record.get("reason")},
                )
            if not _non_negative_int(up) or not _non_negative_int(down):
                raise TunnelError("TUNNEL_METER_INVALID",
                                  {"up": up, "down": down})

            idle_ms = current - record["last_meter_at"]
            if idle_ms < 0:
                idle_ms = 0
            record["last_meter_at"] = current
            record["up"] += up
            record["down"] += down
            record["total"] = record["up"] + record["down"]

            policy = self._policy_locked(record["tenant"])
            policy_denied_code = None
            if policy["version"] != record.get("policy_version", 0):
                try:
                    self._stored_target_allowed_locked(record, policy)
                except TunnelError as exc:
                    policy_denied_code = exc.code
                record["policy_version"] = policy["version"]

            if policy_denied_code is not None:
                self._close_locked(record, "policy_changed", current)
            elif idle_ms > self.idle_timeout_ms:
                self._close_locked(record, "idle_timeout", current)
            elif policy["max_bytes"] and record["total"] > policy["max_bytes"]:
                self._close_locked(record, "quota_exceeded", current)

            view = self._tunnel_view(record)
            view["idle_ms"] = idle_ms
            if policy_denied_code is not None:
                view["policy_code"] = policy_denied_code
            return view

    def close_tunnel(self, tunnel_id: str, reason: str = "client_closed", *,
                     now: int = 0) -> dict:
        with self._lock:
            current = self._current_now(now)
            record = self._tunnels.get(tunnel_id)
            if record is None:
                raise TunnelError("TUNNEL_UNKNOWN", {"tunnel_id": tunnel_id})

            already_closed = record["state"] != "open"
            if not already_closed:
                close_reason = reason if isinstance(reason, str) and reason \
                    else "client_closed"
                self._close_locked(record, close_reason, current)

            view = self._tunnel_view(record)
            view["already_closed"] = already_closed
            return view

    def revalidate(self, tunnel_id: str, *, now: int = 0) -> dict:
        with self._lock:
            current = self._current_now(now)
            record = self._tunnels.get(tunnel_id)
            if record is None:
                raise TunnelError("TUNNEL_UNKNOWN", {"tunnel_id": tunnel_id})
            if record["state"] != "open":
                raise TunnelError(
                    "TUNNEL_CLOSED",
                    {"tunnel_id": tunnel_id, "reason": record.get("reason")},
                )

            record["cycles"] = record.get("cycles", 0) + 1
            record["last_revalidated_at"] = current
            self._stats["revalidations"] += 1
            policy = self._policy_locked(record["tenant"])

            try:
                resolved = self._resolve_target_locked(record["host"], policy)
            except TunnelError as exc:
                if exc.code != "TUNNEL_PRIVATE_TARGET":
                    raise
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                resolved = detail.get("resolved", record.get("resolved", []))
                record["resolved"] = list(resolved)
                self._close_locked(record, "private_target", current)
                return {
                    "tunnel_id": tunnel_id,
                    "tenant": record["tenant"],
                    "status": "closed",
                    "reason": "private_target",
                    "code": exc.code,
                    "resolved": copy.deepcopy(record["resolved"]),
                    "cycles": record["cycles"],
                    "policy_version": record["policy_version"],
                    "state": record["state"],
                    "closed_at": record["closed_at"],
                }

            record["resolved"] = resolved
            return {
                "tunnel_id": tunnel_id,
                "tenant": record["tenant"],
                "status": "ok",
                "resolved": list(resolved),
                "cycles": record["cycles"],
                "policy_version": record["policy_version"],
                "state": "open",
            }

    def list_tunnels(self, tenant: str | None = None, *,
                     state: str | None = None) -> list:
        with self._lock:
            result = []
            for record in self._tunnels.values():
                if tenant is not None and record["tenant"] != tenant:
                    continue
                if state is not None and record["state"] != state:
                    continue
                result.append(self._tunnel_view(record))
            return result

    def tenant_usage(self, tenant: str) -> dict:
        with self._lock:
            concurrent = 0
            up = 0
            down = 0
            for record in self._tunnels.values():
                if record["tenant"] != tenant:
                    continue
                if record["state"] == "open":
                    concurrent += 1
                up += record.get("up", 0)
                down += record.get("down", 0)

            return {
                "tenant": tenant,
                "concurrent": concurrent,
                "queued": len(self._queues.get(tenant, [])),
                "up": up,
                "down": down,
                "total": up + down,
                "rejected": self._tenant_rejected.get(tenant, 0),
            }

    def stats(self) -> dict:
        with self._lock:
            return {
                "opened": self._stats["opened"],
                "closed": self._stats["closed"],
                "rejected": self._stats["rejected"],
                "revalidations": self._stats["revalidations"],
            }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "policies": copy.deepcopy(self._policies),
                "tunnels": [
                    self._tunnel_view(record)
                    for record in self._tunnels.values()
                ],
                "queues": copy.deepcopy(self._queues),
                "stats": copy.deepcopy(self._stats),
                "seq": self._seq,
                "queue_seq": self._queue_seq,
                "tenant_rejected": copy.deepcopy(self._tenant_rejected),
            }

    @staticmethod
    def _invalid_snapshot(detail: object = None) -> TunnelError:
        return TunnelError("TUNNEL_SNAPSHOT_INVALID", detail)

    def _validate_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise self._invalid_snapshot("snapshot must be a dict")
        version = state.get("version")
        if not _is_int(version) or version != _SNAPSHOT_VERSION:
            raise self._invalid_snapshot({"field": "version"})

        policies = state.get("policies")
        tunnels = state.get("tunnels")
        queues = state.get("queues")
        stats = state.get("stats")
        seq = state.get("seq")
        queue_seq = state.get("queue_seq")

        if not isinstance(policies, dict):
            raise self._invalid_snapshot({"field": "policies"})
        if not isinstance(tunnels, list):
            raise self._invalid_snapshot({"field": "tunnels"})
        if not isinstance(queues, dict):
            raise self._invalid_snapshot({"field": "queues"})
        if not isinstance(stats, dict):
            raise self._invalid_snapshot({"field": "stats"})
        if not _non_negative_int(seq) or not _non_negative_int(queue_seq):
            raise self._invalid_snapshot({"field": "seq"})

        required_stats = ("opened", "closed", "rejected", "revalidations")
        if any(not _non_negative_int(stats.get(key)) for key in required_stats):
            raise self._invalid_snapshot({"field": "stats"})

        restored_policies = {}
        for tenant, policy in policies.items():
            if not isinstance(tenant, str) or not isinstance(policy, dict):
                raise self._invalid_snapshot({"field": "policies"})
            version = policy.get("version")
            if not _positive_int(version):
                raise self._invalid_snapshot({"field": "policy.version"})
            try:
                normalised = self._normalise_policy(tenant, policy, version)
            except TunnelError as exc:
                raise self._invalid_snapshot(exc.detail) from exc
            normalised["version"] = version
            restored_policies[tenant] = normalised

        tunnel_ids = set()
        restored_tunnels = {}
        max_tunnel_seq = 0
        for record in tunnels:
            if not isinstance(record, dict):
                raise self._invalid_snapshot({"field": "tunnels"})
            tunnel_id = record.get("tunnel_id")
            tenant = record.get("tenant")
            host = record.get("host")
            port = record.get("port")
            if (not isinstance(tunnel_id, str)
                    or not tunnel_id.startswith("tn-")
                    or tunnel_id in tunnel_ids):
                raise self._invalid_snapshot({"field": "tunnel_id"})
            try:
                max_tunnel_seq = max(max_tunnel_seq,
                                     int(tunnel_id.removeprefix("tn-")))
            except ValueError:
                raise self._invalid_snapshot({"field": "tunnel_id"})
            if not isinstance(tenant, str) or not isinstance(host, str):
                raise self._invalid_snapshot({"field": "tenant/host"})
            if not _positive_int(port) or port > 65535:
                raise self._invalid_snapshot({"field": "port"})
            if record.get("state") not in ("open", "closed"):
                raise self._invalid_snapshot({"field": "state"})
            if not _non_negative_int(record.get("opened_at")):
                raise self._invalid_snapshot({"field": "opened_at"})
            if not _non_negative_int(record.get("up")) or \
                    not _non_negative_int(record.get("down")):
                raise self._invalid_snapshot({"field": "bytes"})
            if not _non_negative_int(record.get("last_meter_at")):
                raise self._invalid_snapshot({"field": "last_meter_at"})
            if not _non_negative_int(record.get("cycles", 0)):
                raise self._invalid_snapshot({"field": "cycles"})
            resolved = record.get("resolved", [])
            if not isinstance(resolved, list) or not all(
                    isinstance(item, str) for item in resolved):
                raise self._invalid_snapshot({"field": "resolved"})
            policy_version = record.get("policy_version", 0)
            if not _non_negative_int(policy_version):
                raise self._invalid_snapshot({"field": "policy_version"})

            restored_record = copy.deepcopy(record)
            restored_record["total"] = (
                restored_record["up"] + restored_record["down"]
            )
            restored_record.setdefault("reason", None)
            restored_record.setdefault("closed_at", None)
            restored_record.setdefault("last_revalidated_at", None)
            tunnel_ids.add(tunnel_id)
            restored_tunnels[tunnel_id] = restored_record

        restored_queues = {}
        queue_ids = set()
        max_queue_seq = 0
        for tenant, requests in queues.items():
            if not isinstance(tenant, str) or not isinstance(requests, list):
                raise self._invalid_snapshot({"field": "queues"})
            restored_requests = []
            for request in requests:
                if not isinstance(request, dict):
                    raise self._invalid_snapshot({"field": "queues"})
                request_id = request.get("request_id")
                if not isinstance(request_id, str) or \
                        not request_id.startswith("q-") or request_id in queue_ids:
                    raise self._invalid_snapshot({"field": "request_id"})
                try:
                    max_queue_seq = max(max_queue_seq,
                                        int(request_id.removeprefix("q-")))
                except ValueError:
                    raise self._invalid_snapshot({"field": "request_id"})
                if not _non_negative_int(request.get("queued_at")) or \
                        not _non_negative_int(request.get("expires_at")):
                    raise self._invalid_snapshot({"field": "queue time"})
                if not isinstance(request.get("host"), str) or \
                        not _positive_int(request.get("port")):
                    raise self._invalid_snapshot({"field": "queue target"})
                queue_ids.add(request_id)
                restored_requests.append(copy.deepcopy(request))
            restored_queues[tenant] = restored_requests

        tenant_rejected = state.get("tenant_rejected", {})
        if not isinstance(tenant_rejected, dict):
            raise self._invalid_snapshot({"field": "tenant_rejected"})
        restored_rejected = {}
        for tenant, count in tenant_rejected.items():
            if not isinstance(tenant, str) or not _non_negative_int(count):
                raise self._invalid_snapshot({"field": "tenant_rejected"})
            restored_rejected[tenant] = count

        return {
            "policies": restored_policies,
            "tunnels": restored_tunnels,
            "queues": restored_queues,
            "stats": {key: stats[key] for key in required_stats},
            "seq": max(seq, max_tunnel_seq),
            "queue_seq": max(queue_seq, max_queue_seq),
            "tenant_rejected": restored_rejected,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            candidate = self._validate_snapshot(state)
            self._policies = candidate["policies"]
            self._tunnels = candidate["tunnels"]
            self._queues = candidate["queues"]
            self._stats = candidate["stats"]
            self._seq = candidate["seq"]
            self._queue_seq = candidate["queue_seq"]
            self._tenant_rejected = candidate["tenant_rejected"]
