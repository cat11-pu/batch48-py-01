"""Recursive DNS resolution with per view authorities and negative caching."""

from __future__ import annotations

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
        self.max_cname_depth = max_cname_depth
        self.default_ttl = default_ttl
        self.max_cache_entries = max_cache_entries
        self._clock = clock
        self._lock = threading.RLock()

    def declare_view(self, view: str, *, ndots: int = 1, search=(),
                     external: str = None) -> dict:
        raise NotImplementedError

    def declare_zone(self, view: str, zone: str, *, serial: int = 1,
                     soa_ttl: int = 300, soa_minimum: int = 60) -> dict:
        raise NotImplementedError

    def add_record(self, view: str, zone: str, name: str, rtype: str, value: str,
                   *, ttl: int = 300) -> dict:
        raise NotImplementedError

    def set_serial(self, view: str, zone: str, serial: int) -> dict:
        raise NotImplementedError

    def zone_info(self, view: str, zone: str) -> dict:
        raise NotImplementedError

    def resolve(self, name: str, qtype: str, *, view: str = "default",
                now: int = 0, dnssec: bool = False) -> dict:
        raise NotImplementedError

    def cache_entries(self) -> list:
        raise NotImplementedError

    def negative_cache(self) -> list:
        raise NotImplementedError

    def expire(self, *, now: int = 0) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
