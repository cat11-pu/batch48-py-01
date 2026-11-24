"""Ready check coordination between a successful match and match start."""

from __future__ import annotations

import copy
import threading

STATS_KEYS = (
    "checks_opened",
    "tickets_issued",
    "accepts",
    "declines",
    "timeouts",
    "closed",
    "rejected",
)

_OPEN = "open"
_RESOLVED = "resolved"
_CLOSED = "closed"

_ACCEPT = "accept"
_DECLINE = "decline"

_REASON_PLAYER = "player"
_REASON_TIMEOUT = "timeout"
_REASON_DISCONNECT = "disconnect"


class ReadyCheckError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReadyCheckCoordinator:
    """Issue ready tickets, collect answers, time out and requeue players."""

    def __init__(self, *, ticket_limit: int = 64, player_limit: int = 512,
                 mode_windows=None, default_window: int = 5,
                 penalty_ticks: int = 3, clock=None) -> None:
        self.ticket_limit = ticket_limit
        self.player_limit = player_limit
        self.mode_windows = {} if mode_windows is None else dict(mode_windows)
        self.default_window = default_window
        self.penalty_ticks = penalty_ticks
        self._clock = clock
        self._lock = threading.RLock()

        self._checks: dict[str, dict] = {}
        self._check_order: list[str] = []
        self._player_index: dict[str, str] = {}
        self._penalties: dict[str, dict] = {}
        self._stats: dict[str, int] = {key: 0 for key in STATS_KEYS}

    # ------------------------------------------------------------------
    # Internal helpers

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise ReadyCheckError(code, detail)

    def _window_for_locked(self, mode: str) -> int:
        if not isinstance(mode, str) or not mode:
            self._reject("READY_INVALID_MODE", {"mode": mode})
        return self.mode_windows.get(mode, self.default_window)

    @staticmethod
    def _positive_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _non_negative_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    def _require_check_locked(self, check_id: str) -> dict:
        check = self._checks.get(check_id)
        if check is None:
            self._reject("READY_UNKNOWN_CHECK", {"check_id": check_id})
        return check

    def _ticket_row(self, ticket: dict) -> dict:
        return {
            "player": ticket["player"],
            "team": copy.deepcopy(ticket["team"]),
            "answer": ticket["answer"],
            "reason": ticket["reason"],
            "at": ticket["at"],
        }

    def _check_status_locked(self, check: dict) -> dict:
        accepted = 0
        declined = 0
        pending = 0
        rows = []
        for player in check["order"]:
            ticket = check["tickets"][player]
            answer = ticket["answer"]
            if answer == _ACCEPT:
                accepted += 1
            elif answer == _DECLINE:
                declined += 1
            else:
                pending += 1
            rows.append(self._ticket_row(ticket))
        return {
            "check_id": check["check_id"],
            "mode": check["mode"],
            "window": check["window"],
            "state": check["state"],
            "elapsed": check["elapsed"],
            "verdict": check["verdict"],
            "pending": pending,
            "accepted": accepted,
            "declined": declined,
            "tickets": rows,
        }

    def _theoretical_verdict_locked(self, check: dict) -> str:
        for player in check["order"]:
            if check["tickets"][player]["answer"] != _ACCEPT:
                return "partial"
        return "ready"

    def _resolve_if_complete_locked(self, check: dict) -> None:
        if check["state"] != _OPEN:
            return
        for player in check["order"]:
            if check["tickets"][player]["answer"] is None:
                return
        check["state"] = _RESOLVED
        check["verdict"] = self._theoretical_verdict_locked(check)

    def _mark_pending_timeouts_locked(self, check: dict, at: int) -> int:
        marked = 0
        for player in check["order"]:
            ticket = check["tickets"][player]
            if ticket["answer"] is None:
                ticket["answer"] = _DECLINE
                ticket["reason"] = _REASON_TIMEOUT
                ticket["at"] = at
                marked += 1
        if marked:
            self._stats["timeouts"] += marked
        return marked

    def _prepare_players_locked(self, players) -> list[dict]:
        try:
            raw_players = list(players)
        except TypeError:
            self._reject("READY_INVALID_PLAYER", {"players": players})

        prepared: list[dict] = []
        seen: set[str] = set()
        for entry in raw_players:
            if not isinstance(entry, dict):
                self._reject("READY_INVALID_PLAYER", {"player": entry})
            player = entry.get("player")
            team = entry.get("team")
            if not isinstance(player, str) or not player:
                self._reject("READY_INVALID_PLAYER", {"player": player})
            if team is None:
                self._reject("READY_INVALID_PLAYER", {"player": player})
            if player in seen:
                self._reject("READY_DUPLICATE_PLAYER", {"player": player})
            seen.add(player)
            prepared.append({"player": player, "team": copy.deepcopy(team)})
        return prepared

    # ------------------------------------------------------------------
    # Public API

    def window_for(self, mode: str) -> int:
        with self._lock:
            return self._window_for_locked(mode)

    def open_check(self, check_id: str, *, mode: str, players) -> dict:
        with self._lock:
            window = self._window_for_locked(mode)
            if check_id in self._checks:
                self._reject("READY_CHECK_EXISTS", {"check_id": check_id})

            prepared = self._prepare_players_locked(players)
            for row in prepared:
                if row["player"] in self._player_index:
                    self._reject("READY_PLAYER_BUSY", {"player": row["player"]})

            if len(prepared) > self.ticket_limit:
                self._reject(
                    "READY_TICKET_CAPACITY",
                    {"limit": self.ticket_limit, "requested": len(prepared)},
                )
            if len(self._player_index) + len(prepared) > self.player_limit:
                self._reject(
                    "READY_PLAYER_CAPACITY",
                    {"limit": self.player_limit, "requested": len(prepared)},
                )

            tickets = {}
            for row in prepared:
                tickets[row["player"]] = {
                    "player": row["player"],
                    "team": row["team"],
                    "answer": None,
                    "reason": None,
                    "at": None,
                    "disconnected": False,
                }

            check = {
                "check_id": check_id,
                "mode": mode,
                "window": window,
                "state": _OPEN,
                "elapsed": 0,
                "verdict": None,
                "order": [row["player"] for row in prepared],
                "tickets": tickets,
            }
            self._checks[check_id] = check
            self._check_order.append(check_id)
            for row in prepared:
                self._player_index[row["player"]] = check_id

            self._stats["checks_opened"] += 1
            self._stats["tickets_issued"] += len(prepared)
            return {
                "check_id": check_id,
                "mode": mode,
                "window": window,
                "state": _OPEN,
                "tickets": [
                    {"player": row["player"], "team": copy.deepcopy(row["team"])}
                    for row in prepared
                ],
            }

    def submit_ready(self, check_id: str, player: str, *, accept: bool,
                     tick: int = 0) -> dict:
        with self._lock:
            check = self._require_check_locked(check_id)
            if check["state"] == _CLOSED:
                self._reject("READY_CHECK_CLOSED", {"check_id": check_id})

            ticket = check["tickets"].get(player)
            if ticket is None:
                self._reject("READY_UNKNOWN_TICKET", {"player": player})
            if not isinstance(accept, bool):
                self._reject("READY_INVALID_ANSWER", {"accept": accept})
            if not self._non_negative_int(tick):
                self._reject("READY_INVALID_TICK", {"tick": tick})

            if ticket["answer"] is not None:
                return {
                    "status": "duplicate",
                    "player": player,
                    "answer": ticket["answer"],
                    "accept": ticket["answer"] == _ACCEPT,
                    "reason": ticket["reason"],
                }

            if tick > check["window"]:
                self._reject(
                    "READY_TOO_LATE",
                    {"tick": tick, "window": check["window"]},
                )

            answer = _ACCEPT if accept else _DECLINE
            ticket["answer"] = answer
            ticket["reason"] = _REASON_PLAYER
            ticket["at"] = tick
            if tick > check["elapsed"]:
                check["elapsed"] = tick

            if accept:
                self._stats["accepts"] += 1
            else:
                self._stats["declines"] += 1
            self._resolve_if_complete_locked(check)

            return {
                "status": "recorded",
                "player": player,
                "answer": answer,
                "team": ticket["team"],
                "tick": tick,
            }

    def tick(self, check_id: str, *, ticks: int = 1) -> dict:
        with self._lock:
            check = self._require_check_locked(check_id)
            if check["state"] != _OPEN:
                self._reject("READY_CHECK_CLOSED", {"check_id": check_id})
            if not self._positive_int(ticks):
                self._reject("READY_INVALID_TICK", {"ticks": ticks})

            check["elapsed"] = min(check["elapsed"] + ticks, check["window"])
            if check["elapsed"] >= check["window"]:
                self._mark_pending_timeouts_locked(check, check["elapsed"])
                check["state"] = _RESOLVED
                check["verdict"] = self._theoretical_verdict_locked(check)

            status = self._check_status_locked(check)
            return {
                "check_id": check_id,
                "elapsed": status["elapsed"],
                "state": status["state"],
                "verdict": status["verdict"],
                "pending": status["pending"],
            }

    def close_check(self, check_id: str) -> dict:
        with self._lock:
            check = self._require_check_locked(check_id)
            if check["state"] == _CLOSED:
                self._reject("READY_CHECK_CLOSED", {"check_id": check_id})

            if check["state"] == _OPEN:
                self._mark_pending_timeouts_locked(check, check["elapsed"])
                check["state"] = _RESOLVED
            if check["verdict"] is None:
                check["verdict"] = self._theoretical_verdict_locked(check)

            accepted_groups = []
            for player in check["order"]:
                if check["tickets"][player]["answer"] != _ACCEPT:
                    continue
                team = check["tickets"][player]["team"]
                for group_team, members in accepted_groups:
                    if group_team == team:
                        members.append(player)
                        break
                else:
                    accepted_groups.append((team, [player]))
            accepted = [
                player for _, members in accepted_groups for player in members
            ]
            declined = [
                player for player in check["order"]
                if check["tickets"][player]["answer"] == _DECLINE
            ]

            penalized = []
            for player in declined:
                penalty = self._penalties.setdefault(
                    player, {"declines": 0, "cooldown": 0}
                )
                penalty["declines"] += 1
                penalty["cooldown"] += self.penalty_ticks
                penalized.append({
                    "player": player,
                    "declines": penalty["declines"],
                    "cooldown": penalty["cooldown"],
                })

            for player in check["order"]:
                if self._player_index.get(player) == check_id:
                    del self._player_index[player]
            check["state"] = _CLOSED
            self._stats["closed"] += 1

            return {
                "check_id": check_id,
                "elapsed": check["elapsed"],
                "state": _CLOSED,
                "verdict": check["verdict"],
                "accepted": accepted,
                "declined": declined,
                "priority_requeue": list(accepted),
                "penalized": penalized,
            }

    def disconnect(self, check_id: str, player: str) -> dict:
        with self._lock:
            check = self._require_check_locked(check_id)
            if check["state"] == _CLOSED:
                self._reject("READY_CHECK_CLOSED", {"check_id": check_id})
            ticket = check["tickets"].get(player)
            if ticket is None:
                self._reject("READY_UNKNOWN_TICKET", {"player": player})

            ticket["disconnected"] = True
            if ticket["answer"] is None:
                ticket["answer"] = _DECLINE
                ticket["reason"] = _REASON_DISCONNECT
                ticket["at"] = check["elapsed"]
                self._resolve_if_complete_locked(check)

            return {
                "status": "disconnected",
                "player": player,
                "answer": ticket["answer"],
                "reason": ticket["reason"],
            }

    def status(self, check_id: str) -> dict:
        with self._lock:
            check = self._require_check_locked(check_id)
            return self._check_status_locked(check)

    def ticket_for(self, player: str) -> dict:
        with self._lock:
            check_id = self._player_index.get(player)
            if check_id is None:
                for candidate in reversed(self._check_order):
                    if player in self._checks[candidate]["tickets"]:
                        check_id = candidate
                        break
            if check_id is None:
                self._reject("READY_UNKNOWN_TICKET", {"player": player})
            ticket = self._checks[check_id]["tickets"][player]
            row = self._ticket_row(ticket)
            row["check_id"] = check_id
            return row

    def requeue_penalty(self, player: str) -> dict:
        with self._lock:
            penalty = self._penalties.get(player)
            if penalty is None:
                declines = 0
                cooldown = 0
            else:
                declines = penalty["declines"]
                cooldown = penalty["cooldown"]
            return {
                "player": player,
                "declines": declines,
                "cooldown": cooldown,
                "penalty_ticks": self.penalty_ticks,
            }

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STATS_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "checks": copy.deepcopy(self._checks),
                "check_order": list(self._check_order),
                "players": dict(self._player_index),
                "penalties": copy.deepcopy(self._penalties),
                "stats": dict(self._stats),
            }

    def restore(self, state) -> None:
        with self._lock:
            restored = self._validated_snapshot(state)
            self._checks = restored["checks"]
            self._check_order = restored["check_order"]
            self._player_index = restored["players"]
            self._penalties = restored["penalties"]
            self._stats = restored["stats"]

    # ------------------------------------------------------------------
    # Snapshot validation

    def _snapshot_invalid(self, detail: object = None) -> None:
        raise ReadyCheckError("READY_SNAPSHOT_INVALID", detail)

    def _validated_snapshot(self, state) -> dict:
        if not isinstance(state, dict):
            self._snapshot_invalid("state must be a mapping")
        if state.get("version") != 1:
            self._snapshot_invalid("unsupported version")

        raw_checks = state.get("checks")
        if not isinstance(raw_checks, dict):
            self._snapshot_invalid("checks must be a mapping")
        raw_order = state.get("check_order")
        if not isinstance(raw_order, list):
            self._snapshot_invalid("check_order must be a list")
        raw_players = state.get("players")
        if not isinstance(raw_players, dict):
            self._snapshot_invalid("players must be a mapping")
        raw_penalties = state.get("penalties")
        if not isinstance(raw_penalties, dict):
            self._snapshot_invalid("penalties must be a mapping")
        raw_stats = state.get("stats")
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(STATS_KEYS):
            self._snapshot_invalid("stats keys are invalid")
        if any(not self._non_negative_int(value) for value in raw_stats.values()):
            self._snapshot_invalid("stats values are invalid")

        if len(raw_order) != len(set(raw_order)):
            self._snapshot_invalid("check_order has duplicates")
        if set(raw_order) != set(raw_checks):
            self._snapshot_invalid("check_order does not match checks")

        checks: dict[str, dict] = {}
        index: dict[str, str] = {}
        for check_id in raw_order:
            raw = raw_checks.get(check_id)
            if not isinstance(raw, dict):
                self._snapshot_invalid("check must be a mapping")
            check = self._validated_check(check_id, raw)
            checks[check_id] = check
            if check["state"] != _CLOSED:
                for player in check["order"]:
                    if player in index:
                        self._snapshot_invalid("player appears in two checks")
                    index[player] = check_id

        if set(raw_players) != set(index):
            self._snapshot_invalid("players index does not match active checks")
        for player, check_id in raw_players.items():
            if not isinstance(player, str) or index.get(player) != check_id:
                self._snapshot_invalid("players index is invalid")

        penalties: dict[str, dict] = {}
        for player, penalty in raw_penalties.items():
            if not isinstance(player, str) or not isinstance(penalty, dict):
                self._snapshot_invalid("penalty is invalid")
            declines = penalty.get("declines")
            cooldown = penalty.get("cooldown")
            if (not self._non_negative_int(declines)
                    or not self._non_negative_int(cooldown)):
                self._snapshot_invalid("penalty value is invalid")
            penalties[player] = {
                "declines": declines,
                "cooldown": cooldown,
            }

        return {
            "checks": checks,
            "check_order": list(raw_order),
            "players": index,
            "penalties": penalties,
            "stats": {key: raw_stats[key] for key in STATS_KEYS},
        }

    def _validated_check(self, check_id: str, raw: dict) -> dict:
        mode = raw.get("mode")
        window = raw.get("window")
        state = raw.get("state")
        elapsed = raw.get("elapsed")
        verdict = raw.get("verdict")
        order = raw.get("order")
        raw_tickets = raw.get("tickets")

        if raw.get("check_id") != check_id:
            self._snapshot_invalid("check id mismatch")
        if not isinstance(mode, str) or not mode:
            self._snapshot_invalid("invalid mode")
        if not self._non_negative_int(window):
            self._snapshot_invalid("invalid window")
        if state not in (_OPEN, _RESOLVED, _CLOSED):
            self._snapshot_invalid("invalid state")
        if not self._non_negative_int(elapsed) or elapsed > window:
            self._snapshot_invalid("invalid elapsed")
        if verdict not in (None, "ready", "partial"):
            self._snapshot_invalid("invalid verdict")
        if state == _OPEN and verdict is not None:
            self._snapshot_invalid("open check cannot have a verdict")
        if state != _OPEN and verdict is None:
            self._snapshot_invalid("finished check needs a verdict")
        if not isinstance(order, list) or len(order) != len(set(order)):
            self._snapshot_invalid("invalid ticket order")
        if not isinstance(raw_tickets, dict) or set(order) != set(raw_tickets):
            self._snapshot_invalid("tickets do not match order")

        tickets = {}
        for player in order:
            raw_ticket = raw_tickets[player]
            if not isinstance(player, str) or not isinstance(raw_ticket, dict):
                self._snapshot_invalid("invalid ticket")
            ticket = self._validated_ticket(player, raw_ticket)
            tickets[player] = ticket

        if state == _OPEN:
            if tickets and all(
                ticket["answer"] is not None for ticket in tickets.values()
            ):
                self._snapshot_invalid("open check cannot be fully answered")
        if state == _RESOLVED:
            for ticket in tickets.values():
                if ticket["answer"] is None:
                    self._snapshot_invalid("resolved check has pending tickets")

        if state != _OPEN:
            computed = (
                "partial"
                if any(ticket["answer"] != _ACCEPT for ticket in tickets.values())
                else "ready"
            )
            if verdict != computed:
                self._snapshot_invalid("verdict does not match tickets")

        return {
            "check_id": check_id,
            "mode": mode,
            "window": window,
            "state": state,
            "elapsed": elapsed,
            "verdict": verdict,
            "order": list(order),
            "tickets": tickets,
        }

    def _validated_ticket(self, player: str, raw: dict) -> dict:
        if raw.get("player") != player:
            self._snapshot_invalid("ticket player mismatch")
        if "team" not in raw:
            self._snapshot_invalid("ticket team missing")
        team = raw.get("team")
        answer = raw.get("answer")
        reason = raw.get("reason")
        at = raw.get("at")
        disconnected = raw.get("disconnected", False)

        if answer not in (None, _ACCEPT, _DECLINE):
            self._snapshot_invalid("invalid ticket answer")
        if reason not in (
            None, _REASON_PLAYER, _REASON_TIMEOUT, _REASON_DISCONNECT
        ):
            self._snapshot_invalid("invalid ticket reason")
        if answer is None:
            if reason is not None or at is not None:
                self._snapshot_invalid("pending ticket has an answer state")
        else:
            if not self._non_negative_int(at):
                self._snapshot_invalid("answered ticket needs a tick")
            if reason is None:
                self._snapshot_invalid("answered ticket needs a reason")
            if answer == _ACCEPT and reason != _REASON_PLAYER:
                self._snapshot_invalid("accepted ticket has an invalid reason")
        if not isinstance(disconnected, bool):
            self._snapshot_invalid("invalid disconnect flag")

        return {
            "player": player,
            "team": copy.deepcopy(team),
            "answer": answer,
            "reason": reason,
            "at": at,
            "disconnected": disconnected,
        }
