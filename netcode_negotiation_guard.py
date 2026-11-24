"""Protocol version and capability negotiation with a gray-release window."""

from __future__ import annotations

import copy
import threading

CAPABILITY_NAMES = (
    "delta_input",
    "rollback",
    "prediction",
    "interest_management",
    "compression",
)

_ALL_CAP_MASK = (1 << len(CAPABILITY_NAMES)) - 1
_STAT_KEYS = (
    "negotiations",
    "reconnects",
    "reused",
    "rejected",
    "degraded",
    "dropped",
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
        values = (server_min, server_max, supported_caps, profile_limit)
        if not all(self._is_int(value) for value in values):
            raise NetcodeError("NET_INVALID_ARGUMENT")
        if server_min < 1 or server_min > server_max:
            raise NetcodeError("NET_INVALID_ARGUMENT")
        if supported_caps < 0 or supported_caps & ~_ALL_CAP_MASK:
            raise NetcodeError("NET_INVALID_ARGUMENT")
        if profile_limit < 1:
            raise NetcodeError("NET_INVALID_ARGUMENT")

        self.server_min = server_min
        self.server_max = server_max
        self.supported_caps = supported_caps
        self.profile_limit = profile_limit
        self._lock = threading.RLock()
        self._grace_min: int | None = None
        self._next_profile_number = 1
        self._profiles: dict[str, dict] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @staticmethod
    def _require_connection_id(connection_id: object) -> None:
        if not isinstance(connection_id, str) or not connection_id:
            raise NetcodeError("NET_INVALID_ARGUMENT")

    def _require_version(self, version: object) -> None:
        if not self._is_int(version) or version < 1:
            raise NetcodeError("NET_INVALID_ARGUMENT")

    def _validate_negotiation_arguments(
        self,
        connection_id: object,
        *,
        client_min: object,
        client_max: object,
        client_caps: object,
    ) -> None:
        self._require_connection_id(connection_id)
        self._require_version(client_min)
        self._require_version(client_max)
        if client_min > client_max:
            raise NetcodeError("NET_INVALID_ARGUMENT")
        if not self._is_int(client_caps) or client_caps < 0:
            raise NetcodeError("NET_INVALID_ARGUMENT")
        if client_caps & ~_ALL_CAP_MASK:
            raise NetcodeError("NET_INVALID_ARGUMENT")

    def _effective_min_locked(self) -> int:
        return self.server_min if self._grace_min is None else self._grace_min

    def _resolve_locked(
        self,
        *,
        client_min: int,
        client_max: int,
        client_caps: int,
    ) -> tuple[int, int]:
        effective_min = self._effective_min_locked()
        lower = max(client_min, effective_min)
        upper = min(client_max, self.server_max)
        if lower > upper:
            raise NetcodeError(
                "NET_VERSION_UNREACHABLE",
                {
                    "client": [client_min, client_max],
                    "server": [effective_min, self.server_max],
                },
            )

        caps = client_caps & self.supported_caps
        if caps == 0:
            raise NetcodeError("NET_CAP_NONE_SHARED")
        return upper, caps

    def _make_profile_locked(
        self,
        connection_id: str,
        version: int,
        caps: int,
    ) -> dict:
        number = self._next_profile_number
        self._next_profile_number += 1
        return {
            "connection": connection_id,
            "version": version,
            "caps": caps,
            "profile": f"profile-{number}",
            "degraded": version < self.server_max,
        }

    def _store_profile_locked(self, connection_id: str, profile: dict) -> None:
        self._profiles.pop(connection_id, None)
        if len(self._profiles) >= self.profile_limit:
            oldest_connection = next(iter(self._profiles))
            self._profiles.pop(oldest_connection, None)
        self._profiles[connection_id] = profile

    def compat_window(self) -> dict:
        with self._lock:
            return {"min": self._effective_min_locked(), "max": self.server_max}

    def set_grace_window(self, low: int) -> dict:
        with self._lock:
            if not self._is_int(low) or low < 1 or low > self.server_min:
                raise NetcodeError("NET_GRACE_INVALID")
            self._grace_min = low
            return {"min": low, "max": self.server_max}

    def clear_grace_window(self) -> dict:
        with self._lock:
            self._grace_min = None
            return {"min": self.server_min, "max": self.server_max}

    def negotiate(self, connection_id: str, *, client_min: int, client_max: int,
                  client_caps: int) -> dict:
        with self._lock:
            self._validate_negotiation_arguments(
                connection_id,
                client_min=client_min,
                client_max=client_max,
                client_caps=client_caps,
            )
            try:
                version, caps = self._resolve_locked(
                    client_min=client_min,
                    client_max=client_max,
                    client_caps=client_caps,
                )
            except NetcodeError:
                self._stats["rejected"] += 1
                raise

            profile = self._make_profile_locked(connection_id, version, caps)
            self._store_profile_locked(connection_id, profile)
            self._stats["negotiations"] += 1
            if profile["degraded"]:
                self._stats["degraded"] += 1
            return {**profile, "reused": False}

    def reconnect(self, connection_id: str, *, client_min: int, client_max: int,
                  client_caps: int) -> dict:
        with self._lock:
            self._validate_negotiation_arguments(
                connection_id,
                client_min=client_min,
                client_max=client_max,
                client_caps=client_caps,
            )
            if connection_id not in self._profiles:
                raise NetcodeError("NET_UNKNOWN_CONNECTION")

            self._stats["reconnects"] += 1
            try:
                version, caps = self._resolve_locked(
                    client_min=client_min,
                    client_max=client_max,
                    client_caps=client_caps,
                )
            except NetcodeError:
                self._stats["rejected"] += 1
                raise

            previous = self._profiles[connection_id]
            if previous["version"] == version and previous["caps"] == caps:
                self._stats["reused"] += 1
                return {**previous, "reused": True}

            profile = self._make_profile_locked(connection_id, version, caps)
            self._store_profile_locked(connection_id, profile)
            return {**profile, "reused": False}

    def drop(self, connection_id: str) -> None:
        with self._lock:
            self._require_connection_id(connection_id)
            if connection_id not in self._profiles:
                raise NetcodeError("NET_UNKNOWN_CONNECTION")
            del self._profiles[connection_id]
            self._stats["dropped"] += 1

    def profile(self, connection_id: str) -> dict:
        with self._lock:
            self._require_connection_id(connection_id)
            try:
                profile = self._profiles[connection_id]
            except KeyError:
                raise NetcodeError("NET_UNKNOWN_CONNECTION") from None
            return dict(profile)

    def profiles(self) -> int:
        with self._lock:
            return len(self._profiles)

    def capability_names(self, mask: int) -> list:
        return [
            name
            for bit, name in enumerate(CAPABILITY_NAMES)
            if mask & (1 << bit)
        ]

    def is_compatible(self, version: int) -> bool:
        with self._lock:
            self._require_version(version)
            return self._effective_min_locked() <= version <= self.server_max

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "grace_min": self._grace_min,
                "next_profile_number": self._next_profile_number,
                "profiles": self._profiles,
                "stats": self._stats,
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                candidate = copy.deepcopy(state)
            except Exception as exc:
                raise NetcodeError("NET_SNAPSHOT_INVALID") from exc
            self._validate_snapshot(candidate)

            profiles = {
                connection_id: dict(profile)
                for connection_id, profile in candidate["profiles"].items()
            }
            stats = {
                key: candidate["stats"][key]
                for key in _STAT_KEYS
            }
            self._grace_min = candidate["grace_min"]
            self._next_profile_number = candidate["next_profile_number"]
            self._profiles = profiles
            self._stats = stats

    def _validate_snapshot(self, state: object) -> None:
        if not isinstance(state, dict):
            raise NetcodeError("NET_SNAPSHOT_INVALID")
        version = state.get("version")
        if not self._is_int(version) or version != 1:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        required = {
            "version",
            "grace_min",
            "next_profile_number",
            "profiles",
            "stats",
        }
        if not required.issubset(state):
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        grace_min = state["grace_min"]
        if grace_min is not None and (
            not self._is_int(grace_min)
            or grace_min < 1
            or grace_min > self.server_min
        ):
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        next_number = state["next_profile_number"]
        if not self._is_int(next_number) or next_number < 1:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        profiles = state["profiles"]
        if not isinstance(profiles, dict) or len(profiles) > self.profile_limit:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        max_profile_number = 0
        for connection_id, profile in profiles.items():
            self._validate_snapshot_profile(
                connection_id,
                profile,
                next_number=next_number,
            )
            number_text = profile["profile"][len("profile-"):]
            max_profile_number = max(max_profile_number, int(number_text))

        if max_profile_number >= next_number:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        stats = state["stats"]
        if not isinstance(stats, dict) or set(stats) != set(_STAT_KEYS):
            raise NetcodeError("NET_SNAPSHOT_INVALID")
        if not all(
            self._is_int(value) and value >= 0
            for value in stats.values()
        ):
            raise NetcodeError("NET_SNAPSHOT_INVALID")

    def _validate_snapshot_profile(
        self,
        connection_id: object,
        profile: object,
        *,
        next_number: int,
    ) -> None:
        if not isinstance(connection_id, str) or not connection_id:
            raise NetcodeError("NET_SNAPSHOT_INVALID")
        if not isinstance(profile, dict):
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        required = {
            "connection",
            "version",
            "caps",
            "profile",
            "degraded",
        }
        if set(profile) != required:
            raise NetcodeError("NET_SNAPSHOT_INVALID")
        if profile["connection"] != connection_id:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        version = profile["version"]
        if not self._is_int(version) or version < 1 or version > self.server_max:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        caps = profile["caps"]
        if (
            not self._is_int(caps)
            or caps <= 0
            or caps & ~_ALL_CAP_MASK
            or caps & ~self.supported_caps
        ):
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        profile_name = profile["profile"]
        prefix = "profile-"
        if (
            not isinstance(profile_name, str)
            or not profile_name.startswith(prefix)
            or not profile_name[len(prefix):].isdigit()
        ):
            raise NetcodeError("NET_SNAPSHOT_INVALID")
        profile_number = int(profile_name[len(prefix):])
        if profile_number < 1 or profile_number >= next_number:
            raise NetcodeError("NET_SNAPSHOT_INVALID")

        if not isinstance(profile["degraded"], bool):
            raise NetcodeError("NET_SNAPSHOT_INVALID")
        if profile["degraded"] != (version < self.server_max):
            raise NetcodeError("NET_SNAPSHOT_INVALID")
