"""Append-only game match result ledger."""

from __future__ import annotations

import copy
import math
import threading

STATS_KEYS = (
    "accepted",
    "duplicates",
    "conflicts",
    "amended",
    "voided",
    "entries",
    "rejected",
)

_SNAPSHOT_VERSION = 1

_INVALID_RESULT = "LEDGER_INVALID_RESULT"
_UNKNOWN_SEASON = "LEDGER_UNKNOWN_SEASON"
_UNKNOWN_MATCH = "LEDGER_UNKNOWN_MATCH"
_ENTRY_LIMIT = "LEDGER_ENTRY_LIMIT"
_SNAPSHOT_INVALID = "LEDGER_SNAPSHOT_INVALID"
_CONFLICT = "LEDGER_CONFLICT"


class MatchLedgerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_time(value: object):
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise MatchLedgerError(
            _INVALID_RESULT,
            {"field": "ended_at", "value": value},
        )
    return value


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


class MatchResultLedger:
    """Append-only settlement ledger keyed by match identifier."""

    def __init__(
        self,
        *,
        season_starts=None,
        base_rating: int = 1000,
        max_entries: int = 5000,
        clock=None,
    ) -> None:
        self.base_rating = base_rating
        self.max_entries = max_entries
        self._clock = clock
        self._lock = threading.RLock()
        self._season_starts = self._normalize_season_starts(season_starts)
        self._reset_state()

    def _reset_state(self) -> None:
        self._balances: dict[str, dict[str, int]] = {}
        self._matches: dict[str, dict] = {}
        self._season_matches: dict[str, int] = {}
        self._entries: list[dict] = []
        self._entry_owners: list[dict] = []
        self._player_seqs: dict[str, list[int]] = {}
        self._seq = 0
        self._stats = {key: 0 for key in STATS_KEYS}

    @staticmethod
    def _normalize_season_starts(season_starts):
        if season_starts is None:
            return []

        if isinstance(season_starts, dict):
            items = season_starts.items()
        elif isinstance(season_starts, (list, tuple)):
            items = []
            for item in season_starts:
                if isinstance(item, dict):
                    name = item.get("season", item.get("name"))
                    start = item.get("start", item.get("started_at"))
                    items.append((name, start))
                else:
                    name, start = item
                    items.append((name, start))
        else:
            raise MatchLedgerError(
                _INVALID_RESULT,
                {"field": "season_starts", "value": season_starts},
            )

        normalized = []
        for name, start in items:
            if not _nonempty_string(name):
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"field": "season_name", "value": name},
                )
            if (
                isinstance(start, bool)
                or not isinstance(start, (int, float))
                or not math.isfinite(start)
            ):
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"field": "season_start", "season": name, "value": start},
                )
            normalized.append((start, name))

        normalized.sort(key=lambda item: item[0])
        return normalized

    def _season_for_time(self, ended_at) -> str:
        season = None
        for start, season_name in self._season_starts:
            if start <= ended_at:
                season = season_name
            else:
                break
        if season is None:
            raise MatchLedgerError(_UNKNOWN_SEASON, {"ended_at": ended_at})
        return season

    def resolve_season(self, ended_at: int) -> str:
        with self._lock:
            valid_time = _valid_time(ended_at)
            return self._season_for_time(valid_time)

    @staticmethod
    def _normalize_results(results: dict) -> dict[str, dict]:
        if not isinstance(results, dict) or not results:
            raise MatchLedgerError(
                _INVALID_RESULT,
                {"field": "results", "reason": "empty_or_not_mapping"},
            )

        normalized: dict[str, dict] = {}
        for player, item in results.items():
            if not _nonempty_string(player):
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"field": "player", "value": player},
                )
            if not isinstance(item, dict):
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"player": player, "reason": "result_not_mapping"},
                )
            if "delta" not in item:
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"player": player, "field": "delta"},
                )

            delta = item["delta"]
            if not _is_int(delta):
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"player": player, "field": "delta", "value": delta},
                )
            if "win" not in item:
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {"player": player, "field": "win"},
                )

            normalized[player] = {"delta": delta, "win": item["win"]}

        return normalized

    @staticmethod
    def _fingerprint(ended_at, results: dict) -> dict:
        return {
            "ended_at": ended_at,
            "results": copy.deepcopy(results),
        }

    def _prepare_submission(self, match_id: str, ended_at: int, results: dict):
        if not _nonempty_string(match_id):
            raise MatchLedgerError(
                _INVALID_RESULT,
                {"field": "match_id", "value": match_id},
            )

        valid_ended_at = _valid_time(ended_at)
        normalized_results = self._normalize_results(results)
        season = self._season_for_time(valid_ended_at)
        return match_id, valid_ended_at, season, normalized_results

    @staticmethod
    def _unknown_match(match_id: object) -> MatchLedgerError:
        return MatchLedgerError(_UNKNOWN_MATCH, {"match_id": match_id})

    def _live_match(self, match_id: object) -> dict:
        if not _nonempty_string(match_id):
            raise self._unknown_match(match_id)
        record = self._matches.get(match_id)
        if record is None or record.get("status") == "voided":
            raise self._unknown_match(match_id)
        return record

    def _append_plan(
        self,
        plan: list,
        *,
        seq: int,
        match_id: str,
        season: str,
        player: str,
        kind: str,
        delta: int,
        balance_after: int,
        applied_delta: int,
    ) -> None:
        entry = {
            "seq": seq,
            "match_id": match_id,
            "kind": kind,
            "delta": delta,
            "balance_after": balance_after,
        }
        owner = {
            "player": player,
            "season": season,
            "applied_delta": applied_delta,
        }
        plan.append((entry, owner))

    def _commit_plan(self, plan: list) -> list[int]:
        seqs: list[int] = []
        for entry, owner in plan:
            self._entries.append(entry)
            self._entry_owners.append(owner)
            self._player_seqs.setdefault(owner["player"], []).append(entry["seq"])
            seqs.append(entry["seq"])
        self._seq += len(plan)
        self._stats["entries"] += len(plan)
        return seqs

    def submit_result(self, match_id: str, *, ended_at: int, results: dict) -> dict:
        with self._lock:
            try:
                match_id, valid_ended_at, season, normalized = self._prepare_submission(
                    match_id,
                    ended_at,
                    results,
                )
            except MatchLedgerError:
                self._stats["rejected"] += 1
                raise

            fingerprint = self._fingerprint(valid_ended_at, normalized)
            existing = self._matches.get(match_id)
            if existing is not None:
                if existing.get("fingerprint") == fingerprint:
                    self._stats["duplicates"] += 1
                    response = copy.deepcopy(existing["first_response"])
                    response["status"] = "duplicate"
                    return response

                self._stats["conflicts"] += 1
                raise MatchLedgerError(_CONFLICT, {"match_id": match_id})

            players = list(normalized)
            player_count = len(players)
            if len(self._entries) + player_count > self.max_entries:
                self._stats["rejected"] += 1
                raise MatchLedgerError(
                    _ENTRY_LIMIT,
                    {
                        "match_id": match_id,
                        "requested": player_count,
                        "limit": self.max_entries,
                        "entries": len(self._entries),
                    },
                )

            working_balances = dict(self._balances.get(season, {}))
            ratings: dict[str, int] = {}
            plan: list = []
            active_seqs: dict[str, int] = {}

            for index, player in enumerate(players):
                delta = normalized[player]["delta"]
                before = working_balances.get(player, self.base_rating)
                balance_after = max(0, before + delta)
                applied_delta = balance_after - before
                working_balances[player] = balance_after
                ratings[player] = balance_after

                seq = self._seq + index + 1
                self._append_plan(
                    plan,
                    seq=seq,
                    match_id=match_id,
                    season=season,
                    player=player,
                    kind="settle",
                    delta=delta,
                    balance_after=balance_after,
                    applied_delta=applied_delta,
                )
                active_seqs[player] = seq

            seqs = self._commit_plan(plan)
            self._balances[season] = working_balances
            self._season_matches[season] = self._season_matches.get(season, 0) + 1

            response = {
                "match_id": match_id,
                "season": season,
                "status": "accepted",
                "players": players,
                "ratings": ratings,
                "entry_count": player_count,
                "revision": 1,
            }
            self._matches[match_id] = {
                "match_id": match_id,
                "season": season,
                "ended_at": valid_ended_at,
                "players": players,
                "results": copy.deepcopy(normalized),
                "deltas": {player: normalized[player]["delta"] for player in players},
                "status": "settled",
                "revision": 1,
                "amendment_count": 0,
                "entry_count": player_count,
                "entry_seqs": seqs,
                "active_seqs": active_seqs,
                "player_by_seq": {
                    str(entry["seq"]): owner["player"] for entry, owner in plan
                },
                "applied_by_seq": {
                    str(entry["seq"]): owner["applied_delta"]
                    for entry, owner in plan
                },
                "fingerprint": fingerprint,
                "first_response": copy.deepcopy(response),
                "first_ratings": copy.deepcopy(ratings),
                "first_entry_count": player_count,
                "amendments": [],
                "void": None,
            }

            self._stats["accepted"] += 1
            return response

    def amend_result(
        self,
        match_id: str,
        *,
        results: dict,
        reason: str = "",
    ) -> dict:
        with self._lock:
            try:
                record = self._live_match(match_id)
            except MatchLedgerError:
                self._stats["rejected"] += 1
                raise

            try:
                normalized = self._normalize_results(results)
            except MatchLedgerError:
                self._stats["rejected"] += 1
                raise

            players = list(record["players"])
            if set(normalized) != set(players):
                self._stats["rejected"] += 1
                raise MatchLedgerError(
                    _INVALID_RESULT,
                    {
                        "match_id": match_id,
                        "reason": "player_set_mismatch",
                        "expected": players,
                        "actual": list(normalized),
                    },
                )

            player_count = len(players)
            new_entry_count = player_count * 2
            if len(self._entries) + new_entry_count > self.max_entries:
                self._stats["rejected"] += 1
                raise MatchLedgerError(
                    _ENTRY_LIMIT,
                    {
                        "match_id": match_id,
                        "requested": new_entry_count,
                        "limit": self.max_entries,
                        "entries": len(self._entries),
                    },
                )

            season = record["season"]
            working_balances = dict(self._balances.get(season, {}))
            plan: list = []
            reversal_balances: dict[str, int] = {}

            for index, player in enumerate(players):
                old_delta = record["deltas"][player]
                active_seq = record["active_seqs"][player]
                owner = self._entry_owners[active_seq - 1]
                old_applied = owner.get("applied_delta", old_delta)

                before = working_balances.get(player, self.base_rating)
                reversal_balance = max(0, before - old_applied)
                reversal_applied = reversal_balance - before
                working_balances[player] = reversal_balance
                reversal_balances[player] = reversal_balance

                self._append_plan(
                    plan,
                    seq=self._seq + index + 1,
                    match_id=match_id,
                    season=season,
                    player=player,
                    kind="amend_reversal",
                    delta=-old_delta,
                    balance_after=reversal_balance,
                    applied_delta=reversal_applied,
                )

            ratings: dict[str, int] = {}
            new_deltas: dict[str, int] = {}
            new_active_seqs: dict[str, int] = {}
            amendment_number = record["amendment_count"] + 1
            new_revision = record["revision"] + 1

            for index, player in enumerate(players):
                delta = normalized[player]["delta"]
                before = reversal_balances[player]
                balance_after = max(0, before + delta)
                applied_delta = balance_after - before
                working_balances[player] = balance_after
                ratings[player] = balance_after
                new_deltas[player] = delta

                seq = self._seq + player_count + index + 1
                new_active_seqs[player] = seq
                self._append_plan(
                    plan,
                    seq=seq,
                    match_id=match_id,
                    season=season,
                    player=player,
                    kind="amend",
                    delta=delta,
                    balance_after=balance_after,
                    applied_delta=applied_delta,
                )

            all_seqs = self._commit_plan(plan)
            for entry, owner in plan:
                record.setdefault("player_by_seq", {})[str(entry["seq"])] = owner[
                    "player"
                ]
                record.setdefault("applied_by_seq", {})[str(entry["seq"])] = owner[
                    "applied_delta"
                ]
            record["entry_seqs"].extend(all_seqs)
            record["results"] = copy.deepcopy(normalized)
            record["deltas"] = new_deltas
            record["active_seqs"] = new_active_seqs
            record["status"] = "amended"
            record["revision"] = new_revision
            record["amendment_count"] = amendment_number
            record["entry_count"] += new_entry_count
            record["amendments"].append(
                {
                    "amendment": amendment_number,
                    "revision": new_revision,
                    "reason": reason,
                    "results": copy.deepcopy(normalized),
                    "seqs": all_seqs,
                }
            )
            self._balances[season] = working_balances
            self._stats["amended"] += 1

            return {
                "status": "amended",
                "amendment": amendment_number,
                "revision": new_revision,
                "ratings": copy.deepcopy(ratings),
            }

    def void_match(self, match_id: str, *, reason: str = "") -> dict:
        with self._lock:
            try:
                record = self._live_match(match_id)
            except MatchLedgerError:
                self._stats["rejected"] += 1
                raise

            players = list(record["players"])
            player_count = len(players)
            if len(self._entries) + player_count > self.max_entries:
                self._stats["rejected"] += 1
                raise MatchLedgerError(
                    _ENTRY_LIMIT,
                    {
                        "match_id": match_id,
                        "requested": player_count,
                        "limit": self.max_entries,
                        "entries": len(self._entries),
                    },
                )

            season = record["season"]
            working_balances = dict(self._balances.get(season, {}))
            plan: list = []
            ratings: dict[str, int] = {}

            for index, player in enumerate(players):
                old_delta = record["deltas"][player]
                active_seq = record["active_seqs"][player]
                owner = self._entry_owners[active_seq - 1]
                old_applied = owner.get("applied_delta", old_delta)

                before = working_balances.get(player, self.base_rating)
                balance_after = max(0, before - old_applied)
                applied_delta = balance_after - before
                working_balances[player] = balance_after
                ratings[player] = balance_after

                self._append_plan(
                    plan,
                    seq=self._seq + index + 1,
                    match_id=match_id,
                    season=season,
                    player=player,
                    kind="void_reversal",
                    delta=-old_delta,
                    balance_after=balance_after,
                    applied_delta=applied_delta,
                )

            seqs = self._commit_plan(plan)
            for entry, owner in plan:
                record.setdefault("player_by_seq", {})[str(entry["seq"])] = owner[
                    "player"
                ]
                record.setdefault("applied_by_seq", {})[str(entry["seq"])] = owner[
                    "applied_delta"
                ]
            record["entry_seqs"].extend(seqs)
            record["entry_count"] += player_count
            record["status"] = "voided"
            record["void"] = {"reason": reason, "seqs": seqs}
            self._balances[season] = working_balances
            self._stats["voided"] += 1

            return {
                "match_id": match_id,
                "status": "voided",
                "revision": record["revision"],
                "ratings": copy.deepcopy(ratings),
                "entry_count": record["entry_count"],
            }

    def rating_of(self, player: str, *, season: str) -> int:
        with self._lock:
            return self._balances.get(season, {}).get(player, self.base_rating)

    def history_of(self, player: str, *, season: str | None = None) -> list:
        with self._lock:
            history = []
            for seq in self._player_seqs.get(player, ()):
                owner = self._entry_owners[seq - 1]
                if season is not None and owner["season"] != season:
                    continue
                history.append(copy.deepcopy(self._entries[seq - 1]))
            return history

    def settlement_of(self, match_id: str) -> dict:
        with self._lock:
            if not _nonempty_string(match_id) or match_id not in self._matches:
                raise self._unknown_match(match_id)
            record = self._matches[match_id]
            return {
                "match_id": record["match_id"],
                "season": record["season"],
                "status": record["status"],
                "players": copy.deepcopy(record["players"]),
                "deltas": copy.deepcopy(record["deltas"]),
                "entry_count": record["entry_count"],
                "revision": record["revision"],
            }

    def season_matches(self, season: str) -> int:
        with self._lock:
            return self._season_matches.get(season, 0)

    def match_state(self, match_id: str) -> str:
        with self._lock:
            if not _nonempty_string(match_id):
                return "unknown"
            return self._matches.get(match_id, {}).get("status", "unknown")

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            season_starts = {
                season_name: start for start, season_name in self._season_starts
            }
            state = {
                "version": _SNAPSHOT_VERSION,
                "seq": self._seq,
                "base_rating": self.base_rating,
                "season_starts": season_starts,
                "balances": self._balances,
                "matches": self._matches,
                "entries": self._entries,
                "season_matches": self._season_matches,
                "stats": self._stats,
            }
            return copy.deepcopy(state)

    @staticmethod
    def _invalid_snapshot(detail: object) -> MatchLedgerError:
        return MatchLedgerError(_SNAPSHOT_INVALID, detail)

    def _parse_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise self._invalid_snapshot({"reason": "not_mapping"})

        version = state.get("version")
        if not _is_int(version) or version != _SNAPSHOT_VERSION:
            raise self._invalid_snapshot({"field": "version", "value": version})

        required = ("balances", "matches", "entries", "season_matches", "stats")
        for key in required:
            if key not in state:
                raise self._invalid_snapshot({"missing": key})

        balances = state["balances"]
        if not isinstance(balances, dict):
            raise self._invalid_snapshot({"field": "balances"})
        for season, players in balances.items():
            if not _nonempty_string(season) or not isinstance(players, dict):
                raise self._invalid_snapshot({"field": "balances", "season": season})
            for player, rating in players.items():
                if not _nonempty_string(player):
                    raise self._invalid_snapshot(
                        {"field": "balances.player", "season": season}
                    )
                if not _is_int(rating) or rating < 0:
                    raise self._invalid_snapshot(
                        {"field": "balances.rating", "season": season, "player": player}
                    )

        matches = state["matches"]
        if not isinstance(matches, dict):
            raise self._invalid_snapshot({"field": "matches"})
        for match_id, record in matches.items():
            if not _nonempty_string(match_id) or not isinstance(record, dict):
                raise self._invalid_snapshot(
                    {"field": "matches", "match_id": match_id}
                )

        entries = state["entries"]
        if not isinstance(entries, list):
            raise self._invalid_snapshot({"field": "entries"})
        for index, entry in enumerate(entries):
            expected_seq = index + 1
            if not isinstance(entry, dict):
                raise self._invalid_snapshot(
                    {"field": "entries", "seq": expected_seq}
                )
            if entry.get("seq") != expected_seq:
                raise self._invalid_snapshot(
                    {"field": "entries.seq", "expected": expected_seq}
                )
            if not _nonempty_string(entry.get("match_id")):
                raise self._invalid_snapshot(
                    {"field": "entries.match_id", "seq": expected_seq}
                )
            if not _nonempty_string(entry.get("kind")):
                raise self._invalid_snapshot(
                    {"field": "entries.kind", "seq": expected_seq}
                )
            if not _is_int(entry.get("delta")):
                raise self._invalid_snapshot(
                    {"field": "entries.delta", "seq": expected_seq}
                )
            balance_after = entry.get("balance_after")
            if not _is_int(balance_after) or balance_after < 0:
                raise self._invalid_snapshot(
                    {"field": "entries.balance_after", "seq": expected_seq}
                )

        season_matches = state["season_matches"]
        if not isinstance(season_matches, dict):
            raise self._invalid_snapshot({"field": "season_matches"})
        for season, count in season_matches.items():
            if not _nonempty_string(season) or not _is_int(count) or count < 0:
                raise self._invalid_snapshot(
                    {"field": "season_matches", "season": season}
                )

        stats = state["stats"]
        if not isinstance(stats, dict):
            raise self._invalid_snapshot({"field": "stats"})
        for key in STATS_KEYS:
            if not _is_int(stats.get(key)) or stats[key] < 0:
                raise self._invalid_snapshot({"field": f"stats.{key}"})

        owners = state.get("entry_owners")
        if owners is None:
            owners = self._owners_from_matches(entries, matches)
        if not isinstance(owners, list) or len(owners) != len(entries):
            raise self._invalid_snapshot({"field": "entry_owners"})
        for index, owner in enumerate(owners):
            if not isinstance(owner, dict):
                raise self._invalid_snapshot(
                    {"field": "entry_owners", "seq": index + 1}
                )
            if not _nonempty_string(owner.get("player")):
                raise self._invalid_snapshot(
                    {"field": "entry_owners.player", "seq": index + 1}
                )
            if not _nonempty_string(owner.get("season")):
                raise self._invalid_snapshot(
                    {"field": "entry_owners.season", "seq": index + 1}
                )
            if "applied_delta" in owner and not _is_int(owner.get("applied_delta")):
                raise self._invalid_snapshot(
                    {"field": "entry_owners.applied_delta", "seq": index + 1}
                )

        base_rating = self.base_rating
        if "base_rating" in state:
            base_rating = state["base_rating"]
            if not _is_int(base_rating):
                raise self._invalid_snapshot({"field": "base_rating"})

        max_entries = self.max_entries
        if "max_entries" in state:
            max_entries = state["max_entries"]
            if not _is_int(max_entries) or max_entries < 0:
                raise self._invalid_snapshot({"field": "max_entries"})

        season_starts = self._season_starts
        if state.get("season_starts") is not None:
            try:
                season_starts = self._normalize_season_starts(state["season_starts"])
            except MatchLedgerError as exc:
                raise self._invalid_snapshot(exc.detail) from exc

        try:
            copied = copy.deepcopy(state)
        except Exception as exc:
            raise self._invalid_snapshot({"reason": "deepcopy_failed"}) from exc

        return {
            "seq": len(entries),
            "base_rating": base_rating,
            "max_entries": max_entries,
            "season_starts": season_starts,
            "balances": copied["balances"],
            "matches": copied["matches"],
            "entries": copied["entries"],
            "owners": copy.deepcopy(owners),
            "season_matches": copied["season_matches"],
            "stats": {key: copied["stats"][key] for key in STATS_KEYS},
        }

    def _owners_from_matches(self, entries: list[dict], matches: dict) -> list[dict]:
        owners: list[dict] = []
        for entry in entries:
            match_id = entry["match_id"]
            record = matches.get(match_id)
            player = None
            season = None
            applied_delta = entry["delta"]
            if isinstance(record, dict):
                season = record.get("season")
                player_by_seq = record.get("player_by_seq")
                if isinstance(player_by_seq, dict):
                    player = player_by_seq.get(str(entry["seq"]))
                    if player is None:
                        player = player_by_seq.get(entry["seq"])
                if player is None:
                    player_seqs = record.get("player_seqs")
                    if isinstance(player_seqs, dict):
                        for candidate, seq_list in player_seqs.items():
                            if entry["seq"] in seq_list:
                                player = candidate
                                break

                applied_by_seq = record.get("applied_by_seq")
                if isinstance(applied_by_seq, dict):
                    applied = applied_by_seq.get(str(entry["seq"]))
                    if applied is None:
                        applied = applied_by_seq.get(entry["seq"])
                    if _is_int(applied):
                        applied_delta = applied

            if player is None or season is None:
                raise self._invalid_snapshot(
                    {"field": "entry_owners", "seq": entry["seq"]}
                )

            owners.append(
                {
                    "player": player,
                    "season": season,
                    "applied_delta": applied_delta,
                }
            )
        return owners

    def restore(self, state) -> None:
        with self._lock:
            candidate = self._parse_snapshot(state)

            player_seqs: dict[str, list[int]] = {}
            for index, owner in enumerate(candidate["owners"]):
                seq = index + 1
                owner.setdefault(
                    "applied_delta",
                    candidate["entries"][index]["delta"],
                )
                player_seqs.setdefault(owner["player"], []).append(seq)

            self.base_rating = candidate["base_rating"]
            self.max_entries = candidate["max_entries"]
            self._season_starts = candidate["season_starts"]
            self._balances = candidate["balances"]
            self._matches = candidate["matches"]
            self._entries = candidate["entries"]
            self._entry_owners = candidate["owners"]
            self._player_seqs = player_seqs
            self._seq = candidate["seq"]
            self._season_matches = candidate["season_matches"]
            self._stats = candidate["stats"]
