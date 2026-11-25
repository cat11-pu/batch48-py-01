"""Dungeon instance slot allocation with party binding and reset anchors."""

from __future__ import annotations

import threading
import time


_OPEN = "open"
_RECLAIMING = "reclaiming"
_CLOSED = "closed"


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value) -> bool:
    return _is_int(value) and value > 0


def _current_ms() -> int:
    return time.time_ns() // 1_000_000


class InstanceError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class InstanceSlotAllocator:
    """Allocate dungeon instances, bind parties and reclaim slots."""

    def __init__(self, *, reclaim_cooldown_ms: int = 60000,
                 reentry_cooldown_ms: int = 30000) -> None:
        if (not _is_int(reclaim_cooldown_ms) or reclaim_cooldown_ms < 0
                or not _is_int(reentry_cooldown_ms)
                or reentry_cooldown_ms < 0):
            raise ValueError("cooldowns must be non-negative integers")
        self.reclaim_cooldown_ms = reclaim_cooldown_ms
        self.reentry_cooldown_ms = reentry_cooldown_ms
        self._lock = threading.RLock()
        self._dungeons: dict[str, dict] = {}
        self._instances: dict[str, dict] = {}
        self._active_by_party: dict[frozenset, str] = {}
        self._party_cooldowns: dict[frozenset, int] = {}
        self._player_cooldowns: dict[object, int] = {}
        self._next_seq: dict[str, int] = {}
        self._stats = {
            "acquired": 0,
            "entered": 0,
            "left": 0,
            "reclaimed": 0,
        }

    @staticmethod
    def _now(now=None) -> int:
        if now is None:
            return _current_ms()
        if not _is_int(now):
            raise InstanceError("INSTANCE_TIME_INVALID", now)
        return now

    @staticmethod
    def _party(party) -> list:
        if isinstance(party, (str, bytes)):
            raise InstanceError("INSTANCE_PARTY_INVALID", party)
        try:
            members = list(party)
        except TypeError:
            raise InstanceError("INSTANCE_PARTY_INVALID", party) from None
        if not members:
            raise InstanceError("INSTANCE_PARTY_INVALID", party)
        try:
            if len(set(members)) != len(members):
                raise InstanceError("INSTANCE_PARTY_INVALID", party)
        except TypeError:
            raise InstanceError("INSTANCE_PARTY_INVALID", party) from None
        return members

    @staticmethod
    def _party_key(party) -> frozenset:
        return frozenset(party)

    def _instance(self, instance_id) -> dict:
        instance = self._instances.get(instance_id)
        if instance is None:
            raise InstanceError("INSTANCE_UNKNOWN_INSTANCE", instance_id)
        return instance

    @staticmethod
    def _instance_view(instance: dict) -> dict:
        return {
            "instance_id": instance["instance_id"],
            "dungeon_id": instance["dungeon_id"],
            "party": list(instance["party"]),
            "capacity": instance["capacity"],
            "members": list(instance["members"]),
            "state": instance["state"],
            "created_at": instance["created_at"],
            "reset_at": instance["reset_at"],
        }

    @staticmethod
    def _reset_at(anchor_ms: int, reset_every_ms: int, now: int) -> int:
        return anchor_ms + ((now - anchor_ms) // reset_every_ms + 1) * reset_every_ms

    def _active_count(self, dungeon_id: str) -> int:
        return sum(
            1
            for instance in self._instances.values()
            if instance["dungeon_id"] == dungeon_id
            and instance["state"] in (_OPEN, _RECLAIMING)
        )

    def _close(self, instance: dict, cooldown_until: int) -> None:
        if instance["state"] == _CLOSED:
            return
        key = self._party_key(instance["party"])
        instance["state"] = _CLOSED
        instance["reclaim_at"] = None
        if self._active_by_party.get(key) == instance["instance_id"]:
            del self._active_by_party[key]
        if cooldown_until > self._party_cooldowns.get(key, 0):
            self._party_cooldowns[key] = cooldown_until
        self._stats["reclaimed"] += 1

    def RegisterDungeon(self, dungeon_id, *, capacity, max_slots,
                        reset_every_ms, anchor_ms=0):
        with self._lock:
            if not isinstance(dungeon_id, str) or not dungeon_id:
                raise InstanceError("INSTANCE_DUNGEON_INVALID", dungeon_id)
            if dungeon_id in self._dungeons:
                raise InstanceError("INSTANCE_DUNGEON_DEFINED", dungeon_id)
            if (not _positive_int(capacity) or not _positive_int(max_slots)
                    or not _positive_int(reset_every_ms)
                    or not _is_int(anchor_ms)):
                raise InstanceError(
                    "INSTANCE_DUNGEON_INVALID",
                    {
                        "capacity": capacity,
                        "max_slots": max_slots,
                        "reset_every_ms": reset_every_ms,
                        "anchor_ms": anchor_ms,
                    },
                )
            self._dungeons[dungeon_id] = {
                "capacity": capacity,
                "max_slots": max_slots,
                "reset_every_ms": reset_every_ms,
                "anchor_ms": anchor_ms,
            }
            self._next_seq[dungeon_id] = 1
            return {
                "dungeon_id": dungeon_id,
                "capacity": capacity,
                "max_slots": max_slots,
            }

    def Acquire(self, dungeon_id, party, now):
        with self._lock:
            config = self._dungeons.get(dungeon_id)
            if config is None:
                raise InstanceError("INSTANCE_UNKNOWN_DUNGEON", dungeon_id)
            party = self._party(party)
            if len(party) > config["capacity"]:
                raise InstanceError(
                    "INSTANCE_PARTY_TOO_LARGE",
                    {"party_size": len(party), "capacity": config["capacity"]},
                )
            now = self._now(now)
            key = self._party_key(party)

            current_id = self._active_by_party.get(key)
            if current_id is not None:
                current = self._instances[current_id]
                if current["state"] == _OPEN:
                    return self._instance_view(current)
                if current["state"] == _RECLAIMING:
                    reclaim_at = current["reclaim_at"]
                    if now < reclaim_at:
                        raise InstanceError(
                            "INSTANCE_COOLDOWN_ACTIVE",
                            {"remaining_ms": reclaim_at - now},
                        )
                    self._close(current, reclaim_at)

            cooldown_until = self._party_cooldowns.get(key, 0)
            if now < cooldown_until:
                raise InstanceError(
                    "INSTANCE_COOLDOWN_ACTIVE",
                    {"remaining_ms": cooldown_until - now},
                )
            if cooldown_until:
                del self._party_cooldowns[key]

            if self._active_count(dungeon_id) >= config["max_slots"]:
                raise InstanceError(
                    "INSTANCE_POOL_FULL",
                    {"dungeon_id": dungeon_id, "max_slots": config["max_slots"]},
                )

            seq = self._next_seq.get(dungeon_id, 1)
            instance_id = f"{dungeon_id}#{seq}"
            self._next_seq[dungeon_id] = seq + 1
            instance = {
                "instance_id": instance_id,
                "dungeon_id": dungeon_id,
                "party": party,
                "capacity": config["capacity"],
                "members": [],
                "state": _OPEN,
                "created_at": now,
                "reset_at": self._reset_at(
                    config["anchor_ms"], config["reset_every_ms"], now
                ),
                "reclaim_at": None,
                "seq": seq,
            }
            self._instances[instance_id] = instance
            self._active_by_party[key] = instance_id
            self._stats["acquired"] += 1
            return self._instance_view(instance)

    def Enter(self, instance_id, player, now):
        with self._lock:
            instance = self._instance(instance_id)
            now = self._now(now)
            if instance["state"] != _OPEN:
                raise InstanceError("INSTANCE_CLOSED", instance_id)
            if len(instance["members"]) >= instance["capacity"]:
                raise InstanceError(
                    "INSTANCE_FULL",
                    {"instance_id": instance_id, "capacity": instance["capacity"]},
                )
            if player not in instance["party"]:
                raise InstanceError("INSTANCE_FOREIGN_PLAYER", player)
            if player in instance["members"]:
                return {
                    "instance_id": instance_id,
                    "player": player,
                    "members": list(instance["members"]),
                    "capacity": instance["capacity"],
                }
            cooldown_until = self._player_cooldowns.get(player, 0)
            if now < cooldown_until:
                raise InstanceError(
                    "INSTANCE_REENTRY_COOLDOWN",
                    {"remaining_ms": cooldown_until - now},
                )
            if cooldown_until:
                del self._player_cooldowns[player]
            instance["members"].append(player)
            self._stats["entered"] += 1
            return {
                "instance_id": instance_id,
                "player": player,
                "members": list(instance["members"]),
                "capacity": instance["capacity"],
            }

    def Leave(self, instance_id, player, now=None):
        with self._lock:
            instance = self._instance(instance_id)
            now = self._now(now)
            if instance["state"] != _OPEN:
                raise InstanceError("INSTANCE_CLOSED", instance_id)
            if player not in instance["members"]:
                raise InstanceError("INSTANCE_NOT_MEMBER", player)
            instance["members"].remove(player)
            self._player_cooldowns[player] = now + self.reentry_cooldown_ms
            self._stats["left"] += 1
            if not instance["members"]:
                instance["state"] = _RECLAIMING
                instance["reclaim_at"] = now + self.reclaim_cooldown_ms
            return {
                "instance_id": instance_id,
                "player": player,
                "members": list(instance["members"]),
                "state": instance["state"],
            }

    def Reset(self, instance_id):
        with self._lock:
            instance = self._instance(instance_id)
            if instance["state"] == _CLOSED:
                raise InstanceError("INSTANCE_CLOSED", instance_id)
            instance["members"] = []
            instance["state"] = _OPEN
            instance["reclaim_at"] = None
            return self._instance_view(instance)

    def Sweep(self, now):
        with self._lock:
            now = self._now(now)
            closed = []
            for instance_id, instance in self._instances.items():
                if instance["state"] == _OPEN:
                    if now >= instance["reset_at"]:
                        self._close(
                            instance, now + self.reclaim_cooldown_ms
                        )
                        closed.append(instance_id)
                elif instance["state"] == _RECLAIMING:
                    if (now >= instance["reclaim_at"]
                            or now >= instance["reset_at"]):
                        self._close(instance, instance["reclaim_at"])
                        closed.append(instance_id)
            return sorted(closed)

    def State(self, instance_id):
        with self._lock:
            return self._instance_view(self._instance(instance_id))

    def Cooldown(self, party, now=None):
        with self._lock:
            now = self._now(now)
            if isinstance(party, (str, bytes)):
                return 0
            try:
                members = list(party)
            except TypeError:
                return 0
            if not members:
                return 0

            remaining = 0
            for player in members:
                try:
                    remaining = max(
                        remaining, self._player_cooldowns.get(player, 0) - now
                    )
                except TypeError:
                    continue
            try:
                key = self._party_key(members)
            except TypeError:
                return max(0, remaining)
            remaining = max(
                remaining, self._party_cooldowns.get(key, 0) - now
            )
            current_id = self._active_by_party.get(key)
            if current_id is not None:
                current = self._instances[current_id]
                if current["state"] == _RECLAIMING:
                    remaining = max(remaining, current["reclaim_at"] - now)
            return max(0, remaining)

    def Snapshot(self):
        with self._lock:
            instances = {}
            for instance_id, instance in self._instances.items():
                instances[instance_id] = {
                    "instance_id": instance["instance_id"],
                    "dungeon_id": instance["dungeon_id"],
                    "party": list(instance["party"]),
                    "capacity": instance["capacity"],
                    "members": list(instance["members"]),
                    "state": instance["state"],
                    "created_at": instance["created_at"],
                    "reset_at": instance["reset_at"],
                    "reclaim_at": instance["reclaim_at"],
                    "seq": instance["seq"],
                }
            bindings = [
                {
                    "party": list(self._instances[instance_id]["party"]),
                    "instance_id": instance_id,
                }
                for instance_id in sorted(self._active_by_party.values())
            ]
            party_cooldowns = [
                {"party": list(key), "until": until}
                for key, until in self._party_cooldowns.items()
            ]
            player_cooldowns = [
                {"player": player, "until": until}
                for player, until in self._player_cooldowns.items()
            ]
            return {
                "version": 1,
                "reclaim_cooldown_ms": self.reclaim_cooldown_ms,
                "reentry_cooldown_ms": self.reentry_cooldown_ms,
                "dungeons": {
                    dungeon_id: dict(config)
                    for dungeon_id, config in self._dungeons.items()
                },
                "next_seq": dict(self._next_seq),
                "instances": instances,
                "bindings": bindings,
                "party_cooldowns": party_cooldowns,
                "player_cooldowns": player_cooldowns,
                "stats": dict(self._stats),
            }

    def Restore(self, state):
        try:
            restored = self._validated_restore(state)
        except InstanceError:
            raise
        except Exception as exc:
            raise InstanceError("INSTANCE_SNAPSHOT_INVALID", str(exc)) from None
        with self._lock:
            (self.reclaim_cooldown_ms, self.reentry_cooldown_ms,
             self._dungeons, self._next_seq, self._instances,
             self._active_by_party, self._party_cooldowns,
             self._player_cooldowns, self._stats) = restored

    def Stats(self):
        with self._lock:
            return dict(self._stats)

    def _validated_restore(self, state):
        invalid = "INSTANCE_SNAPSHOT_INVALID"
        if not isinstance(state, dict) or state.get("version") != 1:
            raise InstanceError(invalid, "missing or unsupported version")

        reclaim_ms = state.get("reclaim_cooldown_ms")
        reentry_ms = state.get("reentry_cooldown_ms")
        if (not _is_int(reclaim_ms) or reclaim_ms < 0
                or not _is_int(reentry_ms) or reentry_ms < 0):
            raise InstanceError(invalid, "invalid cooldown configuration")

        raw_dungeons = state.get("dungeons")
        if not isinstance(raw_dungeons, dict):
            raise InstanceError(invalid, "dungeons must be a mapping")
        dungeons = {}
        for dungeon_id, raw in raw_dungeons.items():
            if not isinstance(dungeon_id, str) or not dungeon_id:
                raise InstanceError(invalid, "invalid dungeon id")
            if not isinstance(raw, dict):
                raise InstanceError(invalid, "invalid dungeon record")
            capacity = raw.get("capacity")
            max_slots = raw.get("max_slots")
            reset_every_ms = raw.get("reset_every_ms")
            anchor_ms = raw.get("anchor_ms")
            if (not _positive_int(capacity) or not _positive_int(max_slots)
                    or not _positive_int(reset_every_ms)
                    or not _is_int(anchor_ms)):
                raise InstanceError(invalid, "invalid dungeon configuration")
            dungeons[dungeon_id] = {
                "capacity": capacity,
                "max_slots": max_slots,
                "reset_every_ms": reset_every_ms,
                "anchor_ms": anchor_ms,
            }

        raw_next_seq = state.get("next_seq")
        if not isinstance(raw_next_seq, dict):
            raise InstanceError(invalid, "next_seq must be a mapping")
        next_seq = {}
        for dungeon_id, seq in raw_next_seq.items():
            if dungeon_id not in dungeons or not _positive_int(seq):
                raise InstanceError(invalid, "invalid next sequence")
            next_seq[dungeon_id] = seq

        raw_instances = state.get("instances")
        if not isinstance(raw_instances, dict):
            raise InstanceError(invalid, "instances must be a mapping")
        instances = {}
        active_by_party = {}
        max_seen = {dungeon_id: 0 for dungeon_id in dungeons}
        for instance_id, raw in raw_instances.items():
            if not isinstance(instance_id, str) or not isinstance(raw, dict):
                raise InstanceError(invalid, "invalid instance record")
            dungeon_id = raw.get("dungeon_id")
            if dungeon_id not in dungeons:
                raise InstanceError(invalid, "instance has unknown dungeon")
            capacity = raw.get("capacity")
            if not _positive_int(capacity) or capacity != dungeons[dungeon_id]["capacity"]:
                raise InstanceError(invalid, "invalid instance capacity")
            party = raw.get("party")
            if not isinstance(party, list):
                raise InstanceError(invalid, "invalid instance party")
            try:
                if (not party or len(set(party)) != len(party)
                        or len(party) > capacity):
                    raise InstanceError(invalid, "invalid instance party")
            except TypeError:
                raise InstanceError(invalid, "invalid instance party") from None
            members = raw.get("members")
            if not isinstance(members, list):
                raise InstanceError(invalid, "invalid instance members")
            try:
                if (len(set(members)) != len(members)
                        or any(member not in party for member in members)):
                    raise InstanceError(invalid, "invalid instance members")
            except TypeError:
                raise InstanceError(invalid, "invalid instance members") from None
            instance_state = raw.get("state")
            if instance_state not in (_OPEN, _RECLAIMING, _CLOSED):
                raise InstanceError(invalid, "invalid instance state")
            created_at = raw.get("created_at")
            reset_at = raw.get("reset_at")
            reclaim_at = raw.get("reclaim_at")
            seq = raw.get("seq")
            if (not _is_int(created_at) or not _is_int(reset_at)
                    or not _positive_int(seq)
                    or (reclaim_at is not None and not _is_int(reclaim_at))):
                raise InstanceError(invalid, "invalid instance timestamps")
            if instance_id != f"{dungeon_id}#{seq}":
                raise InstanceError(invalid, "instance id does not match sequence")
            if instance_state == _RECLAIMING and reclaim_at is None:
                raise InstanceError(invalid, "reclaiming instance needs deadline")
            if instance_state != _RECLAIMING and reclaim_at is not None:
                raise InstanceError(invalid, "unexpected reclaim deadline")
            if instance_state == _CLOSED and members:
                raise InstanceError(invalid, "closed instance has members")

            instance = {
                "instance_id": instance_id,
                "dungeon_id": dungeon_id,
                "party": list(party),
                "capacity": capacity,
                "members": list(members),
                "state": instance_state,
                "created_at": created_at,
                "reset_at": reset_at,
                "reclaim_at": reclaim_at,
                "seq": seq,
            }
            instances[instance_id] = instance
            max_seen[dungeon_id] = max(max_seen[dungeon_id], seq)
            if instance_state != _CLOSED:
                key = self._party_key(party)
                if key in active_by_party:
                    raise InstanceError(invalid, "party bound to multiple instances")
                active_by_party[key] = instance_id

        for dungeon_id, seq in next_seq.items():
            if seq <= max_seen[dungeon_id]:
                raise InstanceError(invalid, "next sequence is not monotonic")
        for dungeon_id in dungeons:
            if dungeon_id not in next_seq:
                next_seq[dungeon_id] = max_seen[dungeon_id] + 1

        raw_bindings = state.get("bindings")
        if not isinstance(raw_bindings, list):
            raise InstanceError(invalid, "bindings must be a list")
        snapshot_bindings = {}
        for raw in raw_bindings:
            if not isinstance(raw, dict):
                raise InstanceError(invalid, "invalid binding record")
            instance_id = raw.get("instance_id")
            party = raw.get("party")
            if instance_id not in instances or not isinstance(party, list):
                raise InstanceError(invalid, "invalid binding record")
            try:
                key = self._party_key(party)
            except TypeError:
                raise InstanceError(invalid, "invalid binding party") from None
            if key in snapshot_bindings:
                raise InstanceError(invalid, "duplicate binding")
            snapshot_bindings[key] = instance_id
        if snapshot_bindings != active_by_party:
            raise InstanceError(invalid, "binding set does not match instances")

        raw_party_cooldowns = state.get("party_cooldowns")
        if not isinstance(raw_party_cooldowns, list):
            raise InstanceError(invalid, "party_cooldowns must be a list")
        party_cooldowns = {}
        for raw in raw_party_cooldowns:
            if not isinstance(raw, dict):
                raise InstanceError(invalid, "invalid party cooldown")
            party = raw.get("party")
            until = raw.get("until")
            if not isinstance(party, list) or not party or not _is_int(until):
                raise InstanceError(invalid, "invalid party cooldown")
            try:
                key = self._party_key(party)
            except TypeError:
                raise InstanceError(invalid, "invalid party cooldown") from None
            party_cooldowns[key] = max(party_cooldowns.get(key, 0), until)

        raw_player_cooldowns = state.get("player_cooldowns")
        if not isinstance(raw_player_cooldowns, list):
            raise InstanceError(invalid, "player_cooldowns must be a list")
        player_cooldowns = {}
        for raw in raw_player_cooldowns:
            if not isinstance(raw, dict):
                raise InstanceError(invalid, "invalid player cooldown")
            player = raw.get("player")
            until = raw.get("until")
            if not _is_int(until):
                raise InstanceError(invalid, "invalid player cooldown")
            try:
                hash(player)
            except TypeError:
                raise InstanceError(invalid, "invalid player cooldown") from None
            player_cooldowns[player] = max(
                player_cooldowns.get(player, 0), until
            )

        raw_stats = state.get("stats")
        if not isinstance(raw_stats, dict):
            raise InstanceError(invalid, "stats must be a mapping")
        stats = {}
        for name in ("acquired", "entered", "left", "reclaimed"):
            value = raw_stats.get(name)
            if not _is_int(value) or value < 0:
                raise InstanceError(invalid, "invalid stats")
            stats[name] = value

        return (
            reclaim_ms,
            reentry_ms,
            dungeons,
            next_seq,
            instances,
            active_by_party,
            party_cooldowns,
            player_cooldowns,
            stats,
        )
