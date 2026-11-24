"""HTTP CONNECT tunnel admission and byte metering guard."""

from __future__ import annotations

import threading


class TunnelError(ValueError):
    """Raised when a tunnel operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


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

    def set_policy(self, tenant: str, policy: dict) -> dict:
        raise NotImplementedError

    def policy_of(self, tenant: str) -> dict:
        raise NotImplementedError

    def open_tunnel(self, authority: str, *, tenant: str, now: int = 0) -> dict:
        raise NotImplementedError

    def admit_queued(self, tenant: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def meter(self, tunnel_id: str, up: int, down: int, *,
              now: int = 0) -> dict:
        raise NotImplementedError

    def close_tunnel(self, tunnel_id: str, reason: str = "client_closed", *,
                     now: int = 0) -> dict:
        raise NotImplementedError

    def revalidate(self, tunnel_id: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def list_tunnels(self, tenant: str | None = None, *,
                     state: str | None = None) -> list:
        raise NotImplementedError

    def tenant_usage(self, tenant: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
