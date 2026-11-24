"""Client assertion guard: private_key_jwt verification, kid rotation and jti ledger."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import threading
import time
from typing import Any


class ClientAssertionError(ValueError):
    """Raised when a client assertion is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STAT_KEYS = ("keys", "retired", "verified", "rejected", "jti", "purged")
_SNAPSHOT_VERSION = 1


def _fail(code: str, detail: object = None) -> ClientAssertionError:
    return ClientAssertionError(code, detail)


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(value: str, *, code: str = "ASSERTION_MALFORMED") -> bytes:
    if not isinstance(value, str) or not value:
        raise _fail(code)
    try:
        raw = value.encode("ascii")
        raw += b"=" * (-len(raw) % 4)
        return base64.b64decode(raw, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError, binascii.Error) as exc:
        raise _fail(code) from exc


def _secret_bytes(secret: Any) -> bytes:
    if isinstance(secret, bytes):
        return secret
    if isinstance(secret, bytearray):
        return bytes(secret)
    if isinstance(secret, str):
        return secret.encode("utf-8")
    raise TypeError("secret must be bytes or str")


class ClientAssertionGuard:
    """Verify HS256 client assertions and keep a bounded one-time jti ledger."""

    def __init__(self, endpoint: str = "https://idp.example.com/token",
                 max_ttl: int = 300, jti_ttl: int = 600, max_jti: int = 256,
                 clock=None) -> None:
        self.endpoint = endpoint
        self.max_ttl = max_ttl
        self.jti_ttl = jti_ttl
        self.max_jti = max_jti
        self._clock = clock
        self._lock = threading.RLock()

        self._keys: dict[str, dict[str, dict[str, Any]]] = {}
        self._active: dict[str, str | None] = {}
        self._jti: dict[tuple[str, str], float] = {}
        self._verified = 0
        self._rejected = 0
        self._jti_total = 0
        self._purged = 0

    # ------------------------------------------------------------------
    # Key management
    # ------------------------------------------------------------------
    def register_key(self, client_id: str, kid: str, secret: bytes,
                     active: bool = True) -> None:
        secret_bytes = _secret_bytes(secret)
        with self._lock:
            client_keys = self._keys.get(client_id)
            if client_keys is None:
                client_keys = {}
                self._keys[client_id] = client_keys
                self._active[client_id] = None

            record = client_keys.get(kid)
            if record is not None:
                if not hmac.compare_digest(record["secret"], secret_bytes):
                    raise _fail("ASSERTION_KEY_CONFLICT", kid)
                if active and not record["retired"]:
                    self._set_active_locked(client_id, kid)
                return

            client_keys[kid] = {"secret": secret_bytes, "retired": False}
            if active:
                self._set_active_locked(client_id, kid)

    def activate_key(self, *, client_id: str, kid: str) -> None:
        with self._lock:
            record = self._lookup_key_locked(client_id, kid)
            if record["retired"]:
                raise _fail("ASSERTION_KID_RETIRED", kid)
            self._set_active_locked(client_id, kid)

    def retire_key(self, *, client_id: str, kid: str) -> None:
        with self._lock:
            record = self._lookup_key_locked(client_id, kid)
            if record["retired"]:
                return
            record["retired"] = True
            if self._active.get(client_id) == kid:
                self._active[client_id] = None

    def active_kid(self, client_id: str) -> str | None:
        with self._lock:
            if client_id not in self._keys:
                raise _fail("ASSERTION_UNKNOWN_CLIENT", client_id)
            return self._active.get(client_id)

    def _set_active_locked(self, client_id: str, kid: str) -> None:
        self._active[client_id] = kid

    def _lookup_key_locked(self, client_id: str, kid: str) -> dict[str, Any]:
        client_keys = self._keys.get(client_id)
        if client_keys is None:
            raise _fail("ASSERTION_UNKNOWN_CLIENT", client_id)
        record = client_keys.get(kid)
        if record is None:
            raise _fail("ASSERTION_UNKNOWN_KID", kid)
        return record

    # ------------------------------------------------------------------
    # Assertion verification
    # ------------------------------------------------------------------
    def verify_assertion(self, assertion: str, now=None,
                         require_active: bool = False) -> dict:
        with self._lock:
            try:
                claims, _kid = self._verify_assertion_locked(
                    assertion, now, require_active
                )
            except ClientAssertionError:
                self._rejected += 1
                raise
            self._verified += 1
            return claims

    def _verify_assertion_locked(self, assertion: str, now,
                                 require_active: bool) -> tuple[dict, str]:
        if not isinstance(assertion, str):
            raise _fail("ASSERTION_MALFORMED")
        parts = assertion.split(".")
        if len(parts) != 3:
            raise _fail("ASSERTION_MALFORMED")
        head_segment, body_segment, signature_segment = parts

        header = self._decode_json_object(head_segment)
        claims = self._decode_json_object(body_segment)
        signature = _b64decode(signature_segment)

        if header.get("alg") != "HS256":
            raise _fail("ASSERTION_MALFORMED", "alg")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise _fail("ASSERTION_MALFORMED", "kid")

        iss = claims.get("iss")
        sub = claims.get("sub")
        jti = claims.get("jti")
        if not isinstance(iss, str) or not isinstance(sub, str):
            raise _fail("ASSERTION_MALFORMED", "iss/sub")
        if not isinstance(jti, str) or not jti:
            raise _fail("ASSERTION_MALFORMED", "jti")
        if iss != sub:
            raise _fail("ASSERTION_ISS_MISMATCH")

        client_keys = self._keys.get(iss)
        if client_keys is None:
            raise _fail("ASSERTION_UNKNOWN_CLIENT", iss)
        record = client_keys.get(kid)
        if record is None:
            raise _fail("ASSERTION_UNKNOWN_KID", kid)
        if record["retired"]:
            raise _fail("ASSERTION_KID_RETIRED", kid)
        if require_active and self._active.get(iss) != kid:
            raise _fail("ASSERTION_KID_INACTIVE", kid)

        try:
            signing_input = f"{head_segment}.{body_segment}".encode("ascii")
        except UnicodeEncodeError as exc:
            raise _fail("ASSERTION_MALFORMED") from exc
        expected = hmac.new(
            record["secret"], signing_input, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(expected, signature):
            raise _fail("ASSERTION_SIGNATURE_INVALID")

        if not self._audience_matches(claims.get("aud")):
            raise _fail("ASSERTION_AUD_MISMATCH")

        current = self._resolve_now(now)
        iat = claims.get("iat")
        exp = claims.get("exp")
        if not self._is_number(iat) or not self._is_number(exp):
            raise _fail("ASSERTION_MALFORMED", "iat/exp")
        if not math.isfinite(iat) or not math.isfinite(exp):
            raise _fail("ASSERTION_MALFORMED", "iat/exp")
        if exp <= current:
            raise _fail("ASSERTION_EXPIRED")
        if iat > current:
            raise _fail("ASSERTION_NOT_YET_VALID")
        if exp - iat > self.max_ttl:
            raise _fail("ASSERTION_TTL_EXCEEDED")

        result = dict(claims)
        result["client_id"] = iss
        result["kid"] = kid
        return result, kid

    @staticmethod
    def _decode_json_object(segment: str) -> dict:
        raw = _b64decode(segment)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise _fail("ASSERTION_MALFORMED") from exc
        if not isinstance(value, dict):
            raise _fail("ASSERTION_MALFORMED")
        return value

    def _audience_matches(self, audience: Any) -> bool:
        if isinstance(audience, str):
            return hmac.compare_digest(audience, self.endpoint)
        if isinstance(audience, list):
            return any(
                isinstance(item, str)
                and hmac.compare_digest(item, self.endpoint)
                for item in audience
            )
        return False

    @staticmethod
    def _is_number(value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    # ------------------------------------------------------------------
    # jti ledger
    # ------------------------------------------------------------------
    def consume_jti(self, client_id: str, jti: str, now=None) -> None:
        with self._lock:
            current = self._resolve_now(now)
            self._purge_locked(current)
            key = (client_id, jti)
            if key in self._jti:
                raise _fail("ASSERTION_JTI_REPLAY", jti)
            if len(self._jti) >= self.max_jti:
                raise _fail("ASSERTION_JTI_LEDGER_FULL")
            self._jti[key] = current + self.jti_ttl
            self._jti_total += 1

    def is_jti_consumed(self, client_id: str, jti: str, now=None) -> bool:
        with self._lock:
            current = self._resolve_now(now)
            self._purge_locked(current)
            return (client_id, jti) in self._jti

    def purge_jti(self, now=None) -> int:
        with self._lock:
            return self._purge_locked(self._resolve_now(now))

    def _purge_locked(self, now) -> int:
        expired = [
            key for key, expires_at in self._jti.items()
            if expires_at <= now
        ]
        for key in expired:
            del self._jti[key]
        self._purged += len(expired)
        return len(expired)

    # ------------------------------------------------------------------
    # Snapshot / restore / stats
    # ------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            keys = []
            for client_id, client_keys in self._keys.items():
                for kid, record in client_keys.items():
                    keys.append({
                        "client_id": client_id,
                        "kid": kid,
                        "secret": _b64encode(record["secret"]),
                        "active": self._active.get(client_id) == kid,
                        "retired": bool(record["retired"]),
                    })
            jti = [
                {"client_id": client_id, "jti": jti_value,
                 "expires_at": expires_at}
                for (client_id, jti_value), expires_at in self._jti.items()
            ]
            return {
                "version": _SNAPSHOT_VERSION,
                "keys": keys,
                "jti": jti,
                "stats": self.stats(),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            keys, active, jti, stats = self._parse_snapshot(state)
            self._keys = keys
            self._active = active
            self._jti = jti
            self._verified = stats["verified"]
            self._rejected = stats["rejected"]
            self._jti_total = stats["jti"]
            self._purged = stats["purged"]

    def _parse_snapshot(self, state: Any):
        invalid = _fail("ASSERTION_SNAPSHOT_INVALID")
        if not isinstance(state, dict):
            raise invalid
        if state.get("version") != _SNAPSHOT_VERSION:
            raise invalid

        raw_keys = state.get("keys")
        raw_jti = state.get("jti")
        raw_stats = state.get("stats")
        if not isinstance(raw_keys, list) or not isinstance(raw_jti, list):
            raise invalid
        if not isinstance(raw_stats, dict):
            raise invalid

        keys: dict[str, dict[str, dict[str, Any]]] = {}
        active: dict[str, str | None] = {}
        active_kid: dict[str, str | None] = {}
        for item in raw_keys:
            if not isinstance(item, dict):
                raise invalid
            client_id = item.get("client_id")
            kid = item.get("kid")
            secret = item.get("secret")
            is_active = item.get("active")
            retired = item.get("retired")
            if not isinstance(client_id, str) or not isinstance(kid, str):
                raise invalid
            if not isinstance(is_active, bool) or not isinstance(retired, bool):
                raise invalid
            try:
                secret_bytes = self._snapshot_secret(secret)
            except (TypeError, ValueError):
                raise invalid

            client_keys = keys.setdefault(client_id, {})
            if kid in client_keys:
                raise invalid
            client_keys[kid] = {"secret": secret_bytes, "retired": retired}
            active.setdefault(client_id, None)
            if is_active and not retired:
                if active_kid.get(client_id) is not None:
                    raise invalid
                active_kid[client_id] = kid

        for client_id, kid in active_kid.items():
            active[client_id] = kid

        jti: dict[tuple[str, str], float] = {}
        for item in raw_jti:
            if not isinstance(item, dict):
                raise invalid
            client_id = item.get("client_id")
            jti_value = item.get("jti")
            expires_at = item.get("expires_at")
            if not isinstance(client_id, str) or not isinstance(jti_value, str):
                raise invalid
            if not self._is_number(expires_at) or not math.isfinite(expires_at):
                raise invalid
            key = (client_id, jti_value)
            if key in jti:
                raise invalid
            jti[key] = expires_at

        stats = {}
        for name in _STAT_KEYS:
            value = raw_stats.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise invalid
            stats[name] = value

        keys_count = sum(len(client_keys) for client_keys in keys.values())
        retired_count = sum(
            1
            for client_keys in keys.values()
            for record in client_keys.values()
            if record["retired"]
        )
        if stats["keys"] != keys_count or stats["retired"] != retired_count:
            raise invalid

        return keys, active, jti, stats

    @staticmethod
    def _snapshot_secret(value: Any) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        if not isinstance(value, str):
            raise TypeError("invalid snapshot secret")
        return _b64decode(value, code="ASSERTION_SNAPSHOT_INVALID")

    def stats(self) -> dict:
        with self._lock:
            keys_count = sum(
                len(client_keys) for client_keys in self._keys.values()
            )
            retired_count = sum(
                1
                for client_keys in self._keys.values()
                for record in client_keys.values()
                if record["retired"]
            )
            return {
                "keys": keys_count,
                "retired": retired_count,
                "verified": self._verified,
                "rejected": self._rejected,
                "jti": self._jti_total,
                "purged": self._purged,
            }

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------
    def _resolve_now(self, now):
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return time.time()
