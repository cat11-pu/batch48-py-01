"""Pause referee with quotas, quorum voting and timeout recovery."""

from __future__ import annotations

import copy
import threading
import time


STATS_KEYS = (
    "teams",
    "proposals",
    "votes",
    "pauses",
    "resumes",
    "auto_resumes",
    "forced_resumes",
    "substitutions",
    "duplicates",
    "rejected",
)

PAUSE_STATES = ("running", "paused")
SNAPSHOT_VERSION = 1


class PauseError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value: object) -> bool:
    return _is_int(value) and value > 0


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _empty_stats() -> dict[str, int]:
    return {key: 0 for key in STATS_KEYS}


class PauseReferee:
    """Referees ranked-match pause requests for both teams."""

    def __init__(self, *, quorum: int = 2, quota_per_match: int = 2,
                 max_duration_ms: int = 60000, max_players: int = 8,
                 clock=None) -> None:
        self.quorum = int(quorum)
        self.quota_per_match = int(quota_per_match)
        self.max_duration_ms = int(max_duration_ms)
        self.max_players = int(max_players)
        self._clock = clock
        self._lock = threading.RLock()

        self._teams: dict[str, list[str]] = {}
        self._quota: dict[str, int] = {}
        self._proposals: dict[str, dict] = {}
        self._proposal_of_team: dict[str, str] = {}
        self._queue: list[dict[str, str]] = []
        self._seq = 0
        self._state = "running"
        self._active_id: str | None = None
        self._last_event = "initialized"
        self._stats = _empty_stats()
        self._time_seen = False
        self._sticky_now = 0

    def register_team(self, team: str, players) -> dict:
        with self._lock:
            if (not isinstance(team, str) or not team.strip()
                    or isinstance(players, (str, bytes))
                    or not isinstance(players, (list, tuple))):
                self._reject("PAUSE_INVALID_TEAM", {"team": team})

            roster: list[str] = []
            seen: set[str] = set()
            for player in players:
                if (not isinstance(player, str) or not player.strip()
                        or player in seen):
                    self._reject("PAUSE_INVALID_TEAM", {"team": team})
                seen.add(player)
                roster.append(player)

            if not roster:
                self._reject("PAUSE_INVALID_TEAM", {"team": team})
            if len(roster) > self.max_players:
                self._reject("PAUSE_TEAM_LIMIT", {
                    "team": team,
                    "players": len(roster),
                    "max_players": self.max_players,
                })

            if team in self._teams:
                if self._teams[team] != roster:
                    self._reject("PAUSE_TEAM_CONFLICT", {"team": team})
                return {
                    "team": team,
                    "players": list(roster),
                    "status": "duplicate",
                }

            self._teams[team] = roster
            self._quota[team] = self.quota_per_match
            self._stats["teams"] += 1
            return {
                "team": team,
                "players": list(roster),
                "status": "registered",
            }

    def roster(self, team: str) -> list:
        with self._lock:
            self._require_team(team)
            return list(self._teams[team])

    def quota_of(self, team: str) -> int:
        with self._lock:
            self._require_team(team)
            return self._quota[team]

    def request_pause(self, team: str, *, player: str,
                      duration: int | None = None, now=None) -> dict:
        with self._lock:
            current_now = self._current_now(now)
            self._expire_due(current_now)
            self._require_team(team)
            roster = self._teams[team]

            if not isinstance(player, str) or player not in roster:
                self._reject("PAUSE_UNKNOWN_PLAYER", {
                    "team": team,
                    "player": player,
                })

            if duration is None:
                duration = self.max_duration_ms
            if not _is_positive_int(duration):
                self._reject("PAUSE_INVALID_DURATION", {"duration": duration})
            if duration > self.max_duration_ms:
                self._reject("PAUSE_DURATION_TOO_LONG", {
                    "duration": duration,
                    "max_duration_ms": self.max_duration_ms,
                })

            if self._state == "paused":
                self._reject("PAUSE_ALREADY_PAUSED", {
                    "proposal_id": self._active_id,
                })

            existing_id = self._proposal_of_team.get(team)
            if existing_id is not None:
                existing = self._proposals.get(existing_id)
                if existing is not None and existing.get("status") == "pending":
                    self._stats["duplicates"] += 1
                    return self._request_view(existing, "pending_duplicate")
                self._proposal_of_team.pop(team, None)

            if self._quota[team] <= 0:
                self._reject("PAUSE_QUOTA_EXHAUSTED", {"team": team})

            self._seq += 1
            proposal_id = f"pause-{self._seq}"
            proposal = {
                "proposal_id": proposal_id,
                "team": team,
                "initiator": player,
                "status": "pending",
                "votes": [player],
                "quorum": self.quorum,
                "duration": duration,
                "created_at": current_now,
                "activated_at": None,
                "ends_at": None,
                "closed_at": None,
                "close_reason": None,
            }
            self._proposals[proposal_id] = proposal
            self._proposal_of_team[team] = proposal_id
            self._stats["proposals"] += 1
            self._stats["votes"] += 1

            if len(proposal["votes"]) >= proposal["quorum"]:
                self._activate(proposal, current_now)
                return self._vote_view(proposal, "paused")
            return self._request_view(proposal, "pending")

    def vote_pause(self, proposal_id: str, *, player: str, now=None) -> dict:
        with self._lock:
            current_now = self._current_now(now)
            self._expire_due(current_now)

            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                self._reject("PAUSE_UNKNOWN_PROPOSAL", {
                    "proposal_id": proposal_id,
                })

            team = proposal["team"]
            if (not isinstance(player, str)
                    or team not in self._teams
                    or player not in self._teams[team]):
                self._reject("PAUSE_VOTE_NOT_ALLOWED", {
                    "proposal_id": proposal_id,
                    "player": player,
                })

            if proposal["status"] == "closed":
                self._reject("PAUSE_PROPOSAL_CLOSED", {
                    "proposal_id": proposal_id,
                })

            if player in proposal["votes"]:
                return self._vote_view(proposal, "duplicate_vote")

            if proposal["status"] == "active":
                return self._vote_view(proposal, "already_active")

            proposal["votes"].append(player)
            self._stats["votes"] += 1

            if self._state == "paused" and self._active_id != proposal_id:
                self._close_proposal(proposal, current_now, "superseded")
                self._reject("PAUSE_PROPOSAL_CLOSED", {
                    "proposal_id": proposal_id,
                })

            if len(proposal["votes"]) >= proposal["quorum"]:
                self._activate(proposal, current_now)
                return self._vote_view(proposal, "paused")

            return self._vote_view(proposal, "pending")

    def resume(self, proposal_id: str, *, player: str | None = None,
               now=None) -> dict:
        with self._lock:
            current_now = self._current_now(now)
            self._expire_due(current_now)

            proposal = self._proposals.get(proposal_id)
            if proposal is None:
                self._reject("PAUSE_UNKNOWN_PROPOSAL", {
                    "proposal_id": proposal_id,
                })

            if (self._state != "paused"
                    or self._active_id != proposal_id
                    or proposal.get("status") != "active"):
                self._reject("PAUSE_NOT_PAUSED", {
                    "proposal_id": proposal_id,
                })

            team = proposal["team"]
            self._state = "running"
            self._active_id = None
            self._last_event = "resumed"
            self._close_proposal(proposal, current_now, "resumed")
            proposal["resumed_by"] = player
            self._stats["resumes"] += 1
            self._process_queue(current_now)

            return {
                "proposal_id": proposal_id,
                "team": team,
                "status": "resumed",
                "state": "running",
                "quota": self._quota[team],
            }

    def force_resume(self, *, reason: str = "", now=None) -> dict:
        with self._lock:
            current_now = self._current_now(now)
            self._expire_due(current_now)

            if self._state != "paused" or self._active_id is None:
                self._reject("PAUSE_NOT_PAUSED")

            proposal_id = self._active_id
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.get("status") != "active":
                self._state = "running"
                self._active_id = None
                self._reject("PAUSE_NOT_PAUSED", {
                    "proposal_id": proposal_id,
                })

            team = proposal["team"]
            self._state = "running"
            self._active_id = None
            self._last_event = "forced_resumed"
            proposal["votes"] = []
            proposal["force_reason"] = reason
            self._close_proposal(proposal, current_now, "forced")
            self._stats["forced_resumes"] += 1
            self._process_queue(current_now)

            return {
                "proposal_id": proposal_id,
                "team": team,
                "status": "forced",
                "state": "running",
                "quota": self._quota[team],
            }

    def request_substitution(self, team: str, *, out_player: str,
                             in_player: str, now=None) -> dict:
        with self._lock:
            current_now = self._current_now(now)
            self._expire_due(current_now)
            self._require_team(team)
            roster = self._teams[team]

            if not isinstance(out_player, str) or out_player not in roster:
                self._reject("PAUSE_UNKNOWN_PLAYER", {
                    "team": team,
                    "player": out_player,
                })
            if not isinstance(in_player, str) or not in_player.strip():
                self._reject("PAUSE_INVALID_PLAYER", {
                    "team": team,
                    "player": in_player,
                })
            if in_player in roster:
                self._reject("PAUSE_ROSTER_CONFLICT", {
                    "team": team,
                    "player": in_player,
                })

            if self._state == "paused":
                team_queued = sum(
                    item["team"] == team for item in self._queue
                )
                if team_queued >= self.max_players:
                    self._reject("PAUSE_SUBSTITUTION_QUEUE_FULL", {
                        "team": team,
                        "queued": team_queued,
                        "limit": self.max_players,
                    })

                self._queue.append({
                    "team": team,
                    "out_player": out_player,
                    "in_player": in_player,
                })
                return {
                    "team": team,
                    "status": "queued",
                    "roster": list(roster),
                    "queue": len(self._queue),
                }

            index = roster.index(out_player)
            roster[index] = in_player
            self._stats["substitutions"] += 1
            return {
                "team": team,
                "status": "applied",
                "roster": list(roster),
                "queue": len(self._queue),
            }

    def state(self, *, now=None) -> dict:
        with self._lock:
            current_now = self._current_now(now)
            self._expire_due(current_now)

            if self._state == "paused" and self._active_id is not None:
                proposal = self._proposals.get(self._active_id)
                if proposal is not None:
                    ends_at = proposal.get("ends_at")
                    remaining = 0
                    if _is_number(ends_at):
                        remaining = max(0, ends_at - current_now)
                    return {
                        "state": "paused",
                        "frozen": True,
                        "proposal_id": self._active_id,
                        "team": proposal["team"],
                        "ends_at": ends_at,
                        "remaining_ms": remaining,
                        "last_event": self._last_event,
                    }

            return {
                "state": "running",
                "frozen": False,
                "proposal_id": None,
                "team": None,
                "ends_at": None,
                "remaining_ms": 0,
                "last_event": self._last_event,
            }

    def proposal_of(self, team: str) -> dict | None:
        with self._lock:
            proposal_id = self._proposal_of_team.get(team)
            if proposal_id is None:
                return None
            proposal = self._proposals.get(proposal_id)
            if proposal is None or proposal.get("status") == "closed":
                return None
            return {
                "proposal_id": proposal["proposal_id"],
                "team": proposal["team"],
                "status": proposal["status"],
                "votes": list(proposal["votes"]),
                "quorum": proposal["quorum"],
                "ends_at": proposal.get("ends_at"),
            }

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = _empty_stats()

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                "version": SNAPSHOT_VERSION,
                "teams": self._teams,
                "proposals": self._proposals,
                "proposal_of_team": self._proposal_of_team,
                "quota": self._quota,
                "stats": self._stats,
                "queue": self._queue,
                "seq": self._seq,
                "state": self._state,
                "active": self._active_id,
            }
            return copy.deepcopy(data)

    def restore(self, state) -> None:
        with self._lock:
            parsed = self._parse_snapshot(state)
            (teams, quota, proposals, proposal_of_team, queue, seq,
             match_state, active_id, stats) = parsed
            self._teams = teams
            self._quota = quota
            self._proposals = proposals
            self._proposal_of_team = proposal_of_team
            self._queue = queue
            self._seq = seq
            self._state = match_state
            self._active_id = active_id
            self._stats = stats
            self._last_event = "paused" if match_state == "paused" else "restored"
            self._time_seen = True
            active_proposal = proposals.get(active_id) if active_id else None
            self._sticky_now = (
                active_proposal.get("activated_at")
                if active_proposal is not None
                and active_proposal.get("activated_at") is not None
                else 0
            )

    def _require_team(self, team: object) -> None:
        if not isinstance(team, str) or team not in self._teams:
            self._reject("PAUSE_UNKNOWN_TEAM", {"team": team})

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise PauseError(code, detail)

    def _current_now(self, now=None):
        if now is not None:
            self._sticky_now = now
            self._time_seen = True
            return now

        if self._clock is not None:
            clock_value = (
                self._clock() if callable(self._clock) else self._clock
            )
            if clock_value is not None:
                self._sticky_now = clock_value
                self._time_seen = True
                return clock_value

        if self._time_seen:
            return self._sticky_now

        return time.monotonic_ns() // 1_000_000

    def _request_view(self, proposal: dict, status: str) -> dict:
        return {
            "proposal_id": proposal["proposal_id"],
            "team": proposal["team"],
            "status": status,
            "votes": len(proposal["votes"]),
            "quorum": proposal["quorum"],
            "duration": proposal["duration"],
            "quota": self._quota[proposal["team"]],
        }

    def _vote_view(self, proposal: dict, status: str) -> dict:
        return {
            "proposal_id": proposal["proposal_id"],
            "team": proposal["team"],
            "status": status,
            "votes": len(proposal["votes"]),
            "quorum": proposal["quorum"],
            "ends_at": proposal.get("ends_at"),
        }

    def _activate(self, proposal: dict, now) -> None:
        proposal["status"] = "active"
        proposal["activated_at"] = now
        proposal["ends_at"] = now + proposal["duration"]
        self._state = "paused"
        self._active_id = proposal["proposal_id"]
        self._last_event = "paused"
        self._quota[proposal["team"]] -= 1
        self._stats["pauses"] += 1

        active_id = proposal["proposal_id"]
        for other in self._proposals.values():
            if (other.get("proposal_id") != active_id
                    and other.get("status") == "pending"):
                self._close_proposal(other, now, "superseded")
                other_team = other.get("team")
                index_id = self._proposal_of_team.get(other_team)
                if index_id == other["proposal_id"]:
                    del self._proposal_of_team[other_team]

    def _close_proposal(self, proposal: dict, now, reason: str) -> None:
        proposal["status"] = "closed"
        proposal["closed_at"] = now
        proposal["close_reason"] = reason
        team = proposal.get("team")
        proposal_id = proposal.get("proposal_id")
        if self._proposal_of_team.get(team) == proposal_id:
            del self._proposal_of_team[team]

    def _expire_due(self, now) -> bool:
        if self._state != "paused" or self._active_id is None:
            return False

        proposal = self._proposals.get(self._active_id)
        ends_at = proposal.get("ends_at") if proposal is not None else None
        if proposal is None or not _is_number(ends_at) or now < ends_at:
            return False

        proposal_id = proposal["proposal_id"]
        self._state = "running"
        self._active_id = None
        self._last_event = "auto_resumed"
        self._close_proposal(proposal, now, "timeout")
        self._stats["auto_resumes"] += 1
        self._process_queue(now)
        return True

    def _process_queue(self, now) -> None:
        queued = self._queue
        self._queue = []

        for item in queued:
            team = item.get("team")
            out_player = item.get("out_player")
            in_player = item.get("in_player")
            roster = self._teams.get(team)
            if roster is None:
                continue
            try:
                index = roster.index(out_player)
            except ValueError:
                continue
            if in_player in roster:
                continue

            roster[index] = in_player
            self._stats["substitutions"] += 1

    def _parse_snapshot(self, data: object):
        if not isinstance(data, dict):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "snapshot must be object")
        version = data.get("version")
        if version != SNAPSHOT_VERSION or isinstance(version, bool):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "unsupported version")

        required = (
            "teams", "proposals", "proposal_of_team", "quota", "stats",
            "queue", "seq", "state", "active",
        )
        if any(key not in data for key in required):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "missing field")

        teams = self._parse_teams(data["teams"])
        quota = self._parse_quota(data["quota"], teams)
        stats = self._parse_stats(data["stats"])
        proposals = self._parse_proposals(data["proposals"], teams)
        proposal_of_team = self._parse_proposal_index(
            data["proposal_of_team"], teams, proposals
        )
        queue = self._parse_queue(data["queue"], teams)

        seq = data["seq"]
        if not _is_int(seq) or seq < 0:
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid seq")

        match_state = data["state"]
        if match_state not in PAUSE_STATES:
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid state")

        active_id = data["active"]
        active_proposal_count = sum(
            proposal.get("status") == "active"
            for proposal in proposals.values()
        )
        if match_state == "paused":
            if not isinstance(active_id, str):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "missing active")
            active = proposals.get(active_id)
            if active is None or active.get("status") != "active":
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "active mismatch")
            if proposal_of_team.get(active["team"]) != active_id:
                raise PauseError(
                    "PAUSE_SNAPSHOT_INVALID", "active index mismatch"
                )
            if not _is_number(active.get("ends_at")):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "active deadline")
            if active_proposal_count != 1:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "active count")
        else:
            if active_id is not None or active_proposal_count != 0:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "running active")
            if queue:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "running queue")

        for team, proposal_id in proposal_of_team.items():
            proposal = proposals[proposal_id]
            if proposal.get("team") != team:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "index team mismatch")
            if proposal.get("status") not in ("pending", "active"):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "index closed")

        for proposal_id, proposal in proposals.items():
            if proposal.get("status") == "pending":
                team = proposal["team"]
                if proposal_of_team.get(team) != proposal_id:
                    raise PauseError("PAUSE_SNAPSHOT_INVALID", "pending index")

        return (
            copy.deepcopy(teams),
            copy.deepcopy(quota),
            copy.deepcopy(proposals),
            copy.deepcopy(proposal_of_team),
            copy.deepcopy(queue),
            seq,
            match_state,
            active_id if match_state == "paused" else None,
            copy.deepcopy(stats),
        )

    def _parse_teams(self, raw: object) -> dict[str, list[str]]:
        if not isinstance(raw, dict):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "teams must be object")

        teams: dict[str, list[str]] = {}
        for team, players in raw.items():
            if not isinstance(team, str) or not team.strip():
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid team")
            if not isinstance(players, list):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid roster")

            roster: list[str] = []
            seen: set[str] = set()
            for player in players:
                if (not isinstance(player, str) or not player.strip()
                        or player in seen):
                    raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid player")
                roster.append(player)
                seen.add(player)

            if not roster or len(roster) > self.max_players:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "roster limit")
            teams[team] = roster
        return teams

    def _parse_quota(self, raw: object,
                    teams: dict[str, list[str]]) -> dict[str, int]:
        if not isinstance(raw, dict) or set(raw) != set(teams):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "quota mismatch")

        quota: dict[str, int] = {}
        for team, value in raw.items():
            if not _is_int(value) or value < 0:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid quota")
            quota[team] = value
        return quota

    def _parse_stats(self, raw: object) -> dict[str, int]:
        if not isinstance(raw, dict):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "stats must be object")

        stats = _empty_stats()
        for key in STATS_KEYS:
            value = raw.get(key)
            if not _is_int(value) or value < 0:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", f"stat {key}")
            stats[key] = value
        return stats

    def _parse_proposals(self, raw: object,
                         teams: dict[str, list[str]]) -> dict[str, dict]:
        if not isinstance(raw, dict):
            raise PauseError(
                "PAUSE_SNAPSHOT_INVALID", "proposals must be object"
            )

        proposals: dict[str, dict] = {}
        for proposal_id, source in raw.items():
            if not isinstance(proposal_id, str) or not proposal_id:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid proposal id")
            if not isinstance(source, dict):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid proposal")

            proposal = copy.deepcopy(source)
            team = proposal.get("team")
            if not isinstance(team, str) or team not in teams:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "proposal team")
            if proposal.get("proposal_id") != proposal_id:
                proposal["proposal_id"] = proposal_id

            status = proposal.get("status")
            if status not in ("pending", "active", "closed"):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "proposal status")

            votes_raw = proposal.get("votes")
            if not isinstance(votes_raw, list):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "votes must be list")
            votes: list[str] = []
            seen: set[str] = set()
            for voter in votes_raw:
                if not isinstance(voter, str) or voter in seen:
                    raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid vote")
                votes.append(voter)
                seen.add(voter)
            proposal["votes"] = votes

            quorum = proposal.get("quorum", self.quorum)
            if not _is_int(quorum) or quorum < 0:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid quorum")
            proposal["quorum"] = quorum

            duration = proposal.get("duration", self.max_duration_ms)
            if not _is_positive_int(duration):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid duration")
            proposal["duration"] = duration

            ends_at = proposal.get("ends_at")
            if ends_at is not None and not _is_number(ends_at):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid deadline")

            if status == "pending":
                proposal["ends_at"] = None
            proposals[proposal_id] = proposal
        return proposals

    def _parse_proposal_index(
        self, raw: object, teams: dict[str, list[str]],
        proposals: dict[str, dict]
    ) -> dict[str, str]:
        if not isinstance(raw, dict):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "index must be object")

        index: dict[str, str] = {}
        for team, proposal_id in raw.items():
            if team not in teams or not isinstance(proposal_id, str):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid index entry")
            proposal = proposals.get(proposal_id)
            if proposal is None or proposal.get("team") != team:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "index proposal")
            index[team] = proposal_id
        return index

    def _parse_queue(self, raw: object,
                     teams: dict[str, list[str]]) -> list[dict[str, str]]:
        if not isinstance(raw, list):
            raise PauseError("PAUSE_SNAPSHOT_INVALID", "queue must be list")

        queue: list[dict[str, str]] = []
        counts: dict[str, int] = {}
        for item in raw:
            if not isinstance(item, dict):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "invalid queue item")
            team = item.get("team")
            out_player = item.get("out_player")
            in_player = item.get("in_player")
            roster = teams.get(team) if isinstance(team, str) else None
            if roster is None:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "queue team")
            if (not isinstance(out_player, str)
                    or out_player not in roster
                    or not isinstance(in_player, str)
                    or not in_player.strip()
                    or in_player in roster):
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "queue player")

            counts[team] = counts.get(team, 0) + 1
            if counts[team] > self.max_players:
                raise PauseError("PAUSE_SNAPSHOT_INVALID", "queue limit")
            queue.append({
                "team": team,
                "out_player": out_player,
                "in_player": in_player,
            })
        return queue
