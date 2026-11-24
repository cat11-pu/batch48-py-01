"""Pre-match readiness barrier with staged gates, takeover and grace slots."""

from __future__ import annotations

import copy
import threading
from typing import Any

STAGES = ("assets", "scene", "baseline")
_STAGE_ORDER = {stage: index for index, stage in enumerate(STAGES)}
_PLAYER_STATES = {
    "waiting",
    "released",
    "hosted",
    "left",
    "grace",
    "absent",
    "replaced",
    "substituted",
}
_OCCUPIED_STATES = {"waiting", "released", "hosted", "grace", "substituted"}
_SNAPSHOT_VERSION = 1
_STAT_KEYS = (
    "joins",
    "reports",
    "barriers",
    "released",
    "hosted",
    "dropped",
    "substituted",
    "short_handed",
)


class MatchBarrierError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MatchBarrierGuard:
    """Gates the match start on staged readiness and arbitrates late entries."""

    def __init__(self, *, threshold: str = "baseline", timeout_ticks: int = 10,
                 grace_ticks: int = 5, max_players: int = 8) -> None:
        if threshold not in _STAGE_ORDER:
            raise MatchBarrierError("MATCH_INVALID_ARGUMENT")

        self.threshold = threshold
        self.timeout_ticks = self._non_negative_int(timeout_ticks, "timeout_ticks")
        self.grace_ticks = self._non_negative_int(grace_ticks, "grace_ticks")
        self.max_players = self._non_negative_int(max_players, "max_players")

        self._lock = threading.RLock()
        self._players: dict[str, dict[str, Any]] = {}
        self._graces: dict[str, dict[str, int]] = {}
        self._released = False
        self._released_at: int | None = None
        self._first_join_tick: int | None = None
        self._baseline_ticks: dict[str, int | None] = {}
        self._hosted_players: list[str] = []
        self._short_handed: list[str] = []
        self._grace_sequence = 0
        self._stats = {key: 0 for key in _STAT_KEYS}

    def join(self, player: str, *, slot: int | None = None, tick: int = 0) -> dict:
        with self._lock:
            self._require_player_name(player)
            join_tick = self._require_tick(tick)

            if self._released:
                raise MatchBarrierError("MATCH_ALREADY_RELEASED")
            if player in self._players:
                raise MatchBarrierError("MATCH_DUPLICATE_PLAYER")

            assigned_slot = self._assign_slot(slot)
            self._players[player] = {
                "slot": assigned_slot,
                "stage": None,
                "ready": False,
                "state": "waiting",
            }
            if self._first_join_tick is None or join_tick < self._first_join_tick:
                self._first_join_tick = join_tick
            self._stats["joins"] += 1

            return {
                "player": player,
                "slot": assigned_slot,
                "stage": None,
                "ready": False,
                "state": "waiting",
            }

    def report_ready(self, player: str, stage: str, *, tick: int = 0) -> dict:
        with self._lock:
            report_tick = self._require_tick(tick)
            if self._released:
                raise MatchBarrierError("MATCH_ALREADY_RELEASED")

            record = self._players.get(player)
            if record is None:
                raise MatchBarrierError("MATCH_UNKNOWN_PLAYER")
            if stage not in _STAGE_ORDER:
                raise MatchBarrierError("MATCH_INVALID_STAGE")

            current_stage = record["stage"]
            current_order = -1 if current_stage is None else _STAGE_ORDER[current_stage]
            next_order = _STAGE_ORDER[stage]
            if next_order < current_order:
                raise MatchBarrierError("MATCH_STAGE_REGRESSION")

            if next_order > current_order:
                record["stage"] = stage
                record["ready"] = next_order >= _STAGE_ORDER[self.threshold]
                self._stats["reports"] += 1

            return {
                "player": player,
                "stage": record["stage"],
                "ready": record["ready"],
                "tick": report_tick,
            }

    def barrier(self, tick: int) -> dict:
        with self._lock:
            current_tick = self._require_tick(tick)
            self._stats["barriers"] += 1

            if self._released:
                return self._barrier_result(
                    current_tick,
                    True,
                    [],
                    list(self._hosted_players),
                    self._released_at,
                )

            waiting_entries = [
                (record["slot"], name)
                for name, record in self._players.items()
                if record["state"] == "waiting" and not record["ready"]
            ]
            waiting_entries.sort()
            waiting = [name for _slot, name in waiting_entries]

            if not waiting:
                self._release_locked(current_tick)
            elif current_tick >= self._join_deadline_locked():
                for name in waiting:
                    self._players[name]["state"] = "hosted"
                self._hosted_players.extend(waiting)
                self._stats["hosted"] += len(waiting)
                self._release_locked(current_tick)
                waiting = []

            if self._released:
                return self._barrier_result(
                    current_tick,
                    True,
                    [],
                    list(self._hosted_players),
                    self._released_at,
                )

            return self._barrier_result(current_tick, False, waiting, [], None)

    def drop(self, player: str, *, tick: int) -> dict:
        with self._lock:
            drop_tick = self._require_tick(tick)
            self._require_player_name(player)
            record = self._players.get(player)
            if record is None:
                raise MatchBarrierError("MATCH_UNKNOWN_PLAYER")

            if not self._released:
                if record["state"] == "left":
                    raise MatchBarrierError("MATCH_INVALID_STATE")
                record["state"] = "left"
                self._graces.pop(player, None)
                self._stats["dropped"] += 1
                return {"player": player, "state": "left", "deadline": None}

            if record["state"] not in _OCCUPIED_STATES or record["state"] == "grace":
                raise MatchBarrierError("MATCH_INVALID_STATE")

            deadline = drop_tick + self.grace_ticks
            self._grace_sequence += 1
            record["state"] = "grace"
            self._graces[player] = {
                "slot": record["slot"],
                "deadline": deadline,
                "seq": self._grace_sequence,
            }
            self._stats["dropped"] += 1
            return {"player": player, "state": "grace", "deadline": deadline}

    def substitute(self, player: str, *, slot: int | None = None,
                   tick: int = 0) -> dict:
        with self._lock:
            self._require_player_name(player)
            substitute_tick = self._require_tick(tick)
            if not self._released:
                raise MatchBarrierError("MATCH_INVALID_STATE")
            if player in self._players:
                raise MatchBarrierError("MATCH_DUPLICATE_PLAYER")

            old_name, grace = self._select_grace_locked(slot, substitute_tick)
            inherited_slot = grace["slot"]
            baseline_tick = self._released_at

            self._players[old_name]["state"] = "replaced"
            self._graces.pop(old_name, None)
            self._players[player] = {
                "slot": inherited_slot,
                "stage": None,
                "ready": False,
                "state": "substituted",
            }
            self._baseline_ticks[player] = baseline_tick
            self._stats["substituted"] += 1

            return {
                "player": player,
                "slot": inherited_slot,
                "state": "substituted",
                "baseline_tick": baseline_tick,
            }

    def advance(self, tick: int) -> dict:
        with self._lock:
            current_tick = self._require_tick(tick)
            expired = self._expire_graces_locked(current_tick)
            return {
                "tick": current_tick,
                "expired": expired,
                "short_handed": list(self._short_handed),
            }

    def state(self) -> str:
        with self._lock:
            return "released" if self._released else "waiting"

    def released_at(self) -> int | None:
        with self._lock:
            return self._released_at

    def deadline(self) -> int:
        with self._lock:
            return self._join_deadline_locked()

    def short_handed(self) -> list:
        with self._lock:
            return list(self._short_handed)

    def players(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._players)

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                "version": _SNAPSHOT_VERSION,
                "threshold": self.threshold,
                "timeout_ticks": self.timeout_ticks,
                "grace_ticks": self.grace_ticks,
                "max_players": self.max_players,
                "released": self._released,
                "state": self.state(),
                "released_at": self._released_at,
                "first_join_tick": self._first_join_tick,
                "baseline_ticks": copy.deepcopy(self._baseline_ticks),
                "deadline": self._join_deadline_locked(),
                "hosted": list(self._hosted_players),
                "short_handed": list(self._short_handed),
                "grace_sequence": self._grace_sequence,
                "graces": copy.deepcopy(self._graces),
                "players": copy.deepcopy(self._players),
                "stats": dict(self._stats),
            }
            return copy.deepcopy(data)

    def restore(self, state: dict) -> None:
        with self._lock:
            candidate = self._parse_snapshot(state)
            self.threshold = candidate["threshold"]
            self.timeout_ticks = candidate["timeout_ticks"]
            self.grace_ticks = candidate["grace_ticks"]
            self.max_players = candidate["max_players"]
            self._released = candidate["released"]
            self._released_at = candidate["released_at"]
            self._first_join_tick = candidate["first_join_tick"]
            self._baseline_ticks = candidate["baseline_ticks"]
            self._hosted_players = candidate["hosted"]
            self._short_handed = candidate["short_handed"]
            self._grace_sequence = candidate["grace_sequence"]
            self._graces = candidate["graces"]
            self._players = candidate["players"]
            self._stats = candidate["stats"]

    @staticmethod
    def _require_player_name(player: Any) -> None:
        if not isinstance(player, str) or not player.strip():
            raise MatchBarrierError("MATCH_INVALID_ARGUMENT")

    @staticmethod
    def _require_tick(tick: Any) -> int:
        if isinstance(tick, bool) or not isinstance(tick, int):
            raise MatchBarrierError("MATCH_INVALID_ARGUMENT", "tick")
        if tick < 0:
            raise MatchBarrierError("MATCH_INVALID_ARGUMENT", "tick")
        return tick

    @staticmethod
    def _non_negative_int(value: Any, name: str) -> int:
        try:
            converted = int(value)
        except (TypeError, ValueError) as exc:
            raise MatchBarrierError("MATCH_INVALID_ARGUMENT", name) from exc
        if converted < 0:
            raise MatchBarrierError("MATCH_INVALID_ARGUMENT", name)
        return converted

    def _join_deadline_locked(self) -> int:
        first_tick = 0 if self._first_join_tick is None else self._first_join_tick
        return first_tick + self.timeout_ticks

    def _slot_occupied_locked(self, slot: int) -> bool:
        return any(
            record["slot"] == slot and record["state"] in _OCCUPIED_STATES
            for record in self._players.values()
        )

    def _assign_slot(self, requested_slot: int | None) -> int:
        if requested_slot is not None:
            if (
                isinstance(requested_slot, bool)
                or not isinstance(requested_slot, int)
                or not 0 <= requested_slot < self.max_players
            ):
                raise MatchBarrierError("MATCH_INVALID_ARGUMENT", "slot")
            if self._slot_occupied_locked(requested_slot):
                raise MatchBarrierError("MATCH_INVALID_ARGUMENT", "slot")
            return requested_slot

        for slot in range(self.max_players):
            if not self._slot_occupied_locked(slot):
                return slot
        raise MatchBarrierError("MATCH_FULL")

    def _release_locked(self, tick: int) -> None:
        for record in self._players.values():
            if record["state"] == "waiting":
                record["state"] = "released" if record["ready"] else "hosted"
        self._released = True
        self._released_at = tick
        self._stats["released"] += 1

    @staticmethod
    def _barrier_result(tick: int, released: bool, waiting: list[str],
                        hosted: list[str], released_at: int | None) -> dict:
        return {
            "tick": tick,
            "released": released,
            "waiting": list(waiting),
            "hosted": list(hosted),
            "released_at": released_at,
        }

    def _expire_graces_locked(self, tick: int) -> list[str]:
        candidates = [
            (grace["deadline"], grace["seq"], name)
            for name, grace in self._graces.items()
            if self._players.get(name, {}).get("state") == "grace"
        ]
        candidates.sort()

        expired: list[str] = []
        for deadline, _sequence, name in candidates:
            if tick <= deadline:
                continue
            self._players[name]["state"] = "absent"
            self._graces.pop(name, None)
            if name not in self._short_handed:
                self._short_handed.append(name)
                self._stats["short_handed"] += 1
            expired.append(name)
        return expired

    def _select_grace_locked(self, requested_slot: int | None, tick: int):
        grace_items = [
            (name, grace)
            for name, grace in self._graces.items()
            if self._players.get(name, {}).get("state") == "grace"
        ]

        if requested_slot is not None:
            if (
                isinstance(requested_slot, bool)
                or not isinstance(requested_slot, int)
                or not 0 <= requested_slot < self.max_players
            ):
                raise MatchBarrierError("MATCH_INVALID_ARGUMENT", "slot")

            for name, grace in grace_items:
                if grace["slot"] == requested_slot:
                    if tick > grace["deadline"]:
                        raise MatchBarrierError("MATCH_GRACE_EXPIRED")
                    return name, grace

            if any(
                record["slot"] == requested_slot and record["state"] == "absent"
                for record in self._players.values()
            ):
                raise MatchBarrierError("MATCH_GRACE_EXPIRED")
            raise MatchBarrierError("MATCH_INVALID_STATE")

        if not grace_items:
            if any(record["state"] == "absent" for record in self._players.values()):
                raise MatchBarrierError("MATCH_GRACE_EXPIRED")
            raise MatchBarrierError("MATCH_INVALID_STATE")

        grace_items.sort(key=lambda item: (item[1]["deadline"], item[1]["seq"]))
        name, grace = grace_items[0]
        if tick > grace["deadline"]:
            raise MatchBarrierError("MATCH_GRACE_EXPIRED")
        return name, grace

    def _parse_snapshot(self, state: Any) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise MatchBarrierError("MATCH_SNAPSHOT_INVALID")
        if state.get("version") != _SNAPSHOT_VERSION:
            raise MatchBarrierError("MATCH_SNAPSHOT_INVALID")
        if type(state.get("version")) is not int:
            raise MatchBarrierError("MATCH_SNAPSHOT_INVALID")

        try:
            data = copy.deepcopy(state)
            threshold = data["threshold"]
            if threshold not in _STAGE_ORDER:
                raise ValueError("threshold")

            timeout_ticks = self._snapshot_non_negative_int(data["timeout_ticks"])
            grace_ticks = self._snapshot_non_negative_int(data["grace_ticks"])
            max_players = self._snapshot_non_negative_int(data["max_players"])

            released = data["released"]
            if not isinstance(released, bool):
                raise ValueError("released")

            released_at = data["released_at"]
            if released_at is not None and not self._is_snapshot_int(released_at):
                raise ValueError("released_at")
            if released and released_at is None:
                raise ValueError("released_at")

            first_join_tick = data["first_join_tick"]
            if first_join_tick is not None and not self._is_snapshot_int(first_join_tick):
                raise ValueError("first_join_tick")

            baseline_ticks_data = data.get("baseline_ticks", {})
            if not isinstance(baseline_ticks_data, dict):
                raise ValueError("baseline_ticks")
            baseline_ticks: dict[str, int | None] = {}
            for name, baseline_tick in baseline_ticks_data.items():
                if not isinstance(name, str) or (
                    baseline_tick is not None
                    and not self._is_snapshot_int(baseline_tick)
                ):
                    raise ValueError("baseline_ticks")
                baseline_ticks[name] = baseline_tick

            grace_sequence = data["grace_sequence"]
            if not self._is_snapshot_int(grace_sequence) or grace_sequence < 0:
                raise ValueError("grace_sequence")

            hosted = list(data["hosted"])
            short_handed = list(data["short_handed"])
            if not all(isinstance(name, str) for name in hosted + short_handed):
                raise ValueError("name lists")

            stats_data = data["stats"]
            if not isinstance(stats_data, dict):
                raise ValueError("stats")
            stats = {}
            for key in _STAT_KEYS:
                value = stats_data[key]
                if not self._is_snapshot_int(value) or value < 0:
                    raise ValueError(f"stats.{key}")
                stats[key] = value

            players_data = data["players"]
            if not isinstance(players_data, dict):
                raise ValueError("players")

            players: dict[str, dict[str, Any]] = {}
            for name, source in players_data.items():
                if not isinstance(name, str) or not name.strip():
                    raise ValueError("player name")
                if not isinstance(source, dict):
                    raise ValueError("player record")

                slot = source["slot"]
                if not self._is_snapshot_int(slot) or not 0 <= slot < max_players:
                    raise ValueError("slot")

                stage = source["stage"]
                if stage is not None and stage not in _STAGE_ORDER:
                    raise ValueError("stage")

                ready = source["ready"]
                if not isinstance(ready, bool):
                    raise ValueError("ready")

                player_state = source["state"]
                if player_state not in _PLAYER_STATES:
                    raise ValueError("state")

                players[name] = {
                    "slot": slot,
                    "stage": stage,
                    "ready": ready,
                    "state": player_state,
                }

            graces_data = data["graces"]
            if not isinstance(graces_data, dict):
                raise ValueError("graces")

            graces: dict[str, dict[str, int]] = {}
            for name, grace in graces_data.items():
                if name not in players or not isinstance(grace, dict):
                    raise ValueError("grace")
                slot = grace["slot"]
                deadline = grace["deadline"]
                sequence = grace["seq"]
                if not self._is_snapshot_int(slot) or not 0 <= slot < max_players:
                    raise ValueError("grace slot")
                if not self._is_snapshot_int(deadline) or deadline < 0:
                    raise ValueError("grace deadline")
                if not self._is_snapshot_int(sequence) or sequence < 0:
                    raise ValueError("grace seq")
                if players[name]["state"] != "grace":
                    raise ValueError("grace state")
                graces[name] = {
                    "slot": slot,
                    "deadline": deadline,
                    "seq": sequence,
                }

            for name in hosted:
                if name not in players:
                    raise ValueError("hosted player")
            for name in short_handed:
                if name not in players or players[name]["state"] != "absent":
                    raise ValueError("short-handed player")

            return {
                "threshold": threshold,
                "timeout_ticks": timeout_ticks,
                "grace_ticks": grace_ticks,
                "max_players": max_players,
                "released": released,
                "released_at": released_at,
                "first_join_tick": first_join_tick,
                "baseline_ticks": baseline_ticks,
                "hosted": hosted,
                "short_handed": short_handed,
                "grace_sequence": grace_sequence,
                "graces": graces,
                "players": players,
                "stats": stats,
            }
        except MatchBarrierError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise MatchBarrierError("MATCH_SNAPSHOT_INVALID") from exc

    @staticmethod
    def _is_snapshot_int(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _snapshot_non_negative_int(cls, value: Any) -> int:
        if not cls._is_snapshot_int(value) or value < 0:
            raise ValueError("non-negative integer required")
        return value
