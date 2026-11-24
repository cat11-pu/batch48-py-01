"""Replay view projector with time-accurate visibility filtering."""

from __future__ import annotations

import copy
import threading
from bisect import bisect_right
from collections.abc import Iterable


STATS_KEYS = (
    "views",
    "entities",
    "vision_samples",
    "projected",
    "visible",
    "hidden",
    "cropped",
    "duplicates",
    "conflicts",
    "rejected",
)

EVENT_TYPES = ("move", "ability", "damage", "death", "spawn", "chat", "voice")
VIEW_KINDS = ("player", "observer")
SPEECH_TYPES = ("chat", "voice")
CATEGORIES = ("unit", "projectile", "structure")
SCOPES = ("all", "team", "private")

STATUS_VISIBLE = "visible"
STATUS_HIDDEN = "hidden"
STATUS_CROPPED = "cropped"
STORED_STATUSES = (STATUS_VISIBLE, STATUS_HIDDEN, STATUS_CROPPED)


class ReplayError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ReplayViewProjector:
    """Projects a recorded replay into independently filtered viewer perspectives."""

    SNAPSHOT_VERSION = 1
    SAMPLE_LIMIT = 2000

    def __init__(self, *, view_limit: int = 8, entity_limit: int = 64,
                 event_limit: int = 2000, clock=None) -> None:
        self.view_limit = int(view_limit)
        self.entity_limit = int(entity_limit)
        self.event_limit = int(event_limit)
        self.sample_limit = self.SAMPLE_LIMIT
        self._clock = clock
        self._lock = threading.RLock()
        self._views: dict[str, dict] = {}
        self._entities: dict[str, dict] = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    @staticmethod
    def _positive_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _non_negative_int(value) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _nonempty_str(value) -> bool:
        return isinstance(value, str) and bool(value)

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise ReplayError(code, detail)

    def _conflict(self, code: str, detail: object = None) -> None:
        self._stats["conflicts"] += 1
        raise ReplayError(code, detail)

    @staticmethod
    def _invalid_snapshot(detail: object = None) -> None:
        raise ReplayError("REPLAY_SNAPSHOT_INVALID", detail)

    def register_view(self, view_id: str, *, kind: str = "player",
                      team: str | None = None, owner: str | None = None,
                      version: int = 1) -> dict:
        with self._lock:
            if not self._nonempty_str(view_id):
                self._reject("REPLAY_INVALID_VIEW", {"view_id": view_id})
            if kind not in VIEW_KINDS:
                self._reject("REPLAY_INVALID_VIEW", {"kind": kind})
            if kind == "player":
                if not self._nonempty_str(team) or not self._nonempty_str(owner):
                    self._reject("REPLAY_INVALID_VIEW",
                                 {"team": team, "owner": owner})
            else:
                if team is not None and not self._nonempty_str(team):
                    self._reject("REPLAY_INVALID_VIEW", {"team": team})
                if owner is not None and not self._nonempty_str(owner):
                    self._reject("REPLAY_INVALID_VIEW", {"owner": owner})
            if not self._positive_int(version):
                self._reject("REPLAY_INVALID_VIEW", {"version": version})

            existing = self._views.get(view_id)
            if existing is not None:
                if (existing["kind"] != kind or existing["team"] != team
                        or existing["owner"] != owner
                        or existing["version"] != version):
                    self._conflict("REPLAY_VIEW_CONFLICT", {"view_id": view_id})
                self._stats["duplicates"] += 1
                return self._view_response(existing, "duplicate")

            if len(self._views) >= self.view_limit:
                self._reject("REPLAY_VIEW_LIMIT",
                             {"view_id": view_id, "limit": self.view_limit})

            view = {
                "view_id": view_id,
                "kind": kind,
                "team": team,
                "owner": owner,
                "version": version,
                "cursor": None,
                "samples": [],
                "times": [],
                "journal": {},
                "sources": {},
            }
            self._views[view_id] = view
            self._stats["views"] += 1
            return self._view_response(view, "registered")

    @staticmethod
    def _view_response(view: dict, status: str) -> dict:
        return {
            "view_id": view["view_id"],
            "kind": view["kind"],
            "team": view["team"],
            "owner": view["owner"],
            "version": view["version"],
            "status": status,
        }

    def register_entity(self, entity_id: str, *, team: str,
                        category: str = "unit") -> dict:
        with self._lock:
            if not self._nonempty_str(entity_id):
                self._reject("REPLAY_INVALID_ENTITY", {"entity_id": entity_id})
            if not self._nonempty_str(team):
                self._reject("REPLAY_INVALID_ENTITY", {"team": team})
            if category not in CATEGORIES:
                self._reject("REPLAY_INVALID_ENTITY", {"category": category})

            existing = self._entities.get(entity_id)
            if existing is not None:
                if existing["team"] != team or existing["category"] != category:
                    self._conflict("REPLAY_ENTITY_CONFLICT",
                                   {"entity_id": entity_id})
                self._stats["duplicates"] += 1
                return self._entity_response(existing, "duplicate")

            if len(self._entities) >= self.entity_limit:
                self._reject("REPLAY_ENTITY_LIMIT",
                             {"entity_id": entity_id, "limit": self.entity_limit})

            entity = {
                "entity_id": entity_id,
                "team": team,
                "category": category,
            }
            self._entities[entity_id] = entity
            self._stats["entities"] += 1
            return self._entity_response(entity, "registered")

    @staticmethod
    def _entity_response(entity: dict, status: str) -> dict:
        return {
            "entity_id": entity["entity_id"],
            "team": entity["team"],
            "category": entity["category"],
            "status": status,
        }

    def record_vision(self, view_id: str, *, at: int, visible) -> dict:
        with self._lock:
            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})
            if not self._non_negative_int(at):
                self._reject("REPLAY_INVALID_VISION", {"at": at})
            if visible is None or (
                    not isinstance(visible, Iterable)
                    or isinstance(visible, (str, bytes, bytearray))):
                self._reject("REPLAY_INVALID_VISION", {"visible": visible})

            try:
                requested = list(visible)
            except TypeError:
                self._reject("REPLAY_INVALID_VISION", {"visible": visible})

            for entity_id in requested:
                try:
                    known = entity_id in self._entities
                except TypeError:
                    known = False
                if not known:
                    self._reject("REPLAY_UNKNOWN_ENTITY", {"entity": entity_id})

            ordered = sorted(set(requested))
            samples = view["samples"]
            times = view["times"]

            if times and at < times[-1]:
                self._reject("REPLAY_VISION_ORDER",
                             {"at": at, "last_at": times[-1]})

            if times and at == times[-1]:
                if samples[-1]["visible"] != ordered:
                    self._conflict("REPLAY_VISION_CONFLICT", {"at": at})
                self._stats["duplicates"] += 1
                return self._vision_response(view_id, at, ordered,
                                              len(samples), "duplicate")

            if len(samples) >= self.sample_limit:
                self._reject("REPLAY_VISION_LIMIT",
                             {"view_id": view_id, "limit": self.sample_limit})

            samples.append({"at": at, "visible": ordered})
            times.append(at)
            self._stats["vision_samples"] += 1
            return self._vision_response(view_id, at, ordered,
                                         len(samples), "recorded")

    @staticmethod
    def _vision_response(view_id: str, at: int, visible: list[str],
                         samples: int, status: str) -> dict:
        return {
            "view_id": view_id,
            "at": at,
            "visible": list(visible),
            "samples": samples,
            "status": status,
        }

    def _clean_event(self, event) -> dict:
        if not isinstance(event, dict):
            self._reject("REPLAY_INVALID_EVENT", {"event": event})

        clean = copy.deepcopy(event)
        clean.pop("status", None)
        clean.pop("reason", None)

        seq = clean.get("seq")
        at = clean.get("at")
        event_type = clean.get("type")
        if (not self._positive_int(seq) or not self._non_negative_int(at)
                or event_type not in EVENT_TYPES):
            self._reject("REPLAY_INVALID_EVENT",
                         {"seq": seq, "at": at, "type": event_type})

        payload = clean.get("payload", {})
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            self._reject("REPLAY_INVALID_EVENT", {"field": "payload"})
        clean["payload"] = payload

        if event_type in SPEECH_TYPES:
            team = clean.get("team")
            if team is not None and not self._nonempty_str(team):
                self._reject("REPLAY_INVALID_EVENT", {"field": "team"})

            scope = payload.get("scope")
            if event_type == "voice" and scope is None:
                scope = "team"
                payload["scope"] = scope
            if scope not in SCOPES:
                self._reject("REPLAY_INVALID_EVENT", {"field": "scope"})

            if scope == "private":
                raw_recipients = payload.get("to", [])
                if raw_recipients is None:
                    recipients = []
                elif isinstance(raw_recipients, str):
                    recipients = [raw_recipients]
                elif isinstance(raw_recipients, Iterable) and not isinstance(
                        raw_recipients, (bytes, bytearray, dict)):
                    try:
                        recipients = list(raw_recipients)
                    except TypeError:
                        self._reject("REPLAY_INVALID_EVENT", {"field": "to"})
                else:
                    self._reject("REPLAY_INVALID_EVENT", {"field": "to"})
                if not all(self._nonempty_str(item) for item in recipients):
                    self._reject("REPLAY_INVALID_EVENT", {"field": "to"})
                payload["to"] = recipients
            return clean

        entity_id = clean.get("entity")
        if not self._nonempty_str(entity_id):
            self._reject("REPLAY_INVALID_EVENT", {"field": "entity"})
        if entity_id not in self._entities:
            self._reject("REPLAY_UNKNOWN_ENTITY", {"entity": entity_id})
        return clean

    def _classify_speech(self, view: dict, event: dict) -> tuple[str, str]:
        event_type = event["type"]
        payload = event["payload"]
        scope = payload["scope"]

        if (view["kind"] == "observer" and event_type == "voice"
                and scope == "team"):
            return STATUS_CROPPED, "team"

        if scope == "all":
            return STATUS_VISIBLE, ""

        if scope == "team":
            if (view["kind"] == "player"
                    and view.get("team") == event.get("team")):
                return STATUS_VISIBLE, ""
            return STATUS_CROPPED, "team"

        recipients = payload.get("to", [])
        if view["kind"] == "player" and view.get("owner") in recipients:
            return STATUS_VISIBLE, ""
        return STATUS_CROPPED, "private"

    def _classify_event(self, view: dict, event: dict) -> tuple[str, str]:
        if event["type"] in SPEECH_TYPES:
            return self._classify_speech(view, event)

        entity_id = event["entity"]
        if view["kind"] == "observer":
            return STATUS_VISIBLE, ""

        position = bisect_right(view["times"], event["at"]) - 1
        if position < 0:
            return STATUS_HIDDEN, "not_visible"
        visible = view["samples"][position]["visible"]
        if entity_id in visible:
            return STATUS_VISIBLE, ""
        return STATUS_HIDDEN, "not_visible"

    def project_event(self, view_id: str, event: dict) -> dict:
        with self._lock:
            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})

            clean = self._clean_event(event)
            seq = clean["seq"]
            journal = view["journal"]
            sources = view["sources"]

            if seq in journal:
                if sources.get(seq) != clean:
                    self._conflict("REPLAY_EVENT_CONFLICT",
                                   {"view_id": view_id, "seq": seq})
                self._stats["duplicates"] += 1
                response = copy.deepcopy(journal[seq])
                response["status"] = "duplicate"
                return response

            if len(journal) >= self.event_limit:
                self._reject("REPLAY_EVENT_LIMIT",
                             {"view_id": view_id, "limit": self.event_limit})

            status, reason = self._classify_event(view, clean)
            if clean["type"] in SPEECH_TYPES:
                team = clean.get("team")
            else:
                team = self._entities[clean["entity"]]["team"]

            record = copy.deepcopy(clean)
            record["team"] = team
            record["status"] = status
            record["reason"] = reason
            if status != STATUS_VISIBLE:
                record["payload"] = {}

            sources[seq] = copy.deepcopy(clean)
            journal[seq] = record
            self._stats["projected"] += 1
            self._stats[status] += 1
            return copy.deepcopy(record)

    @staticmethod
    def _validate_range(start, end, limit) -> None:
        if not ReplayViewProjector._non_negative_int(start):
            raise ReplayError("REPLAY_INVALID_RANGE", {"field": "start"})
        if end is not None:
            if not ReplayViewProjector._non_negative_int(end) or end < start:
                raise ReplayError("REPLAY_INVALID_RANGE", {"field": "end"})
        if limit is not None and (
                not isinstance(limit, int) or isinstance(limit, bool)
                or limit < 0):
            raise ReplayError("REPLAY_INVALID_RANGE", {"field": "limit"})

    def project_range(self, view_id: str, *, start: int = 0,
                      end: int | None = None, limit: int | None = None) -> dict:
        with self._lock:
            try:
                self._validate_range(start, end, limit)
            except ReplayError:
                self._stats["rejected"] += 1
                raise

            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})

            matched = []
            for record in view["journal"].values():
                at = record["at"]
                if at < start:
                    continue
                if end is not None and at >= end:
                    continue
                matched.append(copy.deepcopy(record))

            matched.sort(key=lambda item: item["seq"])
            total = len(matched)
            if limit is not None:
                events = matched[:limit]
                truncated = total > limit
            else:
                events = matched
                truncated = False

            return {
                "events": events,
                "count": len(events),
                "truncated": truncated,
            }

    def seek(self, view_id: str, at: int) -> dict:
        with self._lock:
            if not self._non_negative_int(at):
                self._reject("REPLAY_INVALID_RANGE", {"at": at})
            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})

            view["cursor"] = at
            events_before = sum(
                1 for record in view["journal"].values()
                if record["at"] <= at
            )
            return {
                "view_id": view_id,
                "cursor": at,
                "events_before": events_before,
            }

    def _consistency_report(self, view: dict) -> tuple[bool, dict[str, int]]:
        counts = {STATUS_VISIBLE: 0, STATUS_HIDDEN: 0, STATUS_CROPPED: 0}
        consistent = self._positive_int(view.get("version"))
        journal = view.get("journal", {})
        sources = view.get("sources", {})

        if not isinstance(journal, dict) or not isinstance(sources, dict):
            return False, counts
        if set(journal) != set(sources):
            consistent = False

        for seq, record in journal.items():
            if not self._positive_int(seq):
                consistent = False
            if not isinstance(record, dict) or record.get("seq") != seq:
                consistent = False
                continue
            status = record.get("status")
            if status not in STORED_STATUSES:
                consistent = False
            else:
                counts[status] += 1
            if not self._non_negative_int(record.get("at")):
                consistent = False
            if not isinstance(record.get("payload"), dict):
                consistent = False
            source = sources.get(seq)
            if not isinstance(source, dict) or source.get("seq") != seq:
                consistent = False

        last_at = None
        for sample in view.get("samples", []):
            if not isinstance(sample, dict):
                consistent = False
                continue
            at = sample.get("at")
            visible = sample.get("visible")
            if not self._non_negative_int(at):
                consistent = False
            if (last_at is not None and self._non_negative_int(at)
                    and at <= last_at):
                consistent = False
            if self._non_negative_int(at):
                last_at = at
            if not isinstance(visible, list):
                consistent = False
                continue
            if visible != sorted(set(visible)):
                consistent = False
            if not all(entity_id in self._entities for entity_id in visible):
                consistent = False

        return consistent, counts

    def verify_view(self, view_id: str) -> dict:
        with self._lock:
            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})

            consistent, counts = self._consistency_report(view)
            return {
                "view_id": view_id,
                "version": view["version"],
                "events": len(view["journal"]),
                "visible": counts[STATUS_VISIBLE],
                "hidden": counts[STATUS_HIDDEN],
                "cropped": counts[STATUS_CROPPED],
                "consistent": consistent,
            }

    def event_count(self, view_id: str) -> int:
        with self._lock:
            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})
            return len(view["journal"])

    def content_of(self, view_id: str, seq: int) -> dict:
        with self._lock:
            view = self._views.get(view_id)
            if view is None:
                self._reject("REPLAY_UNKNOWN_VIEW", {"view_id": view_id})
            if not self._positive_int(seq) or seq not in view["journal"]:
                self._reject("REPLAY_EVENT_NOT_FOUND",
                             {"view_id": view_id, "seq": seq})
            return copy.deepcopy(view["journal"][seq])

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats.get(key, 0)) for key in STATS_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STATS_KEYS}

    def snapshot(self) -> dict:
        with self._lock:
            view_state = {}
            sample_state = {}
            journal_state = {}
            source_state = {}

            for view_id, view in self._views.items():
                view_state[view_id] = {
                    "view_id": view["view_id"],
                    "kind": view["kind"],
                    "team": view["team"],
                    "owner": view["owner"],
                    "version": view["version"],
                    "cursor": view["cursor"],
                }
                sample_state[view_id] = copy.deepcopy(view["samples"])
                journal_state[view_id] = copy.deepcopy(view["journal"])
                source_state[view_id] = copy.deepcopy(view["sources"])

            state = {
                "version": self.SNAPSHOT_VERSION,
                "limits": {
                    "view_limit": self.view_limit,
                    "entity_limit": self.entity_limit,
                    "event_limit": self.event_limit,
                    "sample_limit": self.sample_limit,
                },
                "stats": self.stats(),
                "views": view_state,
                "entities": copy.deepcopy(self._entities),
                "samples": sample_state,
                "vision": copy.deepcopy(sample_state),
                "journal": journal_state,
                "sources": source_state,
            }
            return copy.deepcopy(state)

    @staticmethod
    def _snapshot_seq(key) -> int | None:
        if ReplayViewProjector._positive_int(key):
            return key
        if isinstance(key, str):
            try:
                value = int(key)
            except (TypeError, ValueError):
                return None
            if ReplayViewProjector._positive_int(value):
                return value
        return None

    def _parse_snapshot(self, state) -> dict:
        if not isinstance(state, dict):
            self._invalid_snapshot("state")
        version = state.get("version")
        if type(version) is not int or version != self.SNAPSHOT_VERSION:
            self._invalid_snapshot({"field": "version"})

        limits_raw = state.get("limits", {})
        if not isinstance(limits_raw, dict):
            self._invalid_snapshot({"field": "limits"})

        def limit_value(name: str, current: int) -> int:
            if name not in limits_raw:
                return current
            value = limits_raw[name]
            if not self._positive_int(value):
                self._invalid_snapshot({"field": f"limits.{name}"})
            return value

        view_limit = limit_value("view_limit", self.view_limit)
        entity_limit = limit_value("entity_limit", self.entity_limit)
        event_limit = limit_value("event_limit", self.event_limit)
        sample_limit = limit_value("sample_limit", self.sample_limit)

        views_raw = state.get("views")
        entities_raw = state.get("entities")
        journal_raw = state.get("journal")
        stats_raw = state.get("stats")
        samples_raw = state.get("samples")
        if samples_raw is None:
            samples_raw = state.get("vision", {})
        sources_raw = state.get("sources", {})

        if not isinstance(views_raw, dict):
            self._invalid_snapshot({"field": "views"})
        if not isinstance(entities_raw, dict):
            self._invalid_snapshot({"field": "entities"})
        if not isinstance(journal_raw, dict):
            self._invalid_snapshot({"field": "journal"})
        if not isinstance(stats_raw, dict):
            self._invalid_snapshot({"field": "stats"})
        if not isinstance(samples_raw, dict):
            self._invalid_snapshot({"field": "samples"})
        if not isinstance(sources_raw, dict):
            self._invalid_snapshot({"field": "sources"})

        entities = {}
        for entity_id, entity in entities_raw.items():
            if not self._nonempty_str(entity_id) or not isinstance(entity, dict):
                self._invalid_snapshot({"entity_id": entity_id})
            if "entity_id" in entity and entity["entity_id"] != entity_id:
                self._invalid_snapshot({"entity_id": entity_id})
            team = entity.get("team")
            category = entity.get("category", "unit")
            if not self._nonempty_str(team) or category not in CATEGORIES:
                self._invalid_snapshot({"entity_id": entity_id})
            entities[entity_id] = {
                "entity_id": entity_id,
                "team": team,
                "category": category,
            }
        if len(entities) > entity_limit:
            self._invalid_snapshot({"field": "entity_limit"})

        if set(samples_raw) - set(views_raw):
            self._invalid_snapshot({"field": "samples"})

        views = {}
        for view_id, meta in views_raw.items():
            if not self._nonempty_str(view_id) or not isinstance(meta, dict):
                self._invalid_snapshot({"view_id": view_id})
            if "view_id" in meta and meta["view_id"] != view_id:
                self._invalid_snapshot({"view_id": view_id})

            kind = meta.get("kind", "player")
            team = meta.get("team")
            owner = meta.get("owner")
            version = meta.get("version", 1)
            if kind not in VIEW_KINDS or not self._positive_int(version):
                self._invalid_snapshot({"view_id": view_id})
            if kind == "player" and (
                    not self._nonempty_str(team) or not self._nonempty_str(owner)):
                self._invalid_snapshot({"view_id": view_id})
            if (kind == "observer" and team is not None
                    and not self._nonempty_str(team)):
                self._invalid_snapshot({"view_id": view_id})

            cursor = meta.get("cursor")
            if cursor is not None and not self._non_negative_int(cursor):
                self._invalid_snapshot({"field": "cursor"})

            raw_samples = meta.get("samples", samples_raw.get(view_id, []))
            if not isinstance(raw_samples, list):
                self._invalid_snapshot({"field": "samples"})

            samples = []
            times = []
            last_at = None
            for sample in raw_samples:
                if not isinstance(sample, dict):
                    self._invalid_snapshot({"field": "samples"})
                at = sample.get("at")
                visible = sample.get("visible")
                if not self._non_negative_int(at) or not isinstance(visible, list):
                    self._invalid_snapshot({"field": "samples"})
                if last_at is not None and at <= last_at:
                    self._invalid_snapshot({"field": "samples.at"})
                if any(not self._nonempty_str(entity_id)
                       or entity_id not in entities for entity_id in visible):
                    self._invalid_snapshot({"field": "samples.visible"})
                ordered = sorted(set(visible))
                samples.append({"at": at, "visible": ordered})
                times.append(at)
                last_at = at

            if len(samples) > sample_limit:
                self._invalid_snapshot({"field": "sample_limit"})

            views[view_id] = {
                "view_id": view_id,
                "kind": kind,
                "team": team,
                "owner": owner,
                "version": version,
                "cursor": cursor,
                "samples": samples,
                "times": times,
                "journal": {},
                "sources": {},
            }

        if len(views) > view_limit:
            self._invalid_snapshot({"field": "view_limit"})

        if set(journal_raw) - set(views) or set(sources_raw) - set(views):
            self._invalid_snapshot({"field": "journal"})

        for key in STATS_KEYS:
            if key not in stats_raw or not self._non_negative_int(stats_raw[key]):
                self._invalid_snapshot({"field": f"stats.{key}"})

        for view_id, raw_journal in journal_raw.items():
            if view_id not in views or not isinstance(raw_journal, dict):
                self._invalid_snapshot({"view_id": view_id})
            view = views[view_id]
            journal = view["journal"]
            for raw_seq, record in raw_journal.items():
                seq = self._snapshot_seq(raw_seq)
                if seq is None or seq in journal:
                    self._invalid_snapshot({"seq": raw_seq})
                if not isinstance(record, dict) or record.get("seq") != seq:
                    self._invalid_snapshot({"seq": raw_seq})
                if not self._non_negative_int(record.get("at")):
                    self._invalid_snapshot({"seq": seq})
                if record.get("type") not in EVENT_TYPES:
                    self._invalid_snapshot({"seq": seq})
                if record.get("status") not in STORED_STATUSES:
                    self._invalid_snapshot({"seq": seq})
                if not isinstance(record.get("reason", ""), str):
                    self._invalid_snapshot({"seq": seq})
                if not isinstance(record.get("payload"), dict):
                    self._invalid_snapshot({"seq": seq})
                if record["type"] not in SPEECH_TYPES:
                    entity_id = record.get("entity")
                    if (not self._nonempty_str(entity_id)
                            or entity_id not in entities):
                        self._invalid_snapshot({"seq": seq})
                journal[seq] = copy.deepcopy(record)

            if len(journal) > event_limit:
                self._invalid_snapshot({"field": "event_limit"})

            raw_sources = sources_raw.get(view_id, {})
            if not isinstance(raw_sources, dict):
                self._invalid_snapshot({"view_id": view_id})
            for raw_seq, source in raw_sources.items():
                seq = self._snapshot_seq(raw_seq)
                if seq is None or not isinstance(source, dict):
                    self._invalid_snapshot({"seq": raw_seq})
                if seq not in journal:
                    self._invalid_snapshot({"seq": seq})
                if source.get("seq") != seq or source.get("type") not in EVENT_TYPES:
                    self._invalid_snapshot({"seq": seq})
                view["sources"][seq] = copy.deepcopy(source)

            for seq, record in journal.items():
                if seq not in view["sources"]:
                    reconstructed = copy.deepcopy(record)
                    reconstructed.pop("status", None)
                    reconstructed.pop("reason", None)
                    view["sources"][seq] = reconstructed

        stats = {key: int(stats_raw[key]) for key in STATS_KEYS}
        return {
            "view_limit": view_limit,
            "entity_limit": entity_limit,
            "event_limit": event_limit,
            "sample_limit": sample_limit,
            "views": views,
            "entities": entities,
            "stats": stats,
        }

    def restore(self, state) -> None:
        with self._lock:
            restored = self._parse_snapshot(state)
            self.view_limit = restored["view_limit"]
            self.entity_limit = restored["entity_limit"]
            self.event_limit = restored["event_limit"]
            self.sample_limit = restored["sample_limit"]
            self._views = restored["views"]
            self._entities = restored["entities"]
            self._stats = restored["stats"]
