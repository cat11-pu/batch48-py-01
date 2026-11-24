"""Protocol version and capability negotiation with a gray-release window."""

from __future__ import annotations

import threading

CAPABILITY_NAMES = (
    "delta_input",
    "rollback",
    "prediction",
    "interest_management",
    "compression",
)


class NetcodeError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class NetcodeNegotiator:
    """Negotiates versions and capabilities per connection and caches profiles."""

    def __init__(self, *, server_min: int, server_max: int, supported_caps: int,
                 profile_limit: int = 8) -> None:
        self.server_min = int(server_min)
        self.server_max = int(server_max)
        self.supported_caps = int(supported_caps)
        self.profile_limit = int(profile_limit)
        self._lock = threading.RLock()

    def compat_window(self) -> dict:
        raise NotImplementedError

    def set_grace_window(self, low: int) -> dict:
        raise NotImplementedError

    def clear_grace_window(self) -> dict:
        raise NotImplementedError

    def negotiate(self, connection_id: str, *, client_min: int, client_max: int,
                  client_caps: int) -> dict:
        raise NotImplementedError

    def reconnect(self, connection_id: str, *, client_min: int, client_max: int,
                  client_caps: int) -> dict:
        raise NotImplementedError

    def drop(self, connection_id: str) -> None:
        raise NotImplementedError

    def profile(self, connection_id: str) -> dict:
        raise NotImplementedError

    def profiles(self) -> int:
        raise NotImplementedError

    def capability_names(self, mask: int) -> list:
        raise NotImplementedError

    def is_compatible(self, version: int) -> bool:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
