"""Spectator stand and bench seat orchestration for hosted matches."""

from __future__ import annotations

from copy import deepcopy
import threading


STATS_KEYS = (
    "spectators_joined",
    "bench_joined",
    "promotions",
    "demotions",
    "swaps",
    "left",
    "settled",
    "rejected",
    "stale_replacements",
)

SNAPSHOT_VERSION = 1


class SpectatorError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SpectatorBenchOrchestrator:
    """Own active seats, the ordered bench and read-only spectator slots."""

    def __init__(self, *, active_slots: int = 5, spectator_limit: int = 8,
                 bench_limit: int = 4, clock=None) -> None:
        self.active_slots = active_slots
        self.spectator_limit = spectator_limit
        self.bench_limit = bench_limit
        self._clock = clock
        self._lock = threading.RLock()
        self._active: dict[int, str] = {}
        self._bench: list[dict] = []
        self._spectators: dict[int, str] = {}
        self._permissions: dict[str, bool] = {}
        self._settled = False
        self._result = None
        self._stats = {key: 0 for key in STATS_KEYS}
        self._last_promotion: dict[int, dict] = {}

    def assign_active(self, seat: int, player: str) -> dict:
        with self._lock:
            self._ensure_open()
            if not self._valid_seat(seat):
                self._reject("SPECTATOR_SEAT_INVALID", {"seat": seat})
            if seat in self._active:
                self._reject(
                    "SPECTATOR_SEAT_OCCUPIED",
                    {"seat": seat, "occupant": self._active[seat]},
                )
            if self._role_of(player) is not None:
                self._reject("SPECTATOR_ALREADY_SEATED", {"player": player})

            self._active[seat] = player
            self._permissions[player] = True
            return {
                "player": player,
                "seat": seat,
                "role": "active",
                "permission": True,
            }

    def join_spectator(self, player: str) -> dict:
        with self._lock:
            self._ensure_open()
            if self._role_of(player) is not None:
                self._reject("SPECTATOR_ALREADY_SEATED", {"player": player})
            if len(self._spectators) >= self.spectator_limit:
                self._reject("SPECTATOR_ROOM_FULL", {
                    "spectator_limit": self.spectator_limit,
                })

            seat = self._next_free_spectator_seat()
            self._spectators[seat] = player
            self._permissions[player] = False
            self._stats["spectators_joined"] += 1
            return {
                "player": player,
                "role": "spectator",
                "seat": seat,
                "permission": False,
            }

    def join_bench(self, player: str) -> dict:
        with self._lock:
            self._ensure_open()
            if self._role_of(player) is not None:
                self._reject("SPECTATOR_ALREADY_SEATED", {"player": player})
            if len(self._bench) >= self.bench_limit:
                self._reject("SPECTATOR_BENCH_FULL", {
                    "bench_limit": self.bench_limit,
                })

            bench_index = len(self._bench) + 1
            self._bench.append({"player": player, "reason": None})
            self._permissions[player] = False
            self._stats["bench_joined"] += 1
            return self._bench_join_response(player, bench_index, False)

    def upgrade_spectator(self, player: str) -> dict:
        with self._lock:
            self._ensure_open()
            role = self._role_of(player)
            if role is None:
                self._reject("SPECTATOR_UNKNOWN_PLAYER", {"player": player})
            if role != "spectator":
                self._reject("SPECTATOR_NOT_SPECTATOR", {
                    "player": player,
                    "role": role,
                })
            if len(self._bench) >= self.bench_limit:
                self._reject("SPECTATOR_BENCH_FULL", {
                    "bench_limit": self.bench_limit,
                })

            spectator_seat = self._spectator_seat(player)
            del self._spectators[spectator_seat]
            bench_index = len(self._bench) + 1
            self._bench.append({"player": player, "reason": None})
            self._permissions[player] = False
            self._stats["bench_joined"] += 1
            return self._bench_join_response(player, bench_index, False)

    def promote(self, player: str, seat: int, *, reason: str = "voluntary",
                expect_occupant=None) -> dict:
        with self._lock:
            self._ensure_open()
            if not self._valid_seat(seat):
                self._reject("SPECTATOR_SEAT_INVALID", {"seat": seat})

            current = self._active.get(seat)
            if self._active_seat(player) == seat:
                return self._already_processed(seat, player, reason)
            if current is None:
                self._reject("SPECTATOR_SEAT_EMPTY", {"seat": seat})
            if expect_occupant is not None and expect_occupant != current:
                return self._already_processed(seat, player, reason)

            role = self._role_of(player)
            if role is None:
                self._reject("SPECTATOR_UNKNOWN_PLAYER", {"player": player})
            if role != "bench":
                self._reject("SPECTATOR_BENCH_REQUIRED", {
                    "player": player,
                    "role": role,
                })

            replaced = current
            self._remove_bench_player(player)
            self._active[seat] = player
            self._bench.append({"player": replaced, "reason": reason})
            self._permissions[player] = True
            self._permissions[replaced] = False

            result = {
                "status": "promoted",
                "player": player,
                "seat": seat,
                "replaced": replaced,
                "reason": reason,
            }
            self._last_promotion[seat] = dict(result)
            self._stats["promotions"] += 1
            return dict(result)

    def demote(self, player: str, *, reason: str = "timeout") -> dict:
        with self._lock:
            self._ensure_open()
            role = self._role_of(player)
            if role is None:
                self._reject("SPECTATOR_UNKNOWN_PLAYER", {"player": player})
            if role != "active":
                self._reject("SPECTATOR_NOT_ACTIVE", {
                    "player": player,
                    "role": role,
                })
            if len(self._bench) >= self.bench_limit:
                self._reject("SPECTATOR_BENCH_FULL", {
                    "bench_limit": self.bench_limit,
                })

            seat = self._active_seat(player)
            del self._active[seat]
            self._bench.append({"player": player, "reason": reason})
            self._permissions[player] = False
            bench_index = len(self._bench)
            self._stats["demotions"] += 1
            return {
                "player": player,
                "seat": seat,
                "role": "bench",
                "reason": reason,
                "bench_index": bench_index,
            }

    def swap_seat(self, player_a: str, player_b: str) -> dict:
        with self._lock:
            self._ensure_open()
            if player_a == player_b:
                self._reject("SPECTATOR_SAME_PLAYER", {"player": player_a})

            seat_a = self._required_active_seat(player_a)
            seat_b = self._required_active_seat(player_b)

            self._active[seat_a] = player_b
            self._active[seat_b] = player_a
            self._stats["swaps"] += 1
            return {
                "player_a": player_a,
                "seat_a": seat_b,
                "player_b": player_b,
                "seat_b": seat_a,
            }

    def set_permission(self, player: str, allowed: bool) -> dict:
        with self._lock:
            self._ensure_open()
            role = self._role_of(player)
            if role is None:
                self._reject("SPECTATOR_UNKNOWN_PLAYER", {"player": player})
            if role == "spectator":
                self._reject("SPECTATOR_READ_ONLY", {"player": player})

            permission = bool(allowed)
            self._permissions[player] = permission
            return {"player": player, "role": role, "permission": permission}

    def can_operate(self, player: str) -> bool:
        with self._lock:
            if self._settled or self._role_of(player) != "active":
                return False
            return self._permissions.get(player, False) is True

    def leave(self, player: str) -> dict:
        with self._lock:
            self._ensure_open()
            role = self._role_of(player)
            if role is None:
                self._reject("SPECTATOR_UNKNOWN_PLAYER", {"player": player})

            seat = None
            if role == "active":
                seat = self._active_seat(player)
                del self._active[seat]
            elif role == "bench":
                self._remove_bench_player(player)
            else:
                seat = self._spectator_seat(player)
                del self._spectators[seat]

            self._permissions.pop(player, None)
            self._stats["left"] += 1
            return {
                "player": player,
                "role": role,
                "seat": seat,
                "settled": self._settled,
            }

    def seat_state(self, player: str) -> dict:
        with self._lock:
            role = self._role_of(player)
            if role is None:
                raise SpectatorError(
                    "SPECTATOR_UNKNOWN_PLAYER", {"player": player}
                )

            seat = None
            bench_index = None
            reason = None
            if role == "active":
                seat = self._active_seat(player)
            elif role == "bench":
                bench_index = self._bench_index(player)
                reason = self._bench_reason(player)
            else:
                seat = self._spectator_seat(player)

            return {
                "player": player,
                "role": role,
                "seat": seat,
                "bench_index": bench_index,
                "permission": self._permissions.get(player, False),
                "reason": reason,
            }

    def roster(self) -> dict:
        with self._lock:
            active = [
                {"seat": seat, "player": self._active[seat]}
                for seat in sorted(self._active)
            ]
            bench = [
                {"player": entry["player"], "reason": entry["reason"]}
                for entry in self._bench
            ]
            spectators = [
                {"seat": seat, "player": self._spectators[seat]}
                for seat in sorted(self._spectators)
            ]
            return {
                "active": active,
                "bench": bench,
                "spectators": spectators,
                "settled": self._settled,
                "result": self._result,
                "counts": {
                    "active": len(self._active),
                    "bench": len(self._bench),
                    "spectators": len(self._spectators),
                },
            }

    def settle_match(self, result: str) -> dict:
        with self._lock:
            self._ensure_open()
            self._settled = True
            self._result = result
            self._stats["settled"] += 1
            return {
                "settled": True,
                "result": result,
                "counts": {
                    "active": len(self._active),
                    "bench": len(self._bench),
                    "spectators": len(self._spectators),
                },
            }

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STATS_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": SNAPSHOT_VERSION,
                "active": dict(self._active),
                "bench": [dict(entry) for entry in self._bench],
                "spectators": dict(self._spectators),
                "permissions": dict(self._permissions),
                "settled": self._settled,
                "result": self._result,
                "stats": dict(self._stats),
            }
            return deepcopy(state)

    def restore(self, state) -> None:
        with self._lock:
            candidate = self._validated_snapshot(state)
            self._active = candidate["active"]
            self._bench = candidate["bench"]
            self._spectators = candidate["spectators"]
            self._permissions = candidate["permissions"]
            self._settled = candidate["settled"]
            self._result = candidate["result"]
            self._stats = candidate["stats"]
            self._last_promotion = {}

    def _ensure_open(self) -> None:
        if self._settled:
            self._reject("SPECTATOR_MATCH_SETTLED", {"settled": True})

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise SpectatorError(code, detail)

    def _valid_seat(self, seat) -> bool:
        return (
            isinstance(seat, int)
            and not isinstance(seat, bool)
            and 1 <= seat <= self.active_slots
        )

    def _role_of(self, player: str):
        if self._active_seat(player) is not None:
            return "active"
        if self._bench_index(player) is not None:
            return "bench"
        if self._spectator_seat(player) is not None:
            return "spectator"
        return None

    def _active_seat(self, player: str):
        for seat, occupant in self._active.items():
            if occupant == player:
                return seat
        return None

    def _required_active_seat(self, player: str):
        seat = self._active_seat(player)
        if seat is not None:
            return seat
        role = self._role_of(player)
        if role is None:
            self._reject("SPECTATOR_UNKNOWN_PLAYER", {"player": player})
        self._reject("SPECTATOR_NOT_ACTIVE", {"player": player, "role": role})

    def _bench_index(self, player: str):
        for index, entry in enumerate(self._bench, start=1):
            if entry["player"] == player:
                return index
        return None

    def _bench_reason(self, player: str):
        for entry in self._bench:
            if entry["player"] == player:
                return entry["reason"]
        return None

    def _remove_bench_player(self, player: str) -> None:
        index = self._bench_index(player)
        if index is not None:
            self._bench.pop(index - 1)

    def _spectator_seat(self, player: str):
        for seat, occupant in self._spectators.items():
            if occupant == player:
                return seat
        return None

    def _next_free_spectator_seat(self) -> int:
        for seat in range(1, self.spectator_limit + 1):
            if seat not in self._spectators:
                return seat
        self._reject("SPECTATOR_ROOM_FULL", {
            "spectator_limit": self.spectator_limit,
        })

    def _bench_join_response(self, player: str, bench_index: int,
                             permission: bool) -> dict:
        return {
            "player": player,
            "role": "bench",
            "bench_index": bench_index,
            "seat": None,
            "reason": None,
            "permission": permission,
        }

    def _already_processed(self, seat: int, player: str, reason: str) -> dict:
        self._stats["stale_replacements"] += 1
        previous = self._last_promotion.get(seat)
        if previous is not None:
            result = dict(previous)
        else:
            current = self._active.get(seat)
            result = {
                "player": current if current is not None else player,
                "seat": seat,
                "replaced": None,
                "reason": reason,
            }
        result["status"] = "already_processed"
        return result

    def _validated_snapshot(self, state) -> dict:
        if not isinstance(state, dict):
            raise SpectatorError("SPECTATOR_SNAPSHOT_INVALID", {
                "reason": "snapshot must be a dict",
            })

        required = (
            "version", "active", "bench", "spectators", "permissions",
            "settled", "result", "stats",
        )
        missing = [key for key in required if key not in state]
        if missing:
            raise SpectatorError("SPECTATOR_SNAPSHOT_INVALID", {
                "missing": missing,
            })

        version = state["version"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != SNAPSHOT_VERSION
        ):
            raise SpectatorError("SPECTATOR_SNAPSHOT_INVALID", {
                "reason": "unsupported version",
                "version": version,
            })

        active = self._snapshot_active(state["active"])
        bench = self._snapshot_bench(state["bench"])
        spectators = self._snapshot_spectators(state["spectators"])
        permissions = self._snapshot_permissions(state["permissions"])
        settled = state["settled"]
        if not isinstance(settled, bool):
            raise SpectatorError("SPECTATOR_SNAPSHOT_INVALID", {
                "reason": "settled must be bool",
            })

        result = state["result"]
        if result is not None and not isinstance(result, str):
            raise SpectatorError("SPECTATOR_SNAPSHOT_INVALID", {
                "reason": "result must be str or None",
            })

        stats = self._snapshot_stats(state["stats"])
        self._validate_snapshot_players(
            active, bench, spectators, permissions
        )

        for seat_player in active.values():
            permissions.setdefault(seat_player, True)
        for entry in bench:
            permissions.setdefault(entry["player"], False)
        for seat_player in spectators.values():
            permissions.setdefault(seat_player, False)

        return {
            "active": active,
            "bench": bench,
            "spectators": spectators,
            "permissions": permissions,
            "settled": settled,
            "result": result,
            "stats": stats,
        }

    def _snapshot_seat(self, raw_seat, limit: int, context: str) -> int:
        if isinstance(raw_seat, bool):
            self._invalid_snapshot(f"invalid {context} seat", raw_seat)
        if isinstance(raw_seat, int):
            seat = raw_seat
        elif isinstance(raw_seat, str) and raw_seat.isdigit():
            seat = int(raw_seat)
        else:
            self._invalid_snapshot(f"invalid {context} seat", raw_seat)

        if not 1 <= seat <= limit:
            self._invalid_snapshot(f"{context} seat out of range", raw_seat)
        return seat

    def _snapshot_player(self, player) -> str:
        if not isinstance(player, str) or not player:
            self._invalid_snapshot(
                "player must be a non-empty string", player
            )
        return player

    def _invalid_snapshot(self, reason: str, value: object = None) -> None:
        raise SpectatorError("SPECTATOR_SNAPSHOT_INVALID", {
            "reason": reason,
            "value": value,
        })

    def _snapshot_active(self, raw) -> dict:
        if not isinstance(raw, dict):
            self._invalid_snapshot("active must be a dict", raw)
        active = {}
        for raw_seat, raw_player in raw.items():
            seat = self._snapshot_seat(raw_seat, self.active_slots, "active")
            if seat in active:
                self._invalid_snapshot("duplicate active seat", seat)
            active[seat] = self._snapshot_player(raw_player)
        return active

    def _snapshot_bench(self, raw) -> list:
        if not isinstance(raw, list):
            self._invalid_snapshot("bench must be a list", raw)
        if len(raw) > self.bench_limit:
            self._invalid_snapshot("bench exceeds capacity", len(raw))

        bench = []
        for entry in raw:
            if isinstance(entry, str):
                player = self._snapshot_player(entry)
                reason = None
            elif isinstance(entry, dict):
                if "player" not in entry or "reason" not in entry:
                    self._invalid_snapshot("bench entry is incomplete", entry)
                player = self._snapshot_player(entry["player"])
                reason = entry["reason"]
                if reason is not None and not isinstance(reason, str):
                    self._invalid_snapshot(
                        "bench reason must be str or None", reason
                    )
            else:
                self._invalid_snapshot("invalid bench entry", entry)
            bench.append({"player": player, "reason": reason})
        return bench

    def _snapshot_spectators(self, raw) -> dict:
        if not isinstance(raw, dict):
            self._invalid_snapshot("spectators must be a dict", raw)
        if len(raw) > self.spectator_limit:
            self._invalid_snapshot(
                "spectator count exceeds capacity", len(raw)
            )

        spectators = {}
        for raw_seat, raw_player in raw.items():
            seat = self._snapshot_seat(
                raw_seat, self.spectator_limit, "spectator"
            )
            if seat in spectators:
                self._invalid_snapshot("duplicate spectator seat", seat)
            spectators[seat] = self._snapshot_player(raw_player)
        return spectators

    def _snapshot_permissions(self, raw) -> dict:
        if not isinstance(raw, dict):
            self._invalid_snapshot("permissions must be a dict", raw)

        permissions = {}
        for player, allowed in raw.items():
            valid_player = self._snapshot_player(player)
            if not isinstance(allowed, bool):
                self._invalid_snapshot("permission must be bool", allowed)
            permissions[valid_player] = allowed
        return permissions

    def _snapshot_stats(self, raw) -> dict:
        if not isinstance(raw, dict):
            self._invalid_snapshot("stats must be a dict", raw)

        stats = {}
        for key in STATS_KEYS:
            if key not in raw:
                self._invalid_snapshot("stats are incomplete", key)
            value = raw[key]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                self._invalid_snapshot(
                    "stat must be a non-negative int", key
                )
            stats[key] = value
        return stats

    def _validate_snapshot_players(self, active, bench, spectators,
                                   permissions) -> None:
        locations = {}
        for player in active.values():
            if player in locations:
                self._invalid_snapshot(
                    "player occupies multiple roles", player
                )
            locations[player] = "active"

        for entry in bench:
            player = entry["player"]
            if player in locations:
                self._invalid_snapshot(
                    "player occupies multiple roles", player
                )
            locations[player] = "bench"

        for player in spectators.values():
            if player in locations:
                self._invalid_snapshot(
                    "player occupies multiple roles", player
                )
            locations[player] = "spectator"

        for player in permissions:
            if not isinstance(player, str) or not player:
                self._invalid_snapshot("invalid permission player", player)
