"""Frame desync localizer over reported per-peer entity digests."""

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping

STATS_KEYS = (
    "reports",
    "duplicates",
    "conflicts",
    "matched",
    "diverged",
    "insufficient",
    "rejected",
    "evidence_dropped",
    "quarantined",
)

PEER_STATES = ("active", "quarantined")
SNAPSHOT_VERSION = 1


class DesyncError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _error(code: str, detail: object = None) -> DesyncError:
    return DesyncError(code, detail)


class DesyncLocalizer:
    """Compares peer frame digests and pinpoints the first divergent frame."""

    def __init__(self, *, peer_limit: int = 16, frame_limit: int = 1000,
                 evidence_limit: int = 256, clock=None) -> None:
        self.peer_limit = int(peer_limit)
        self.frame_limit = int(frame_limit)
        self.evidence_limit = int(evidence_limit)
        if min(self.peer_limit, self.frame_limit, self.evidence_limit) < 0:
            raise _error("DESYNC_INVALID_LIMIT", "limits must be non-negative")
        self._clock = clock
        self._lock = threading.RLock()
        self._peers: dict[str, dict] = {}
        self._digests: dict[str, dict[str, dict[int, dict]]] = {}
        self._evidence: list[dict] = []
        self._stats = {key: 0 for key in STATS_KEYS}

    @staticmethod
    def _valid_peer_id(peer_id: object) -> bool:
        return isinstance(peer_id, str) and bool(peer_id)

    @staticmethod
    def _valid_match_id(match_id: object) -> bool:
        return isinstance(match_id, str)

    @staticmethod
    def _valid_frame(frame: object) -> bool:
        return (
            not isinstance(frame, bool)
            and isinstance(frame, int)
            and frame >= 0
        )

    @staticmethod
    def _scalar_text(value: object) -> str:
        if value is None:
            raise _error("DESYNC_BAD_DIGEST", "entity value cannot be null")
        if isinstance(value, bool):
            return str(value)
        if isinstance(value, (str, int, float)):
            return str(value)
        raise _error("DESYNC_BAD_DIGEST", "entity value must be a scalar")

    @classmethod
    def _entities_from_digest(cls, digest: object) -> dict[str, str]:
        entities: dict[str, str] = {}

        if isinstance(digest, str):
            if not digest:
                raise _error("DESYNC_BAD_DIGEST", "digest is empty")
            for part in digest.split("|"):
                if "=" not in part:
                    raise _error("DESYNC_BAD_DIGEST", "malformed digest entry")
                entity, raw_value = part.split("=", 1)
                if not isinstance(entity, str) or not entity:
                    raise _error("DESYNC_BAD_DIGEST", "entity name is empty")
                if entity in entities:
                    raise _error("DESYNC_BAD_DIGEST", "duplicate entity")
                entities[entity] = raw_value
        elif isinstance(digest, Mapping):
            if not digest:
                raise _error("DESYNC_BAD_DIGEST", "digest is empty")
            for entity, value in digest.items():
                if not isinstance(entity, str) or not entity:
                    raise _error("DESYNC_BAD_DIGEST", "entity name is empty")
                entities[entity] = cls._scalar_text(value)
        else:
            raise _error("DESYNC_BAD_DIGEST", "digest must be a mapping or string")

        if not entities:
            raise _error("DESYNC_BAD_DIGEST", "digest is empty")
        return entities

    @classmethod
    def _canonicalize_digest(cls, digest: object) -> tuple[str, dict[str, str]]:
        entities = cls._entities_from_digest(digest)
        canonical = "|".join(
            f"{entity}={entities[entity]}" for entity in sorted(entities)
        )
        return canonical, dict(entities)

    def _now(self):
        if self._clock is None:
            return None
        if callable(self._clock):
            return self._clock()
        now = getattr(self._clock, "now", None)
        if callable(now):
            return now()
        return self._clock

    def register_peer(self, peer_id: str, *, endpoint: str | None = None) -> dict:
        with self._lock:
            if not self._valid_peer_id(peer_id):
                self._stats["rejected"] += 1
                raise _error("DESYNC_INVALID_PEER", repr(peer_id))

            record = self._peers.get(peer_id)
            if record is not None:
                if record.get("endpoint") != endpoint:
                    self._stats["conflicts"] += 1
                    raise _error(
                        "DESYNC_PEER_CONFLICT",
                        {
                            "peer": peer_id,
                            "endpoint": endpoint,
                            "existing_endpoint": record.get("endpoint"),
                        },
                    )
                return {
                    "peer": peer_id,
                    "endpoint": record.get("endpoint"),
                    "state": record.get("state", "active"),
                    "status": "duplicate",
                }

            if len(self._peers) >= self.peer_limit:
                self._stats["rejected"] += 1
                raise _error(
                    "DESYNC_PEER_LIMIT",
                    {"peer_limit": self.peer_limit, "peer": peer_id},
                )

            record = {
                "endpoint": endpoint,
                "state": "active",
                "reason": "",
            }
            self._peers[peer_id] = record
            return {
                "peer": peer_id,
                "endpoint": endpoint,
                "state": "active",
                "status": "registered",
            }

    def quarantine_peer(self, peer_id: str, *, reason: str = "") -> dict:
        with self._lock:
            if not self._valid_peer_id(peer_id):
                raise _error("DESYNC_INVALID_PEER", repr(peer_id))

            record = self._peers.get(peer_id)
            if record is None:
                raise _error("DESYNC_UNKNOWN_PEER", repr(peer_id))

            if record.get("state") == "quarantined":
                return {
                    "peer": peer_id,
                    "state": "quarantined",
                    "reason": record.get("reason", ""),
                    "status": "already_quarantined",
                }

            record["state"] = "quarantined"
            record["reason"] = reason
            self._stats["quarantined"] += 1
            return {
                "peer": peer_id,
                "state": "quarantined",
                "reason": reason,
                "status": "quarantined",
            }

    def peer_state(self, peer_id: str) -> str:
        with self._lock:
            record = self._peers.get(peer_id)
            if record is None:
                return "unknown"
            return str(record.get("state", "unknown"))

    def report_frame(self, peer_id: str, *, match_id: str, frame: int,
                     digest, inputs=None) -> dict:
        with self._lock:
            if not self._valid_peer_id(peer_id):
                self._stats["rejected"] += 1
                raise _error("DESYNC_INVALID_PEER", repr(peer_id))

            peer = self._peers.get(peer_id)
            if peer is None:
                self._stats["rejected"] += 1
                raise _error("DESYNC_UNKNOWN_PEER", repr(peer_id))
            if peer.get("state") == "quarantined":
                self._stats["rejected"] += 1
                raise _error("DESYNC_PEER_QUARANTINED", repr(peer_id))

            if not self._valid_match_id(match_id):
                self._stats["rejected"] += 1
                raise _error("DESYNC_INVALID_MATCH", repr(match_id))
            if not self._valid_frame(frame):
                self._stats["rejected"] += 1
                raise _error("DESYNC_INVALID_FRAME", repr(frame))

            try:
                canonical, entities = self._canonicalize_digest(digest)
            except DesyncError:
                self._stats["rejected"] += 1
                raise

            match_frames = self._digests.get(match_id)
            peer_frames = None
            if match_frames is not None:
                peer_frames = match_frames.get(peer_id)
            existing = None
            if peer_frames is not None:
                existing = peer_frames.get(frame)

            if existing is not None:
                if existing.get("digest") == canonical:
                    self._stats["duplicates"] += 1
                    return {
                        "peer": peer_id,
                        "match_id": match_id,
                        "frame": frame,
                        "digest": canonical,
                        "frames": len(peer_frames),
                        "status": "duplicate",
                    }

                self._stats["conflicts"] += 1
                raise _error(
                    "DESYNC_FRAME_CONFLICT",
                    {
                        "peer": peer_id,
                        "match_id": match_id,
                        "frame": frame,
                        "digest": canonical,
                        "existing_digest": existing.get("digest"),
                    },
                )

            frame_count = len(peer_frames) if peer_frames is not None else 0
            if frame_count >= self.frame_limit:
                self._stats["rejected"] += 1
                raise _error(
                    "DESYNC_FRAME_LIMIT",
                    {
                        "peer": peer_id,
                        "match_id": match_id,
                        "frame_limit": self.frame_limit,
                    },
                )

            if match_frames is None:
                match_frames = {}
                self._digests[match_id] = match_frames
            if peer_frames is None:
                peer_frames = {}
                match_frames[peer_id] = peer_frames

            record = {"digest": canonical, "entities": entities}
            peer_frames[frame] = record

            try:
                stored_inputs = copy.deepcopy(inputs)
            except Exception:
                stored_inputs = inputs
            evidence_entry = {
                "peer": peer_id,
                "match_id": match_id,
                "frame": frame,
                "digest": canonical,
                "inputs": stored_inputs,
            }
            moment = self._now()
            if moment is not None:
                evidence_entry["at"] = moment

            self._evidence.append(evidence_entry)
            if len(self._evidence) > self.evidence_limit:
                overflow = len(self._evidence) - self.evidence_limit
                if overflow > 0:
                    del self._evidence[:overflow]
                    self._stats["evidence_dropped"] += overflow

            self._stats["reports"] += 1
            return {
                "peer": peer_id,
                "match_id": match_id,
                "frame": frame,
                "digest": canonical,
                "frames": len(peer_frames),
                "status": "recorded",
            }

    def compare(self, *, frame: int, left: str, right: str,
                match_id: str = "default") -> dict:
        with self._lock:
            self._validate_comparison_request(
                frame=frame, left=left, right=right, match_id=match_id
            )
            result = self._evaluate_frame_locked(
                frame=frame, left=left, right=right, match_id=match_id
            )
            self._count_compare_locked(result["status"])
            return result

    def first_divergence(self, *, left: str, right: str, start: int = 0,
                         end: int | None = None,
                         match_id: str = "default") -> dict:
        with self._lock:
            self._validate_range_request(
                left=left, right=right, start=start, end=end,
                match_id=match_id,
            )

            frames = self._pair_frames_locked(
                left=left, right=right, start=start, end=end,
                match_id=match_id,
            )
            scanned = 0
            for frame in frames:
                scanned += 1
                result = self._evaluate_frame_locked(
                    frame=frame, left=left, right=right, match_id=match_id
                )
                self._count_compare_locked(result["status"])
                if result["status"] == "diverged":
                    return {
                        "frame": frame,
                        "status": "found",
                        "scanned": scanned,
                        "entities": list(result["entities"]),
                    }

            return {
                "frame": None,
                "status": "none",
                "scanned": scanned,
                "entities": [],
            }

    def bisect_range(self, *, left: str, right: str, start: int,
                     end: int, match_id: str = "default") -> dict:
        with self._lock:
            self._validate_range_request(
                left=left, right=right, start=start, end=end,
                match_id=match_id,
            )

            cache: dict[int, dict] = {}
            probes = 0

            def probe(frame: int) -> dict:
                nonlocal probes
                if frame not in cache:
                    probes += 1
                    cache[frame] = self._evaluate_frame_locked(
                        frame=frame, left=left, right=right,
                        match_id=match_id,
                    )
                    self._count_compare_locked(cache[frame]["status"])
                return cache[frame]

            start_result = probe(start)
            if start_result["status"] == "diverged":
                return {
                    "frame": start,
                    "status": "found",
                    "probes": probes,
                    "entities": list(start_result["entities"]),
                }

            comparable_frames = self._comparable_frames_locked(
                left=left,
                right=right,
                start=start,
                end=end,
                match_id=match_id,
            )
            search_frames = [frame for frame in comparable_frames if frame > start]

            lo = 0
            hi = len(search_frames)
            while lo < hi:
                mid = (lo + hi) // 2
                frame = search_frames[mid]
                result = probe(frame)
                if result["status"] == "diverged":
                    hi = mid
                else:
                    lo = mid + 1

            if lo < len(search_frames):
                frame = search_frames[lo]
                result = probe(frame)
                if result["status"] == "diverged":
                    return {
                        "frame": frame,
                        "status": "found",
                        "probes": probes,
                        "entities": list(result["entities"]),
                    }

            incomplete_frame = self._first_incomplete_frame_locked(
                left=left,
                right=right,
                start=start,
                end=end,
                match_id=match_id,
            )
            if incomplete_frame is not None:
                reason = self._missing_reason_locked(
                    frame=incomplete_frame,
                    left=left,
                    right=right,
                    match_id=match_id,
                )
                return {
                    "frame": incomplete_frame,
                    "status": "insufficient",
                    "probes": probes,
                    "entities": [],
                    "reason": reason,
                }

            return {
                "frame": None,
                "status": "none",
                "probes": probes,
                "entities": [],
            }

    def evidence(self, *, match_id: str | None = None,
                 limit: int | None = None) -> dict:
        with self._lock:
            entries = self._evidence
            if match_id is not None:
                if not self._valid_match_id(match_id):
                    raise _error("DESYNC_INVALID_MATCH", repr(match_id))
                entries = [
                    entry for entry in entries
                    if entry.get("match_id") == match_id
                ]

            if limit is not None:
                if (
                    isinstance(limit, bool)
                    or not isinstance(limit, int)
                    or limit < 0
                ):
                    raise _error("DESYNC_INVALID_LIMIT", repr(limit))
                if limit == 0:
                    entries = []
                elif limit < len(entries):
                    entries = entries[-limit:]

            return {
                "match_id": match_id,
                "count": len(entries),
                "entries": copy.deepcopy(entries),
                "dropped": self._stats["evidence_dropped"],
            }

    def clear_match(self, match_id: str) -> int:
        with self._lock:
            if not self._valid_match_id(match_id):
                raise _error("DESYNC_INVALID_MATCH", repr(match_id))

            match_frames = self._digests.pop(match_id, None)
            removed = 0
            if match_frames is not None:
                removed = sum(
                    len(peer_frames)
                    for peer_frames in match_frames.values()
                    if isinstance(peer_frames, dict)
                )

            self._evidence = [
                entry for entry in self._evidence
                if entry.get("match_id") != match_id
            ]
            return removed

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "peers": copy.deepcopy(self._peers),
                "digests": copy.deepcopy(self._digests),
                "evidence": copy.deepcopy(self._evidence),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state) -> None:
        with self._lock:
            restored = self._validate_snapshot(state)
            self._peers = restored["peers"]
            self._digests = restored["digests"]
            self._evidence = restored["evidence"]
            self._stats = restored["stats"]

    def _validate_comparison_request(
        self, *, frame: object, left: object, right: object,
        match_id: object,
    ) -> None:
        if left == right:
            raise _error(
                "DESYNC_SAME_PEER",
                {"left": repr(left), "right": repr(right)},
            )

        for peer_id in (left, right):
            if not self._valid_peer_id(peer_id):
                raise _error("DESYNC_INVALID_PEER", repr(peer_id))
            record = self._peers.get(peer_id)
            if record is None:
                raise _error("DESYNC_UNKNOWN_PEER", repr(peer_id))
            if record.get("state") == "quarantined":
                raise _error("DESYNC_PEER_QUARANTINED", repr(peer_id))

        if not self._valid_match_id(match_id):
            raise _error("DESYNC_INVALID_MATCH", repr(match_id))
        if not self._valid_frame(frame):
            raise _error("DESYNC_INVALID_FRAME", repr(frame))

    def _validate_range_request(
        self, *, left: object, right: object, start: object,
        end: object, match_id: object,
    ) -> None:
        if left == right:
            raise _error("DESYNC_SAME_PEER", {"left": left, "right": right})
        for peer_id in (left, right):
            if not self._valid_peer_id(peer_id):
                raise _error("DESYNC_INVALID_PEER", repr(peer_id))
            record = self._peers.get(peer_id)
            if record is None:
                raise _error("DESYNC_UNKNOWN_PEER", repr(peer_id))
            if record.get("state") == "quarantined":
                raise _error("DESYNC_PEER_QUARANTINED", repr(peer_id))

        if not self._valid_match_id(match_id):
            raise _error("DESYNC_INVALID_MATCH", repr(match_id))
        if not self._valid_frame(start):
            raise _error("DESYNC_INVALID_FRAME", repr(start))
        if end is not None:
            if not self._valid_frame(end):
                raise _error("DESYNC_INVALID_FRAME", repr(end))
            if start > end:
                raise _error(
                    "DESYNC_INVALID_RANGE",
                    {"start": start, "end": end},
                )

    def _evaluate_frame_locked(
        self, *, frame: int, left: str, right: str, match_id: str,
    ) -> dict:
        left_record = self._get_frame_record_locked(match_id, left, frame)
        right_record = self._get_frame_record_locked(match_id, right, frame)

        if left_record is None or right_record is None:
            if left_record is None and right_record is None:
                reason = "missing_both"
            elif left_record is None:
                reason = "missing_left"
            else:
                reason = "missing_right"
            return {
                "status": "insufficient",
                "reason": reason,
                "entities": [],
                "details": {},
            }

        left_entities = left_record.get("entities", {})
        right_entities = right_record.get("entities", {})
        if not isinstance(left_entities, dict):
            left_entities = {}
        if not isinstance(right_entities, dict):
            right_entities = {}

        entities: list[str] = []
        details: dict[str, dict[str, object]] = {}
        for entity in sorted(set(left_entities) | set(right_entities)):
            left_value = left_entities.get(entity)
            right_value = right_entities.get(entity)
            if left_value == right_value:
                continue
            entities.append(entity)
            details[entity] = {
                "left": left_value if entity in left_entities else None,
                "right": right_value if entity in right_entities else None,
            }

        if entities:
            return {
                "status": "diverged",
                "reason": "",
                "entities": entities,
                "details": details,
            }
        return {
            "status": "match",
            "reason": "",
            "entities": [],
            "details": {},
        }

    def _get_frame_record_locked(
        self, match_id: str, peer_id: str, frame: int,
    ) -> dict | None:
        return (
            self._digests.get(match_id, {})
            .get(peer_id, {})
            .get(frame)
        )

    def _missing_reason_locked(
        self, *, frame: int, left: str, right: str, match_id: str,
    ) -> str:
        left_record = self._get_frame_record_locked(match_id, left, frame)
        right_record = self._get_frame_record_locked(match_id, right, frame)
        if left_record is None and right_record is None:
            return "missing_both"
        if left_record is None:
            return "missing_left"
        return "missing_right"

    def _count_compare_locked(self, status: str) -> None:
        if status == "match":
            self._stats["matched"] += 1
        elif status == "diverged":
            self._stats["diverged"] += 1
        else:
            self._stats["insufficient"] += 1

    def _pair_frame_map_locked(
        self, *, left: str, right: str, match_id: str,
    ) -> tuple[dict[int, dict], dict[int, dict]]:
        match_frames = self._digests.get(match_id, {})
        return (
            match_frames.get(left, {}),
            match_frames.get(right, {}),
        )

    def _pair_frames_locked(
        self, *, left: str, right: str, start: int, end: int | None,
        match_id: str,
    ) -> list[int]:
        left_frames, right_frames = self._pair_frame_map_locked(
            left=left, right=right, match_id=match_id
        )
        frames = set(left_frames) | set(right_frames)
        return sorted(
            frame for frame in frames
            if frame >= start and (end is None or frame <= end)
        )

    def _comparable_frames_locked(
        self, *, left: str, right: str, start: int, end: int,
        match_id: str,
    ) -> list[int]:
        left_frames, right_frames = self._pair_frame_map_locked(
            left=left, right=right, match_id=match_id
        )
        frames = set(left_frames) & set(right_frames)
        return sorted(
            frame for frame in frames if start <= frame <= end
        )

    def _first_incomplete_frame_locked(
        self, *, left: str, right: str, start: int, end: int,
        match_id: str,
    ) -> int | None:
        left_frames, right_frames = self._pair_frame_map_locked(
            left=left, right=right, match_id=match_id
        )
        for frame in sorted(set(left_frames) | set(right_frames)):
            if frame < start or frame > end:
                continue
            if frame not in left_frames or frame not in right_frames:
                return frame
        return None

    @classmethod
    def _validate_snapshot(cls, state: object) -> dict:
        invalid = lambda detail=None: _error(  # noqa: E731
            "DESYNC_SNAPSHOT_INVALID", detail
        )

        if not isinstance(state, dict):
            raise invalid("snapshot must be an object")
        if state.get("version") != SNAPSHOT_VERSION:
            raise invalid("unsupported snapshot version")

        for key in ("peers", "digests", "evidence", "stats"):
            if key not in state:
                raise invalid(f"missing snapshot field: {key}")

        peers = cls._validate_snapshot_peers(state["peers"], invalid)
        digests = cls._validate_snapshot_digests(state["digests"], invalid)
        evidence = cls._validate_snapshot_evidence(
            state["evidence"], invalid
        )
        stats = cls._validate_snapshot_stats(state["stats"], invalid)

        return {
            "peers": peers,
            "digests": digests,
            "evidence": evidence,
            "stats": stats,
        }

    @classmethod
    def _validate_snapshot_peers(cls, value, invalid) -> dict:
        if not isinstance(value, dict):
            raise invalid("peers must be an object")

        peers = {}
        for peer_id, record in value.items():
            if not cls._valid_peer_id(peer_id) or not isinstance(record, dict):
                raise invalid("invalid peer record")
            if "state" not in record:
                raise invalid("missing peer state")
            state = record.get("state")
            if state not in PEER_STATES:
                raise invalid("invalid peer state")
            if "endpoint" not in record:
                raise invalid("missing peer endpoint")
            endpoint = record.get("endpoint")
            if endpoint is not None and not isinstance(endpoint, str):
                raise invalid("invalid peer endpoint")
            if "reason" not in record:
                raise invalid("missing quarantine reason")
            reason = record.get("reason")
            if not isinstance(reason, str):
                raise invalid("invalid quarantine reason")
            peers[peer_id] = {
                "endpoint": endpoint,
                "state": state,
                "reason": reason,
            }
        return peers

    @classmethod
    def _coerce_snapshot_frame(cls, frame, invalid) -> int:
        if isinstance(frame, bool):
            raise invalid("frame must be an integer")
        if isinstance(frame, int):
            value = frame
        elif isinstance(frame, str):
            try:
                value = int(frame)
            except (TypeError, ValueError) as exc:
                raise invalid("frame must be an integer") from exc
        else:
            raise invalid("frame must be an integer")
        if value < 0:
            raise invalid("frame must be non-negative")
        return value

    @classmethod
    def _validate_snapshot_digests(cls, value, invalid) -> dict:
        if not isinstance(value, dict):
            raise invalid("digests must be an object")

        digests: dict[str, dict[str, dict[int, dict]]] = {}
        for match_id, peers in value.items():
            if not cls._valid_match_id(match_id) or not isinstance(peers, dict):
                raise invalid("invalid match digest collection")
            digests[match_id] = {}

            for peer_id, frames in peers.items():
                if not cls._valid_peer_id(peer_id) or not isinstance(frames, dict):
                    raise invalid("invalid peer digest collection")
                digests[match_id][peer_id] = {}

                for raw_frame, raw_record in frames.items():
                    frame = cls._coerce_snapshot_frame(raw_frame, invalid)
                    if isinstance(raw_record, str):
                        canonical, entities = cls._canonical_or_invalid(
                            raw_record, invalid
                        )
                    elif isinstance(raw_record, dict):
                        if "digest" not in raw_record:
                            raise invalid("missing digest")
                        if "entities" not in raw_record or not isinstance(
                            raw_record.get("entities"), dict
                        ):
                            raise invalid("missing digest entities")
                        digest = raw_record.get("digest")
                        canonical, parsed_entities = cls._canonical_or_invalid(
                            digest, invalid
                        )
                        entities = raw_record.get("entities")
                        if entities != parsed_entities:
                            raise invalid("digest entities do not match digest")
                    else:
                        raise invalid("invalid frame digest record")

                    digests[match_id][peer_id][frame] = {
                        "digest": canonical,
                        "entities": dict(entities),
                    }
        return digests

    @classmethod
    def _canonical_or_invalid(cls, digest, invalid):
        try:
            return cls._canonicalize_digest(digest)
        except DesyncError as exc:
            raise invalid("invalid persisted digest") from exc

    @classmethod
    def _validate_snapshot_evidence(cls, value, invalid) -> list[dict]:
        if not isinstance(value, list):
            raise invalid("evidence must be a list")

        entries = []
        for raw_entry in value:
            if not isinstance(raw_entry, dict):
                raise invalid("evidence entry must be an object")
            peer_id = raw_entry.get("peer")
            match_id = raw_entry.get("match_id")
            if not cls._valid_peer_id(peer_id):
                raise invalid("invalid evidence peer")
            if not cls._valid_match_id(match_id):
                raise invalid("invalid evidence match")
            if "inputs" not in raw_entry:
                raise invalid("missing evidence inputs")
            frame = cls._coerce_snapshot_frame(raw_entry.get("frame"), invalid)
            canonical, _ = cls._canonical_or_invalid(
                raw_entry.get("digest"), invalid
            )

            entry = copy.deepcopy(raw_entry)
            entry["peer"] = peer_id
            entry["match_id"] = match_id
            entry["frame"] = frame
            entry["digest"] = canonical
            if "inputs" not in entry:
                entry["inputs"] = None
            entries.append(entry)
        return entries

    @classmethod
    def _validate_snapshot_stats(cls, value, invalid) -> dict:
        if not isinstance(value, dict):
            raise invalid("stats must be an object")

        stats = {}
        for key in STATS_KEYS:
            number = value.get(key)
            if (
                isinstance(number, bool)
                or not isinstance(number, int)
                or number < 0
            ):
                raise invalid(f"missing or invalid stat: {key}")
            stats[key] = number
        return stats
