"""CSRF token guard with session, action and origin binding."""

from __future__ import annotations

import copy
import hmac
import math
import secrets
import threading
from typing import Any


class CsrfError(ValueError):
    """Raised when a CSRF token operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CsrfTokenGuard:
    """Issue, verify, rotate and revoke session-bound CSRF tokens."""

    _SNAPSHOT_VERSION = 1
    _STAT_KEYS = (
        "issued",
        "verified",
        "rejected",
        "consumed",
        "rotated",
        "revoked",
    )
    _SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

    def __init__(self, *, token_ttl: int = 600, max_tokens_per_session: int = 8,
                 clock=None) -> None:
        self.token_ttl = token_ttl
        self.max_tokens_per_session = max_tokens_per_session
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._stats: dict[str, int] = {key: 0 for key in self._STAT_KEYS}

    def create_session(self, session_id: str, *, now: int) -> dict:
        with self._lock:
            if session_id in self._sessions:
                raise CsrfError("CSRF_SESSION_EXISTS", session_id)

            session_token = self._new_session_token_locked()
            session = {
                "session_id": session_id,
                "epoch": 1,
                "session_token": session_token,
                "revoked": False,
                "created_at": now,
                "rotated_at": None,
                "revoked_at": None,
                "tokens": {},
            }
            self._sessions[session_id] = session
            return self._session_view(session)

    def issue_token(self, session_id: str, action: str, *, origin: str,
                    one_time: bool = True, now: int) -> dict:
        with self._lock:
            session = self._require_session_locked(session_id)
            self._require_active_session_locked(session)

            if len(session["tokens"]) >= self.max_tokens_per_session:
                self._purge_expired_locked(now)
            if len(session["tokens"]) >= self.max_tokens_per_session:
                raise CsrfError("CSRF_TOKEN_CAPACITY", {
                    "session_id": session_id,
                    "limit": self.max_tokens_per_session,
                })

            token = self._new_token_locked(session)
            record = {
                "token": token,
                "session_id": session_id,
                "epoch": session["epoch"],
                "action": action,
                "origin": origin,
                "one_time": bool(one_time),
                "issued_at": now,
                "expires_at": now + self.token_ttl,
                "consumed": False,
                "consumed_at": None,
                "last_verified_at": None,
            }
            session["tokens"][token] = record
            self._stats["issued"] += 1
            return self._token_view(record)

    def verify_token(self, session_id: str, token: str, *, action: str,
                     origin: str, cookie_token: str, method: str = "POST",
                     now: int) -> dict:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise self._reject_locked(
                    "CSRF_TOKEN_UNKNOWN", {"session_id": session_id}
                )
            if session.get("revoked"):
                raise self._reject_locked(
                    "CSRF_SESSION_REVOKED", {"session_id": session_id}
                )

            expected_cookie = session.get("session_token")
            if (not isinstance(cookie_token, str)
                    or not isinstance(expected_cookie, str)
                    or not hmac.compare_digest(cookie_token, expected_cookie)):
                raise self._reject_locked(
                    "CSRF_DOUBLE_SUBMIT_MISMATCH", {"session_id": session_id}
                )

            record = session["tokens"].get(token) if isinstance(token, str) else None
            if record is None or record.get("epoch", session["epoch"]) != session["epoch"]:
                raise self._reject_locked(
                    "CSRF_TOKEN_UNKNOWN", {"session_id": session_id, "token": token}
                )

            if now >= record["expires_at"]:
                raise self._reject_locked("CSRF_TOKEN_EXPIRED", {
                    "session_id": session_id,
                    "token": token,
                    "expires_at": record["expires_at"],
                })

            if record.get("action") != action:
                raise self._reject_locked("CSRF_ACTION_MISMATCH", {
                    "expected": record.get("action"),
                    "actual": action,
                })

            if record.get("origin") != origin:
                raise self._reject_locked("CSRF_ORIGIN_MISMATCH", {
                    "expected": record.get("origin"),
                    "actual": origin,
                })

            if record.get("consumed"):
                raise self._reject_locked("CSRF_TOKEN_CONSUMED", {
                    "session_id": session_id,
                    "token": token,
                })

            normalized_method = method.upper() if isinstance(method, str) else ""
            safe_method = normalized_method in self._SAFE_METHODS
            consumed_now = False

            if record.get("one_time") and not safe_method:
                record["consumed"] = True
                record["consumed_at"] = now
                consumed_now = True
                self._stats["consumed"] += 1

            record["last_verified_at"] = now
            self._stats["verified"] += 1
            return {
                "status": "ok",
                "session_id": session_id,
                "action": record["action"],
                "origin": record["origin"],
                "one_time": record["one_time"],
                "method": normalized_method or method,
                "safe_method": safe_method,
                "consumed": consumed_now,
                "epoch": session["epoch"],
            }

    def rotate_session_token(self, session_id: str, *, now: int) -> dict:
        with self._lock:
            session = self._require_session_locked(session_id)
            self._require_active_session_locked(session)

            session["epoch"] += 1
            session["session_token"] = self._new_session_token_locked()
            session["rotated_at"] = now
            self._stats["rotated"] += 1
            return self._session_view(session)

    def revoke_for_session(self, session_id: str) -> int:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return 0

            revoked_count = len(session["tokens"])
            session["tokens"].clear()
            session["revoked"] = True
            self._stats["revoked"] += revoked_count
            return revoked_count

    def purge_expired(self, now: int) -> int:
        with self._lock:
            return self._purge_expired_locked(now)

    def token_count(self, session_id: str) -> int:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return 0
            return len(session["tokens"])

    def session_state(self, session_id: str) -> dict:
        with self._lock:
            session = self._require_session_locked(session_id)
            return copy.deepcopy(session)

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": self._SNAPSHOT_VERSION,
                "sessions": self._sessions,
                "stats": self._stats,
            })

    def restore(self, state: dict) -> None:
        try:
            candidate = copy.deepcopy(state)
            self._validate_snapshot(candidate)
        except CsrfError:
            raise
        except Exception as exc:
            raise CsrfError("CSRF_SNAPSHOT_INVALID") from exc

        with self._lock:
            self._sessions = candidate["sessions"]
            self._stats = candidate["stats"]

    def _require_session_locked(self, session_id: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if session is None:
            raise CsrfError("CSRF_SESSION_UNKNOWN", session_id)
        return session

    @staticmethod
    def _require_active_session_locked(session: dict[str, Any]) -> None:
        if session.get("revoked"):
            raise CsrfError("CSRF_SESSION_REVOKED", session.get("session_id"))

    def _reject_locked(self, code: str, detail: object = None) -> CsrfError:
        self._stats["rejected"] += 1
        return CsrfError(code, detail)

    def _new_session_token_locked(self) -> str:
        used = {
            session["session_token"]
            for session in self._sessions.values()
            if isinstance(session.get("session_token"), str)
        }
        while True:
            value = secrets.token_urlsafe(24)
            if value not in used:
                return value

    @staticmethod
    def _new_token_locked(session: dict[str, Any]) -> str:
        tokens = session["tokens"]
        while True:
            value = secrets.token_urlsafe(24)
            if value not in tokens:
                return value

    @staticmethod
    def _session_view(session: dict[str, Any]) -> dict[str, Any]:
        return {
            "session_id": session["session_id"],
            "epoch": session["epoch"],
            "session_token": session["session_token"],
            "revoked": session["revoked"],
            "created_at": session.get("created_at"),
            "rotated_at": session.get("rotated_at"),
        }

    @staticmethod
    def _token_view(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "token": record["token"],
            "session_id": record["session_id"],
            "epoch": record["epoch"],
            "action": record["action"],
            "origin": record["origin"],
            "one_time": record["one_time"],
            "issued_at": record["issued_at"],
            "expires_at": record["expires_at"],
            "consumed": record["consumed"],
            "consumed_at": record["consumed_at"],
        }

    def _purge_expired_locked(self, now: int) -> int:
        removed = 0
        for session in self._sessions.values():
            tokens = session["tokens"]
            expired = [
                token
                for token, record in tokens.items()
                if now >= record["expires_at"]
            ]
            for token in expired:
                del tokens[token]
                removed += 1
        return removed

    @classmethod
    def _validate_snapshot(cls, state: Any) -> None:
        if not isinstance(state, dict):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "snapshot must be a dict")
        if state.get("version") != cls._SNAPSHOT_VERSION:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "unsupported snapshot version")

        sessions = state.get("sessions")
        stats = state.get("stats")
        if not isinstance(sessions, dict):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "sessions must be a dict")
        if not isinstance(stats, dict):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "stats must be a dict")

        if set(stats) != set(cls._STAT_KEYS):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid stats keys")
        for key in cls._STAT_KEYS:
            value = stats[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CsrfError("CSRF_SNAPSHOT_INVALID", f"invalid stat: {key}")

        for session_id, session in sessions.items():
            cls._validate_session_snapshot(session_id, session)

        # Replace arbitrary top-level data with the validated internal schema.
        state.clear()
        state["version"] = cls._SNAPSHOT_VERSION
        state["sessions"] = sessions
        state["stats"] = stats

    @classmethod
    def _validate_session_snapshot(cls, session_id: Any, session: Any) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid session id")
        if not isinstance(session, dict):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "session must be a dict")
        if session.get("session_id") != session_id:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "session id mismatch")
        if not cls._is_positive_int(session.get("epoch")):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid session epoch")
        if not isinstance(session.get("session_token"), str) or not session["session_token"]:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid session token")
        if not isinstance(session.get("revoked"), bool):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid revoked flag")
        if "created_at" in session and not cls._is_optional_number(session["created_at"]):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid created_at")
        if "rotated_at" in session and not cls._is_optional_number(session["rotated_at"]):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid rotated_at")
        if "revoked_at" in session and not cls._is_optional_number(session["revoked_at"]):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid revoked_at")

        tokens = session.get("tokens")
        if not isinstance(tokens, dict):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "tokens must be a dict")

        for token, record in tokens.items():
            cls._validate_token_snapshot(session_id, session["epoch"], token, record)

    @classmethod
    def _validate_token_snapshot(cls, session_id: str, epoch: int,
                                 token: Any, record: Any) -> None:
        if not isinstance(token, str) or not token:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid token key")
        if not isinstance(record, dict):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "token record must be a dict")
        if record.get("token") != token:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "token value mismatch")
        if record.get("session_id") != session_id:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "token session mismatch")
        if not cls._is_positive_int(record.get("epoch", epoch)):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid token epoch")
        if not isinstance(record.get("action"), str):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid action")
        if not isinstance(record.get("origin"), str):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid origin")
        if not isinstance(record.get("one_time"), bool):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid one_time flag")
        if not cls._is_number(record.get("issued_at")):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid issued_at")
        if not cls._is_number(record.get("expires_at")):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid expires_at")
        if record["expires_at"] < record["issued_at"]:
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid token lifetime")
        if not isinstance(record.get("consumed"), bool):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid consumed flag")
        if not cls._is_optional_number(record.get("consumed_at")):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid consumed_at")
        if not cls._is_optional_number(record.get("last_verified_at")):
            raise CsrfError("CSRF_SNAPSHOT_INVALID", "invalid verified time")

    @staticmethod
    def _is_number(value: Any) -> bool:
        return (isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value))

    @classmethod
    def _is_optional_number(cls, value: Any) -> bool:
        return value is None or cls._is_number(value)

    @classmethod
    def _is_positive_int(cls, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 1
