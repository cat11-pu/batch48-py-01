"""Download grant guard: short-lived signed grants bound to a resource root."""

from __future__ import annotations

import copy
import re
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote

RESOURCE_ROOT = "/data"
ALLOWED_METHODS = ("GET", "HEAD")
_GRANT_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_GRANT_FIELDS = frozenset((
    "grant_id", "resource_id", "subject", "root",
    "issued_at", "expires_at", "consumed",
))


class DownloadError(ValueError):
    """Raised when a download grant operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class GrantDecision:
    allowed: bool
    reason: str
    expires_at: int | None


class DownloadGrantGuard:
    """Issue, authorize, consume and revoke download grants."""

    def __init__(self, *, max_grants: int = 1000) -> None:
        if not isinstance(max_grants, int) or isinstance(max_grants, bool) or max_grants < 0:
            raise DownloadError("DOWNLOAD_GRANT_LIMIT", "invalid max_grants")
        self.max_grants = max_grants
        self._lock = threading.RLock()
        self._grants: dict[str, dict] = {}
        self._watermarks: dict[str, int] = {}

    def issue(self, resource_id: str, subject: str, ttl: int, now: int,
              *, grant_id: str | None = None) -> dict:
        self._validate_resource_id(resource_id)
        if not isinstance(subject, str) or subject == "":
            raise DownloadError("DOWNLOAD_INVALID_SUBJECT", "subject must be a non-empty string")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl <= 0:
            raise DownloadError("DOWNLOAD_INVALID_TTL", "ttl must be a positive integer")
        self._validate_now(now)
        with self._lock:
            if len(self._grants) >= self.max_grants:
                raise DownloadError("DOWNLOAD_GRANT_LIMIT", "grant table is full")
            if grant_id is None:
                candidate = secrets.token_hex(16)
                while candidate in self._grants:
                    candidate = secrets.token_hex(16)
            else:
                if not isinstance(grant_id, str) or not _GRANT_ID_RE.fullmatch(grant_id):
                    raise DownloadError("DOWNLOAD_INVALID_GRANT", "grant_id must be 32 lowercase hex chars")
                if grant_id in self._grants:
                    raise DownloadError("DOWNLOAD_INVALID_GRANT", "grant_id already issued")
                candidate = grant_id
            record = {
                "grant_id": candidate,
                "resource_id": resource_id,
                "subject": subject,
                "root": f"{RESOURCE_ROOT}/{resource_id}",
                "issued_at": now,
                "expires_at": now + ttl,
                "consumed": False,
            }
            self._grants[candidate] = record
            return copy.deepcopy(record)

    def authorize(self, grant: object, request_path: str, method: str,
                  now: int) -> GrantDecision:
        self._validate_now(now)
        if method not in ALLOWED_METHODS:
            return GrantDecision(False, "method-not-allowed", self._expires_of(grant))
        normalized, escaped = self._normalize_path(request_path)
        if normalized is None:
            return GrantDecision(False, "path-invalid", self._expires_of(grant))
        record = self._lookup(grant)
        expires_at = record["expires_at"]
        root_segments = (RESOURCE_ROOT.strip("/").split("/")
                         + [record["resource_id"]])
        if escaped or normalized[: len(root_segments)] != root_segments:
            return GrantDecision(False, "path-outside-resource", expires_at)
        if record["issued_at"] <= self._watermarks.get(record["resource_id"], -1):
            return GrantDecision(False, "revoked", expires_at)
        if record["consumed"]:
            return GrantDecision(False, "consumed", expires_at)
        if now >= record["expires_at"]:
            return GrantDecision(False, "expired", expires_at)
        return GrantDecision(True, "ok", expires_at)

    def consume(self, grant_id: str, now: int) -> bool:
        self._validate_now(now)
        with self._lock:
            record = self._grants.get(self._grant_key(grant_id))
            if record is None:
                raise DownloadError("DOWNLOAD_UNKNOWN_GRANT", grant_id)
            if record["consumed"]:
                raise DownloadError("DOWNLOAD_TOKEN_REPLAYED", grant_id)
            if now >= record["expires_at"]:
                raise DownloadError("DOWNLOAD_GRANT_EXPIRED", grant_id)
            if record["issued_at"] <= self._watermarks.get(record["resource_id"], -1):
                raise DownloadError("DOWNLOAD_GRANT_REVOKED", grant_id)
            record["consumed"] = True
            return True

    def revoke(self, resource_id: str, now: int = 0) -> int:
        self._validate_resource_id(resource_id)
        self._validate_now(now)
        with self._lock:
            previous = self._watermarks.get(resource_id, -1)
            count = 0
            for record in self._grants.values():
                if (record["resource_id"] == resource_id
                        and not record["consumed"]
                        and previous < record["issued_at"] <= now):
                    count += 1
            if now > previous:
                self._watermarks[resource_id] = now
            return count

    def purge(self, now: int) -> int:
        self._validate_now(now)
        with self._lock:
            expired = [grant_id for grant_id, record in self._grants.items()
                       if record["expires_at"] <= now]
            for grant_id in expired:
                del self._grants[grant_id]
            return len(expired)

    def inspect(self, grant_id: str) -> dict:
        with self._lock:
            record = self._grants.get(self._grant_key(grant_id))
            if record is None:
                raise DownloadError("DOWNLOAD_UNKNOWN_GRANT", grant_id)
            return copy.deepcopy(record)

    def grants(self) -> list:
        with self._lock:
            return [copy.deepcopy(record) for record in self._grants.values()]

    def watermark(self, resource_id: str) -> int | None:
        with self._lock:
            return self._watermarks.get(resource_id)

    def check_range(self, grant: object, header: str, size: int, now: int) -> dict:
        self._validate_now(now)
        record = self._lookup(grant)
        if now >= record["expires_at"]:
            return {"allowed": False, "reason": "expired", "ranges": []}
        if record["consumed"]:
            return {"allowed": False, "reason": "consumed", "ranges": []}
        if record["issued_at"] <= self._watermarks.get(record["resource_id"], -1):
            return {"allowed": False, "reason": "revoked", "ranges": []}
        parsed, reason = self._parse_range(header, size)
        if reason is not None:
            return {"allowed": False, "reason": reason, "ranges": []}
        return {"allowed": True, "reason": "ok", "ranges": parsed}

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "grants": copy.deepcopy(self._grants),
                "watermarks": copy.deepcopy(self._watermarks),
                "observed_at": _now_seconds(),
            }

    def restore(self, state: dict) -> None:
        grants, watermarks = self._validate_snapshot(state)
        with self._lock:
            self._grants = copy.deepcopy(grants)
            self._watermarks = copy.deepcopy(watermarks)

    @staticmethod
    def _validate_resource_id(resource_id: object) -> None:
        if not isinstance(resource_id, str) or not _RESOURCE_ID_RE.fullmatch(resource_id):
            raise DownloadError("DOWNLOAD_INVALID_RESOURCE", resource_id)

    @staticmethod
    def _validate_now(now: object) -> None:
        if not isinstance(now, int) or isinstance(now, bool) or now < 0:
            raise DownloadError("DOWNLOAD_INVALID_NOW", now)

    @staticmethod
    def _grant_key(grant_id: object) -> str:
        if not isinstance(grant_id, str) or not _GRANT_ID_RE.fullmatch(grant_id):
            raise DownloadError("DOWNLOAD_INVALID_GRANT", grant_id)
        return grant_id

    def _lookup(self, grant: object) -> dict:
        if not isinstance(grant, dict):
            raise DownloadError("DOWNLOAD_INVALID_GRANT", "grant must be a dict")
        grant_id = grant.get("grant_id")
        if not isinstance(grant_id, str) or not _GRANT_ID_RE.fullmatch(grant_id):
            raise DownloadError("DOWNLOAD_INVALID_GRANT", "missing or malformed grant_id")
        with self._lock:
            record = self._grants.get(grant_id)
            if record is None:
                raise DownloadError("DOWNLOAD_UNKNOWN_GRANT", grant_id)
            return record

    def _expires_of(self, grant: object) -> int | None:
        try:
            record = self._lookup(grant)
        except DownloadError:
            return None
        return record["expires_at"]

    @staticmethod
    def _normalize_path(request_path: object) -> tuple[list[str] | None, bool]:
        if not isinstance(request_path, str) or request_path == "":
            return None, False
        decoded = request_path
        previous = None
        iterations = 0
        while decoded != previous:
            if iterations >= 16:
                return None, False
            previous = decoded
            decoded = unquote(decoded)
            iterations += 1
        if "\x00" in decoded:
            return None, False
        raw_segments = decoded.split("/")
        stack: list[str] = []
        depth = 0
        escaped = False
        for segment in raw_segments:
            if segment == "":
                continue
            if segment == ".":
                continue
            if segment == "..":
                if depth == 0:
                    escaped = True
                    continue
                stack.pop()
                depth -= 1
                continue
            if segment.endswith(".") or segment.endswith(" "):
                return None, False
            stack.append(segment)
            depth += 1
        if not stack:
            if escaped:
                return [], True
            return None, False
        return stack, escaped

    def _parse_range(self, header: object, size: object) -> tuple[list[tuple[int, int]] | None, str | None]:
        if not isinstance(header, str) or not header.startswith("bytes="):
            return None, "range-invalid"
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            return None, "range-invalid"
        body = header[len("bytes="):]
        if body == "":
            return None, "range-invalid"
        pieces = [part.strip() for part in body.split(",")]
        if any(piece == "" for piece in pieces):
            return None, "range-invalid"
        ranges: list[tuple[int, int]] = []
        for piece in pieces:
            if piece.count("-") != 1:
                return None, "range-invalid"
            start_text, end_text = piece.split("-")
            if start_text == "":
                if end_text == "" or not end_text.isdigit():
                    return None, "range-invalid"
                suffix = int(end_text)
                if suffix <= 0 or size == 0:
                    return None, "range-unsatisfiable"
                start = max(0, size - suffix)
                end = size - 1
            elif not start_text.isdigit():
                return None, "range-invalid"
            else:
                start = int(start_text)
                if start >= size:
                    return None, "range-unsatisfiable"
                if end_text == "":
                    end = size - 1
                elif not end_text.isdigit():
                    return None, "range-invalid"
                else:
                    end = int(end_text)
                    if end < start:
                        return None, "range-unsatisfiable"
                    end = min(end, size - 1)
            ranges.append((start, end))
        return ranges, None

    def _validate_snapshot(self, state: object) -> tuple[dict[str, dict], dict[str, int]]:
        if not isinstance(state, dict):
            raise DownloadError("DOWNLOAD_SNAPSHOT_INVALID", "state must be a dict")
        grants = state.get("grants")
        watermarks = state.get("watermarks", {})
        if not isinstance(grants, dict) or not isinstance(watermarks, dict):
            raise DownloadError("DOWNLOAD_SNAPSHOT_INVALID", "grants and watermarks must be dicts")
        clean_grants: dict[str, dict] = {}
        for grant_id, record in grants.items():
            if (not isinstance(grant_id, str) or not _GRANT_ID_RE.fullmatch(grant_id)
                    or not isinstance(record, dict)
                    or set(record.keys()) != _GRANT_FIELDS):
                raise DownloadError("DOWNLOAD_SNAPSHOT_INVALID", "malformed grant record")
            if (record["grant_id"] != grant_id
                    or not isinstance(record["resource_id"], str)
                    or not isinstance(record["subject"], str)
                    or not isinstance(record["root"], str)
                    or not isinstance(record["issued_at"], int)
                    or isinstance(record["issued_at"], bool)
                    or not isinstance(record["expires_at"], int)
                    or isinstance(record["expires_at"], bool)
                    or not isinstance(record["consumed"], bool)
                    or record["expires_at"] < record["issued_at"]):
                raise DownloadError("DOWNLOAD_SNAPSHOT_INVALID", "malformed grant field")
            clean_grants[grant_id] = record
        clean_watermarks: dict[str, int] = {}
        for resource_id, mark in watermarks.items():
            if not isinstance(resource_id, str) or not isinstance(mark, int) or isinstance(mark, bool):
                raise DownloadError("DOWNLOAD_SNAPSHOT_INVALID", "malformed watermark")
            clean_watermarks[resource_id] = mark
        return clean_grants, clean_watermarks


def _now_seconds() -> int:
    return int(time.time())
