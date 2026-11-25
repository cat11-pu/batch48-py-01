"""Signed incremental trust-root store with atomic root set activation."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import threading
from datetime import datetime, timezone
from typing import Any


class TrustStoreError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class TrustStoreUpdateGuard:
    """Verify signed bundles, keep versions monotonic and swap root sets."""

    _STAT_KEYS = ("install_ok", "install_rejected", "lookups", "lookups_missed")
    _ROOT_KEYS = ("subject", "fingerprint", "not_before", "not_after", "ca")
    _BUNDLE_KEYS = ("version", "issuer", "roots", "digest", "signature")

    def __init__(self, *, max_roots: int = 32, clock=None) -> None:
        if (
            not isinstance(max_roots, int)
            or isinstance(max_roots, bool)
            or max_roots < 0
        ):
            raise ValueError("max_roots must be a non-negative integer")
        self.max_roots = max_roots
        self._clock = clock
        self._lock = threading.RLock()
        self._version = 0
        self._active: list[dict[str, Any]] = []
        self._legacy: list[dict[str, Any]] = []
        self._issuers: dict[str, dict[str, Any]] = {}
        self._rejections: list[dict[str, Any]] = []
        self._stats = {key: 0 for key in self._STAT_KEYS}

    def register_issuer(self, issuer_id: str, secret: bytes) -> None:
        if not isinstance(issuer_id, str) or not issuer_id:
            raise TrustStoreError("TRUST_BUNDLE_MALFORMED")
        normalized_secret = self._normalize_secret(secret)
        with self._lock:
            existing = self._issuers.get(issuer_id)
            if existing is not None:
                if not hmac.compare_digest(existing["secret"], normalized_secret):
                    raise TrustStoreError("TRUST_ISSUER_CONFLICT")
                existing["revoked"] = False
                return
            self._issuers[issuer_id] = {
                "secret": normalized_secret,
                "revoked": False,
            }

    def revoke_issuer(self, issuer_id: str) -> None:
        with self._lock:
            issuer = self._issuers.get(issuer_id)
            if issuer is not None:
                issuer["revoked"] = True

    def install(self, bundle: dict, *, now=None) -> dict:
        with self._lock:
            version_hint = self._version_hint(bundle)
            try:
                validated = self._validate_bundle(bundle)
                issuer_id = validated["issuer"]
                issuer = self._issuers.get(issuer_id)
                if issuer is None or issuer["revoked"]:
                    raise TrustStoreError("TRUST_ISSUER_UNKNOWN")

                roots = validated["roots"]
                recomputed_digest = self._digest_roots(roots)
                if not hmac.compare_digest(validated["digest"], recomputed_digest):
                    raise TrustStoreError("TRUST_DIGEST_MISMATCH")

                message = "%s:%d:%s" % (
                    issuer_id,
                    validated["version"],
                    validated["digest"],
                )
                expected_signature = hmac.new(
                    issuer["secret"], message.encode("utf-8"), hashlib.sha256
                ).hexdigest()
                if not hmac.compare_digest(
                    validated["signature"], expected_signature
                ):
                    raise TrustStoreError("TRUST_SIGNATURE_INVALID")

                normalized_roots = self._validate_roots(roots)
                if validated["version"] <= self._version:
                    raise TrustStoreError("TRUST_VERSION_REGRESSION")

                current = self._resolve_now(now)
                next_active = [
                    copy.deepcopy(root)
                    for root in normalized_roots
                    if not self._is_expired(root, current)
                ]
                if len(next_active) > self.max_roots:
                    raise TrustStoreError("TRUST_CAPACITY_EXCEEDED")

                next_legacy = copy.deepcopy(self._legacy)
                next_legacy.extend(
                    copy.deepcopy(root)
                    for root in self._active
                    if self._is_expired(root, current)
                )
                next_legacy.extend(
                    copy.deepcopy(root)
                    for root in normalized_roots
                    if self._is_expired(root, current)
                )
                next_legacy = self._deduplicate_roots(next_legacy)
                active_fingerprints = {
                    root["fingerprint"] for root in next_active
                }
                next_legacy = [
                    root
                    for root in next_legacy
                    if root["fingerprint"] not in active_fingerprints
                ]

                self._version = validated["version"]
                self._active = next_active
                self._legacy = next_legacy
                self._stats["install_ok"] += 1
                return {
                    "status": "installed",
                    "version": self._version,
                    "active": len(self._active),
                    "legacy": len(self._legacy),
                }
            except TrustStoreError as exc:
                self._rejections.append(
                    {"code": exc.code, "version": version_hint}
                )
                self._stats["install_rejected"] += 1
                if exc.code == "TRUST_SIGNATURE_INVALID" and self._version > 0:
                    return {
                        "status": "rejected",
                        "code": exc.code,
                        "version": self._version,
                        "active": len(self._active),
                        "legacy": len(self._legacy),
                    }
                raise

    def active_roots(self, *, now=None) -> list:
        with self._lock:
            current = self._resolve_now(now)
            return copy.deepcopy(
                [
                    root
                    for root in self._active
                    if not self._is_expired(root, current)
                ]
            )

    def legacy_roots(self, *, now=None) -> list:
        with self._lock:
            current = self._resolve_now(now)
            legacy = copy.deepcopy(self._legacy)
            legacy.extend(
                copy.deepcopy(root)
                for root in self._active
                if self._is_expired(root, current)
            )
            active_fingerprints = {
                root["fingerprint"]
                for root in self._active
                if not self._is_expired(root, current)
            }
            return [
                root
                for root in self._deduplicate_roots(legacy)
                if root["fingerprint"] not in active_fingerprints
            ]

    def lookup(self, subject: str, *, now=None) -> dict:
        with self._lock:
            current = self._resolve_now(now)
            for root in self._active:
                if (
                    root["subject"] == subject
                    and not self._is_expired(root, current)
                ):
                    self._stats["lookups"] += 1
                    return {
                        "subject": root["subject"],
                        "fingerprint": root["fingerprint"],
                        "not_before": root["not_before"],
                        "not_after": root["not_after"],
                        "ca": root["ca"],
                    }
            self._stats["lookups_missed"] += 1
            raise TrustStoreError("TRUST_ROOT_NOT_FOUND")

    def current_version(self) -> int:
        with self._lock:
            return self._version

    def rejection_log(self) -> list:
        with self._lock:
            return copy.deepcopy(self._rejections)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "roots": {
                    "active": copy.deepcopy(self._active),
                    "legacy": copy.deepcopy(self._legacy),
                },
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            restored = self._validate_snapshot(state)
            self._version = restored["version"]
            self._active = restored["active"]
            self._legacy = restored["legacy"]
            self._stats = restored["stats"]

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in self._STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in self._STAT_KEYS}

    def _validate_bundle(self, bundle: object) -> dict[str, Any]:
        if not isinstance(bundle, dict):
            raise TrustStoreError("TRUST_BUNDLE_MALFORMED")
        if any(key not in bundle for key in self._BUNDLE_KEYS):
            raise TrustStoreError("TRUST_BUNDLE_MALFORMED")

        version = bundle["version"]
        issuer = bundle["issuer"]
        roots = bundle["roots"]
        digest = bundle["digest"]
        signature = bundle["signature"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version <= 0
            or not isinstance(issuer, str)
            or not issuer
            or not isinstance(roots, list)
            or not isinstance(digest, str)
            or not isinstance(signature, str)
            or len(signature) != 64
        ):
            raise TrustStoreError("TRUST_BUNDLE_MALFORMED")
        return {
            "version": version,
            "issuer": issuer,
            "roots": roots,
            "digest": digest,
            "signature": signature,
        }

    def _validate_roots(self, roots: list) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        try:
            copied_roots = copy.deepcopy(roots)
        except Exception as exc:
            raise TrustStoreError("TRUST_ROOT_INVALID") from exc

        for root in copied_roots:
            if not isinstance(root, dict):
                raise TrustStoreError("TRUST_ROOT_INVALID")
            if any(key not in root for key in self._ROOT_KEYS):
                raise TrustStoreError("TRUST_ROOT_INVALID")
            subject = root["subject"]
            fingerprint = root["fingerprint"]
            if (
                not isinstance(subject, str)
                or not subject.strip()
                or not isinstance(fingerprint, str)
                or not fingerprint
                or root["ca"] is not True
            ):
                raise TrustStoreError("TRUST_ROOT_INVALID")
            not_before = self._parse_time(root["not_before"])
            not_after = self._parse_time(root["not_after"])
            if not_before > not_after:
                raise TrustStoreError("TRUST_ROOT_INVALID")
            if fingerprint in seen:
                raise TrustStoreError("TRUST_DUPLICATE_FINGERPRINT")
            seen.add(fingerprint)
            normalized.append(root)
        return normalized

    def _validate_snapshot(self, state: object) -> dict[str, Any]:
        if not isinstance(state, dict):
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID")
        if not {"version", "roots", "stats"}.issubset(state):
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID")
        version = state["version"]
        roots = state["roots"]
        stats = state["stats"]
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version < 0
            or not isinstance(roots, dict)
            or not {"active", "legacy"}.issubset(roots)
            or not isinstance(roots["active"], list)
            or not isinstance(roots["legacy"], list)
            or not isinstance(stats, dict)
            or set(stats) != set(self._STAT_KEYS)
        ):
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID")
        for value in stats.values():
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise TrustStoreError("TRUST_SNAPSHOT_INVALID")

        try:
            active = self._validate_roots(roots["active"])
            legacy = self._validate_roots(roots["legacy"])
            active_copy = copy.deepcopy(active)
            legacy_copy = copy.deepcopy(legacy)
            stats_copy = {key: int(stats[key]) for key in self._STAT_KEYS}
        except TrustStoreError as exc:
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID") from exc
        except Exception as exc:
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID") from exc

        if len(active_copy) > self.max_roots:
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID")
        active_fingerprints = {root["fingerprint"] for root in active_copy}
        legacy_fingerprints = {root["fingerprint"] for root in legacy_copy}
        if active_fingerprints & legacy_fingerprints:
            raise TrustStoreError("TRUST_SNAPSHOT_INVALID")
        return {
            "version": version,
            "active": active_copy,
            "legacy": legacy_copy,
            "stats": stats_copy,
        }

    def _version_hint(self, bundle: object) -> Any:
        if not isinstance(bundle, dict):
            return 0
        version = bundle.get("version", 0)
        if isinstance(version, int) and not isinstance(version, bool):
            return version
        return 0

    @staticmethod
    def _normalize_secret(secret: object) -> bytes:
        if isinstance(secret, bytes):
            return secret
        if isinstance(secret, bytearray):
            return bytes(secret)
        if isinstance(secret, memoryview):
            return secret.tobytes()
        if isinstance(secret, str):
            return secret.encode("utf-8")
        raise ValueError("secret must be bytes-like or a string")

    @staticmethod
    def _digest_roots(roots: list) -> str:
        try:
            blob = json.dumps(
                roots,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError) as exc:
            raise TrustStoreError("TRUST_BUNDLE_MALFORMED") from exc
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _resolve_now(self, now: object) -> datetime:
        value = now
        if value is None:
            value = self._clock() if self._clock is not None else None
        if value is None:
            return datetime.now(timezone.utc)
        return self._parse_time(value)

    @staticmethod
    def _parse_time(value: object) -> datetime:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise TrustStoreError("TRUST_ROOT_INVALID") from exc
        else:
            raise TrustStoreError("TRUST_ROOT_INVALID")
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _is_expired(self, root: dict[str, Any], current: datetime) -> bool:
        return self._parse_time(root["not_after"]) <= current

    @staticmethod
    def _deduplicate_roots(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: list[dict[str, Any]] = []
        seen: set[str] = set()
        for root in roots:
            fingerprint = root["fingerprint"]
            if fingerprint not in seen:
                seen.add(fingerprint)
                unique.append(root)
        return unique
