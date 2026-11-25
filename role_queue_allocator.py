"""Role preference queue allocation with autofill protection."""

from __future__ import annotations

import copy
import threading


STATS_KEYS = (
    "queued",
    "cancelled",
    "teams_formed",
    "autofilled",
    "swapped",
    "changed",
    "locked",
    "departed",
    "rejected",
)

DEFAULT_ROLES = ("tank", "healer", "dps")
DEFAULT_QUOTAS = {"tank": 1, "healer": 1, "dps": 3}
ASSIGNMENT_KINDS = {"primary", "secondary", "autofill", "changed"}
SNAPSHOT_VERSION = 1


class RoleQueueError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _strict_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class RoleQueueAllocator:
    """Hold role preference tickets and assemble quota bound teams."""

    def __init__(self, *, roles=DEFAULT_ROLES, quotas=None, team_size: int = 5,
                 queue_limit: int = 200, protection_cap: int = 5, clock=None) -> None:
        self.roles = tuple(roles)
        self.team_size = team_size
        self.queue_limit = queue_limit
        self.protection_cap = protection_cap
        self._clock = clock
        self._lock = threading.RLock()
        self.quotas = self._normalize_quotas(
            DEFAULT_QUOTAS if quotas is None else quotas
        )
        if self.quotas is None:
            raise RoleQueueError("ROLE_INVALID_QUOTA", quotas)

        self._seq = 0
        self._queue: list[dict] = []
        self._queued_players: set[str] = set()
        self._teams: dict[str, dict] = {}
        self._protection: dict[str, dict] = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    def enqueue(self, player: str, *, primary: str,
                secondary: str | None = None, at: int = 0) -> dict:
        with self._lock:
            if not self._valid_player(player):
                self._reject("ROLE_INVALID_PLAYER", player)
            if not isinstance(primary, str) or primary not in self.roles:
                self._reject("ROLE_INVALID_ROLE", primary)
            if secondary is not None:
                if not isinstance(secondary, str) or secondary not in self.roles:
                    self._reject("ROLE_INVALID_ROLE", secondary)
                if secondary == primary:
                    self._reject("ROLE_PREFERENCE_INVALID", (primary, secondary))
            if not _strict_int(at) or at < 0:
                self._reject("ROLE_INVALID_TIME", at)
            if player in self._queued_players:
                self._reject("ROLE_ALREADY_QUEUED", player)
            if len(self._queue) >= self.queue_limit:
                self._reject("ROLE_QUEUE_FULL", self.queue_limit)

            self._seq += 1
            ticket = {
                "player": player,
                "primary": primary,
                "secondary": secondary,
                "at": at,
                "seq": self._seq,
            }
            self._queue.append(ticket)
            self._queued_players.add(player)
            self._stats["queued"] += 1

            result = copy.deepcopy(ticket)
            result["position"] = len(self._queue)
            return result

    def cancel(self, player: str) -> dict:
        with self._lock:
            if not self._valid_player(player):
                self._reject("ROLE_INVALID_PLAYER", player)
            if player not in self._queued_players:
                self._reject("ROLE_NOT_QUEUED", player)

            index = next(
                index for index, ticket in enumerate(self._queue)
                if ticket["player"] == player
            )
            ticket = self._queue.pop(index)
            self._queued_players.remove(player)
            self._stats["cancelled"] += 1
            return copy.deepcopy(ticket)

    def queue_state(self, now: int = 0) -> list:
        with self._lock:
            rows = []
            for position, ticket in enumerate(self._queue, start=1):
                row = copy.deepcopy(ticket)
                row["position"] = position
                row["wait"] = now - ticket["at"]
                rows.append(row)
            return rows

    def autofill_protection(self, player: str) -> dict:
        with self._lock:
            if not self._valid_player(player):
                self._reject("ROLE_INVALID_PLAYER", player)

            record = self._protection.get(player)
            if record is None:
                return {"player": player, "autofills": 0, "protection": 0}
            return {
                "player": player,
                "autofills": record["autofills"],
                "protection": record["protection"],
            }

    def form_team(self, team_id: str, *, quotas=None) -> dict:
        with self._lock:
            if not self._valid_team_id(team_id):
                self._reject("ROLE_INVALID_TEAM", team_id)
            if team_id in self._teams:
                self._reject("ROLE_TEAM_EXISTS", team_id)

            effective_quotas = self._normalize_quotas(quotas)
            if effective_quotas is None:
                self._reject("ROLE_INVALID_QUOTA", quotas)
            if len(self._queue) < self.team_size:
                self._reject("ROLE_NOT_ENOUGH_PLAYERS", len(self._queue))

            slots, assignment, autofilled = self._build_team_plan(effective_quotas)
            selected = set(assignment)
            team = {
                "team_id": team_id,
                "slots": slots,
                "assignment": assignment,
                "autofilled": autofilled,
                "quotas": effective_quotas,
                "locked": False,
                "size": sum(len(members) for members in slots.values()),
            }

            self._queue = [
                ticket for ticket in self._queue
                if ticket["player"] not in selected
            ]
            self._queued_players = {
                ticket["player"] for ticket in self._queue
            }
            self._teams[team_id] = team

            for player in autofilled:
                self._add_autofill_protection(player)

            self._stats["teams_formed"] += 1
            self._stats["autofilled"] += len(autofilled)
            return copy.deepcopy(team)

    def team_state(self, team_id: str) -> dict:
        with self._lock:
            team = self._get_team(team_id)
            return copy.deepcopy(team)

    def swap_role(self, team_id: str, player_a: str, player_b: str) -> dict:
        with self._lock:
            team = self._get_team(team_id)
            if team["locked"]:
                self._reject("ROLE_TEAM_LOCKED", team_id)
            if player_a == player_b:
                self._reject("ROLE_SAME_PLAYER", (player_a, player_b))

            role_a = self._member_role(team, player_a)
            if role_a is None:
                self._reject("ROLE_UNKNOWN_MEMBER", player_a)
            role_b = self._member_role(team, player_b)
            if role_b is None:
                self._reject("ROLE_UNKNOWN_MEMBER", player_b)
            if role_a == role_b:
                self._reject("ROLE_SAME_ROLE", (player_a, role_a))

            slots = team["slots"]
            index_a = slots[role_a].index(player_a)
            index_b = slots[role_b].index(player_b)
            slots[role_a][index_a] = player_b
            slots[role_b][index_b] = player_a

            self._stats["swapped"] += 1
            return {
                "team_id": team_id,
                "player_a": player_a,
                "role_a": role_b,
                "player_b": player_b,
                "role_b": role_a,
            }

    def change_role(self, team_id: str, player: str, new_role: str) -> dict:
        with self._lock:
            team = self._get_team(team_id)
            if team["locked"]:
                self._reject("ROLE_TEAM_LOCKED", team_id)

            old_role = self._member_role(team, player)
            if old_role is None:
                self._reject("ROLE_UNKNOWN_MEMBER", player)
            if not isinstance(new_role, str) or new_role not in self.roles:
                self._reject("ROLE_INVALID_ROLE", new_role)
            if len(team["slots"][new_role]) >= team["quotas"][new_role]:
                self._reject(
                    "ROLE_QUOTA_EXCEEDED",
                    {"role": new_role, "quota": team["quotas"][new_role]},
                )

            team["slots"][old_role].remove(player)
            team["slots"][new_role].append(player)
            team["assignment"][player] = "changed"
            self._stats["changed"] += 1

            return {
                "team_id": team_id,
                "player": player,
                "role": new_role,
                "from": old_role,
            }

    def leave_team(self, team_id: str, player: str) -> dict:
        with self._lock:
            team = self._get_team(team_id)
            if team["locked"]:
                self._reject("ROLE_TEAM_LOCKED", team_id)

            role = self._member_role(team, player)
            if role is None:
                self._reject("ROLE_UNKNOWN_MEMBER", player)

            team["slots"][role].remove(player)
            team["assignment"].pop(player, None)
            if player in team["autofilled"]:
                team["autofilled"].remove(player)
            team["size"] -= 1
            self._stats["departed"] += 1
            return {"team_id": team_id, "player": player, "role": role}

    def lock_team(self, team_id: str) -> dict:
        with self._lock:
            team = self._get_team(team_id)
            if team["locked"]:
                self._reject("ROLE_ALREADY_LOCKED", team_id)

            team["locked"] = True
            self._stats["locked"] += 1
            return {"team_id": team_id, "locked": True, "size": team["size"]}

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": SNAPSHOT_VERSION,
                "seq": self._seq,
                "queue": self._queue,
                "teams": self._teams,
                "protection": self._protection,
                "stats": self._stats,
            })

    def restore(self, state) -> None:
        with self._lock:
            try:
                copied_state = copy.deepcopy(state)
                restored = self._parse_snapshot(copied_state)
            except RoleQueueError:
                raise
            except Exception as exc:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID") from exc

            self._seq = restored["seq"]
            self._queue = restored["queue"]
            self._queued_players = {
                ticket["player"] for ticket in restored["queue"]
            }
            self._teams = restored["teams"]
            self._protection = restored["protection"]
            self._stats = restored["stats"]

    def _build_team_plan(self, quotas: dict) -> tuple[dict, dict, list]:
        slots = {role: [] for role in self.roles}
        assignment = {}
        autofilled = []

        def assign(ticket: dict, role: str, kind: str) -> None:
            player = ticket["player"]
            slots[role].append(player)
            assignment[player] = kind
            if kind == "autofill":
                autofilled.append(player)

        for ticket in self._queue:
            role = ticket["primary"]
            if len(slots[role]) < quotas[role]:
                assign(ticket, role, "primary")

        for ticket in self._queue:
            player = ticket["player"]
            role = ticket["secondary"]
            if player not in assignment and role is not None:
                if len(slots[role]) < quotas[role]:
                    assign(ticket, role, "secondary")

        candidates = [
            ticket for ticket in self._queue
            if ticket["player"] not in assignment
        ]
        candidate_index = 0
        for role in self.roles:
            while len(slots[role]) < quotas[role]:
                ticket = candidates[candidate_index]
                candidate_index += 1
                assign(ticket, role, "autofill")

        return slots, assignment, autofilled

    def _add_autofill_protection(self, player: str) -> None:
        record = self._protection.setdefault(
            player, {"autofills": 0, "protection": 0}
        )
        record["autofills"] += 1
        record["protection"] = min(
            self.protection_cap, record["protection"] + 1
        )

    def _normalize_quotas(self, quotas) -> dict | None:
        if quotas is None:
            raw_quotas = self.quotas
        else:
            raw_quotas = quotas

        if not isinstance(raw_quotas, dict):
            return None
        if set(raw_quotas) != set(self.roles):
            return None

        normalized = {}
        for role in self.roles:
            value = raw_quotas[role]
            if not _strict_int(value) or value <= 0:
                return None
            normalized[role] = value

        if sum(normalized.values()) != self.team_size:
            return None
        return normalized

    def _get_team(self, team_id: str) -> dict:
        if not self._valid_team_id(team_id):
            self._reject("ROLE_INVALID_TEAM", team_id)
        team = self._teams.get(team_id)
        if team is None:
            self._reject("ROLE_UNKNOWN_TEAM", team_id)
        return team

    @staticmethod
    def _member_role(team: dict, player: str) -> str | None:
        for role, members in team["slots"].items():
            if player in members:
                return role
        return None

    @staticmethod
    def _valid_player(player: object) -> bool:
        return isinstance(player, str) and player != ""

    @staticmethod
    def _valid_team_id(team_id: object) -> bool:
        return isinstance(team_id, str) and team_id != ""

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise RoleQueueError(code, detail)

    def _parse_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "snapshot must be an object")
        if state.get("version") != SNAPSHOT_VERSION:
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "unsupported version")

        seq = state.get("seq")
        if not _strict_int(seq) or seq < 0:
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid seq")

        queue = self._parse_snapshot_queue(state.get("queue"), seq)
        teams = self._parse_snapshot_teams(state.get("teams"))
        protection = self._parse_snapshot_protection(state.get("protection"))
        stats = self._parse_snapshot_stats(state.get("stats"))

        return {
            "seq": seq,
            "queue": queue,
            "teams": teams,
            "protection": protection,
            "stats": stats,
        }

    def _parse_snapshot_queue(self, raw_queue: object, seq: int) -> list[dict]:
        if not isinstance(raw_queue, list):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "queue must be a list")

        queue = []
        seen_players = set()
        max_seq = 0
        for raw_ticket in raw_queue:
            if not isinstance(raw_ticket, dict):
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "ticket must be an object")
            if set(raw_ticket) != {"player", "primary", "secondary", "at", "seq"}:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid ticket fields")

            player = raw_ticket["player"]
            primary = raw_ticket["primary"]
            secondary = raw_ticket["secondary"]
            at = raw_ticket["at"]
            ticket_seq = raw_ticket["seq"]

            if not self._valid_player(player) or player in seen_players:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid ticket player")
            if not isinstance(primary, str) or primary not in self.roles:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid primary role")
            if secondary is not None:
                if not isinstance(secondary, str) or secondary not in self.roles:
                    raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid secondary role")
                if secondary == primary:
                    raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "preferences match")
            if not _strict_int(at) or at < 0:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid ticket time")
            if not _strict_int(ticket_seq) or ticket_seq <= 0:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid ticket seq")

            seen_players.add(player)
            max_seq = max(max_seq, ticket_seq)
            queue.append({
                "player": player,
                "primary": primary,
                "secondary": secondary,
                "at": at,
                "seq": ticket_seq,
            })

        if seq < max_seq:
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "seq is behind tickets")
        return queue

    def _parse_snapshot_protection(self, raw_protection: object) -> dict:
        if not isinstance(raw_protection, dict):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "protection must be an object")

        protection = {}
        for player, raw_record in raw_protection.items():
            if not self._valid_player(player):
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid protection player")
            if not isinstance(raw_record, dict):
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid protection record")
            if set(raw_record) != {"autofills", "protection"}:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid protection fields")

            autofills = raw_record["autofills"]
            value = raw_record["protection"]
            if not _strict_int(autofills) or autofills < 0:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid autofill count")
            if not _strict_int(value) or value < 0 or value > self.protection_cap:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid protection weight")

            protection[player] = {
                "autofills": autofills,
                "protection": value,
            }
        return protection

    def _parse_snapshot_stats(self, raw_stats: object) -> dict:
        if not isinstance(raw_stats, dict):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "stats must be an object")
        if set(raw_stats) != set(STATS_KEYS):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid stats fields")

        stats = {}
        for key in STATS_KEYS:
            value = raw_stats[key]
            if not _strict_int(value) or value < 0:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", f"invalid stat {key}")
            stats[key] = value
        return stats

    def _parse_snapshot_teams(self, raw_teams: object) -> dict:
        if not isinstance(raw_teams, dict):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "teams must be an object")

        teams = {}
        for team_id, raw_team in raw_teams.items():
            team = self._parse_snapshot_team(team_id, raw_team)
            teams[team_id] = team
        return teams

    def _parse_snapshot_team(self, team_id: object, raw_team: object) -> dict:
        if not self._valid_team_id(team_id):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid team id")
        if not isinstance(raw_team, dict):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "team must be an object")
        if set(raw_team) != {
            "team_id", "slots", "assignment", "autofilled",
            "quotas", "locked", "size",
        }:
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid team fields")
        if raw_team["team_id"] != team_id:
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "team id mismatch")
        if not isinstance(raw_team["locked"], bool):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid locked flag")

        quotas = self._normalize_quotas(raw_team["quotas"])
        if quotas is None:
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid team quotas")

        raw_slots = raw_team["slots"]
        if not isinstance(raw_slots, dict) or set(raw_slots) != set(self.roles):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid team slots")

        slots = {role: [] for role in self.roles}
        members = []
        for role in self.roles:
            raw_members = raw_slots[role]
            if not isinstance(raw_members, list):
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid role members")
            for player in raw_members:
                if not self._valid_player(player):
                    raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid team member")
                if player in members:
                    raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "duplicate team member")
                slots[role].append(player)
                members.append(player)
            if len(slots[role]) > quotas[role]:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "role quota exceeded")

        member_set = set(members)
        raw_assignment = raw_team["assignment"]
        if not isinstance(raw_assignment, dict):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid assignment")
        if set(raw_assignment) != member_set:
            raise RoleQueueError(
                "ROLE_SNAPSHOT_INVALID", "assignment does not match members"
            )
        assignment = {}
        for player, kind in raw_assignment.items():
            if player not in member_set or kind not in ASSIGNMENT_KINDS:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid assignment kind")
            assignment[player] = kind

        raw_autofilled = raw_team["autofilled"]
        if not isinstance(raw_autofilled, list):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid autofilled list")
        if len(raw_autofilled) != len(set(raw_autofilled)):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "duplicate autofilled member")
        for player in raw_autofilled:
            if player not in member_set:
                raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "autofilled player missing")

        size = raw_team["size"]
        if not _strict_int(size) or size != len(members):
            raise RoleQueueError("ROLE_SNAPSHOT_INVALID", "invalid team size")

        return {
            "team_id": team_id,
            "slots": slots,
            "assignment": assignment,
            "autofilled": list(raw_autofilled),
            "quotas": quotas,
            "locked": raw_team["locked"],
            "size": size,
        }
