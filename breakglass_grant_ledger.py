"""Break-glass grant ledger with review, action trail and retention."""

from __future__ import annotations

import copy
import math
import threading
import time


class BreakglassError(ValueError):
    """Refusal raised while managing a break-glass grant."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_GRANT_STATES = ("active", "expired", "closed")
_STAT_KEYS = (
    "activations",
    "acknowledgements",
    "uses",
    "closes",
    "swept",
    "unreviewed_uses",
)
_SNAPSHOT_VERSION = 1


def _is_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _valid_scope(scope: object) -> bool:
    if not isinstance(scope, str) or not scope or scope != scope.strip():
        return False
    for segment in scope.split("/"):
        if not segment or any(char.isspace() for char in segment):
            return False
        parts = segment.split(":")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            return False
    return True


def _scope_within(scope: str, parent: str) -> bool:
    return scope == parent or scope.startswith(parent + "/")


def _valid_action_scope(scope: object) -> bool:
    if not isinstance(scope, str) or not scope or scope != scope.strip():
        return False
    return not any(
        not segment or any(char.isspace() for char in segment)
        for segment in scope.split("/")
    )


def _grant_id_number(grant_id: object) -> int | None:
    if not isinstance(grant_id, str) or not grant_id.startswith("bg-"):
        return None
    suffix = grant_id[3:]
    if not suffix.isdigit():
        return None
    number = int(suffix)
    return number if number > 0 else None


class BreakglassLedger:
    """Track emergency grants, reviewers, actions and cleanup."""

    def __init__(self, *, default_ttl: int = 600, max_ttl: int = 3600,
                 closed_retention: int = 86400, clock=None) -> None:
        self.default_ttl = default_ttl
        self.max_ttl = max_ttl
        self.closed_retention = closed_retention
        self._clock = clock
        self._lock = threading.RLock()
        self._grants: dict[str, dict] = {}
        self._audit: dict[str, list[dict]] = {}
        self._audit_meta: dict[str, dict] = {}
        self._audit_purge_at: dict[str, int | float] = {}
        self._next_id = 1
        self._next_seq: dict[str, int] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    def _current_time(self, now: int | float | None) -> int | float:
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return time.time()

    @staticmethod
    def _clean_operator(operator: object) -> str:
        if not isinstance(operator, str):
            raise BreakglassError("BREAKGLASS_OPERATOR_INVALID", operator)
        cleaned = operator.strip()
        if not cleaned:
            raise BreakglassError("BREAKGLASS_OPERATOR_INVALID", operator)
        return cleaned

    @staticmethod
    def _clean_incident(incident_id: object) -> str:
        if not isinstance(incident_id, str):
            raise BreakglassError("BREAKGLASS_INCIDENT_REQUIRED", incident_id)
        cleaned = incident_id.strip()
        if not cleaned:
            raise BreakglassError("BREAKGLASS_INCIDENT_REQUIRED", incident_id)
        return cleaned

    @staticmethod
    def _clean_scope(scope: object) -> str:
        if not _valid_scope(scope):
            raise BreakglassError("BREAKGLASS_SCOPE_INVALID", scope)
        return scope

    def _clean_ttl(self, ttl: object) -> int | float:
        if ttl is None:
            ttl = self.default_ttl
        if not _is_number(ttl) or ttl <= 0 or ttl > self.max_ttl:
            raise BreakglassError("BREAKGLASS_TTL_INVALID", ttl)
        return ttl

    @staticmethod
    def _clean_reviewer(reviewer: object) -> str:
        if not isinstance(reviewer, str) or not reviewer.strip():
            raise BreakglassError("BREAKGLASS_REVIEWER_REQUIRED", reviewer)
        return reviewer.strip()

    @staticmethod
    def _clean_note(note: object) -> str:
        if not isinstance(note, str) or not note.strip():
            raise BreakglassError("BREAKGLASS_NOTE_INVALID", note)
        return note.strip()

    @staticmethod
    def _clean_action(action: object) -> str:
        if not isinstance(action, str) or not action.strip():
            raise BreakglassError("BREAKGLASS_ACTION_INVALID", action)
        return action.strip()

    def _grant(self, grant_id: object) -> dict:
        if not isinstance(grant_id, str):
            raise BreakglassError("BREAKGLASS_GRANT_UNKNOWN", grant_id)
        grant = self._grants.get(grant_id)
        if grant is None:
            raise BreakglassError("BREAKGLASS_GRANT_UNKNOWN", grant_id)
        return grant

    @staticmethod
    def _expire_if_needed(grant: dict, now: int | float) -> None:
        if grant["state"] == "active" and now >= grant["expires_at"]:
            grant["state"] = "expired"

    @staticmethod
    def _activation_view(grant: dict) -> dict:
        return {
            "grant_id": grant["grant_id"],
            "operator": grant["operator"],
            "scope": grant["scope"],
            "incident_id": grant["incident_id"],
            "activated_at": grant["activated_at"],
            "expires_at": grant["expires_at"],
            "ttl": grant["ttl"],
            "acknowledged": grant["acknowledged"],
            "reviewer": grant["reviewer"],
            "note": grant["note"],
            "state": grant["state"],
        }

    @staticmethod
    def _grant_view(grant: dict) -> dict:
        view = BreakglassLedger._activation_view(grant)
        view["acknowledged_at"] = grant["acknowledged_at"]
        view["closed_at"] = grant["closed_at"]
        return view

    def _newest_id(self, grants: list[dict]) -> dict | None:
        if not grants:
            return None
        grants.sort(key=lambda item: _grant_id_number(item["grant_id"]) or 0)
        return grants[-1]

    def activate(self, operator: str, scope: str, incident_id: str,
                 ttl: int | None = None, now: int | None = None) -> dict:
        operator = self._clean_operator(operator)
        incident_id = self._clean_incident(incident_id)
        scope = self._clean_scope(scope)
        ttl = self._clean_ttl(ttl)

        with self._lock:
            at = self._current_time(now)
            candidates = [
                grant
                for grant in self._grants.values()
                if grant["incident_id"] == incident_id
                and grant["state"] in ("active", "expired")
            ]
            for candidate in candidates:
                self._expire_if_needed(candidate, at)

            existing = self._newest_id([
                grant for grant in candidates if grant["state"] == "active"
            ])
            if existing is not None:
                if existing["operator"] != operator:
                    raise BreakglassError(
                        "BREAKGLASS_INCIDENT_BUSY", incident_id
                    )
            else:
                existing = self._newest_id([
                    grant
                    for grant in candidates
                    if grant["state"] == "expired"
                    and grant["operator"] == operator
                ])

            if existing is not None:
                existing["expires_at"] = max(
                    existing["expires_at"], at + ttl
                )
                existing["ttl"] = ttl
                existing["state"] = "active"
                self._stats["activations"] += 1
                return self._activation_view(existing)

            grant_id = f"bg-{self._next_id}"
            self._next_id += 1
            grant = {
                "grant_id": grant_id,
                "operator": operator,
                "scope": scope,
                "incident_id": incident_id,
                "activated_at": at,
                "expires_at": at + ttl,
                "ttl": ttl,
                "acknowledged": False,
                "reviewer": None,
                "acknowledged_at": None,
                "note": None,
                "closed_at": None,
                "state": "active",
            }
            self._grants[grant_id] = grant
            self._audit[grant_id] = []
            self._next_seq[grant_id] = 1
            self._stats["activations"] += 1
            return self._activation_view(grant)

    def acknowledge(self, grant_id: str, reviewer: str,
                    now: int | None = None) -> dict:
        reviewer = self._clean_reviewer(reviewer)
        with self._lock:
            grant = self._grant(grant_id)
            if reviewer == grant["operator"]:
                raise BreakglassError("BREAKGLASS_SELF_REVIEW", reviewer)
            if grant["state"] == "closed":
                raise BreakglassError("BREAKGLASS_GRANT_CLOSED", grant_id)
            if grant["acknowledged"]:
                raise BreakglassError("BREAKGLASS_ALREADY_REVIEWED", grant_id)
            at = self._current_time(now)
            self._expire_if_needed(grant, at)
            if grant["state"] == "expired":
                raise BreakglassError("BREAKGLASS_GRANT_EXPIRED", grant_id)

            grant["acknowledged"] = True
            grant["reviewer"] = reviewer
            grant["acknowledged_at"] = at
            self._stats["acknowledgements"] += 1
            return {
                "acknowledged": True,
                "reviewer": reviewer,
                "acknowledged_at": at,
            }

    def use(self, grant_id: str, action: str, scope: str | None = None,
            now: int | None = None) -> dict:
        action = self._clean_action(action)
        with self._lock:
            grant = self._grant(grant_id)
            if grant["state"] == "closed":
                raise BreakglassError("BREAKGLASS_GRANT_CLOSED", grant_id)
            at = self._current_time(now)
            self._expire_if_needed(grant, at)
            if grant["state"] == "expired":
                raise BreakglassError("BREAKGLASS_GRANT_EXPIRED", grant_id)

            if scope is None:
                action_scope = grant["scope"]
            else:
                if not _valid_action_scope(scope):
                    raise BreakglassError("BREAKGLASS_SCOPE_INVALID", scope)
                action_scope = scope.strip()
                if not _scope_within(action_scope, grant["scope"]):
                    raise BreakglassError("BREAKGLASS_SCOPE_DENIED", scope)

            seq = self._next_seq.get(grant_id, 1)
            self._next_seq[grant_id] = seq + 1
            entries = self._audit.setdefault(grant_id, [])
            entries.append({
                "seq": seq,
                "at": at,
                "action": action,
                "scope": action_scope,
            })
            self._stats["uses"] += 1
            if not grant["acknowledged"]:
                self._stats["unreviewed_uses"] += 1
            return {
                "grant_id": grant_id,
                "seq": seq,
                "action": action,
                "scope": action_scope,
                "reviewed": grant["acknowledged"],
                "audit_size": len(entries),
            }

    def close(self, grant_id: str, note: str,
              now: int | None = None) -> dict:
        note = self._clean_note(note)
        with self._lock:
            grant = self._grant(grant_id)
            if grant["state"] == "closed":
                return {
                    "grant_id": grant_id,
                    "state": "closed",
                    "closed_at": grant["closed_at"],
                    "note": grant["note"],
                }
            at = self._current_time(now)
            grant["state"] = "closed"
            grant["closed_at"] = at
            grant["note"] = note
            self._stats["closes"] += 1
            return {
                "grant_id": grant_id,
                "state": "closed",
                "closed_at": at,
                "note": note,
            }

    def sweep(self, now: int) -> int:
        processed = 0
        with self._lock:
            for grant in self._grants.values():
                if (
                    grant["state"] == "active"
                    and now >= grant["expires_at"]
                ):
                    grant["state"] = "expired"
                    processed += 1

            purged: list[str] = []
            for grant_id, grant in self._grants.items():
                if (
                    grant["state"] == "closed"
                    and grant["closed_at"] is not None
                    and now >= grant["closed_at"] + self.closed_retention
                ):
                    self._audit_meta[grant_id] = copy.deepcopy(grant)
                    self._audit_purge_at[grant_id] = (
                        now + self.closed_retention
                    )
                    purged.append(grant_id)
                    processed += 1
            for grant_id in purged:
                del self._grants[grant_id]

            expired_trails = [
                grant_id
                for grant_id, purge_at in self._audit_purge_at.items()
                if now >= purge_at
            ]
            for grant_id in expired_trails:
                self._audit_meta.pop(grant_id, None)
                self._audit.pop(grant_id, None)
                self._audit_purge_at.pop(grant_id, None)
                self._next_seq.pop(grant_id, None)

            self._stats["swept"] += processed
        return processed

    def audit_trail(self, grant_id: str) -> dict:
        with self._lock:
            grant = self._grants.get(grant_id)
            if grant is None:
                grant = self._audit_meta.get(grant_id)
            if grant is None:
                raise BreakglassError("BREAKGLASS_GRANT_UNKNOWN", grant_id)
            return {
                "grant_id": grant["grant_id"],
                "incident_id": grant["incident_id"],
                "operator": grant["operator"],
                "state": grant["state"],
                "entries": copy.deepcopy(self._audit.get(grant_id, [])),
                "closed_at": grant["closed_at"],
                "note": grant["note"],
                "acknowledged": grant["acknowledged"],
                "reviewer": grant["reviewer"],
            }

    def active(self) -> list:
        with self._lock:
            grants = [
                grant
                for grant in self._grants.values()
                if grant["state"] == "active"
            ]
            grants.sort(
                key=lambda item: _grant_id_number(item["grant_id"]) or 0
            )
            return [self._grant_view(grant) for grant in grants]

    def get(self, grant_id: str) -> dict:
        with self._lock:
            return self._grant_view(self._grant(grant_id))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "grants": copy.deepcopy(self._grants),
                "audit": copy.deepcopy(self._audit),
                "audit_meta": copy.deepcopy(self._audit_meta),
                "audit_purge_at": copy.deepcopy(self._audit_purge_at),
                "next_id": self._next_id,
                "next_seq": copy.deepcopy(self._next_seq),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = self._parse_snapshot(state)
            except BreakglassError:
                raise
            except Exception as exc:  # noqa: BLE001 - normalize malformed input
                raise BreakglassError(
                    "BREAKGLASS_SNAPSHOT_INVALID", exc
                ) from exc

            (
                grants,
                audit,
                audit_meta,
                audit_purge_at,
                next_id,
                next_seq,
                stats,
            ) = parsed
            self._grants = grants
            self._audit = audit
            self._audit_meta = audit_meta
            self._audit_purge_at = audit_purge_at
            self._next_id = next_id
            self._next_seq = next_seq
            self._stats = stats

    def _parse_snapshot(self, state: object):
        def invalid(detail=None):
            raise BreakglassError("BREAKGLASS_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            invalid("snapshot must be a mapping")
        if state.get("version") != _SNAPSHOT_VERSION:
            invalid("unsupported snapshot version")

        raw_grants = state.get("grants")
        raw_audit = state.get("audit")
        raw_meta = state.get("audit_meta")
        raw_purge_at = state.get("audit_purge_at")
        raw_next_id = state.get("next_id")
        raw_next_seq = state.get("next_seq")
        raw_stats = state.get("stats")

        if not isinstance(raw_grants, dict):
            invalid("grants")
        if not isinstance(raw_audit, dict):
            invalid("audit")
        if not isinstance(raw_meta, dict):
            invalid("audit_meta")
        if not isinstance(raw_purge_at, dict):
            invalid("audit_purge_at")
        if not _is_int(raw_next_id) or raw_next_id <= 0:
            invalid("next_id")
        if not isinstance(raw_next_seq, dict):
            invalid("next_seq")
        if not isinstance(raw_stats, dict):
            invalid("stats")

        grants: dict[str, dict] = {}
        max_id = 0
        active_incidents: set[str] = set()
        for grant_id, raw_grant in raw_grants.items():
            if not isinstance(grant_id, str) or not isinstance(raw_grant, dict):
                invalid("grant")
            number = _grant_id_number(grant_id)
            if number is None or raw_grant.get("grant_id") != grant_id:
                invalid("grant_id")
            grant = self._validate_grant_record(raw_grant)
            if grant["state"] == "active":
                if grant["incident_id"] in active_incidents:
                    invalid("duplicate active incident")
                active_incidents.add(grant["incident_id"])
            max_id = max(max_id, number)
            grants[grant_id] = grant

        if raw_next_id <= max_id:
            invalid("next_id does not advance")

        audit: dict[str, list[dict]] = {}
        for grant_id, raw_entries in raw_audit.items():
            if not isinstance(grant_id, str) or not isinstance(raw_entries, list):
                invalid("audit entries")
            if grant_id not in grants and grant_id not in raw_meta:
                invalid("orphan audit trail")
            entries: list[dict] = []
            for expected_seq, raw_entry in enumerate(raw_entries, start=1):
                if not isinstance(raw_entry, dict):
                    invalid("audit entry")
                seq = raw_entry.get("seq")
                at = raw_entry.get("at")
                action = raw_entry.get("action")
                scope = raw_entry.get("scope")
                if seq != expected_seq or not _is_int(seq):
                    invalid("audit seq")
                if not _is_number(at):
                    invalid("audit at")
                if not isinstance(action, str) or not action.strip():
                    invalid("audit action")
                if not _valid_action_scope(scope):
                    invalid("audit scope")
                entries.append({
                    "seq": seq,
                    "at": at,
                    "action": action,
                    "scope": scope,
                })
            audit[grant_id] = entries

        next_seq: dict[str, int] = {}
        for grant_id, raw_seq in raw_next_seq.items():
            if not isinstance(grant_id, str) or not _is_int(raw_seq) or raw_seq <= 0:
                invalid("next_seq")
            entries = audit.get(grant_id, [])
            minimum = len(entries) + 1
            if raw_seq < minimum:
                invalid("next_seq moved backwards")
            if grant_id in grants or grant_id in raw_meta:
                next_seq[grant_id] = raw_seq
        for grant_id in audit:
            if grant_id not in next_seq:
                invalid("missing next_seq")

        audit_meta: dict[str, dict] = {}
        for grant_id, raw_grant in raw_meta.items():
            if (
                not isinstance(grant_id, str)
                or not isinstance(raw_grant, dict)
                or grant_id in grants
                or _grant_id_number(grant_id) is None
            ):
                invalid("audit_meta")
            grant = self._validate_grant_record(raw_grant)
            if grant["grant_id"] != grant_id:
                invalid("audit_meta grant_id")
            audit_meta[grant_id] = grant

        audit_purge_at: dict[str, int | float] = {}
        for grant_id, purge_at in raw_purge_at.items():
            if (
                not isinstance(grant_id, str)
                or grant_id not in audit_meta
                or not _is_number(purge_at)
            ):
                invalid("audit_purge_at")
            audit_purge_at[grant_id] = purge_at
        if set(audit_meta) != set(audit_purge_at):
            invalid("audit retention metadata")

        stats: dict[str, int] = {}
        if set(raw_stats) != set(_STAT_KEYS):
            invalid("stats keys")
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not _is_int(value) or value < 0:
                invalid("stats")
            stats[key] = value

        return (
            copy.deepcopy(grants),
            copy.deepcopy(audit),
            copy.deepcopy(audit_meta),
            copy.deepcopy(audit_purge_at),
            raw_next_id,
            copy.deepcopy(next_seq),
            stats,
        )

    @staticmethod
    def _validate_grant_record(raw_grant: dict) -> dict:
        def invalid(detail=None):
            raise BreakglassError("BREAKGLASS_SNAPSHOT_INVALID", detail)

        grant_id = raw_grant.get("grant_id")
        operator = raw_grant.get("operator")
        scope = raw_grant.get("scope")
        incident_id = raw_grant.get("incident_id")
        activated_at = raw_grant.get("activated_at")
        expires_at = raw_grant.get("expires_at")
        ttl = raw_grant.get("ttl")
        acknowledged = raw_grant.get("acknowledged")
        reviewer = raw_grant.get("reviewer")
        acknowledged_at = raw_grant.get("acknowledged_at")
        note = raw_grant.get("note")
        closed_at = raw_grant.get("closed_at")
        state = raw_grant.get("state")

        if _grant_id_number(grant_id) is None:
            invalid("grant_id")
        if not isinstance(operator, str) or not operator.strip():
            invalid("operator")
        if not _valid_scope(scope):
            invalid("scope")
        if not isinstance(incident_id, str) or not incident_id.strip():
            invalid("incident")
        if (
            not _is_number(activated_at)
            or not _is_number(expires_at)
            or not _is_number(ttl)
            or ttl <= 0
            or expires_at <= activated_at
        ):
            invalid("grant timing")
        if not isinstance(acknowledged, bool):
            invalid("acknowledged")
        if state not in _GRANT_STATES:
            invalid("state")
        if acknowledged:
            if (
                not isinstance(reviewer, str)
                or not reviewer.strip()
                or reviewer == operator
                or not _is_number(acknowledged_at)
            ):
                invalid("review")
        elif reviewer is not None or acknowledged_at is not None:
            invalid("review")
        if note is not None and not isinstance(note, str):
            invalid("note")
        if closed_at is not None and not _is_number(closed_at):
            invalid("closed_at")
        if state == "closed":
            if (
                closed_at is None
                or not isinstance(note, str)
                or not note.strip()
            ):
                invalid("closed grant")
        elif closed_at is not None or note is not None:
            invalid("open grant has closure fields")

        return copy.deepcopy({
            "grant_id": grant_id,
            "operator": operator,
            "scope": scope,
            "incident_id": incident_id,
            "activated_at": activated_at,
            "expires_at": expires_at,
            "ttl": ttl,
            "acknowledged": acknowledged,
            "reviewer": reviewer,
            "acknowledged_at": acknowledged_at,
            "note": note,
            "closed_at": closed_at,
            "state": state,
        })

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}
