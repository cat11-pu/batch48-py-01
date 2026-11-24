"""Push-based MFA challenge orchestration with number matching."""

from __future__ import annotations

import copy
import secrets
import threading
import uuid


SNAPSHOT_VERSION = 1
STAT_KEYS = ("pushed", "approved", "failed", "expired", "cancelled", "locked")
PENDING = "pending"
TERMINAL_STATES = frozenset(("approved", "denied", "expired", "cancelled"))
VALID_STATES = frozenset((PENDING,)) | TERMINAL_STATES


class MfaPushError(ValueError):
    """Raised when a push challenge operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MfaPushOrchestrator:
    """Create push challenges, validate device responses and track state."""

    def __init__(self, *, ttl: int = 120, max_failures: int = 3,
                 lock_seconds: int = 300, max_challenges: int = 500,
                 clock=None) -> None:
        self.ttl = ttl
        self.max_failures = max_failures
        self.lock_seconds = lock_seconds
        self.max_challenges = max_challenges
        self._clock = clock
        self._lock = threading.RLock()
        self._challenges: dict[str, dict] = {}
        self._users: dict[str, dict] = {}
        self._stats = {key: 0 for key in STAT_KEYS}

    def _user(self, user_id: str) -> dict:
        user = self._users.get(user_id)
        if user is None:
            user = {"failures": 0, "locked_until": None, "pending_id": None}
            self._users[user_id] = user
        return user

    @staticmethod
    def _is_valid_code(code: object) -> bool:
        return (
            isinstance(code, str)
            and len(code) == 2
            and code.isdigit()
        )

    def _require_challenge(self, challenge_id: str) -> dict:
        record = self._challenges.get(challenge_id)
        if record is None:
            raise MfaPushError(
                "MFA_UNKNOWN_CHALLENGE", {"challenge_id": challenge_id}
            )
        return record

    def _clear_pending_pointer(self, record: dict) -> None:
        user = self._users.get(record["user_id"])
        if user is not None and user.get("pending_id") == record["challenge_id"]:
            user["pending_id"] = None

    def _expire_record(self, record: dict) -> None:
        record["state"] = "expired"
        self._clear_pending_pointer(record)
        self._stats["expired"] += 1

    def create_challenge(self, user_id: str, *, now: int) -> dict:
        with self._lock:
            user = self._user(user_id)
            locked_until = user["locked_until"]
            if locked_until is not None and now < locked_until:
                raise MfaPushError(
                    "MFA_USER_LOCKED", {"locked_until": locked_until}
                )

            active_id = user.get("pending_id")
            if active_id is not None:
                active = self._challenges.get(active_id)
                if active is not None and active["state"] == PENDING:
                    if now < active["expires_at"]:
                        reused = dict(active)
                        reused["reused"] = True
                        return reused
                    self._expire_record(active)

            active_challenges = sum(
                1
                for item in self._challenges.values()
                if item["state"] == PENDING
            )
            if active_challenges >= self.max_challenges:
                raise MfaPushError(
                    "MFA_TOO_MANY_CHALLENGES",
                    {"limit": self.max_challenges},
                )

            record = {
                "challenge_id": uuid.uuid4().hex,
                "user_id": user_id,
                "code": f"{secrets.randbelow(100):02d}",
                "state": PENDING,
                "attempts": 0,
                "created_at": now,
                "expires_at": now + self.ttl,
                "reused": False,
            }
            self._challenges[record["challenge_id"]] = record
            user["pending_id"] = record["challenge_id"]
            self._stats["pushed"] += 1
            return dict(record)

    def submit_response(self, challenge_id: str, code: object,
                        decision: str, *, now: int) -> dict:
        with self._lock:
            record = self._require_challenge(challenge_id)
            if record["state"] != PENDING:
                raise MfaPushError(
                    "MFA_INVALID_TRANSITION",
                    {"challenge_id": challenge_id, "state": record["state"]},
                )

            if decision == "deny":
                record["state"] = "denied"
                self._clear_pending_pointer(record)
                return dict(record)
            if decision != "approve":
                raise MfaPushError(
                    "MFA_INVALID_DECISION", {"decision": decision}
                )

            if not self._is_valid_code(code):
                raise MfaPushError("MFA_CODE_INVALID", {"code": code})

            if code != record["code"]:
                user = self._user(record["user_id"])
                locked_until = user["locked_until"]
                if locked_until is not None and now < locked_until:
                    raise MfaPushError(
                        "MFA_USER_LOCKED", {"locked_until": locked_until}
                    )
                record["attempts"] += 1
                user["failures"] += 1
                self._stats["failed"] += 1
                if user["failures"] >= self.max_failures:
                    user["locked_until"] = now + self.lock_seconds
                    self._stats["locked"] += 1
                raise MfaPushError(
                    "MFA_CODE_MISMATCH", {"attempts": record["attempts"]}
                )

            record["state"] = "approved"
            user = self._user(record["user_id"])
            user["failures"] = 0
            user["locked_until"] = None
            self._clear_pending_pointer(record)
            self._stats["approved"] += 1
            return dict(record)

    def cancel_challenge(self, challenge_id: str, *, now: int) -> dict:
        with self._lock:
            record = self._require_challenge(challenge_id)
            if record["state"] != PENDING:
                raise MfaPushError(
                    "MFA_INVALID_TRANSITION",
                    {"challenge_id": challenge_id, "state": record["state"]},
                )
            record["state"] = "cancelled"
            self._clear_pending_pointer(record)
            self._stats["cancelled"] += 1
            return dict(record)

    def poll_status(self, challenge_id: str, *, now: int) -> dict:
        with self._lock:
            return dict(self._require_challenge(challenge_id))

    def expire_stale(self, now: int) -> int:
        count = 0
        with self._lock:
            for record in list(self._challenges.values()):
                if record["state"] == PENDING and record["expires_at"] <= now:
                    self._expire_record(record)
                    count += 1
        return count

    def lock_state(self, user_id: str) -> dict:
        with self._lock:
            user = self._users.get(user_id)
            if user is None:
                return {"failures": 0, "locked_until": None}
            return {
                "failures": user["failures"],
                "locked_until": user["locked_until"],
            }

    def pending_count(self, user_id: str) -> int:
        with self._lock:
            return sum(
                1
                for record in self._challenges.values()
                if record["user_id"] == user_id and record["state"] == PENDING
            )

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "challenges": copy.deepcopy(self._challenges),
                "users": copy.deepcopy(self._users),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        challenges, users, stats = self._validate_snapshot(state)
        with self._lock:
            self._challenges = challenges
            self._users = users
            self._stats = stats

    @staticmethod
    def _validate_snapshot(state: object) -> tuple[dict, dict, dict]:
        if not isinstance(state, dict):
            raise MfaPushError("MFA_SNAPSHOT_INVALID", "state must be an object")
        if state.get("version") != SNAPSHOT_VERSION:
            raise MfaPushError(
                "MFA_SNAPSHOT_INVALID", {"version": state.get("version")}
            )

        challenges = state.get("challenges")
        users = state.get("users")
        stats = state.get("stats")
        if not isinstance(challenges, dict):
            raise MfaPushError("MFA_SNAPSHOT_INVALID", "challenges must be an object")
        if not isinstance(users, dict):
            raise MfaPushError("MFA_SNAPSHOT_INVALID", "users must be an object")
        if not isinstance(stats, dict):
            raise MfaPushError("MFA_SNAPSHOT_INVALID", "stats must be an object")
        if any(key not in stats for key in STAT_KEYS):
            raise MfaPushError("MFA_SNAPSHOT_INVALID", "stats keys are incomplete")

        challenge_fields = (
            "challenge_id", "user_id", "code", "state",
            "attempts", "created_at", "expires_at",
        )
        for challenge_id, record in challenges.items():
            if not isinstance(challenge_id, str) or not isinstance(record, dict):
                raise MfaPushError("MFA_SNAPSHOT_INVALID", "bad challenge entry")
            if any(field not in record for field in challenge_fields):
                raise MfaPushError("MFA_SNAPSHOT_INVALID", "challenge fields missing")
            if record["state"] not in VALID_STATES:
                raise MfaPushError("MFA_SNAPSHOT_INVALID", "bad challenge state")
            if not isinstance(record["attempts"], int) or isinstance(record["attempts"], bool):
                raise MfaPushError("MFA_SNAPSHOT_INVALID", "bad attempts value")

        for user_id, user in users.items():
            if not isinstance(user_id, str) or not isinstance(user, dict):
                raise MfaPushError("MFA_SNAPSHOT_INVALID", "bad user entry")
            failures = user.get("failures")
            if not isinstance(failures, int) or isinstance(failures, bool):
                raise MfaPushError("MFA_SNAPSHOT_INVALID", "bad failure counter")

        for key in STAT_KEYS:
            value = stats[key]
            if not isinstance(value, int) or isinstance(value, bool):
                raise MfaPushError("MFA_SNAPSHOT_INVALID", f"bad stat value: {key}")

        clean_stats = {key: stats[key] for key in STAT_KEYS}
        return (
            copy.deepcopy(challenges),
            copy.deepcopy(users),
            clean_stats,
        )
