"""Auth code exchange guard: PKCE verification, one-time redemption, replay revocation."""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import secrets
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

_CODE_PREFIX = "AUTHCODE_"
_TOKEN_PREFIX = "TOKEN_"
_SNAPSHOT_VERSION = 2
_PKCE_METHODS = ("S256", "plain")
_STATS_KEYS = ("issued", "redeemed", "replays", "revoked_tokens", "purged")
_RECORD_KEYS = frozenset({
    "code", "client_id", "redirect_uri", "scopes", "challenge", "method",
    "issued_at", "expires_at", "consumed", "token_ids",
})
_STATE_KEYS = frozenset({
    "version", "ttl", "max_codes", "codes", "tokens", "stats",
})


def _is_nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _is_real_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise _SnapshotInvalid("timestamp must be a string")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise _SnapshotInvalid("unparseable timestamp") from exc


class _SnapshotInvalid(Exception):
    """Internal signal for a malformed snapshot; mapped to AuthCodeError."""


def _parse_record(entry: object, tokens: set[str]) -> "AuthCodeRecord":
    if not isinstance(entry, dict) or set(entry.keys()) != _RECORD_KEYS:
        raise _SnapshotInvalid("bad record shape")
    code = entry["code"]
    client_id = entry["client_id"]
    redirect_uri = entry["redirect_uri"]
    challenge = entry["challenge"]
    method = entry["method"]
    issued_at = entry["issued_at"]
    expires_at = entry["expires_at"]
    consumed = entry["consumed"]
    scopes = entry["scopes"]
    token_ids = entry["token_ids"]
    for value in (code, client_id, redirect_uri, challenge, issued_at, expires_at):
        if not _is_nonempty_str(value):
            raise _SnapshotInvalid("bad record string field")
    if not code.startswith(_CODE_PREFIX):
        raise _SnapshotInvalid("bad code prefix")
    if method not in _PKCE_METHODS:
        raise _SnapshotInvalid("bad pkce method")
    if not isinstance(consumed, bool):
        raise _SnapshotInvalid("bad consumed flag")
    if not isinstance(scopes, list) or not all(_is_nonempty_str(scope) for scope in scopes):
        raise _SnapshotInvalid("bad scopes")
    if not isinstance(token_ids, list) or not all(_is_nonempty_str(token) for token in token_ids):
        raise _SnapshotInvalid("bad token ids")
    if any(token_id not in tokens for token_id in token_ids):
        raise _SnapshotInvalid("derived token missing from token table")
    issued_dt = _parse_timestamp(issued_at)
    expires_dt = _parse_timestamp(expires_at)
    if expires_dt < issued_dt:
        raise _SnapshotInvalid("expires_at before issued_at")
    return AuthCodeRecord(
        code=code,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=tuple(scopes),
        challenge=challenge,
        method=method,
        issued_at=issued_at,
        expires_at=expires_at,
        consumed=consumed,
        token_ids=tuple(token_ids),
    )


class AuthCodeError(ValueError):
    """Raised when an authorization code operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AuthCodeRecord:
    code: str
    client_id: str
    redirect_uri: str
    scopes: tuple[str, ...]
    challenge: str
    method: str
    issued_at: str
    expires_at: str
    consumed: bool
    token_ids: tuple[str, ...]


class AuthCodeExchangeGuard:
    """Track authorization codes from issuance to redemption and revoke on replay."""

    def __init__(self, *, ttl: int = 600, max_codes: int = 100, clock=None) -> None:
        self.ttl = ttl
        self.max_codes = max_codes
        self._clock = clock
        self._lock = threading.RLock()
        self._codes: dict[str, AuthCodeRecord] = {}
        self._tokens: set[str] = set()
        self._stats = {key: 0 for key in _STATS_KEYS}

    def _resolve_now(self, now: object) -> str:
        if now is not None:
            return now
        if self._clock is not None:
            value = self._clock()
            return value if isinstance(value, str) else value.isoformat()
        return datetime.now(timezone.utc).isoformat()

    def _unique_code(self) -> str:
        while True:
            code = _CODE_PREFIX + secrets.token_urlsafe(24)
            if code not in self._codes:
                return code

    @staticmethod
    def _pkce_matches(record: AuthCodeRecord, verifier: str) -> bool:
        if record.method == "S256":
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
            return hmac.compare_digest(expected, record.challenge)
        return hmac.compare_digest(record.challenge, verifier)

    def _revoke_record_tokens(self, record: AuthCodeRecord) -> int:
        revoked = 0
        for token_id in record.token_ids:
            if token_id in self._tokens:
                self._tokens.discard(token_id)
                revoked += 1
        return revoked

    @staticmethod
    def _record_to_dict(record: AuthCodeRecord) -> dict:
        return {
            "code": record.code,
            "client_id": record.client_id,
            "redirect_uri": record.redirect_uri,
            "scopes": list(record.scopes),
            "challenge": record.challenge,
            "method": record.method,
            "issued_at": record.issued_at,
            "expires_at": record.expires_at,
            "consumed": record.consumed,
            "token_ids": list(record.token_ids),
        }

    def issue_code(self, *, client_id: str, redirect_uri: str, challenge: str,
                   method: str = "S256", scopes=(), now=None) -> str:
        if (not all(map(_is_nonempty_str, (client_id, redirect_uri, challenge)))
                or not _is_nonempty_str(method) or method not in _PKCE_METHODS):
            raise AuthCodeError(
                "AUTHCODE_INVALID_REQUEST",
                "client_id, redirect_uri, challenge and a known method are required",
            )
        with self._lock:
            if len(self._codes) >= self.max_codes:
                raise AuthCodeError(
                    "AUTHCODE_CAPACITY_EXCEEDED",
                    {"capacity": self.max_codes, "size": len(self._codes)},
                )
            issued_at = self._resolve_now(now)
            expires_at = (
                _parse_timestamp(issued_at) + timedelta(seconds=self.ttl)
            ).isoformat()
            code = self._unique_code()
            self._codes[code] = AuthCodeRecord(
                code=code,
                client_id=client_id,
                redirect_uri=redirect_uri,
                scopes=tuple(sorted(scopes)),
                challenge=challenge,
                method=method,
                issued_at=issued_at,
                expires_at=expires_at,
                consumed=False,
                token_ids=(),
            )
            self._stats["issued"] += 1
            return code

    def redeem(self, *, code: str, client_id: str, redirect_uri: str,
               verifier: str, now=None) -> dict:
        if not all(map(_is_nonempty_str, (code, client_id, redirect_uri, verifier))):
            raise AuthCodeError(
                "AUTHCODE_INVALID_REQUEST",
                "code, client_id, redirect_uri and verifier are required",
            )
        now_dt = _parse_timestamp(self._resolve_now(now))
        with self._lock:
            record = self._codes.get(code)
            if record is None:
                raise AuthCodeError("AUTHCODE_UNKNOWN", {"code": code})
            if now_dt >= _parse_timestamp(record.expires_at):
                raise AuthCodeError("AUTHCODE_EXPIRED", {"code": code})
            if record.consumed:
                revoked = self._revoke_record_tokens(record)
                self._stats["replays"] += 1
                self._stats["revoked_tokens"] += revoked
                raise AuthCodeError(
                    "AUTHCODE_REPLAY",
                    {"code": code, "revoked_tokens": revoked},
                )
            if record.client_id != client_id:
                raise AuthCodeError(
                    "AUTHCODE_CLIENT_MISMATCH",
                    {"expected": record.client_id, "got": client_id},
                )
            if record.redirect_uri != redirect_uri:
                raise AuthCodeError(
                    "AUTHCODE_REDIRECT_MISMATCH",
                    {"expected": record.redirect_uri, "got": redirect_uri},
                )
            if not self._pkce_matches(record, verifier):
                raise AuthCodeError("AUTHCODE_PKCE_MISMATCH", {"code": code})
            token_id = _TOKEN_PREFIX + secrets.token_urlsafe(24)
            self._tokens.add(token_id)
            record = replace(
                record,
                consumed=True,
                token_ids=record.token_ids + (token_id,),
            )
            self._codes[code] = record
            self._stats["redeemed"] += 1
            return {
                "code": code,
                "client_id": record.client_id,
                "scopes": list(record.scopes),
                "token_id": token_id,
            }

    def revoke_derived(self, code: str) -> int:
        with self._lock:
            record = self._codes.get(code)
            if record is None:
                raise AuthCodeError("AUTHCODE_UNKNOWN", {"code": code})
            revoked = self._revoke_record_tokens(record)
            self._stats["revoked_tokens"] += revoked
            return revoked

    def inspect_code(self, code: str) -> dict:
        with self._lock:
            record = self._codes.get(code)
            if record is None:
                raise AuthCodeError("AUTHCODE_UNKNOWN", {"code": code})
            return self._record_to_dict(record)

    def active_codes(self, client_id: str) -> list[str]:
        now_dt = _parse_timestamp(self._resolve_now(None))
        with self._lock:
            return [
                record.code
                for record in self._codes.values()
                if record.client_id == client_id
                and not record.consumed
                and _parse_timestamp(record.expires_at) > now_dt
            ]

    def token_usable(self, token_id: str) -> bool:
        with self._lock:
            return token_id in self._tokens

    def purge_expired(self, now=None) -> int:
        now_dt = _parse_timestamp(self._resolve_now(now))
        with self._lock:
            expired = [
                code
                for code, record in self._codes.items()
                if _parse_timestamp(record.expires_at) <= now_dt
            ]
            for code in expired:
                del self._codes[code]
            count = len(expired)
            self._stats["purged"] += count
            return count

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": _SNAPSHOT_VERSION,
                "ttl": self.ttl,
                "max_codes": self.max_codes,
                "codes": [
                    self._record_to_dict(record) for record in self._codes.values()
                ],
                "tokens": sorted(self._tokens),
                "stats": dict(self._stats),
            })

    def restore(self, state: dict) -> None:
        codes, tokens, stats, ttl, max_codes = self._parse_snapshot(state)
        with self._lock:
            self._codes = codes
            self._tokens = tokens
            self._stats = stats
            self.ttl = ttl
            self.max_codes = max_codes

    @staticmethod
    def _parse_snapshot(state: object):
        try:
            if not isinstance(state, dict) or set(state.keys()) != _STATE_KEYS:
                raise _SnapshotInvalid("bad state shape")
            if state["version"] != _SNAPSHOT_VERSION:
                raise _SnapshotInvalid("unsupported snapshot version")
            ttl = state["ttl"]
            max_codes = state["max_codes"]
            if not _is_real_int(ttl) or ttl <= 0:
                raise _SnapshotInvalid("bad ttl")
            if not _is_real_int(max_codes) or max_codes < 0:
                raise _SnapshotInvalid("bad max_codes")
            raw_codes = state["codes"]
            raw_tokens = state["tokens"]
            raw_stats = state["stats"]
            if (not isinstance(raw_codes, list) or not isinstance(raw_tokens, list)
                    or not isinstance(raw_stats, dict)):
                raise _SnapshotInvalid("bad container types")
            tokens: set[str] = set()
            for token_id in raw_tokens:
                if not _is_nonempty_str(token_id) or token_id in tokens:
                    raise _SnapshotInvalid("bad token entry")
                tokens.add(token_id)
            codes: dict[str, AuthCodeRecord] = {}
            for entry in raw_codes:
                record = _parse_record(entry, tokens)
                if record.code in codes:
                    raise _SnapshotInvalid("duplicate code entry")
                codes[record.code] = record
            if set(raw_stats.keys()) != set(_STATS_KEYS):
                raise _SnapshotInvalid("bad stats keys")
            stats: dict[str, int] = {}
            for key in _STATS_KEYS:
                value = raw_stats[key]
                if not _is_real_int(value) or value < 0:
                    raise _SnapshotInvalid("bad stat value")
                stats[key] = value
        except _SnapshotInvalid as exc:
            raise AuthCodeError(
                "AUTHCODE_SNAPSHOT_INVALID", str(exc)
            ) from exc
        return copy.deepcopy((codes, tokens, stats, ttl, max_codes))

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)
