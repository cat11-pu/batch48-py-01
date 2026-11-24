"""TCP Fast Open cookie service with key rotation and address binding."""

from __future__ import annotations

import copy
import hashlib
import heapq
import hmac
import ipaddress
import os
import threading
from collections import OrderedDict
from typing import Any


TFO_INVALID_ADDRESS = "TFO_INVALID_ADDRESS"
TFO_NO_KEY = "TFO_NO_KEY"
TFO_DUPLICATE_KEY = "TFO_DUPLICATE_KEY"
TFO_UNKNOWN_KEY = "TFO_UNKNOWN_KEY"
TFO_RATE_LIMITED = "TFO_RATE_LIMITED"
TFO_SNAPSHOT_INVALID = "TFO_SNAPSHOT_INVALID"

_STATS_KEYS = (
    "issued",
    "verified",
    "accepted",
    "rejected",
    "rotated",
    "rate_limited",
)
_KEY_STATES = ("active", "previous", "revoked")
_SNAPSHOT_KEY_FIELDS = {
    "key_id",
    "generation",
    "state",
    "grace_until_ms",
    "secret_hex",
}


class TfoError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _valid_port(port: str) -> bool:
    if not port or any(char not in "0123456789" for char in port):
        return False
    return 0 <= int(port) <= 65535


def normalize_address(addr: object) -> str:
    """Normalize an IP address, IPv4-mapped IPv6 form, or IP:port input."""
    if not isinstance(addr, str):
        raise TfoError(TFO_INVALID_ADDRESS)

    host = addr
    if addr.startswith("["):
        end = addr.find("]")
        if end < 0:
            raise TfoError(TFO_INVALID_ADDRESS)
        host = addr[1:end]
        tail = addr[end + 1 :]
        if tail and (not tail.startswith(":") or not _valid_port(tail[1:])):
            raise TfoError(TFO_INVALID_ADDRESS)
    elif addr.count(":") == 1:
        host, port = addr.rsplit(":", 1)
        if not _valid_port(port):
            raise TfoError(TFO_INVALID_ADDRESS)

    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise TfoError(TFO_INVALID_ADDRESS) from exc

    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        ip = mapped
    return str(ip)


def _zero_stats() -> dict[str, int]:
    return {name: 0 for name in _STATS_KEYS}


class TfoCookieGuard:
    """Issue, verify and rotate TFO cookies bound to client addresses."""

    def __init__(
        self,
        *,
        cookie_ttl_ms: int = 60000,
        key_grace_ms: int = 30000,
        rate_limit: int = 3,
        rate_window_ms: int = 1000,
    ) -> None:
        self.cookie_ttl_ms = cookie_ttl_ms
        self.key_grace_ms = key_grace_ms
        self.rate_limit = rate_limit
        self.rate_window_ms = rate_window_ms
        self._lock = threading.RLock()
        self._keys: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._current_id: str | None = None
        self._next_generation = 1
        self._rate: OrderedDict[str, list[int]] = OrderedDict()
        self._rate_expiry: dict[str, int] = {}
        self._rate_heap: list[tuple[int, str]] = []
        self._bindings: dict[str, tuple[str, int]] = {}
        self._binding_heap: list[tuple[int, str]] = []
        self._stats = _zero_stats()

    def rotate_key(self, key_id: str, *, now_ms: int = 0) -> dict:
        with self._lock:
            if key_id in self._keys:
                raise TfoError(TFO_DUPLICATE_KEY)

            previous_id = self._current_id
            generation = self._next_generation
            record: dict[str, Any] = {
                "key_id": key_id,
                "generation": generation,
                "secret": hashlib.sha256(
                    f"tfo-key:{key_id}:{generation}:{os.getpid()}".encode("utf-8")
                    + os.urandom(32)
                ).digest(),
                "revoked": False,
                "grace_until_ms": None,
            }

            if previous_id is not None:
                previous = self._keys[previous_id]
                if not previous["revoked"]:
                    previous["grace_until_ms"] = now_ms + self.key_grace_ms

            self._keys[key_id] = record
            retained = {key_id, previous_id}
            for old_id in list(self._keys):
                if old_id not in retained:
                    del self._keys[old_id]

            self._current_id = key_id
            self._next_generation = generation + 1
            self._stats["rotated"] += 1
            return {
                "key_id": key_id,
                "generation": generation,
                "previous": previous_id,
            }

    def revoke_key(self, key_id: str) -> None:
        with self._lock:
            record = self._keys.get(key_id)
            if record is None:
                raise TfoError(TFO_UNKNOWN_KEY)
            record["revoked"] = True
            record["grace_until_ms"] = None

    def issue(self, addr: str, *, now_ms: int = 0) -> dict:
        normalized = self.normalize_addr(addr)
        with self._lock:
            record = self._current_record_locked()
            if record is None:
                raise TfoError(TFO_NO_KEY)

            self._prune_rate_locked(now_ms)
            history = self._rate.get(normalized)
            if history is None:
                history = []
                self._rate[normalized] = history

            cutoff = now_ms - self.rate_window_ms
            history[:] = [timestamp for timestamp in history if timestamp > cutoff]

            if len(history) >= self.rate_limit:
                self._stats["rejected"] += 1
                self._stats["rate_limited"] += 1
                raise TfoError(TFO_RATE_LIMITED)

            expires_ms = now_ms + self.cookie_ttl_ms
            cookie = self._build_cookie_locked(
                record, normalized, record["generation"], expires_ms
            )
            history.append(now_ms)
            self._update_rate_expiry_locked(normalized, history)
            self._remember_binding_locked(cookie, normalized, expires_ms)

            self._stats["issued"] += 1
            return {
                "cookie": cookie,
                "key_id": record["key_id"],
                "expires_ms": expires_ms,
            }

    def verify(self, addr: str, cookie: str, *, now_ms: int = 0) -> dict:
        normalized = self.normalize_addr(addr)
        with self._lock:
            self._stats["verified"] += 1
            self._prune_rate_locked(now_ms)
            self._prune_bindings_locked(now_ms)
            result = self._verify_locked(normalized, cookie, now_ms)
            if result["ok"]:
                self._stats["accepted"] += 1
            else:
                self._stats["rejected"] += 1
            return result

    def normalize_addr(self, addr: str) -> str:
        return normalize_address(addr)

    def sfo_deliverable(self, addr: str, cookie: str, *, now_ms: int = 0) -> bool:
        try:
            result = self.verify(addr, cookie, now_ms=now_ms)
        except TfoError:
            return False
        return result["ok"] is True

    def list_keys(self) -> list:
        with self._lock:
            items = []
            for key_id in sorted(self._keys):
                record = self._keys[key_id]
                state = self._state_locked(record)
                grace_until_ms = (
                    record["grace_until_ms"] if state == "previous" else None
                )
                items.append(
                    {
                        "key_id": key_id,
                        "generation": record["generation"],
                        "state": state,
                        "grace_until_ms": grace_until_ms,
                    }
                )
            return items

    def snapshot(self) -> dict:
        with self._lock:
            keys = {}
            for key_id, record in self._keys.items():
                state = self._state_locked(record)
                keys[key_id] = {
                    "key_id": key_id,
                    "generation": record["generation"],
                    "state": state,
                    "grace_until_ms": (
                        record["grace_until_ms"] if state == "previous" else None
                    ),
                    "secret_hex": record["secret"].hex(),
                }

            state = {
                "version": 1,
                "keys": keys,
                "rate": {
                    address: list(timestamps)
                    for address, timestamps in self._rate.items()
                },
                "stats": dict(self._stats),
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = _parse_snapshot(state, self.rate_window_ms)
            except Exception as exc:
                raise TfoError(TFO_SNAPSHOT_INVALID) from exc

            self._keys = parsed["keys"]
            self._current_id = parsed["current_id"]
            self._next_generation = parsed["next_generation"]
            self._rate = parsed["rate"]
            self._rate_expiry = parsed["rate_expiry"]
            self._rate_heap = parsed["rate_heap"]
            self._stats = parsed["stats"]
            self._bindings = {}
            self._binding_heap = []

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = _zero_stats()

    def _current_record_locked(self) -> dict[str, Any] | None:
        if self._current_id is None:
            return None
        record = self._keys.get(self._current_id)
        if record is None or record["revoked"]:
            return None
        return record

    def _state_locked(self, record: dict[str, Any]) -> str:
        if record["revoked"]:
            return "revoked"
        if record["key_id"] == self._current_id:
            return "active"
        return "previous"

    def _signing_key_locked(
        self, record: dict[str, Any], generation_bytes: bytes
    ) -> bytes:
        return hmac.new(
            record["secret"],
            b"tfo-generation-key-v1" + generation_bytes,
            hashlib.sha256,
        ).digest()

    def _tag_for_locked(
        self,
        record: dict[str, Any],
        normalized: str,
        generation_bytes: bytes,
        expires_bytes: bytes,
    ) -> bytes:
        signing_key = self._signing_key_locked(record, generation_bytes)
        message = normalized.encode("utf-8") + generation_bytes + expires_bytes
        digest = hmac.new(signing_key, message, hashlib.sha256).digest()
        return digest[:16]

    def _build_cookie_locked(
        self,
        record: dict[str, Any],
        normalized: str,
        generation: int,
        expires_ms: int,
    ) -> str:
        generation_bytes = generation.to_bytes(8, "big", signed=False)
        expires_bytes = expires_ms.to_bytes(8, "big", signed=False)
        tag = self._tag_for_locked(
            record, normalized, generation_bytes, expires_bytes
        )
        return (generation_bytes + expires_bytes + tag).hex()

    def _parse_cookie(self, cookie: object) -> tuple[bytes, bytes, bytes] | None:
        if not isinstance(cookie, str) or len(cookie) != 64:
            return None
        if any(char not in "0123456789abcdef" for char in cookie):
            return None
        raw = bytes.fromhex(cookie)
        if len(raw) != 32:
            return None
        return raw[:8], raw[8:16], raw[16:]

    def _verify_locked(
        self, normalized: str, cookie: object, now_ms: int
    ) -> dict[str, Any]:
        if not isinstance(cookie, str):
            return {"ok": False, "key_id": None, "reason": "malformed"}
        parsed = self._parse_cookie(cookie)
        if parsed is None:
            return {"ok": False, "key_id": None, "reason": "malformed"}

        generation_bytes, expires_bytes, presented_tag = parsed
        generation = int.from_bytes(generation_bytes, "big", signed=False)
        expires_ms = int.from_bytes(expires_bytes, "big", signed=False)
        record = next(
            (
                candidate
                for candidate in self._keys.values()
                if candidate["generation"] == generation
            ),
            None,
        )
        if record is None:
            tampered_key_id = self._tampered_generation_key_locked(
                normalized, expires_bytes, presented_tag
            )
            if tampered_key_id is not None:
                return {
                    "ok": False,
                    "key_id": tampered_key_id,
                    "reason": "bad_signature",
                }
            return {"ok": False, "key_id": None, "reason": "key_unknown"}

        key_id = record["key_id"]
        if record["revoked"]:
            return {"ok": False, "key_id": key_id, "reason": "key_revoked"}

        expected_tag = self._tag_for_locked(
            record, normalized, generation_bytes, expires_bytes
        )
        if hmac.compare_digest(expected_tag, presented_tag):
            if now_ms >= expires_ms:
                return {"ok": False, "key_id": key_id, "reason": "expired"}

            state = self._state_locked(record)
            grace_until_ms = record["grace_until_ms"]
            if (
                state == "previous"
                and grace_until_ms is not None
                and now_ms >= grace_until_ms
            ):
                return {
                    "ok": False,
                    "key_id": key_id,
                    "reason": "key_unknown",
                }

            self._remember_binding_locked(
                cookie,
                normalized,
                expires_ms,
            )
            return {"ok": True, "key_id": key_id, "reason": "ok"}

        bound_address = self._bound_address_for_cookie_locked(
            cookie,
            record,
            generation_bytes,
            expires_bytes,
            presented_tag,
        )
        if bound_address is not None and bound_address != normalized:
            return {
                "ok": False,
                "key_id": key_id,
                "reason": "address_mismatch",
            }

        return {"ok": False, "key_id": key_id, "reason": "bad_signature"}

    def _remember_binding_locked(
        self, cookie: str, normalized: str, expires_ms: int
    ) -> None:
        if cookie not in self._bindings:
            self._bindings[cookie] = (normalized, expires_ms)
            heapq.heappush(self._binding_heap, (expires_ms, cookie))

    def _prune_bindings_locked(self, now_ms: int) -> None:
        while self._binding_heap and self._binding_heap[0][0] <= now_ms:
            expires_ms, cookie = heapq.heappop(self._binding_heap)
            binding = self._bindings.get(cookie)
            if binding is not None and binding[1] == expires_ms:
                del self._bindings[cookie]

    def _bound_address_for_cookie_locked(
        self,
        cookie: str,
        record: dict[str, Any],
        generation_bytes: bytes,
        expires_bytes: bytes,
        presented_tag: bytes,
    ) -> str | None:
        binding = self._bindings.get(cookie)
        if binding is None:
            return None

        bound_address = binding[0]
        tag = self._tag_for_locked(
            record, bound_address, generation_bytes, expires_bytes
        )
        if not hmac.compare_digest(tag, presented_tag):
            return None
        return bound_address

    def _tampered_generation_key_locked(
        self,
        normalized: str,
        expires_bytes: bytes,
        presented_tag: bytes,
    ) -> str | None:
        for record in self._keys.values():
            generation_bytes = record["generation"].to_bytes(
                8, "big", signed=False
            )
            tag = self._tag_for_locked(
                record, normalized, generation_bytes, expires_bytes
            )
            if hmac.compare_digest(tag, presented_tag):
                return record["key_id"]
        return None

    def _update_rate_expiry_locked(
        self, normalized: str, history: list[int]
    ) -> None:
        expiry = max(history) + self.rate_window_ms
        if self._rate_expiry.get(normalized) != expiry:
            self._rate_expiry[normalized] = expiry
            heapq.heappush(self._rate_heap, (expiry, normalized))

    def _prune_rate_locked(self, now_ms: int) -> None:
        while self._rate_heap and self._rate_heap[0][0] <= now_ms:
            expiry, address = heapq.heappop(self._rate_heap)
            if self._rate_expiry.get(address) != expiry:
                continue
            history = self._rate.pop(address, None)
            self._rate_expiry.pop(address, None)
            if history is not None:
                history.clear()


def _is_plain_int(value: object, *, minimum: int | None = None) -> bool:
    if type(value) is not int:
        return False
    if minimum is not None and value < minimum:
        return False
    return True


def _parse_snapshot(state: object, rate_window_ms: int) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise ValueError("snapshot must be a dict")
    if set(state.keys()) != {"version", "keys", "rate", "stats"}:
        raise ValueError("invalid snapshot keys")
    if not _is_plain_int(state["version"]) or state["version"] != 1:
        raise ValueError("invalid version")

    raw_keys = state["keys"]
    if not isinstance(raw_keys, dict) or len(raw_keys) > 2:
        raise ValueError("invalid keys")

    keys: OrderedDict[str, dict[str, Any]] = OrderedDict()
    generations: set[int] = set()
    for key_id, raw_record in raw_keys.items():
        if not isinstance(key_id, str):
            raise ValueError("invalid key id")
        if not isinstance(raw_record, dict):
            raise ValueError("invalid key record")
        if set(raw_record.keys()) != _SNAPSHOT_KEY_FIELDS:
            raise ValueError("invalid key record fields")
        if raw_record["key_id"] != key_id:
            raise ValueError("key id mismatch")

        generation = raw_record["generation"]
        if not _is_plain_int(generation, minimum=1) or generation in generations:
            raise ValueError("invalid generation")

        key_state = raw_record["state"]
        if key_state not in _KEY_STATES:
            raise ValueError("invalid key state")

        grace_until_ms = raw_record["grace_until_ms"]
        if key_state == "previous":
            if not _is_plain_int(grace_until_ms, minimum=0):
                raise ValueError("invalid grace")
        elif grace_until_ms is not None:
            raise ValueError("grace must be absent")

        secret_hex = raw_record["secret_hex"]
        if not isinstance(secret_hex, str) or len(secret_hex) != 64:
            raise ValueError("invalid secret")
        try:
            secret = bytes.fromhex(secret_hex)
        except ValueError as exc:
            raise ValueError("invalid secret") from exc
        if len(secret) != 32:
            raise ValueError("invalid secret length")

        generations.add(generation)
        keys[key_id] = {
            "key_id": key_id,
            "generation": generation,
            "secret": secret,
            "revoked": key_state == "revoked",
            "grace_until_ms": grace_until_ms,
        }

    if keys:
        current_id = max(keys, key=lambda item: keys[item]["generation"])
        if len(keys) == 2:
            older_id = next(key_id for key_id in keys if key_id != current_id)
            if keys[older_id]["generation"] != keys[current_id]["generation"] - 1:
                raise ValueError("non-consecutive generations")

        for key_id, record in keys.items():
            raw_state = raw_keys[key_id]["state"]
            if record["revoked"]:
                expected_state = "revoked"
            elif key_id == current_id:
                expected_state = "active"
            else:
                expected_state = "previous"
            if raw_state != expected_state:
                raise ValueError("inconsistent key state")

        next_generation = keys[current_id]["generation"] + 1
    else:
        current_id = None
        next_generation = 1

    raw_rate = state["rate"]
    if not isinstance(raw_rate, dict):
        raise ValueError("invalid rate")

    rate: OrderedDict[str, list[int]] = OrderedDict()
    rate_expiry: dict[str, int] = {}
    rate_heap: list[tuple[int, str]] = []
    for address, timestamps in raw_rate.items():
        normalized = normalize_address(address)
        if normalized != address:
            raise ValueError("rate key is not normalized")
        if not isinstance(timestamps, list):
            raise ValueError("invalid rate history")

        history = []
        for timestamp in timestamps:
            if not _is_plain_int(timestamp, minimum=0):
                raise ValueError("invalid timestamp")
            history.append(timestamp)
        rate[address] = history
        if history:
            expiry = max(history) + rate_window_ms
            rate_expiry[address] = expiry
            rate_heap.append((expiry, address))
    heapq.heapify(rate_heap)

    raw_stats = state["stats"]
    if not isinstance(raw_stats, dict):
        raise ValueError("invalid stats")
    if set(raw_stats.keys()) != set(_STATS_KEYS):
        raise ValueError("invalid stats fields")
    stats = {}
    for name in _STATS_KEYS:
        value = raw_stats[name]
        if not _is_plain_int(value, minimum=0):
            raise ValueError("invalid stat")
        stats[name] = value

    return {
        "keys": keys,
        "current_id": current_id,
        "next_generation": next_generation,
        "rate": rate,
        "rate_expiry": rate_expiry,
        "rate_heap": rate_heap,
        "stats": stats,
    }
