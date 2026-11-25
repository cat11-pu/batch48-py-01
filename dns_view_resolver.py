"""Recursive DNS resolution with per view authorities and negative caching."""

from __future__ import annotations

import copy
import ipaddress
import re
import threading

TYPES = ("A", "AAAA", "CNAME", "TXT")

STATS_KEYS = (
    "queries",
    "cache_hits",
    "negative_hits",
    "cache_misses",
    "cname_followed",
    "expanded",
    "rejected",
)

_LABEL_RE = re.compile(r"^[a-z0-9_](?:[a-z0-9_-]*[a-z0-9_])?$")


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_name(value, *, allow_root: bool = False):
    if not isinstance(value, str) or value != value.strip():
        return None
    name = value.lower()
    if name.endswith("."):
        name = name.rstrip(".")
    if not name:
        return "" if allow_root else None
    if len(name) > 253:
        return None
    labels = name.split(".")
    for label in labels:
        if not label or len(label) > 63 or not _LABEL_RE.match(label):
            return None
    return name


def _valid_view_name(value) -> bool:
    return _normalize_name(value) is not None


def _normalize_value(rtype: str, value):
    if not isinstance(value, str) or value == "":
        return None
    if rtype == "A":
        try:
            return str(ipaddress.IPv4Address(value))
        except ipaddress.AddressValueError:
            return None
    if rtype == "AAAA":
        try:
            return str(ipaddress.IPv6Address(value))
        except ipaddress.AddressValueError:
            return None
    if rtype == "CNAME":
        return _normalize_name(value)
    if len(value) > 65535:
        return None
    return value


class DnsError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DnsViewResolver:
    """Answer queries from the authorities of the client view."""

    def __init__(self, *, max_cname_depth: int = 8, default_ttl: int = 300,
                 max_cache_entries: int = 128, clock=None) -> None:
        if not _is_int(max_cname_depth) or max_cname_depth < 0:
            raise DnsError("DNS_INVALID_VIEW", "max_cname_depth")
        if not _is_int(default_ttl) or default_ttl <= 0:
            raise DnsError("DNS_INVALID_RECORD", "default_ttl")
        if not _is_int(max_cache_entries) or max_cache_entries < 0:
            raise DnsError("DNS_INVALID_RECORD", "max_cache_entries")
        if clock is not None and not callable(clock):
            raise DnsError("DNS_INVALID_VIEW", "clock")
        self.max_cname_depth = max_cname_depth
        self.default_ttl = default_ttl
        self.max_cache_entries = max_cache_entries
        self._clock = clock
        self._lock = threading.RLock()
        self._views = {}
        self._zones = {}
        self._positive = {}
        self._negative = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    def _reject(self, code: str, detail=None):
        self._stats["rejected"] += 1
        raise DnsError(code, detail)

    def _view(self, view):
        if not isinstance(view, str) or view not in self._views:
            self._reject("DNS_UNKNOWN_VIEW", view)
        return self._views[view]

    def _zone(self, view, zone):
        key = (view, zone)
        if key not in self._zones:
            self._reject("DNS_UNKNOWN_ZONE", zone)
        return self._zones[key]

    @staticmethod
    def _zone_info(view, zone, state):
        names = set(state["records"])
        return {
            "view": view,
            "zone": zone,
            "serial": state["serial"],
            "soa_ttl": state["soa_ttl"],
            "soa_minimum": state["soa_minimum"],
            "records": sum(
                len(records)
                for by_type in state["records"].values()
                for records in by_type.values()
            ),
            "names": len(names),
        }

    def declare_view(self, view: str, *, ndots: int = 1, search=(),
                     external: str = None) -> dict:
        with self._lock:
            if not _valid_view_name(view):
                return self._reject("DNS_INVALID_VIEW", "view")
            if view in self._views:
                return self._reject("DNS_DUPLICATE_VIEW", view)
            if not _is_int(ndots) or not 0 <= ndots <= 15:
                return self._reject("DNS_INVALID_VIEW", "ndots")
            if not isinstance(search, (list, tuple)):
                return self._reject("DNS_INVALID_VIEW", "search")
            suffixes = []
            for item in search:
                suffix = _normalize_name(item)
                if suffix is None:
                    return self._reject("DNS_INVALID_VIEW", item)
                if suffix not in suffixes:
                    suffixes.append(suffix)
            if external is not None and not _valid_view_name(external):
                return self._reject("DNS_INVALID_VIEW", "external")
            state = {
                "ndots": ndots,
                "search": suffixes,
                "external": external,
            }
            self._views[view] = state
            return {
                "view": view,
                "ndots": state["ndots"],
                "search": list(state["search"]),
                "external": state["external"],
            }

    def declare_zone(self, view: str, zone: str, *, serial: int = 1,
                     soa_ttl: int = 300, soa_minimum: int = 60) -> dict:
        with self._lock:
            self._view(view)
            normalized_zone = _normalize_name(zone)
            if (normalized_zone is None or not _is_int(serial) or serial <= 0
                    or not _is_int(soa_ttl) or soa_ttl <= 0
                    or not _is_int(soa_minimum) or soa_minimum <= 0):
                return self._reject("DNS_INVALID_ZONE", zone)
            key = (view, normalized_zone)
            if key in self._zones:
                return self._reject("DNS_DUPLICATE_ZONE", normalized_zone)
            state = {
                "serial": serial,
                "soa_ttl": soa_ttl,
                "soa_minimum": soa_minimum,
                "records": {},
            }
            self._zones[key] = state
            return self._zone_info(view, normalized_zone, state)

    def add_record(self, view: str, zone: str, name: str, rtype: str, value: str,
                   *, ttl: int = 300) -> dict:
        with self._lock:
            self._view(view)
            normalized_zone = _normalize_name(zone)
            if not normalized_zone or (view, normalized_zone) not in self._zones:
                return self._reject("DNS_UNKNOWN_ZONE", zone)
            normalized_type = rtype.upper() if isinstance(rtype, str) else None
            if normalized_type not in TYPES:
                return self._reject("DNS_UNSUPPORTED_TYPE", rtype)
            normalized_name = _normalize_name(name)
            if normalized_name is None:
                return self._reject("DNS_INVALID_RECORD", "name")
            if (normalized_name != normalized_zone
                    and not normalized_name.endswith("." + normalized_zone)):
                return self._reject("DNS_OUT_OF_ZONE", normalized_name)
            normalized_value = _normalize_value(normalized_type, value)
            if normalized_value is None or not _is_int(ttl) or ttl <= 0:
                return self._reject(
                    "DNS_INVALID_RECORD",
                    value if normalized_value is None else "ttl")

            zone_state = self._zones[(view, normalized_zone)]
            records_for_name = zone_state["records"].setdefault(
                normalized_name, {})
            records = records_for_name.setdefault(normalized_type, [])
            for existing in records:
                if existing["value"] == normalized_value:
                    return self._reject(
                        "DNS_DUPLICATE_RECORD",
                        (normalized_name, normalized_type, normalized_value))
            records.append({
                "name": normalized_name,
                "type": normalized_type,
                "value": normalized_value,
                "ttl": ttl,
            })
            self._invalidate_name(view, normalized_zone, normalized_name)
            return {
                "view": view,
                "zone": normalized_zone,
                "name": normalized_name,
                "type": normalized_type,
                "value": normalized_value,
                "ttl": ttl,
            }

    def set_serial(self, view: str, zone: str, serial: int) -> dict:
        with self._lock:
            self._view(view)
            normalized_zone = _normalize_name(zone)
            if not normalized_zone or (view, normalized_zone) not in self._zones:
                return self._reject("DNS_UNKNOWN_ZONE", zone)
            if not _is_int(serial) or serial <= 0:
                return self._reject("DNS_INVALID_ZONE", serial)
            zone_state = self._zones[(view, normalized_zone)]
            if serial != zone_state["serial"]:
                zone_state["serial"] = serial
                self._invalidate_zone(view, normalized_zone)
            return self._zone_info(view, normalized_zone, zone_state)

    def zone_info(self, view: str, zone: str) -> dict:
        with self._lock:
            self._view(view)
            normalized_zone = _normalize_name(zone)
            if not normalized_zone or (view, normalized_zone) not in self._zones:
                return self._reject("DNS_UNKNOWN_ZONE", zone)
            return self._zone_info(view, normalized_zone,
                                   self._zones[(view, normalized_zone)])

    @staticmethod
    def _candidates(name: str, absolute: bool, view_state):
        if absolute:
            return [name]
        candidates = []
        if name.count(".") >= view_state["ndots"]:
            candidates.append(name)
        for suffix in view_state["search"]:
            candidates.append(name + "." + suffix)
        if name not in candidates:
            candidates.append(name)
        result = []
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
        return result

    @staticmethod
    def _record_answer(record):
        return {
            "name": record["name"],
            "type": record["type"],
            "ttl": record["ttl"],
            "value": record["value"],
        }

    @staticmethod
    def _negative_ttl(zone_state):
        return min(zone_state["soa_minimum"], zone_state["soa_ttl"])

    @staticmethod
    def _name_exists(zone, zone_state, name):
        if name == zone or name in zone_state["records"]:
            return True
        return any(record_name.endswith("." + name)
                   for record_name in zone_state["records"])

    def _find_zone(self, view, name):
        best = None
        for (zone_view, zone) in self._zones:
            if zone_view != view:
                continue
            if name == zone or name.endswith("." + zone):
                if best is None or len(zone) > len(best):
                    best = zone
        return best

    def _effective_authority(self, view, name):
        current = view
        visited = set()
        while current not in visited:
            visited.add(current)
            zone = self._find_zone(current, name)
            if zone is not None:
                return current, zone
            state = self._views.get(current)
            if state is None:
                break
            external = state["external"]
            if not external or external not in self._views:
                break
            current = external
        return None, None

    def _lookup_cache(self, key, now):
        positive = self._positive.get(key)
        if positive is not None:
            if now >= positive["expires_at"]:
                self._positive.pop(key, None)
            else:
                return "positive", copy.deepcopy(positive)
        negative = self._negative.get(key)
        if negative is not None:
            if now >= negative["expires_at"]:
                self._negative.pop(key, None)
            else:
                zone_key = (negative["view"], negative["zone"])
                zone_state = self._zones.get(zone_key)
                if zone_state is None or zone_state["serial"] != negative["serial"]:
                    self._negative.pop(key, None)
                else:
                    return "negative", copy.deepcopy(negative)
        return None

    def _trim_cache(self):
        while len(self._positive) + len(self._negative) > self.max_cache_entries:
            choices = []
            for kind, store in (("positive", self._positive),
                                ("negative", self._negative)):
                for key, entry in store.items():
                    choices.append((entry["expires_at"], entry["created_at"],
                                    kind, key))
            if not choices:
                return
            _, _, kind, key = min(
                choices,
                key=lambda item: (item[0], item[1], item[2], repr(item[3]))
            )
            if kind == "positive":
                self._positive.pop(key, None)
            else:
                self._negative.pop(key, None)

    def _store_positive(self, view, name, qtype, dnssec, answers, cname_chain,
                        zone, serial, now):
        if not answers:
            return
        ttl = min(answer["ttl"] for answer in answers)
        key = (view, name, qtype, dnssec)
        self._positive[key] = {
            "view": view,
            "name": name,
            "type": qtype,
            "dnssec": dnssec,
            "status": "NOERROR",
            "nodata": False,
            "answers": copy.deepcopy(answers),
            "cname_chain": list(cname_chain),
            "zone": zone,
            "serial": serial,
            "ttl": ttl,
            "created_at": now,
            "expires_at": now + ttl,
        }
        self._trim_cache()

    def _store_negative(self, view, name, qtype, dnssec, status, nodata,
                        zone, zone_state, now, answers=None):
        ttl = self._negative_ttl(zone_state)
        key = (view, name, qtype, dnssec)
        self._negative[key] = {
            "view": view,
            "name": name,
            "type": qtype,
            "dnssec": dnssec,
            "status": status,
            "nodata": nodata,
            "answers": copy.deepcopy(answers or []),
            "zone": zone,
            "serial": zone_state["serial"],
            "ttl": ttl,
            "created_at": now,
            "expires_at": now + ttl,
        }
        self._trim_cache()

    def _invalidate_name(self, view, zone, name):
        affected = []
        current = name
        while current != zone and current.endswith("." + zone):
            affected.append(current)
            current = current.split(".", 1)[1]
        if name == zone:
            affected.append(name)
        for affected_name in affected:
            for key in list(self._positive):
                entry = self._positive[key]
                if (key[0] == view and key[1] == affected_name
                        and entry["zone"] == zone):
                    self._positive.pop(key, None)
            for key in list(self._negative):
                entry = self._negative[key]
                if (key[0] == view and key[1] == affected_name
                        and entry["zone"] == zone):
                    self._negative.pop(key, None)

    def _invalidate_zone(self, view, zone):
        for key in list(self._positive):
            entry = self._positive[key]
            if key[0] == view and entry["zone"] == zone:
                self._positive.pop(key, None)
        for key in list(self._negative):
            entry = self._negative[key]
            if key[0] == view and entry["zone"] == zone:
                self._negative.pop(key, None)

    @staticmethod
    def _fresh_result(status, answers, cname_chain, nodata, ttl, zone, serial,
                      code=None):
        return {
            "status": status,
            "code": code,
            "answers": answers,
            "cname_chain": cname_chain,
            "nodata": nodata,
            "cached": False,
            "remaining_ttl": ttl,
            "zone": zone,
            "serial": serial,
        }

    def _cached_result(self, kind, entry, now):
        if kind == "positive":
            return {
                "status": "NOERROR",
                "code": None,
                "answers": copy.deepcopy(entry["answers"]),
                "cname_chain": list(entry["cname_chain"]),
                "nodata": False,
                "cached": True,
                "remaining_ttl": entry["expires_at"] - now,
                "zone": entry["zone"],
                "serial": entry["serial"],
            }
        return {
            "status": entry["status"],
            "code": None,
            "answers": copy.deepcopy(entry.get("answers", [])),
            "cname_chain": list(entry.get("cname_chain", [])),
            "nodata": entry["nodata"],
            "cached": True,
            "remaining_ttl": entry["expires_at"] - now,
            "zone": entry["zone"],
            "serial": entry["serial"],
        }

    def _resolve_name(self, view, name, qtype, now, dnssec, seen, depth):
        authority_view, zone = self._effective_authority(view, name)
        if authority_view is None:
            self._stats["cache_misses"] += 1
            return self._fresh_result(
                "NXDOMAIN", [], [], False, 0, None, None)

        key = (authority_view, name, qtype, dnssec)
        cached = self._lookup_cache(key, now)
        if cached is not None:
            kind, entry = cached
            if kind == "positive":
                self._stats["cache_hits"] += 1
            else:
                self._stats["negative_hits"] += 1
            return self._cached_result(kind, entry, now)

        self._stats["cache_misses"] += 1
        zone_state = self._zones[(authority_view, zone)]
        serial = zone_state["serial"]
        records_for_name = zone_state["records"].get(name)

        if qtype != "CNAME" and records_for_name is not None:
            cname_records = records_for_name.get("CNAME", [])
            if cname_records:
                target = cname_records[0]["value"]
                if target == name or target in seen:
                    return self._fresh_result(
                        "SERVFAIL", [], [name], False, 0, zone, serial,
                        "DNS_CNAME_LOOP")
                if depth >= self.max_cname_depth:
                    return self._fresh_result(
                        "SERVFAIL", [], [name], False, 0, zone, serial,
                        "DNS_CNAME_DEPTH")
                self._stats["cname_followed"] += 1
                child = self._resolve_name(
                    authority_view, target, qtype, now, dnssec,
                    seen | {name}, depth + 1)
                chain = [name] + child["cname_chain"]
                if child["status"] == "SERVFAIL":
                    return self._fresh_result(
                        "SERVFAIL", [], chain, False, 0, zone, serial,
                        child["code"])
                if child["status"] == "NXDOMAIN":
                    return self._fresh_result(
                        "NXDOMAIN", [], chain, False, 0, zone, serial)
                if child["nodata"]:
                    return self._fresh_result(
                        "NOERROR", [], chain, True, child["remaining_ttl"],
                        zone, serial)
                answers = [self._record_answer(record)
                           for record in cname_records] + child["answers"]
                ttl = min(answer["ttl"] for answer in answers)
                self._store_positive(authority_view, name, qtype, dnssec,
                                     answers, chain, zone, serial, now)
                return self._fresh_result(
                    "NOERROR", answers, chain, False, ttl, zone, serial)

        if records_for_name is not None:
            matching = records_for_name.get(qtype, [])
            if matching:
                answers = [self._record_answer(record) for record in matching]
                ttl = min(answer["ttl"] for answer in answers)
                self._store_positive(authority_view, name, qtype, dnssec,
                                     answers, [], zone, serial, now)
                return self._fresh_result(
                    "NOERROR", answers, [], False, ttl, zone, serial)

        if self._name_exists(zone, zone_state, name):
            ttl = self._negative_ttl(zone_state)
            self._store_negative(authority_view, name, qtype, dnssec,
                                 "NOERROR", True, zone, zone_state, now)
            return self._fresh_result(
                "NOERROR", [], [], True, ttl, zone, serial)

        ttl = self._negative_ttl(zone_state)
        self._store_negative(authority_view, name, qtype, dnssec,
                             "NXDOMAIN", False, zone, zone_state, now)
        return self._fresh_result(
            "NXDOMAIN", [], [], False, ttl, zone, serial)

    def resolve(self, name: str, qtype: str, *, view: str = "default",
                now: int = 0, dnssec: bool = False) -> dict:
        with self._lock:
            self._stats["queries"] += 1
            view_state = self._view(view)
            normalized_type = qtype.upper() if isinstance(qtype, str) else None
            if normalized_type not in TYPES:
                return self._reject("DNS_UNSUPPORTED_TYPE", qtype)
            normalized_name = _normalize_name(name)
            if normalized_name is None:
                return self._reject("DNS_INVALID_RECORD", "name")
            if not isinstance(dnssec, bool):
                return self._reject("DNS_INVALID_RECORD", "dnssec")
            absolute = name.endswith(".")
            candidates = self._candidates(
                normalized_name, absolute, view_state)
            expanded = False
            tried = 0
            result = None
            for candidate in candidates:
                tried += 1
                if candidate != normalized_name:
                    expanded = True
                result = self._resolve_name(
                    view, candidate, normalized_type, now, dnssec, set(), 0)
                if result["status"] != "NXDOMAIN" or tried >= len(candidates):
                    break
            if expanded:
                self._stats["expanded"] += 1
            result["qname"] = candidates[tried - 1]
            result["qtype"] = normalized_type
            result["view"] = view
            result["candidates_tried"] = tried
            result["expanded"] = expanded
            return result

    def cache_entries(self) -> list:
        with self._lock:
            return [
                {
                    "view": entry["view"],
                    "name": entry["name"],
                    "type": entry["type"],
                    "dnssec": entry["dnssec"],
                    "status": entry["status"],
                    "nodata": entry["nodata"],
                    "answers": copy.deepcopy(entry["answers"]),
                    "cname_chain": list(entry["cname_chain"]),
                    "zone": entry["zone"],
                    "serial": entry["serial"],
                    "ttl": entry["ttl"],
                    "created_at": entry["created_at"],
                    "expires_at": entry["expires_at"],
                }
                for entry in self._positive.values()
            ]

    def negative_cache(self) -> list:
        with self._lock:
            return [
                {
                    "view": entry["view"],
                    "name": entry["name"],
                    "type": entry["type"],
                    "dnssec": entry["dnssec"],
                    "status": entry["status"],
                    "nodata": entry["nodata"],
                    "answers": copy.deepcopy(entry.get("answers", [])),
                    "zone": entry["zone"],
                    "serial": entry["serial"],
                    "ttl": entry["ttl"],
                    "created_at": entry["created_at"],
                    "expires_at": entry["expires_at"],
                }
                for entry in self._negative.values()
            ]

    def expire(self, *, now: int = 0) -> dict:
        with self._lock:
            expired = 0
            for key in list(self._positive):
                if now >= self._positive[key]["expires_at"]:
                    self._positive.pop(key, None)
                    expired += 1
            for key in list(self._negative):
                if now >= self._negative[key]["expires_at"]:
                    self._negative.pop(key, None)
                    expired += 1
            return {"expired": expired}

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STATS_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            views = [
                {
                    "view": view,
                    "ndots": state["ndots"],
                    "search": list(state["search"]),
                    "external": state["external"],
                }
                for view, state in self._views.items()
            ]
            zones = []
            for (view, zone), state in self._zones.items():
                records = []
                for name, by_type in state["records"].items():
                    for rtype, values in by_type.items():
                        for record in values:
                            records.append({
                                "name": name,
                                "type": rtype,
                                "value": record["value"],
                                "ttl": record["ttl"],
                            })
                zones.append({
                    "view": view,
                    "zone": zone,
                    "serial": state["serial"],
                    "soa_ttl": state["soa_ttl"],
                    "soa_minimum": state["soa_minimum"],
                    "records": records,
                })
            positive = copy.deepcopy(list(self._positive.values()))
            negative = copy.deepcopy(list(self._negative.values()))
            return {
                "views": copy.deepcopy(views),
                "zones": copy.deepcopy(zones),
                "positive": positive,
                "negative": negative,
                "stats": dict(self._stats),
            }

    @staticmethod
    def _snapshot_invalid(detail):
        raise DnsError("DNS_SNAPSHOT_INVALID", detail)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                self._restore_locked(state)
            except DnsError as exc:
                if exc.code == "DNS_SNAPSHOT_INVALID":
                    raise
                raise DnsError("DNS_SNAPSHOT_INVALID", exc.code) from exc
            except Exception as exc:
                raise DnsError("DNS_SNAPSHOT_INVALID",
                               str(exc)) from exc

    def _restore_locked(self, state: dict) -> None:
        with self._lock:
            if not isinstance(state, dict):
                self._snapshot_invalid("state")
            for key in ("views", "zones", "positive", "negative", "stats"):
                if key not in state:
                    self._snapshot_invalid(key)
            if not all(isinstance(state[key], list)
                       for key in ("views", "zones", "positive", "negative")):
                self._snapshot_invalid("collections")
            if not isinstance(state["stats"], dict):
                self._snapshot_invalid("stats")

            new_views = {}
            for item in state["views"]:
                if not isinstance(item, dict):
                    self._snapshot_invalid("view")
                view = item.get("view")
                if not _valid_view_name(view) or view in new_views:
                    self._snapshot_invalid("view")
                ndots = item.get("ndots")
                search = item.get("search")
                external = item.get("external")
                if not _is_int(ndots) or not 0 <= ndots <= 15:
                    self._snapshot_invalid("ndots")
                if not isinstance(search, list):
                    self._snapshot_invalid("search")
                suffixes = []
                for suffix in search:
                    normalized = _normalize_name(suffix)
                    if normalized is None or normalized != suffix:
                        self._snapshot_invalid("search")
                    if normalized not in suffixes:
                        suffixes.append(normalized)
                if external is not None and not _valid_view_name(external):
                    self._snapshot_invalid("external")
                new_views[view] = {
                    "ndots": ndots,
                    "search": suffixes,
                    "external": external,
                }

            new_zones = {}
            for item in state["zones"]:
                if not isinstance(item, dict):
                    self._snapshot_invalid("zone")
                view = item.get("view")
                zone = item.get("zone")
                if (not isinstance(view, str) or view not in new_views
                        or not isinstance(zone, str)
                        or not zone or zone != _normalize_name(zone)):
                    self._snapshot_invalid("zone")
                serial = item.get("serial")
                soa_ttl = item.get("soa_ttl")
                soa_minimum = item.get("soa_minimum")
                if (not _is_int(serial) or serial <= 0
                        or not _is_int(soa_ttl) or soa_ttl <= 0
                        or not _is_int(soa_minimum) or soa_minimum <= 0):
                    self._snapshot_invalid("soa")
                key = (view, zone)
                if key in new_zones:
                    self._snapshot_invalid("duplicate_zone")
                records_value = item.get("records")
                if not isinstance(records_value, list):
                    self._snapshot_invalid("records")
                zone_state = {
                    "serial": serial,
                    "soa_ttl": soa_ttl,
                    "soa_minimum": soa_minimum,
                    "records": {},
                }
                for record in records_value:
                    if not isinstance(record, dict):
                        self._snapshot_invalid("record")
                    name = _normalize_name(record.get("name"))
                    rtype = record.get("type")
                    ttl = record.get("ttl")
                    if name is None or rtype not in TYPES:
                        self._snapshot_invalid("record")
                    if name != zone and not name.endswith("." + zone):
                        self._snapshot_invalid("record_zone")
                    normalized_value = _normalize_value(
                        rtype, record.get("value"))
                    if (normalized_value is None or not _is_int(ttl)
                            or ttl <= 0):
                        self._snapshot_invalid("record_value")
                    by_type = zone_state["records"].setdefault(name, {})
                    values = by_type.setdefault(rtype, [])
                    if any(existing["value"] == normalized_value
                           for existing in values):
                        self._snapshot_invalid("duplicate_record")
                    values.append({
                        "name": name,
                        "type": rtype,
                        "value": normalized_value,
                        "ttl": ttl,
                    })
                new_zones[key] = zone_state

            new_positive = {}
            for item in state["positive"]:
                if not isinstance(item, dict):
                    self._snapshot_invalid("positive")
                view = item.get("view")
                name = _normalize_name(item.get("name"))
                qtype = item.get("type")
                dnssec = item.get("dnssec")
                answers_value = item.get("answers")
                chain_value = item.get("cname_chain", [])
                zone = item.get("zone")
                serial = item.get("serial")
                ttl = item.get("ttl")
                created_at = item.get("created_at")
                expires_at = item.get("expires_at")
                if (view not in new_views or name is None or qtype not in TYPES
                        or not isinstance(dnssec, bool)
                        or not isinstance(answers_value, list)
                        or not answers_value
                        or not isinstance(chain_value, list)
                        or (view, zone) not in new_zones
                        or not _is_int(serial) or serial <= 0
                        or not _is_int(ttl) or ttl <= 0
                        or not _is_int(created_at) or not _is_int(expires_at)
                        or expires_at != created_at + ttl):
                    self._snapshot_invalid("positive")
                answers = []
                for answer in answers_value:
                    if not isinstance(answer, dict):
                        self._snapshot_invalid("answer")
                    answer_name = _normalize_name(answer.get("name"))
                    answer_type = answer.get("type")
                    answer_ttl = answer.get("ttl")
                    value = _normalize_value(
                        answer_type, answer.get("value"))
                    if (answer_name is None or answer_type not in TYPES
                            or not _is_int(answer_ttl) or answer_ttl <= 0
                            or value is None):
                        self._snapshot_invalid("answer")
                    answers.append({
                        "name": answer_name,
                        "type": answer_type,
                        "ttl": answer_ttl,
                        "value": value,
                    })
                chain = []
                for entry in chain_value:
                    normalized = _normalize_name(entry)
                    if normalized is None:
                        self._snapshot_invalid("cname_chain")
                    chain.append(normalized)
                key = (view, name, qtype, dnssec)
                if key in new_positive:
                    self._snapshot_invalid("duplicate_positive")
                new_positive[key] = {
                    "view": view,
                    "name": name,
                    "type": qtype,
                    "dnssec": dnssec,
                    "status": "NOERROR",
                    "nodata": False,
                    "answers": answers,
                    "cname_chain": chain,
                    "zone": zone,
                    "serial": serial,
                    "ttl": ttl,
                    "created_at": created_at,
                    "expires_at": expires_at,
                }

            new_negative = {}
            for item in state["negative"]:
                if not isinstance(item, dict):
                    self._snapshot_invalid("negative")
                view = item.get("view")
                name = _normalize_name(item.get("name"))
                qtype = item.get("type")
                dnssec = item.get("dnssec")
                status = item.get("status")
                nodata = item.get("nodata")
                zone = item.get("zone")
                serial = item.get("serial")
                ttl = item.get("ttl")
                created_at = item.get("created_at")
                expires_at = item.get("expires_at")
                answers_value = item.get("answers", [])
                if (view not in new_views or name is None or qtype not in TYPES
                        or not isinstance(dnssec, bool)
                        or status not in ("NOERROR", "NXDOMAIN")
                        or not isinstance(nodata, bool)
                        or (status == "NXDOMAIN" and nodata)
                        or (status == "NOERROR" and not nodata)
                        or (view, zone) not in new_zones
                        or not _is_int(serial) or serial <= 0
                        or not _is_int(ttl) or ttl <= 0
                        or not _is_int(created_at) or not _is_int(expires_at)
                        or expires_at != created_at + ttl
                        or not isinstance(answers_value, list)):
                    self._snapshot_invalid("negative")
                answers = []
                for answer in answers_value:
                    if not isinstance(answer, dict):
                        self._snapshot_invalid("negative_answer")
                    answer_name = _normalize_name(answer.get("name"))
                    answer_type = answer.get("type")
                    answer_ttl = answer.get("ttl")
                    value = _normalize_value(
                        answer_type, answer.get("value"))
                    if (answer_name is None or answer_type not in TYPES
                            or not _is_int(answer_ttl) or answer_ttl <= 0
                            or value is None):
                        self._snapshot_invalid("negative_answer")
                    answers.append({
                        "name": answer_name,
                        "type": answer_type,
                        "ttl": answer_ttl,
                        "value": value,
                    })
                key = (view, name, qtype, dnssec)
                if key in new_negative:
                    self._snapshot_invalid("duplicate_negative")
                new_negative[key] = {
                    "view": view,
                    "name": name,
                    "type": qtype,
                    "dnssec": dnssec,
                    "status": status,
                    "nodata": nodata,
                    "answers": answers,
                    "zone": zone,
                    "serial": serial,
                    "ttl": ttl,
                    "created_at": created_at,
                    "expires_at": expires_at,
                }

            new_stats = {}
            for key in STATS_KEYS:
                value = state["stats"].get(key)
                if not _is_int(value) or value < 0:
                    self._snapshot_invalid("stats")
                new_stats[key] = value

            self._views = new_views
            self._zones = new_zones
            self._positive = new_positive
            self._negative = new_negative
            self._stats = new_stats
            self._trim_cache()
