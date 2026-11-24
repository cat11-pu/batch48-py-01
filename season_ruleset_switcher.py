"""Season ruleset switching with pinned in-flight matches and rollback."""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
import threading

STATS_KEYS = (
    "registered",
    "updated",
    "activated",
    "switched",
    "pinned",
    "finished",
    "rollbacks",
    "anomalies",
    "rejected",
)

_SNAPSHOT_VERSION = 1
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def _count_rejection(method):
    @functools.wraps(method)
    def wrapped(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except RulesetError:
            with self._lock:
                self._stats["rejected"] += 1
            raise

    return wrapped


class RulesetError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SeasonRulesetSwitcher:
    """Activate rulesets per season and pin one version per running match."""

    def __init__(self, *, max_rulesets: int = 8, clock=None) -> None:
        self.max_rulesets = max_rulesets
        self._clock = clock
        self._lock = threading.RLock()
        self._rulesets = {}
        self._active = {}
        self._history = {}
        self._rooms = {}
        self._matches = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    @staticmethod
    def canonical_digest(payload) -> str:
        data = json.dumps(payload, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"), default=str)
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    @staticmethod
    def _valid_text(value) -> bool:
        return isinstance(value, str) and bool(value.strip())

    @classmethod
    def _require_name(cls, name: str) -> None:
        if not cls._valid_text(name):
            raise RulesetError("RULESET_INVALID_NAME", name)

    @classmethod
    def _require_season(cls, season: str) -> None:
        if not cls._valid_text(season):
            raise RulesetError("RULESET_INVALID_SEASON", season)

    @classmethod
    def _require_room(cls, room_id: str) -> None:
        if not cls._valid_text(room_id):
            raise RulesetError("RULESET_INVALID_ROOM", room_id)

    @classmethod
    def _require_match(cls, match_id: str) -> None:
        if not cls._valid_text(match_id):
            raise RulesetError("RULESET_INVALID_MATCH", match_id)

    @staticmethod
    def _valid_digest(digest) -> bool:
        return isinstance(digest, str) and _DIGEST_RE.fullmatch(digest) is not None

    @classmethod
    def _require_digest(cls, digest) -> None:
        if not cls._valid_digest(digest):
            raise RulesetError("RULESET_INVALID_DIGEST", digest)

    @staticmethod
    def _public_active(active: dict) -> dict:
        return {
            "season": active["season"],
            "ruleset": active["ruleset"],
            "version": active["version"],
            "digest": active["digest"],
            "generation": active["generation"],
        }

    @staticmethod
    def _public_match(match: dict) -> dict:
        return {
            "match_id": match["match_id"],
            "season": match["season"],
            "ruleset": match["ruleset"],
            "version": match["version"],
            "digest": match["digest"],
            "state": match["state"],
            "result": match["result"],
            "anomaly": match["anomaly"],
        }

    def _version_record(self, season: str, name: str, version: int) -> dict:
        for record in self._rulesets[(season, name)]:
            if record["version"] == version:
                return record
        raise RulesetError("RULESET_UNKNOWN_VERSION", version)

    def _pin_locked(self, match_id: str, season: str) -> dict:
        if match_id in self._matches:
            raise RulesetError("RULESET_MATCH_EXISTS", match_id)
        active = self._active.get(season)
        if active is None:
            raise RulesetError("RULESET_NO_ACTIVE", season)

        record = self._version_record(
            season, active["ruleset"], active["version"]
        )
        match = {
            "match_id": match_id,
            "season": season,
            "ruleset": active["ruleset"],
            "version": active["version"],
            "digest": record["digest"],
            "payload": copy.deepcopy(record["payload"]),
            "state": "pinned",
            "result": None,
            "anomaly": False,
        }
        self._matches[match_id] = match
        self._stats["pinned"] += 1
        return self._public_match(match)

    @_count_rejection
    def register_ruleset(self, name: str, *, season: str, payload) -> dict:
        self._require_name(name)
        self._require_season(season)
        if not isinstance(payload, dict) or not payload:
            raise RulesetError("RULESET_INVALID_PAYLOAD", payload)

        digest = self.canonical_digest(payload)
        key = (season, name)
        with self._lock:
            versions = self._rulesets.get(key)
            if versions is not None and versions[-1]["digest"] == digest:
                current = versions[-1]
                return {
                    "ruleset": name,
                    "season": season,
                    "version": current["version"],
                    "digest": current["digest"],
                    "changed": False,
                }

            if versions is None and len(self._rulesets) >= self.max_rulesets:
                raise RulesetError("RULESET_LIMIT", self.max_rulesets)

            if versions is None:
                version = 1
                self._rulesets[key] = []
                self._stats["registered"] += 1
            else:
                version = versions[-1]["version"] + 1
                self._stats["updated"] += 1

            self._rulesets[key].append({
                "version": version,
                "digest": digest,
                "payload": copy.deepcopy(payload),
            })
            return {
                "ruleset": name,
                "season": season,
                "version": version,
                "digest": digest,
                "changed": True,
            }

    @_count_rejection
    def ruleset_info(
        self, name: str, *, season: str, version: int | None = None
    ) -> dict:
        self._require_name(name)
        self._require_season(season)
        if version is not None and (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 1
        ):
            raise RulesetError("RULESET_UNKNOWN_VERSION", version)

        with self._lock:
            versions = self._rulesets.get((season, name))
            if versions is None:
                raise RulesetError("RULESET_UNKNOWN", name)
            if version is None:
                record = versions[-1]
            else:
                record = next(
                    (item for item in versions if item["version"] == version),
                    None,
                )
                if record is None:
                    raise RulesetError("RULESET_UNKNOWN_VERSION", version)
            return {
                "ruleset": name,
                "season": season,
                "version": record["version"],
                "digest": record["digest"],
                "payload": copy.deepcopy(record["payload"]),
            }

    @_count_rejection
    def activate(self, name: str, *, season: str) -> dict:
        self._require_name(name)
        self._require_season(season)

        with self._lock:
            versions = self._rulesets.get((season, name))
            if versions is None:
                raise RulesetError("RULESET_UNKNOWN", name)
            record = versions[-1]
            current = self._active.get(season)
            if (
                current is not None
                and current["ruleset"] == name
                and current["version"] == record["version"]
                and current["digest"] == record["digest"]
            ):
                out = self._public_active(current)
                out["changed"] = False
                return out

            if current is None:
                generation = 1
                self._stats["activated"] += 1
            else:
                self._history.setdefault(season, []).append({
                    "ruleset": current["ruleset"],
                    "version": current["version"],
                    "digest": current["digest"],
                    "generation": current["generation"],
                })
                generation = current["generation"] + 1
                self._stats["switched"] += 1

            active = {
                "season": season,
                "ruleset": name,
                "version": record["version"],
                "digest": record["digest"],
                "generation": generation,
            }
            self._active[season] = active
            out = self._public_active(active)
            out["changed"] = True
            return out

    @_count_rejection
    def active_for(self, season: str) -> dict:
        self._require_season(season)
        with self._lock:
            active = self._active.get(season)
            if active is None:
                raise RulesetError("RULESET_NO_ACTIVE", season)
            return self._public_active(active)

    @_count_rejection
    def rollback(self, season: str) -> dict:
        self._require_season(season)
        with self._lock:
            history = self._history.get(season)
            current = self._active.get(season)
            if not history or current is None:
                raise RulesetError("RULESET_NOTHING_TO_ROLLBACK", season)

            previous = history[-1]
            record = self._version_record(
                season, previous["ruleset"], previous["version"]
            )
            if record["digest"] != previous["digest"]:
                raise RulesetError("RULESET_SNAPSHOT_INVALID", season)

            active = {
                "season": season,
                "ruleset": previous["ruleset"],
                "version": previous["version"],
                "digest": previous["digest"],
                "generation": current["generation"] + 1,
            }
            history.pop()
            if not history:
                self._history.pop(season, None)
            self._active[season] = active
            self._stats["rollbacks"] += 1
            out = self._public_active(active)
            out["changed"] = True
            return out

    @_count_rejection
    def create_room(self, room_id: str, *, season: str) -> dict:
        self._require_room(room_id)
        self._require_season(season)
        with self._lock:
            if room_id in self._rooms:
                raise RulesetError("RULESET_ROOM_EXISTS", room_id)
            self._rooms[room_id] = {
                "room_id": room_id,
                "season": season,
                "state": "created",
                "match_id": None,
            }
            return {
                "room_id": room_id,
                "season": season,
                "state": "created",
            }

    @_count_rejection
    def start_room(self, room_id: str) -> dict:
        self._require_room(room_id)
        with self._lock:
            room = self._rooms.get(room_id)
            if room is None:
                raise RulesetError("RULESET_UNKNOWN_ROOM", room_id)
            if room["state"] != "created":
                raise RulesetError("RULESET_ROOM_ALREADY_STARTED", room_id)

            match_id = f"match:{room_id}"
            self._pin_locked(match_id, room["season"])
            room["state"] = "started"
            room["match_id"] = match_id
            match = self._matches[match_id]
            return {
                "room_id": room_id,
                "match_id": match_id,
                "season": match["season"],
                "ruleset": match["ruleset"],
                "version": match["version"],
                "digest": match["digest"],
                "state": match["state"],
            }

    @_count_rejection
    def pin_for_match(self, match_id: str, *, season: str) -> dict:
        self._require_match(match_id)
        self._require_season(season)
        with self._lock:
            return self._pin_locked(match_id, season)

    @_count_rejection
    def resolve_for_match(self, match_id: str) -> dict:
        self._require_match(match_id)
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                raise RulesetError("RULESET_UNKNOWN_MATCH", match_id)
            return {
                "match_id": match["match_id"],
                "season": match["season"],
                "ruleset": match["ruleset"],
                "version": match["version"],
                "digest": match["digest"],
                "payload": copy.deepcopy(match["payload"]),
                "state": match["state"],
            }

    @_count_rejection
    def match_state(self, match_id: str) -> dict:
        self._require_match(match_id)
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                raise RulesetError("RULESET_UNKNOWN_MATCH", match_id)
            return self._public_match(match)

    @_count_rejection
    def finish_match(self, match_id: str, *, result: str) -> dict:
        self._require_match(match_id)
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                raise RulesetError("RULESET_UNKNOWN_MATCH", match_id)
            if match["state"] == "finished":
                raise RulesetError("RULESET_MATCH_FINISHED", match_id)
            if not self._valid_text(result):
                raise RulesetError("RULESET_INVALID_RESULT", result)

            match["state"] = "finished"
            match["result"] = result
            self._stats["finished"] += 1
            return {
                "match_id": match_id,
                "state": "finished",
                "result": result,
            }

    @_count_rejection
    def audit_mismatch(self, match_id: str, expected_digest: str) -> dict:
        self._require_match(match_id)
        self._require_digest(expected_digest)
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                raise RulesetError("RULESET_UNKNOWN_MATCH", match_id)

            if match["digest"] == expected_digest:
                status = "ok"
            else:
                status = "mismatch"
                match["anomaly"] = True
                self._stats["anomalies"] += 1
            return {
                "match_id": match_id,
                "status": status,
                "pinned_digest": match["digest"],
                "expected_digest": expected_digest,
            }

    @_count_rejection
    def replay(self, match_id: str, recorded_digest: str) -> dict:
        self._require_match(match_id)
        self._require_digest(recorded_digest)
        with self._lock:
            match = self._matches.get(match_id)
            if match is None:
                raise RulesetError("RULESET_UNKNOWN_MATCH", match_id)
            if match["digest"] != recorded_digest:
                match["anomaly"] = True
                self._stats["anomalies"] += 1
                raise RulesetError("RULESET_DIGEST_MISMATCH", {
                    "match_id": match_id,
                    "pinned_digest": match["digest"],
                    "recorded_digest": recorded_digest,
                })
            return {
                "replayed": True,
                "match_id": match_id,
                "digest": match["digest"],
                "payload": copy.deepcopy(match["payload"]),
            }

    @_count_rejection
    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    @_count_rejection
    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STATS_KEYS}

    @_count_rejection
    def snapshot(self) -> dict:
        with self._lock:
            rulesets = []
            for (season, name), versions in sorted(self._rulesets.items()):
                rulesets.append({
                    "name": name,
                    "ruleset": name,
                    "season": season,
                    "versions": copy.deepcopy(versions),
                })

            active = [
                self._public_active(item)
                for item in sorted(
                    self._active.values(), key=lambda item: item["season"]
                )
            ]
            history = []
            for season in sorted(self._history):
                history.append({
                    "season": season,
                    "entries": copy.deepcopy(self._history[season]),
                })

            rooms = []
            for room_id in sorted(self._rooms):
                room = self._rooms[room_id]
                rooms.append({
                    "room_id": room["room_id"],
                    "season": room["season"],
                    "state": room["state"],
                    "match_id": room["match_id"],
                })

            matches = []
            for match_id in sorted(self._matches):
                matches.append(copy.deepcopy(self._matches[match_id]))

            generation = max(
                [item["generation"] for item in active]
                + [
                    entry["generation"]
                    for entries in self._history.values()
                    for entry in entries
                ]
                + [0]
            )
            return {
                "version": _SNAPSHOT_VERSION,
                "generation": generation,
                "rulesets": rulesets,
                "active": active,
                "history": history,
                "rooms": rooms,
                "matches": matches,
                "stats": dict(self._stats),
            }

    @staticmethod
    def _snapshot_error(detail=None):
        raise RulesetError("RULESET_SNAPSHOT_INVALID", detail)

    @classmethod
    def _snapshot_text(cls, value, detail):
        if not cls._valid_text(value):
            cls._snapshot_error(detail)
        return value

    @classmethod
    def _snapshot_version(cls, value, detail):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            cls._snapshot_error(detail)
        return value

    def _validate_snapshot(self, state):
        if not isinstance(state, dict):
            self._snapshot_error("snapshot must be an object")
        required = {
            "version", "generation", "rulesets", "active",
            "history", "rooms", "matches", "stats",
        }
        if not required.issubset(state):
            self._snapshot_error("missing snapshot fields")
        if (
            not isinstance(state["version"], int)
            or isinstance(state["version"], bool)
            or state["version"] != _SNAPSHOT_VERSION
        ):
            self._snapshot_error("unsupported snapshot version")

        raw_rulesets = state["rulesets"]
        if not isinstance(raw_rulesets, list):
            self._snapshot_error("rulesets must be a list")

        rulesets = {}
        for item in raw_rulesets:
            if not isinstance(item, dict):
                self._snapshot_error("invalid ruleset")
            season = self._snapshot_text(item.get("season"), "invalid season")
            name = item.get("ruleset", item.get("name"))
            if (
                "name" in item
                and "ruleset" in item
                and item["name"] != item["ruleset"]
            ):
                self._snapshot_error("inconsistent ruleset name")
            name = self._snapshot_text(name, "invalid ruleset name")
            key = (season, name)
            if key in rulesets:
                self._snapshot_error("duplicate ruleset")

            raw_versions = item.get("versions")
            if not isinstance(raw_versions, list) or not raw_versions:
                self._snapshot_error("ruleset versions must be a list")
            versions = []
            expected_version = 1
            for raw_version in raw_versions:
                if not isinstance(raw_version, dict):
                    self._snapshot_error("invalid ruleset version")
                version = raw_version.get("version")
                if (
                    not isinstance(version, int)
                    or isinstance(version, bool)
                    or version != expected_version
                ):
                    self._snapshot_error("invalid version sequence")
                digest = raw_version.get("digest")
                if not self._valid_digest(digest):
                    self._snapshot_error("invalid ruleset digest")
                payload = raw_version.get("payload")
                if not isinstance(payload, dict) or not payload:
                    self._snapshot_error("invalid ruleset payload")
                if self.canonical_digest(payload) != digest:
                    self._snapshot_error("ruleset digest mismatch")
                versions.append({
                    "version": version,
                    "digest": digest,
                    "payload": copy.deepcopy(payload),
                })
                expected_version += 1
            rulesets[key] = versions

        if len(rulesets) > self.max_rulesets:
            self._snapshot_error("too many rulesets")

        raw_active = state["active"]
        if not isinstance(raw_active, list):
            self._snapshot_error("active must be a list")
        active = {}
        for item in raw_active:
            if not isinstance(item, dict):
                self._snapshot_error("invalid active entry")
            season = self._snapshot_text(item.get("season"), "invalid season")
            if season in active:
                self._snapshot_error("duplicate active season")
            name = self._snapshot_text(item.get("ruleset"), "invalid ruleset")
            version = self._snapshot_version(item.get("version"), "invalid version")
            digest = item.get("digest")
            generation = self._snapshot_version(
                item.get("generation"), "invalid generation"
            )
            versions = rulesets.get((season, name))
            record = None
            if versions is not None:
                record = next(
                    (entry for entry in versions if entry["version"] == version),
                    None,
                )
            if record is None or record["digest"] != digest:
                self._snapshot_error("active ruleset does not exist")
            active[season] = {
                "season": season,
                "ruleset": name,
                "version": version,
                "digest": digest,
                "generation": generation,
            }

        raw_history = state["history"]
        if not isinstance(raw_history, list):
            self._snapshot_error("history must be a list")
        history = {}
        for item in raw_history:
            if not isinstance(item, dict):
                self._snapshot_error("invalid history")
            season = self._snapshot_text(item.get("season"), "invalid season")
            if season in history or season not in active:
                self._snapshot_error("invalid history season")
            raw_entries = item.get("entries")
            if not isinstance(raw_entries, list) or not raw_entries:
                self._snapshot_error("history entries must be a list")
            entries = []
            previous_generation = 0
            for raw_entry in raw_entries:
                if not isinstance(raw_entry, dict):
                    self._snapshot_error("invalid history entry")
                name = self._snapshot_text(
                    raw_entry.get("ruleset"), "invalid history ruleset"
                )
                version = self._snapshot_version(
                    raw_entry.get("version"), "invalid history version"
                )
                digest = raw_entry.get("digest")
                generation = self._snapshot_version(
                    raw_entry.get("generation"), "invalid history generation"
                )
                versions = rulesets.get((season, name))
                record = None
                if versions is not None:
                    record = next(
                        (entry for entry in versions if entry["version"] == version),
                        None,
                    )
                if record is None or record["digest"] != digest:
                    self._snapshot_error("history ruleset does not exist")
                if (
                    generation <= previous_generation
                    or generation >= active[season]["generation"]
                ):
                    self._snapshot_error("invalid history generation")
                previous_generation = generation
                entries.append({
                    "ruleset": name,
                    "version": version,
                    "digest": digest,
                    "generation": generation,
                })
            history[season] = entries

        raw_matches = state["matches"]
        if not isinstance(raw_matches, list):
            self._snapshot_error("matches must be a list")
        matches = {}
        for item in raw_matches:
            if not isinstance(item, dict):
                self._snapshot_error("invalid match")
            match_id = self._snapshot_text(
                item.get("match_id"), "invalid match id"
            )
            if match_id in matches:
                self._snapshot_error("duplicate match")
            season = self._snapshot_text(item.get("season"), "invalid match season")
            name = self._snapshot_text(
                item.get("ruleset"), "invalid match ruleset"
            )
            version = self._snapshot_version(
                item.get("version"), "invalid match version"
            )
            digest = item.get("digest")
            payload = item.get("payload")
            state_name = item.get("state")
            result = item.get("result")
            anomaly = item.get("anomaly")

            versions = rulesets.get((season, name))
            record = None
            if versions is not None:
                record = next(
                    (entry for entry in versions if entry["version"] == version),
                    None,
                )
            if (
                record is None
                or record["digest"] != digest
                or not isinstance(payload, dict)
                or not payload
                or payload != record["payload"]
                or self.canonical_digest(payload) != digest
            ):
                self._snapshot_error("match ruleset does not exist")
            if state_name not in {"pinned", "finished"}:
                self._snapshot_error("invalid match state")
            if not isinstance(anomaly, bool):
                self._snapshot_error("invalid match anomaly")
            if state_name == "finished":
                if not self._valid_text(result):
                    self._snapshot_error("invalid match result")
            elif result is not None:
                self._snapshot_error("pinned match cannot have a result")

            matches[match_id] = {
                "match_id": match_id,
                "season": season,
                "ruleset": name,
                "version": version,
                "digest": digest,
                "payload": copy.deepcopy(payload),
                "state": state_name,
                "result": result,
                "anomaly": anomaly,
            }

        raw_rooms = state["rooms"]
        if not isinstance(raw_rooms, list):
            self._snapshot_error("rooms must be a list")
        rooms = {}
        room_matches = set()
        for item in raw_rooms:
            if not isinstance(item, dict):
                self._snapshot_error("invalid room")
            room_id = self._snapshot_text(item.get("room_id"), "invalid room id")
            if room_id in rooms:
                self._snapshot_error("duplicate room")
            season = self._snapshot_text(item.get("season"), "invalid room season")
            room_state = item.get("state")
            match_id = item.get("match_id")
            if room_state == "created":
                if match_id is not None:
                    self._snapshot_error("created room cannot have a match")
            elif room_state == "started":
                match_id = self._snapshot_text(match_id, "invalid room match")
                match = matches.get(match_id)
                if (
                    match is None
                    or match["season"] != season
                    or match_id in room_matches
                ):
                    self._snapshot_error("room match does not exist")
                room_matches.add(match_id)
            else:
                self._snapshot_error("invalid room state")
            rooms[room_id] = {
                "room_id": room_id,
                "season": season,
                "state": room_state,
                "match_id": match_id,
            }

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict):
            self._snapshot_error("stats must be an object")
        stats = {}
        for key in STATS_KEYS:
            value = raw_stats.get(key)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                self._snapshot_error("invalid stats")
            stats[key] = value

        generation = max(
            [item["generation"] for item in active.values()]
            + [
                entry["generation"]
                for entries in history.values()
                for entry in entries
            ]
            + [0]
        )
        raw_generation = state["generation"]
        if (
            not isinstance(raw_generation, int)
            or isinstance(raw_generation, bool)
            or raw_generation != generation
        ):
            self._snapshot_error("invalid snapshot generation")

        return rulesets, active, history, rooms, matches, stats

    def restore(self, state) -> None:
        with self._lock:
            try:
                rulesets, active, history, rooms, matches, stats = (
                    self._validate_snapshot(state)
                )
            except RulesetError:
                raise
            except Exception as exc:
                raise RulesetError("RULESET_SNAPSHOT_INVALID", str(exc)) from exc

            self._rulesets = rulesets
            self._active = active
            self._history = history
            self._rooms = rooms
            self._matches = matches
            self._stats = stats
