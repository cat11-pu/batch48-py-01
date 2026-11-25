"""Just-in-time elevation broker for short-lived privileged sessions."""

from __future__ import annotations

import copy
import math
import threading
import time


ROLES = {"viewer": 1, "operator": 2, "admin": 3, "owner": 4}


class ElevationError(ValueError):
    """Refusal raised by the elevation broker."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ElevationBroker:
    """Accept elevation requests, collect approvals and issue session grants."""

    VERSION = 1
    OPEN_REQUEST_STATES = ("pending", "approved")
    REQUEST_STATES = frozenset(
        {"pending", "approved", "denied", "redeemed", "expired"}
    )
    GRANT_STATES = frozenset({"active", "expired", "revoked"})

    def __init__(self, *, max_ttl: int = 3600, default_ttl: int = 900,
                 required_approvals: int = 2, max_pending: int = 64,
                 clock=None) -> None:
        self.max_ttl = max_ttl
        self.default_ttl = default_ttl
        self.required_approvals = required_approvals
        self.max_pending = max_pending
        self._clock = clock
        self._lock = threading.RLock()
        self._approvers: dict[str, list[str]] = {}
        self._requests: dict[str, dict] = {}
        self._grants: dict[str, dict] = {}
        self._request_seq = 0
        self._grant_seq = 0
        self._stats = {
            "requests": 0,
            "approvals": 0,
            "denials": 0,
            "grants": 0,
            "revocations": 0,
        }

    def register_approver(self, approver: str, roles) -> dict:
        normalized_roles = self._normalize_approver_roles(roles)
        with self._lock:
            self._approvers[approver] = normalized_roles
            return {
                "approver": approver,
                "roles": list(normalized_roles),
            }

    def revoke_approver(self, approver: str) -> dict:
        with self._lock:
            self._approvers.pop(approver, None)
            for request in self._requests.values():
                if request["state"] not in self.OPEN_REQUEST_STATES:
                    continue
                if approver not in request["approvals"]:
                    continue
                request["approvals"] = [
                    name for name in request["approvals"] if name != approver
                ]
                if (request["state"] == "approved"
                        and len(request["approvals"]) < self.required_approvals):
                    request["state"] = "pending"
            return {"approver": approver, "state": "revoked"}

    def request_access(self, subject: str, role: str, reason: str,
                       ttl: int | None = None, now: int | None = None) -> dict:
        if not isinstance(reason, str) or not reason.strip():
            raise ElevationError("ELEV_REASON_INVALID", {"reason": reason})
        if not isinstance(role, str) or role not in ROLES:
            raise ElevationError("ELEV_ROLE_INVALID", {"role": role})

        resolved_ttl = self.default_ttl if ttl is None else ttl
        if not self._valid_ttl(resolved_ttl) or resolved_ttl > self.max_ttl:
            raise ElevationError("ELEV_TTL_INVALID", {"ttl": ttl})

        current_time = self._time(now)
        with self._lock:
            for request in self._requests.values():
                if (request["subject"] == subject
                        and request["role"] == role
                        and request["state"] in self.OPEN_REQUEST_STATES):
                    raise ElevationError(
                        "ELEV_REQUEST_DUPLICATE",
                        {"request_id": request["request_id"]},
                    )

            pending_count = sum(
                request["state"] == "pending"
                for request in self._requests.values()
            )
            if pending_count >= self.max_pending:
                raise ElevationError(
                    "ELEV_REQUEST_LIMIT",
                    {"max_pending": self.max_pending},
                )

            self._request_seq += 1
            request_id = f"req-{self._request_seq}"
            request = {
                "request_id": request_id,
                "subject": subject,
                "role": role,
                "reason": reason,
                "ttl": resolved_ttl,
                "created_at": current_time,
                "state": "pending",
                "approvals": [],
                "note": None,
            }
            if len(request["approvals"]) >= self.required_approvals:
                request["state"] = "approved"
            self._requests[request_id] = request
            self._stats["requests"] += 1
            return self._request_view(request)

    def request_status(self, request_id: str) -> dict:
        with self._lock:
            request = self._get_request(request_id)
            return self._request_view(request)

    def approve(self, request_id: str, approver: str,
                now: int | None = None) -> dict:
        with self._lock:
            request = self._get_request(request_id)

            if approver == request["subject"]:
                raise ElevationError(
                    "ELEV_SELF_APPROVAL",
                    {"request_id": request_id, "approver": approver},
                )
            if approver in request["approvals"]:
                raise ElevationError(
                    "ELEV_APPROVER_DUPLICATE",
                    {"request_id": request_id, "approver": approver},
                )
            self._require_authorized_approver(approver, request["role"])
            if request["state"] != "pending":
                raise ElevationError(
                    "ELEV_REQUEST_STATE",
                    {"request_id": request_id, "state": request["state"]},
                )

            self._time(now)
            request["approvals"].append(approver)
            self._stats["approvals"] += 1
            if len(request["approvals"]) >= self.required_approvals:
                request["state"] = "approved"

            return {
                "request_id": request_id,
                "approvals": list(request["approvals"]),
                "required": self.required_approvals,
                "state": request["state"],
            }

    def deny(self, request_id: str, approver: str, note: str,
             now: int | None = None) -> dict:
        if not isinstance(note, str) or not note.strip():
            raise ElevationError("ELEV_NOTE_INVALID", {"note": note})

        with self._lock:
            request = self._get_request(request_id)
            if approver == request["subject"]:
                raise ElevationError(
                    "ELEV_SELF_APPROVAL",
                    {"request_id": request_id, "approver": approver},
                )
            if not isinstance(approver, str) or approver not in self._approvers:
                raise ElevationError(
                    "ELEV_APPROVER_UNAUTHORIZED",
                    {"approver": approver, "role": request["role"]},
                )
            if request["state"] != "pending":
                raise ElevationError(
                    "ELEV_REQUEST_STATE",
                    {"request_id": request_id, "state": request["state"]},
                )

            self._time(now)
            request["state"] = "denied"
            request["note"] = note
            self._stats["denials"] += 1
            return {
                "request_id": request_id,
                "note": note,
                "state": "denied",
            }

    def redeem(self, request_id: str, session_id: str, now: int) -> dict:
        self._require_session(session_id)

        with self._lock:
            request = self._get_request(request_id)
            if request["state"] in {"denied", "redeemed", "expired"}:
                raise ElevationError(
                    "ELEV_REQUEST_STATE",
                    {"request_id": request_id, "state": request["state"]},
                )
            if request["state"] == "pending":
                raise ElevationError(
                    "ELEV_APPROVALS_PENDING",
                    {
                        "request_id": request_id,
                        "approvals": list(request["approvals"]),
                        "required": self.required_approvals,
                    },
                )
            if request["state"] != "approved":
                raise ElevationError(
                    "ELEV_REQUEST_STATE",
                    {"request_id": request_id, "state": request["state"]},
                )

            effective_approvals = self._effective_approvals(request)
            if len(effective_approvals) < self.required_approvals:
                request["approvals"] = effective_approvals
                request["state"] = "pending"
                raise ElevationError(
                    "ELEV_APPROVALS_PENDING",
                    {
                        "request_id": request_id,
                        "approvals": list(effective_approvals),
                        "required": self.required_approvals,
                    },
                )

            current_time = self._time(now)
            self._grant_seq += 1
            grant_id = f"grant-{self._grant_seq}"
            grant = {
                "grant_id": grant_id,
                "request_id": request_id,
                "subject": request["subject"],
                "role": request["role"],
                "session_id": session_id,
                "issued_at": current_time,
                "expires_at": current_time + request["ttl"],
                "state": "active",
            }
            request["state"] = "redeemed"
            self._grants[grant_id] = grant
            self._stats["grants"] += 1
            return self._grant_view(grant)

    def validate(self, grant_id: str, session_id: str, role: str | None = None,
                 now: int | None = None) -> dict:
        self._require_session(session_id)

        with self._lock:
            grant = self._get_grant(grant_id)
            if session_id != grant["session_id"]:
                raise ElevationError(
                    "ELEV_SESSION_MISMATCH",
                    {"grant_id": grant_id, "session_id": session_id},
                )
            if grant["state"] == "revoked":
                raise ElevationError(
                    "ELEV_GRANT_REVOKED",
                    {"grant_id": grant_id},
                )

            current_time = self._time(now)
            if (grant["state"] == "expired"
                    or current_time >= grant["expires_at"]):
                if grant["state"] == "active":
                    grant["state"] = "expired"
                raise ElevationError(
                    "ELEV_GRANT_EXPIRED",
                    {
                        "grant_id": grant_id,
                        "expires_at": grant["expires_at"],
                        "now": current_time,
                    },
                )

            requested_role = grant["role"] if role is None else role
            if (not isinstance(requested_role, str)
                    or requested_role not in ROLES):
                raise ElevationError(
                    "ELEV_ROLE_INVALID",
                    {"requested_role": requested_role},
                )
            if ROLES[requested_role] > ROLES[grant["role"]]:
                raise ElevationError(
                    "ELEV_ROLE_OUT_OF_SCOPE",
                    {
                        "grant_id": grant_id,
                        "role": grant["role"],
                        "requested_role": requested_role,
                    },
                )

            return {
                "grant_id": grant_id,
                "subject": grant["subject"],
                "role": grant["role"],
                "requested_role": requested_role,
                "expires_at": grant["expires_at"],
                "remaining": int(grant["expires_at"] - current_time),
            }

    def revoke(self, grant_id: str, now: int | None = None) -> dict:
        with self._lock:
            grant = self._get_grant(grant_id)
            if grant["state"] != "revoked":
                self._time(now)
                grant["state"] = "revoked"
                self._stats["revocations"] += 1
            return {"grant_id": grant_id, "state": "revoked"}

    def expire(self, now: int) -> int:
        expired_count = 0
        with self._lock:
            for grant in self._grants.values():
                if grant["state"] == "active" and now >= grant["expires_at"]:
                    grant["state"] = "expired"
                    expired_count += 1
        return expired_count

    def grants(self, subject: str | None = None) -> list:
        with self._lock:
            result = []
            for grant in self._grants.values():
                if subject is not None and grant["subject"] != subject:
                    continue
                result.append(self._grant_view(grant))
            return copy.deepcopy(result)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.VERSION,
                "approvers": copy.deepcopy(self._approvers),
                "requests": copy.deepcopy(self._requests),
                "grants": copy.deepcopy(self._grants),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                candidate = copy.deepcopy(state)
                approvers, requests, grants, stats = self._parse_snapshot(
                    candidate
                )
            except ElevationError:
                raise
            except Exception as exc:
                raise ElevationError(
                    "ELEV_SNAPSHOT_INVALID",
                    {"reason": "snapshot is not structurally valid"},
                ) from exc

            self._approvers = approvers
            self._requests = requests
            self._grants = grants
            self._stats = stats
            self._request_seq = self._max_number(requests, "req-")
            self._grant_seq = self._max_number(grants, "grant-")

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {
                "requests": 0,
                "approvals": 0,
                "denials": 0,
                "grants": 0,
                "revocations": 0,
            }

    def _time(self, now):
        if now is not None:
            return now
        if self._clock is None:
            return time.time()
        if callable(self._clock):
            return self._clock()
        now_method = getattr(self._clock, "now", None)
        if callable(now_method):
            return now_method()
        time_method = getattr(self._clock, "time", None)
        if callable(time_method):
            return time_method()
        return self._clock

    @staticmethod
    def _valid_ttl(value) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(value) and value > 0

    @staticmethod
    def _require_session(session_id) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ElevationError(
                "ELEV_SESSION_REQUIRED",
                {"session_id": session_id},
            )

    def _normalize_approver_roles(self, roles) -> list[str]:
        if isinstance(roles, (str, bytes)) or not hasattr(roles, "__iter__"):
            raise ElevationError("ELEV_ROLE_INVALID", {"roles": roles})
        normalized = []
        for role in roles:
            if not isinstance(role, str) or role not in ROLES:
                raise ElevationError("ELEV_ROLE_INVALID", {"role": role})
            if role not in normalized:
                normalized.append(role)
        return normalized

    def _require_authorized_approver(self, approver, role: str) -> None:
        roles = None
        if isinstance(approver, str):
            roles = self._approvers.get(approver)
        if roles is None or role not in roles:
            raise ElevationError(
                "ELEV_APPROVER_UNAUTHORIZED",
                {"approver": approver, "role": role},
            )

    def _effective_approvals(self, request: dict) -> list[str]:
        effective = []
        for approver in request["approvals"]:
            if approver == request["subject"]:
                continue
            roles = self._approvers.get(approver)
            if roles is not None and request["role"] in roles:
                effective.append(approver)
        return effective

    def _get_request(self, request_id: str) -> dict:
        request = self._requests.get(request_id)
        if request is None:
            raise ElevationError(
                "ELEV_REQUEST_STATE",
                {"request_id": request_id, "reason": "not_found"},
            )
        return request

    def _get_grant(self, grant_id: str) -> dict:
        grant = self._grants.get(grant_id)
        if grant is None:
            raise ElevationError(
                "ELEV_REQUEST_STATE",
                {"grant_id": grant_id, "reason": "not_found"},
            )
        return grant

    @staticmethod
    def _request_view(request: dict) -> dict:
        return {
            "request_id": request["request_id"],
            "subject": request["subject"],
            "role": request["role"],
            "reason": request["reason"],
            "ttl": request["ttl"],
            "created_at": request["created_at"],
            "state": request["state"],
            "approvals": list(request["approvals"]),
        }

    @staticmethod
    def _grant_view(grant: dict) -> dict:
        return {
            "grant_id": grant["grant_id"],
            "subject": grant["subject"],
            "role": grant["role"],
            "session_id": grant["session_id"],
            "issued_at": grant["issued_at"],
            "expires_at": grant["expires_at"],
            "state": grant["state"],
        }

    def _parse_snapshot(self, state):
        if not isinstance(state, dict):
            raise self._snapshot_invalid()

        version = state.get("version")
        if (not isinstance(version, int) or isinstance(version, bool)
                or version != self.VERSION):
            raise self._snapshot_invalid({"field": "version"})

        expected_keys = {"version", "approvers", "requests", "grants", "stats"}
        if set(state) != expected_keys:
            raise self._snapshot_invalid({"field": "top_level"})

        approvers = self._parse_approvers(state["approvers"])
        requests = self._parse_requests(state["requests"])
        grants = self._parse_grants(state["grants"], requests)
        stats = self._parse_stats(state["stats"])
        return approvers, requests, grants, stats

    @staticmethod
    def _snapshot_invalid(detail=None):
        return ElevationError("ELEV_SNAPSHOT_INVALID", detail)

    def _parse_approvers(self, approvers):
        if not isinstance(approvers, dict):
            raise self._snapshot_invalid({"field": "approvers"})
        parsed = {}
        for approver, roles in approvers.items():
            if not isinstance(approver, str) or not approver:
                raise self._snapshot_invalid({"field": "approvers"})
            if not isinstance(roles, list):
                raise self._snapshot_invalid({"field": "approvers"})
            parsed_roles = []
            for role in roles:
                if not isinstance(role, str) or role not in ROLES:
                    raise self._snapshot_invalid({"field": "approvers"})
                if role in parsed_roles:
                    raise self._snapshot_invalid({"field": "approvers"})
                parsed_roles.append(role)
            parsed[approver] = parsed_roles
        return parsed

    def _parse_requests(self, requests):
        if not isinstance(requests, dict):
            raise self._snapshot_invalid({"field": "requests"})
        parsed = {}
        for request_id, request in requests.items():
            if not isinstance(request_id, str) or not isinstance(request, dict):
                raise self._snapshot_invalid({"field": "requests"})
            required = {
                "request_id", "subject", "role", "reason", "ttl",
                "created_at", "state", "approvals",
            }
            if not required.issubset(request):
                raise self._snapshot_invalid({"request_id": request_id})
            if request["request_id"] != request_id:
                raise self._snapshot_invalid({"request_id": request_id})
            if not isinstance(request["subject"], str):
                raise self._snapshot_invalid({"request_id": request_id})
            if not isinstance(request["role"], str) or request["role"] not in ROLES:
                raise self._snapshot_invalid({"request_id": request_id})
            if not isinstance(request["reason"], str):
                raise self._snapshot_invalid({"request_id": request_id})
            if not self._valid_ttl(request["ttl"]):
                raise self._snapshot_invalid({"request_id": request_id})
            if not self._valid_time(request["created_at"]):
                raise self._snapshot_invalid({"request_id": request_id})
            if request["state"] not in self.REQUEST_STATES:
                raise self._snapshot_invalid({"request_id": request_id})
            if not isinstance(request["approvals"], list):
                raise self._snapshot_invalid({"request_id": request_id})

            approvals = []
            for approver in request["approvals"]:
                if not isinstance(approver, str) or approver in approvals:
                    raise self._snapshot_invalid({"request_id": request_id})
                approvals.append(approver)

            note = request.get("note")
            if note is not None and not isinstance(note, str):
                raise self._snapshot_invalid({"request_id": request_id})

            normalized = copy.deepcopy(request)
            normalized["approvals"] = approvals
            parsed[request_id] = normalized
        return parsed

    def _parse_grants(self, grants, requests):
        if not isinstance(grants, dict):
            raise self._snapshot_invalid({"field": "grants"})
        parsed = {}
        request_grant_ids = {}
        for grant_id, grant in grants.items():
            if not isinstance(grant_id, str) or not isinstance(grant, dict):
                raise self._snapshot_invalid({"field": "grants"})
            required = {
                "grant_id", "subject", "role", "session_id", "issued_at",
                "expires_at", "state",
            }
            if not required.issubset(grant):
                raise self._snapshot_invalid({"grant_id": grant_id})
            if grant["grant_id"] != grant_id:
                raise self._snapshot_invalid({"grant_id": grant_id})
            if not isinstance(grant["subject"], str):
                raise self._snapshot_invalid({"grant_id": grant_id})
            if not isinstance(grant["role"], str) or grant["role"] not in ROLES:
                raise self._snapshot_invalid({"grant_id": grant_id})
            if (not isinstance(grant["session_id"], str)
                    or not grant["session_id"].strip()):
                raise self._snapshot_invalid({"grant_id": grant_id})
            if not self._valid_time(grant["issued_at"]):
                raise self._snapshot_invalid({"grant_id": grant_id})
            if not self._valid_time(grant["expires_at"]):
                raise self._snapshot_invalid({"grant_id": grant_id})
            if grant["expires_at"] <= grant["issued_at"]:
                raise self._snapshot_invalid({"grant_id": grant_id})
            if grant["state"] not in self.GRANT_STATES:
                raise self._snapshot_invalid({"grant_id": grant_id})

            request_id = grant.get("request_id")
            if request_id is not None:
                if not isinstance(request_id, str) or request_id not in requests:
                    raise self._snapshot_invalid({"grant_id": grant_id})
                if request_id in request_grant_ids:
                    raise self._snapshot_invalid({"request_id": request_id})
                request_grant_ids[request_id] = grant_id

            parsed[grant_id] = copy.deepcopy(grant)
        return parsed

    def _parse_stats(self, stats):
        if not isinstance(stats, dict):
            raise self._snapshot_invalid({"field": "stats"})
        expected = set(self._stats)
        if set(stats) != expected:
            raise self._snapshot_invalid({"field": "stats"})
        parsed = {}
        for key, value in stats.items():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise self._snapshot_invalid({"stats": key})
            parsed[key] = value
        return parsed

    @staticmethod
    def _valid_time(value) -> bool:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return math.isfinite(value)

    @staticmethod
    def _max_number(items: dict, prefix: str) -> int:
        maximum = 0
        for key in items:
            if not isinstance(key, str) or not key.startswith(prefix):
                continue
            suffix = key[len(prefix):]
            if suffix.isdigit():
                maximum = max(maximum, int(suffix))
        return maximum
