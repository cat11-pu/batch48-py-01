"""Proxy chain parsing, bypass matching and egress health selection."""

from __future__ import annotations

import threading


class ProxyError(ValueError):
    """Rejection raised by the proxy chain resolver."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ProxyChainResolver:
    """Parse proxy chains, decide direct bypass and pick a healthy egress."""

    def __init__(self, *, max_hops: int = 8, cooldown: float = 60.0,
                 failure_threshold: int = 3, clock=None) -> None:
        self.max_hops = max_hops
        self.cooldown = cooldown
        self.failure_threshold = failure_threshold
        self._clock = clock
        self._lock = threading.RLock()

    def parse(self, spec: str) -> list:
        raise NotImplementedError

    def set_bypass(self, rules) -> None:
        raise NotImplementedError

    def bypass(self, target: str) -> bool:
        raise NotImplementedError

    def register_egress(self, egress_id: str, *, priority: int, address: str) -> None:
        raise NotImplementedError

    def remove_egress(self, egress_id: str) -> bool:
        raise NotImplementedError

    def select(self, now=None) -> str:
        raise NotImplementedError

    def probe_result(self, egress_id: str, ok: bool, now=None) -> dict:
        raise NotImplementedError

    def health(self, egress_id: str) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
