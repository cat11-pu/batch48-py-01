"""Proxy chain parsing, bypass matching and egress health selection."""

from __future__ import annotations

import ipaddress
import re
import threading
import time
from collections.abc import Iterable
from urllib.parse import urlsplit


class ProxyError(ValueError):
    """Rejection raised by the proxy chain resolver."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ProxyChainResolver:
    """Parse proxy chains, decide direct bypass and pick a healthy egress."""

    _SUPPORTED_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})
    _DEFAULT_PORTS = {
        "http": 80,
        "https": 443,
        "socks5": 1080,
        "socks5h": 1080,
    }
    _STAT_KEYS = ("parses", "probes", "failures", "degradations", "recoveries")

    def __init__(self, *, max_hops: int = 8, cooldown: float = 60.0,
                 failure_threshold: int = 3, clock=None) -> None:
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or max_hops <= 0:
            raise ProxyError("PROXY_INVALID_ARGUMENT")
        if (isinstance(failure_threshold, bool)
                or not isinstance(failure_threshold, int)
                or failure_threshold <= 0):
            raise ProxyError("PROXY_INVALID_ARGUMENT")
        if (isinstance(cooldown, bool)
                or not isinstance(cooldown, (int, float))
                or cooldown < 0):
            raise ProxyError("PROXY_INVALID_ARGUMENT")
        if clock is not None and not callable(clock):
            raise ProxyError("PROXY_INVALID_ARGUMENT")

        self.max_hops = max_hops
        self.cooldown = float(cooldown)
        self.failure_threshold = failure_threshold
        self._clock = clock
        self._lock = threading.RLock()
        self._bypass_rules: tuple[str, ...] = ()
        self._bypass_index = self._compile_bypass(())
        self._egresses: dict[str, dict] = {}
        self._registration_counter = 0
        self._stats = {key: 0 for key in self._STAT_KEYS}

    def parse(self, spec: str) -> list:
        with self._lock:
            self._stats["parses"] += 1
            if not isinstance(spec, str):
                raise ProxyError("PROXY_INVALID_SPEC")

            segments = spec.split(",")
            if len(segments) > self.max_hops:
                raise ProxyError("PROXY_TOO_MANY_HOPS")

            hops = []
            seen_endpoints = set()
            for segment in segments:
                hop, endpoint = self._parse_hop(segment)
                if endpoint in seen_endpoints:
                    raise ProxyError("PROXY_LOOP")
                seen_endpoints.add(endpoint)
                hops.append(hop)
            return hops

    def set_bypass(self, rules) -> None:
        with self._lock:
            raw_rules = self._normalize_rule_collection(rules)
            compiled = self._compile_bypass(raw_rules)
            self._bypass_rules = raw_rules
            self._bypass_index = compiled

    def bypass(self, target: str) -> bool:
        with self._lock:
            if self._bypass_index["catch_all"]:
                return True

            host = self._target_host(target)
            if host is None:
                return False

            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return self._domain_matches(host)

            return any(address in network for network in self._bypass_index["networks"])

    def register_egress(self, egress_id: str, *, priority: int, address: str) -> None:
        with self._lock:
            self._validate_egress_identity(egress_id, address)
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ProxyError("PROXY_INVALID_ARGUMENT")
            if egress_id in self._egresses:
                raise ProxyError("PROXY_DUPLICATE_EGRESS")

            self._registration_counter += 1
            self._egresses[egress_id] = {
                "egress_id": egress_id,
                "priority": priority,
                "address": address.strip(),
                "order": self._registration_counter,
                "consecutive_failures": 0,
                "degraded": False,
                "degraded_until": None,
            }

    def remove_egress(self, egress_id: str) -> bool:
        with self._lock:
            if not isinstance(egress_id, str) or not egress_id.strip():
                raise ProxyError("PROXY_INVALID_ARGUMENT")
            return self._egresses.pop(egress_id, None) is not None

    def select(self, now=None) -> str:
        with self._lock:
            current = self._resolve_time(now)
            candidates = []
            for egress in self._egresses.values():
                if self._is_available(egress, current):
                    candidates.append(egress)
            if not candidates:
                raise ProxyError("PROXY_NO_HEALTHY_EGRESS")

            selected = min(candidates, key=lambda item: (item["priority"], item["order"]))
            return selected["egress_id"]

    def probe_result(self, egress_id: str, ok: bool, now=None) -> dict:
        with self._lock:
            egress = self._require_egress(egress_id)
            if not isinstance(ok, bool):
                raise ProxyError("PROXY_INVALID_ARGUMENT")
            current = self._resolve_time(now)

            self._stats["probes"] += 1
            if ok:
                egress["consecutive_failures"] = 0
                if egress["degraded"]:
                    deadline = egress["degraded_until"]
                    if deadline is not None and current >= deadline:
                        egress["degraded"] = False
                        egress["degraded_until"] = None
                        self._stats["recoveries"] += 1
            else:
                self._stats["failures"] += 1
                egress["consecutive_failures"] += 1
                reached_threshold = (
                    egress["consecutive_failures"] >= self.failure_threshold
                )
                if reached_threshold:
                    was_degraded = egress["degraded"]
                    egress["degraded"] = True
                    egress["degraded_until"] = current + self.cooldown
                    if not was_degraded:
                        self._stats["degradations"] += 1

            return self._health_dict(egress)

    def health(self, egress_id: str) -> dict:
        with self._lock:
            return self._health_dict(self._require_egress(egress_id))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "max_hops": self.max_hops,
                "cooldown": self.cooldown,
                "failure_threshold": self.failure_threshold,
                "bypass_rules": list(self._bypass_rules),
                "egresses": {
                    egress["egress_id"]: {
                        "egress_id": egress["egress_id"],
                        "priority": egress["priority"],
                        "address": egress["address"],
                        "order": egress["order"],
                        "consecutive_failures": egress["consecutive_failures"],
                        "degraded": egress["degraded"],
                        "degraded_until": egress["degraded_until"],
                    }
                    for egress in self._egresses.values()
                },
                "stats": dict(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            candidate = self._build_restore_candidate(state)
            config = candidate["config"]
            if config is not None:
                self.max_hops = config["max_hops"]
                self.cooldown = config["cooldown"]
                self.failure_threshold = config["failure_threshold"]
            self._bypass_rules = candidate["bypass_rules"]
            self._bypass_index = candidate["bypass_index"]
            self._egresses = candidate["egresses"]
            self._registration_counter = candidate["registration_counter"]
            self._stats = candidate["stats"]

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in self._STAT_KEYS}

    def _parse_hop(self, raw_segment):
        if not isinstance(raw_segment, str):
            raise ProxyError("PROXY_INVALID_SPEC")
        segment = raw_segment.strip()
        if not segment or "://" not in segment:
            raise ProxyError("PROXY_INVALID_SPEC")

        scheme_name = segment.split("://", 1)[0].strip().lower()
        if not scheme_name:
            raise ProxyError("PROXY_INVALID_SPEC")
        if scheme_name not in self._SUPPORTED_SCHEMES:
            raise ProxyError("PROXY_UNSUPPORTED_SCHEME")

        try:
            parsed = urlsplit(segment)
        except ValueError as exc:
            raise ProxyError("PROXY_INVALID_SPEC") from exc

        authority = parsed.netloc
        if not authority:
            raise ProxyError("PROXY_INVALID_SPEC")
        if any(character.isspace() for character in authority):
            raise ProxyError("PROXY_INVALID_SPEC")

        auth = "@" in authority
        host_port = authority.rsplit("@", 1)[-1]
        if host_port.startswith("["):
            closing_bracket = host_port.find("]")
            if closing_bracket < 0:
                raise ProxyError("PROXY_INVALID_SPEC")
            raw_host = host_port[1:closing_bracket]
            port_part = host_port[closing_bracket + 1:]
            if not port_part.startswith(":"):
                raise ProxyError("PROXY_INVALID_SPEC")
            raw_port = port_part[1:]
        else:
            raw_host = parsed.hostname or ""
            if ":" not in host_port:
                raise ProxyError("PROXY_INVALID_SPEC")
            if host_port.count(":") > 1:
                raise ProxyError("PROXY_INVALID_SPEC")
            raw_host, raw_port = host_port.rsplit(":", 1)

        host = self._normalize_host(raw_host)
        if not host:
            raise ProxyError("PROXY_INVALID_SPEC")
        if raw_port == "":
            raise ProxyError("PROXY_INVALID_SPEC")
        if not raw_port.isascii() or not raw_port.isdigit():
            raise ProxyError("PROXY_INVALID_PORT")

        try:
            port = int(raw_port)
        except ValueError as exc:
            raise ProxyError("PROXY_INVALID_PORT") from exc
        if not 1 <= port <= 65535:
            raise ProxyError("PROXY_INVALID_PORT")

        normalized_host = self._normalized_ip_or_host(host)
        hop = {
            "scheme": scheme_name,
            "host": normalized_host,
            "port": port,
            "auth": bool(auth),
        }
        endpoint = (
            normalized_host,
            self._normalized_port(scheme_name, port),
        )
        return hop, endpoint

    @classmethod
    def _normalize_host(cls, value) -> str:
        if not isinstance(value, str):
            return ""
        host = value.strip().lower()
        if host.endswith("."):
            host = host[:-1]
        if not host or any(character.isspace() for character in host):
            return ""
        return host

    @classmethod
    def _normalized_ip_or_host(cls, host: str):
        try:
            return ipaddress.ip_address(host).compressed
        except ValueError:
            return host

    @classmethod
    def _normalized_port(cls, scheme: str, port):
        if port is None:
            return cls._DEFAULT_PORTS[scheme]
        return port

    @classmethod
    def _normalize_rule_collection(cls, rules) -> tuple[str, ...]:
        if rules is None:
            return ()
        if isinstance(rules, (str, bytes)) or not isinstance(rules, Iterable):
            raise ProxyError("PROXY_INVALID_BYPASS")
        try:
            return tuple(rules)
        except TypeError as exc:
            raise ProxyError("PROXY_INVALID_BYPASS") from exc

    @classmethod
    def _compile_bypass(cls, rules) -> dict:
        networks = []
        exact_hosts = set()
        suffix_domains = set()
        wildcard_suffixes = set()
        wildcard_patterns = []
        catch_all = False

        if not isinstance(rules, tuple):
            rules = cls._normalize_rule_collection(rules)

        for raw_rule in rules:
            if not isinstance(raw_rule, str):
                raise ProxyError("PROXY_INVALID_BYPASS")
            rule = raw_rule.strip()
            if not rule:
                raise ProxyError("PROXY_INVALID_BYPASS")

            if "/" in rule:
                try:
                    network = ipaddress.ip_network(rule, strict=False)
                except ValueError as exc:
                    raise ProxyError("PROXY_INVALID_BYPASS") from exc
                networks.append(network)
                if network.prefixlen == 0:
                    catch_all = True
                continue

            try:
                address = ipaddress.ip_address(rule)
            except ValueError:
                pass
            else:
                network = ipaddress.ip_network(
                    f"{address.compressed}/{address.max_prefixlen}"
                )
                networks.append(network)
                continue

            if "*" in rule:
                if rule.startswith("*.") and rule.count("*") == 1:
                    domain = cls._valid_domain(rule[2:])
                    wildcard_suffixes.add(domain)
                else:
                    wildcard_patterns.append(cls._compile_wildcard_pattern(rule))
                continue

            if rule.startswith("."):
                domain = cls._valid_domain(rule[1:])
                suffix_domains.add(domain)
                continue

            exact_hosts.add(cls._valid_domain(rule))

        return {
            "networks": tuple(networks),
            "exact_hosts": frozenset(exact_hosts),
            "suffix_domains": frozenset(suffix_domains),
            "wildcard_suffixes": frozenset(wildcard_suffixes),
            "wildcard_patterns": tuple(wildcard_patterns),
            "catch_all": catch_all,
        }

    @staticmethod
    def _valid_domain(value: str) -> str:
        if not isinstance(value, str):
            raise ProxyError("PROXY_INVALID_BYPASS")
        domain = value.strip().lower()
        if domain.endswith("."):
            domain = domain[:-1]
        if not domain or any(character.isspace() for character in domain):
            raise ProxyError("PROXY_INVALID_BYPASS")
        labels = domain.split(".")
        if any(not label for label in labels):
            raise ProxyError("PROXY_INVALID_BYPASS")
        return domain

    @classmethod
    def _compile_wildcard_pattern(cls, rule: str):
        text = rule.strip().lower()
        cls._valid_domain(text.replace("*", "x"))
        pattern = ["^"]
        for character in text:
            if character == "*":
                pattern.append(".*")
            else:
                pattern.append(re.escape(character))
        pattern.append("$")
        return re.compile("".join(pattern))

    @classmethod
    def _target_host(cls, target):
        if not isinstance(target, str) or not target.strip():
            return None
        text = target.strip()
        try:
            if "://" not in text:
                parsed = urlsplit("//" + text)
            else:
                parsed = urlsplit(text)
            host = parsed.hostname
        except ValueError:
            if text.startswith("[") and "]" in text:
                host = text[1:text.find("]")]
            else:
                host = text.split("/", 1)[0].rsplit("@", 1)[-1].rsplit(":", 1)[0]
        return cls._normalize_host(host)

    def _domain_matches(self, host: str) -> bool:
        index = self._bypass_index
        if host in index["exact_hosts"]:
            return True
        if any(host == domain or host.endswith("." + domain)
               for domain in index["suffix_domains"]):
            return True
        if any(host.endswith("." + domain)
               for domain in index["wildcard_suffixes"]):
            return True
        return any(pattern.match(host) is not None
                   for pattern in index["wildcard_patterns"])

    @staticmethod
    def _validate_egress_identity(egress_id, address) -> None:
        if not isinstance(egress_id, str) or not egress_id.strip():
            raise ProxyError("PROXY_INVALID_ARGUMENT")
        if not isinstance(address, str) or not address.strip():
            raise ProxyError("PROXY_INVALID_ARGUMENT")

    def _require_egress(self, egress_id: str) -> dict:
        if not isinstance(egress_id, str) or not egress_id.strip():
            raise ProxyError("PROXY_INVALID_ARGUMENT")
        try:
            return self._egresses[egress_id]
        except KeyError as exc:
            raise ProxyError("PROXY_UNKNOWN_EGRESS") from exc

    def _resolve_time(self, now):
        if now is None:
            current = self._clock() if self._clock is not None else time.time()
        else:
            current = now
        if isinstance(current, bool) or not isinstance(current, (int, float)):
            raise ProxyError("PROXY_INVALID_ARGUMENT")
        return current

    @staticmethod
    def _is_available(egress: dict, current) -> bool:
        return not egress["degraded"]

    def _health_dict(self, egress: dict) -> dict:
        return {
            "egress_id": egress["egress_id"],
            "healthy": not egress["degraded"],
            "degraded": egress["degraded"],
            "consecutive_failures": egress["consecutive_failures"],
            "degraded_until": egress["degraded_until"],
            "priority": egress["priority"],
            "address": egress["address"],
        }

    def _build_restore_candidate(self, state) -> dict:
        if not isinstance(state, dict):
            raise ProxyError("PROXY_SNAPSHOT_INVALID")
        if type(state.get("version")) is not int or state.get("version") != 1:
            raise ProxyError("PROXY_SNAPSHOT_INVALID")

        stats = self._restore_stats(state.get("stats"))
        config = self._restore_config(state)
        bypass_rules = self._restore_bypass_rules(state.get("bypass_rules"))
        egresses, registration_counter = self._restore_egresses(
            state.get("egresses")
        )
        return {
            "config": config,
            "bypass_rules": bypass_rules,
            "bypass_index": self._compile_bypass(bypass_rules),
            "egresses": egresses,
            "registration_counter": registration_counter,
            "stats": stats,
        }

    @classmethod
    def _restore_config(cls, state: dict):
        keys = ("max_hops", "cooldown", "failure_threshold")
        if not any(key in state for key in keys):
            return None

        max_hops = state.get("max_hops", 8)
        cooldown = state.get("cooldown", 60.0)
        failure_threshold = state.get("failure_threshold", 3)
        if isinstance(max_hops, bool) or not isinstance(max_hops, int) or max_hops <= 0:
            raise ProxyError("PROXY_SNAPSHOT_INVALID")
        if (isinstance(failure_threshold, bool)
                or not isinstance(failure_threshold, int)
                or failure_threshold <= 0):
            raise ProxyError("PROXY_SNAPSHOT_INVALID")
        if (isinstance(cooldown, bool)
                or not isinstance(cooldown, (int, float))
                or cooldown < 0):
            raise ProxyError("PROXY_SNAPSHOT_INVALID")
        return {
            "max_hops": max_hops,
            "cooldown": float(cooldown),
            "failure_threshold": failure_threshold,
        }

    @classmethod
    def _restore_stats(cls, raw_stats) -> dict:
        if not isinstance(raw_stats, dict):
            raise ProxyError("PROXY_SNAPSHOT_INVALID")
        stats = {}
        for key in cls._STAT_KEYS:
            value = raw_stats.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            stats[key] = value
        return stats

    @classmethod
    def _restore_bypass_rules(cls, raw_rules) -> tuple[str, ...]:
        if raw_rules is None:
            return ()
        if isinstance(raw_rules, (str, bytes)) or not isinstance(raw_rules, Iterable):
            raise ProxyError("PROXY_SNAPSHOT_INVALID")
        try:
            rules = tuple(raw_rules)
        except TypeError as exc:
            raise ProxyError("PROXY_SNAPSHOT_INVALID") from exc
        try:
            for rule in rules:
                if not isinstance(rule, str):
                    raise ProxyError("PROXY_INVALID_BYPASS")
            cls._compile_bypass(rules)
        except ProxyError as exc:
            raise ProxyError("PROXY_SNAPSHOT_INVALID") from exc
        return rules

    @classmethod
    def _restore_egresses(cls, raw_egresses) -> tuple[dict[str, dict], int]:
        if raw_egresses is None:
            return {}, 0
        if isinstance(raw_egresses, dict):
            records = []
            for egress_id, raw_record in raw_egresses.items():
                if not isinstance(raw_record, dict):
                    raise ProxyError("PROXY_SNAPSHOT_INVALID")
                record = dict(raw_record)
                record.setdefault("egress_id", egress_id)
                records.append(record)
        elif isinstance(raw_egresses, list):
            records = raw_egresses
        else:
            raise ProxyError("PROXY_SNAPSHOT_INVALID")

        egresses = {}
        max_order = 0
        for position, raw_record in enumerate(records, start=1):
            if not isinstance(raw_record, dict):
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            egress_id = raw_record.get("egress_id") or raw_record.get("id")
            priority = raw_record.get("priority")
            address = raw_record.get("address")
            order = raw_record.get("order", position)
            if order is None:
                order = position
            failures = raw_record.get("consecutive_failures", 0)
            degraded = raw_record.get("degraded", False)
            deadline = raw_record.get("degraded_until")

            if not isinstance(egress_id, str) or not egress_id.strip():
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if egress_id in egresses:
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if isinstance(priority, bool) or not isinstance(priority, int):
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if not isinstance(address, str) or not address.strip():
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if isinstance(order, bool) or not isinstance(order, int) or order < 0:
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if isinstance(failures, bool) or not isinstance(failures, int) or failures < 0:
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if not isinstance(degraded, bool):
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if deadline is not None and (
                    isinstance(deadline, bool)
                    or not isinstance(deadline, (int, float))):
                raise ProxyError("PROXY_SNAPSHOT_INVALID")
            if degraded and deadline is None:
                raise ProxyError("PROXY_SNAPSHOT_INVALID")

            max_order = max(max_order, order)
            egresses[egress_id] = {
                "egress_id": egress_id,
                "priority": priority,
                "address": address.strip(),
                "order": order,
                "consecutive_failures": failures,
                "degraded": degraded,
                "degraded_until": float(deadline) if deadline is not None else None,
            }
        return egresses, max_order
