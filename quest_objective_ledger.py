"""Quest objective progress ledger with party sharing and turn-in."""

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping


class QuestLedgerError(Exception):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ObjectiveLedger:
    """Track per-player quest objectives, sources and turn-in state."""

    _STAT_NAMES = ("accepted", "recorded", "delivered", "abandoned", "rewards")

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._quests = {}
        self._players = {}
        self._parties = {}
        self._player_party = {}
        self._stats = {
            "accepted": 0,
            "recorded": 0,
            "delivered": 0,
            "abandoned": 0,
            "rewards": 0,
        }

    @staticmethod
    def _is_positive_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @classmethod
    def _event_key(cls, value):
        if isinstance(value, dict):
            return (
                "dict",
                frozenset(
                    (cls._event_key(key), cls._event_key(item))
                    for key, item in value.items()
                ),
            )
        if isinstance(value, (list, tuple)):
            return ("sequence", tuple(cls._event_key(item) for item in value))
        if isinstance(value, (set, frozenset)):
            return ("set", frozenset(cls._event_key(item) for item in value))
        try:
            hash(value)
        except TypeError:
            return ("unhashable", repr(value))
        return ("value", value)

    @staticmethod
    def _valid_source(source) -> bool:
        return (
            isinstance(source, dict)
            and bool(source.get("kind"))
            and bool(source.get("event_id"))
        )

    def _definition(self, quest):
        if quest not in self._quests:
            raise QuestLedgerError("QUEST_UNKNOWN", quest)
        return self._quests[quest]

    def _entry(self, player, quest):
        quests = self._players.get(player)
        if quests is None:
            return None
        return quests.get(quest)

    @staticmethod
    def _new_entry(objectives):
        return {
            "state": "active",
            "counts": {name: 0 for name in objectives},
            "sources": {name: [] for name in objectives},
            "seen": {name: set() for name in objectives},
        }

    def _progress_payload(self, player, quest, entry):
        definition = self._quests[quest]
        return {
            "player": player,
            "quest": quest,
            "state": entry["state"],
            "objectives": {
                name: {
                    "count": entry["counts"][name],
                    "required": required,
                }
                for name, required in definition["objectives"].items()
            },
        }

    def _record_one(self, entry, definition, objective, delta, source):
        required = definition["objectives"][objective]
        event_key = self._event_key(source["event_id"])
        seen = entry["seen"][objective]
        if event_key in seen:
            count = entry["counts"][objective]
            return count, required, count >= required, False

        seen.add(event_key)
        entry["sources"][objective].append(copy.deepcopy(source))
        count = min(required, entry["counts"][objective] + delta)
        entry["counts"][objective] = count
        return count, required, count >= required, True

    def _shared_entries(self, player, quest):
        entries = [self._players[player][quest]]
        party = self._player_party.get(player)
        if party is None:
            return entries

        for member in self._parties.get(party, {}):
            if member == player:
                continue
            entry = self._entry(member, quest)
            if entry is not None and entry["state"] == "active":
                entries.append(entry)
        return entries

    def DefineQuest(self, quest, objectives, *, turn_in=None, reward=None):
        with self._lock:
            if quest in self._quests:
                raise QuestLedgerError("QUEST_ALREADY_DEFINED", quest)
            if not isinstance(objectives, Mapping) or not objectives:
                raise QuestLedgerError("QUEST_DEFINITION_INVALID", objectives)

            normalized = {}
            for name, spec in objectives.items():
                if not isinstance(spec, Mapping) or "required" not in spec:
                    raise QuestLedgerError("QUEST_DEFINITION_INVALID", name)
                required = spec["required"]
                if not self._is_positive_int(required):
                    raise QuestLedgerError("QUEST_DEFINITION_INVALID", name)
                normalized[name] = required

            if turn_in is None:
                turn_in_copy = {}
            elif isinstance(turn_in, Mapping):
                turn_in_copy = copy.deepcopy(dict(turn_in))
            else:
                raise QuestLedgerError("QUEST_DEFINITION_INVALID", "turn_in")

            if reward is None:
                reward_copy = {}
            elif isinstance(reward, Mapping):
                reward_copy = copy.deepcopy(dict(reward))
            else:
                raise QuestLedgerError("QUEST_DEFINITION_INVALID", "reward")

            definition = {
                "objectives": normalized,
                "turn_in": turn_in_copy,
                "reward": reward_copy,
            }
            self._quests[quest] = definition
            return {
                "quest": quest,
                "objectives": dict(normalized),
                "turn_in": copy.deepcopy(turn_in_copy),
                "reward": copy.deepcopy(reward_copy),
            }

    def Accept(self, player, quest):
        with self._lock:
            definition = self._definition(quest)
            entry = self._entry(player, quest)
            if entry is not None and entry["state"] in ("active", "delivered"):
                raise QuestLedgerError("QUEST_ALREADY_ACCEPTED", quest)

            entry = self._new_entry(definition["objectives"])
            self._players.setdefault(player, {})[quest] = entry
            self._stats["accepted"] += 1
            return self._progress_payload(player, quest, entry)

    def Record(self, player, quest, objective, delta, source):
        with self._lock:
            definition = self._definition(quest)
            entry = self._entry(player, quest)
            if entry is None:
                raise QuestLedgerError("QUEST_NOT_ACCEPTED", quest)
            if entry["state"] != "active":
                raise QuestLedgerError("QUEST_NOT_ACTIVE", quest)
            if objective not in definition["objectives"]:
                raise QuestLedgerError("QUEST_OBJECTIVE_UNKNOWN", objective)
            if not self._is_positive_int(delta):
                raise QuestLedgerError("QUEST_DELTA_INVALID", delta)
            if not self._valid_source(source):
                raise QuestLedgerError("QUEST_SOURCE_INVALID", source)

            result = None
            for target in self._shared_entries(player, quest):
                current = self._record_one(
                    target, definition, objective, delta, source
                )
                if current[3]:
                    self._stats["recorded"] += 1
                if target is entry:
                    result = current

            count, required, capped, counted = result
            return {
                "player": player,
                "quest": quest,
                "objective": objective,
                "count": count,
                "required": required,
                "capped": capped,
                "counted": counted,
            }

    def Progress(self, player, quest):
        with self._lock:
            self._definition(quest)
            entry = self._entry(player, quest)
            if entry is None:
                raise QuestLedgerError("QUEST_NOT_ACCEPTED", quest)
            return self._progress_payload(player, quest, entry)

    def TurnIn(self, player, quest, items):
        with self._lock:
            definition = self._definition(quest)
            entry = self._entry(player, quest)
            if entry is None:
                raise QuestLedgerError("QUEST_NOT_ACCEPTED", quest)
            if entry["state"] == "delivered":
                raise QuestLedgerError("QUEST_ALREADY_DELIVERED", quest)
            if entry["state"] != "active":
                raise QuestLedgerError("QUEST_NOT_ACTIVE", quest)

            if any(
                entry["counts"][name] < required
                for name, required in definition["objectives"].items()
            ):
                raise QuestLedgerError("QUEST_OBJECTIVES_INCOMPLETE", quest)

            if items is None:
                submitted = {}
            elif isinstance(items, Mapping):
                submitted = dict(items)
            else:
                submitted = None
            if submitted != definition["turn_in"]:
                raise QuestLedgerError("QUEST_ITEMS_MISMATCH", items)

            entry["state"] = "delivered"
            self._stats["delivered"] += 1
            self._stats["rewards"] += 1
            return {
                "player": player,
                "quest": quest,
                "state": entry["state"],
                "reward": copy.deepcopy(definition["reward"]),
            }

    def Abandon(self, player, quest):
        with self._lock:
            definition = self._definition(quest)
            entry = self._entry(player, quest)
            if entry is None:
                raise QuestLedgerError("QUEST_NOT_ACCEPTED", quest)
            if entry["state"] == "abandoned":
                return {"player": player, "quest": quest, "state": "abandoned"}
            if entry["state"] != "active":
                raise QuestLedgerError("QUEST_NOT_ACTIVE", quest)

            entry["state"] = "abandoned"
            for name in definition["objectives"]:
                entry["counts"][name] = 0
                entry["sources"][name].clear()
                entry["seen"][name].clear()
            self._stats["abandoned"] += 1
            return {"player": player, "quest": quest, "state": "abandoned"}

    def Sources(self, player, quest, objective):
        with self._lock:
            definition = self._definition(quest)
            entry = self._entry(player, quest)
            if entry is None:
                raise QuestLedgerError("QUEST_NOT_ACCEPTED", quest)
            if objective not in definition["objectives"]:
                raise QuestLedgerError("QUEST_OBJECTIVE_UNKNOWN", objective)
            return copy.deepcopy(entry["sources"][objective])

    def FormParty(self, party, players):
        with self._lock:
            if isinstance(players, str):
                roster = [players]
            else:
                roster = list(players)

            members = dict.fromkeys(roster)
            for player in members:
                current = self._player_party.get(player)
                if current is not None and current != party:
                    raise QuestLedgerError(
                        "QUEST_PARTY_CONFLICT",
                        {"player": player, "party": current},
                    )

            old_members = self._parties.get(party, {})
            for player in old_members:
                if player not in members and self._player_party.get(player) == party:
                    del self._player_party[player]

            self._parties[party] = members
            for player in members:
                self._player_party[player] = party
            return {"party": party, "players": list(members)}

    def Snapshot(self):
        with self._lock:
            quests = {}
            for quest, definition in self._quests.items():
                quests[quest] = {
                    "objectives": dict(definition["objectives"]),
                    "turn_in": copy.deepcopy(definition["turn_in"]),
                    "reward": copy.deepcopy(definition["reward"]),
                }

            players = {}
            for player, player_quests in self._players.items():
                player_state = {}
                for quest, entry in player_quests.items():
                    player_state[quest] = {
                        "state": entry["state"],
                        "objectives": {
                            name: {
                                "count": entry["counts"][name],
                                "sources": copy.deepcopy(entry["sources"][name]),
                            }
                            for name in self._quests[quest]["objectives"]
                        },
                    }
                players[player] = player_state

            return {
                "quests": quests,
                "players": players,
                "parties": {
                    party: list(members) for party, members in self._parties.items()
                },
                "stats": dict(self._stats),
            }

    def Restore(self, state):
        with self._lock:
            try:
                restored = self._parse_snapshot(state)
            except Exception as exc:
                raise QuestLedgerError("QUEST_SNAPSHOT_INVALID", exc) from exc

            (
                self._quests,
                self._players,
                self._parties,
                self._player_party,
                self._stats,
            ) = restored

    def _parse_snapshot(self, state):
        if not isinstance(state, dict):
            raise ValueError("snapshot must be a mapping")
        required_keys = {"quests", "players", "parties", "stats"}
        if set(state) != required_keys:
            raise ValueError("snapshot keys are invalid")
        if not all(
            isinstance(state[key], dict) for key in required_keys
        ):
            raise ValueError("snapshot sections must be mappings")

        quests = {}
        for quest, raw_definition in state["quests"].items():
            if not isinstance(raw_definition, dict):
                raise ValueError("quest definition must be a mapping")
            raw_objectives = raw_definition.get("objectives")
            raw_turn_in = raw_definition.get("turn_in")
            raw_reward = raw_definition.get("reward")
            if (
                not isinstance(raw_objectives, dict)
                or not raw_objectives
                or not isinstance(raw_turn_in, dict)
                or not isinstance(raw_reward, dict)
            ):
                raise ValueError("quest definition is invalid")

            objectives = {}
            for name, required in raw_objectives.items():
                if not self._is_positive_int(required):
                    raise ValueError("objective requirement is invalid")
                objectives[name] = required
            quests[quest] = {
                "objectives": objectives,
                "turn_in": copy.deepcopy(raw_turn_in),
                "reward": copy.deepcopy(raw_reward),
            }

        players = {}
        for player, raw_quests in state["players"].items():
            if not isinstance(raw_quests, dict):
                raise ValueError("player progress must be a mapping")
            player_quests = {}
            for quest, raw_entry in raw_quests.items():
                if quest not in quests or not isinstance(raw_entry, dict):
                    raise ValueError("player quest entry is invalid")
                entry_state = raw_entry.get("state")
                if entry_state not in ("active", "delivered", "abandoned"):
                    raise ValueError("quest state is invalid")
                raw_objectives = raw_entry.get("objectives")
                if not isinstance(raw_objectives, dict):
                    raise ValueError("objective progress must be a mapping")

                required_names = set(quests[quest]["objectives"])
                if set(raw_objectives) != required_names:
                    raise ValueError("objective progress names do not match")

                counts = {}
                sources = {}
                seen = {}
                for name, required in quests[quest]["objectives"].items():
                    raw_progress = raw_objectives[name]
                    if not isinstance(raw_progress, dict):
                        raise ValueError("objective progress entry is invalid")
                    count = raw_progress.get("count")
                    raw_sources = raw_progress.get("sources")
                    if (
                        not isinstance(count, int)
                        or isinstance(count, bool)
                        or count < 0
                        or count > required
                        or not isinstance(raw_sources, (list, tuple))
                    ):
                        raise ValueError("objective progress value is invalid")

                    source_copy = []
                    event_keys = set()
                    for source in raw_sources:
                        if not self._valid_source(source):
                            raise ValueError("source entry is invalid")
                        event_key = self._event_key(source["event_id"])
                        if event_key in event_keys:
                            raise ValueError("duplicate event source")
                        event_keys.add(event_key)
                        source_copy.append(copy.deepcopy(source))

                    counts[name] = count
                    sources[name] = source_copy
                    seen[name] = event_keys

                player_quests[quest] = {
                    "state": entry_state,
                    "counts": counts,
                    "sources": sources,
                    "seen": seen,
                }
            players[player] = player_quests

        parties = {}
        player_party = {}
        for party, raw_roster in state["parties"].items():
            if not isinstance(raw_roster, (list, tuple)):
                raise ValueError("party roster must be a sequence")
            members = {}
            for player in raw_roster:
                if player in members or player in player_party:
                    raise ValueError("player belongs to multiple parties")
                members[player] = None
                player_party[player] = party
            parties[party] = members

        raw_stats = state["stats"]
        if set(raw_stats) != set(self._STAT_NAMES):
            raise ValueError("stats are invalid")
        stats = {}
        for name in self._STAT_NAMES:
            value = raw_stats[name]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ValueError("stat value is invalid")
            stats[name] = value

        return quests, players, parties, player_party, stats

    def Stats(self):
        with self._lock:
            return dict(self._stats)
