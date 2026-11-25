"""DKIM signing and verification guard."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import re
import threading
import time
from datetime import date, datetime


_SUPPORTED_ALGORITHM = "hmac-sha256"
_CANON_MODES = frozenset(("simple", "relaxed"))
_WSP_BYTES_RE = re.compile(rb"[ \t]+")
_HEADER_WSP_RE = re.compile(r"[ \t\r\n]+")


class DkimError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class DkimSignatureGuard:
    """Canonicalise headers and bodies, hash, sign and verify mail."""

    def __init__(self, *, max_keys: int = 64, clock=None) -> None:
        if not isinstance(max_keys, int) or isinstance(max_keys, bool) or max_keys < 1:
            raise DkimError("DKIM_KEY_LIMIT", max_keys)
        self.max_keys = max_keys
        self._clock = clock
        self._lock = threading.RLock()
        self._selectors: dict[tuple[str, str], dict] = {}
        self._stats = {"signed": 0, "verified": 0, "rejected": 0}

    def add_selector_key(self, domain: str, selector: str, private_key: bytes,
                         *, algorithm: str = "hmac-sha256",
                         not_before=None, expires=None, now=None) -> dict:
        with self._lock:
            domain = self._require_text(domain, "DKIM_KEY_INVALID")
            selector = self._require_text(selector, "DKIM_KEY_INVALID")
            if not isinstance(algorithm, str) or \
                    algorithm.lower() != _SUPPORTED_ALGORITHM:
                self._reject("DKIM_UNSUPPORTED_ALGORITHM", algorithm)
            key = self._require_key(private_key)
            current_time = self._now(now)
            not_before_ts = self._optional_time(not_before, "DKIM_KEY_INVALID")
            expires_ts = self._optional_time(expires, "DKIM_KEY_INVALID")
            if not_before_ts is not None and expires_ts is not None and \
                    expires_ts < not_before_ts:
                self._reject("DKIM_KEY_INVALID", "expires before not_before")

            selector_key = (domain, selector)
            existing = self._selectors.get(selector_key)
            if existing is None:
                self._prune_locked(current_time)
                if len(self._selectors) >= self.max_keys:
                    self._reject("DKIM_KEY_LIMIT", self.max_keys)
                generation = 1
                selector_data = {"current_generation": generation, "keys": {}}
            else:
                generation = existing["current_generation"]
                current_record = existing["keys"].get(generation)
                if current_record is not None and \
                        current_record["private_key"] == key:
                    return {
                        "domain": domain,
                        "selector": selector,
                        "generation": generation,
                        "algorithm": _SUPPORTED_ALGORITHM,
                        "not_before": current_record.get("not_before"),
                        "expires": current_record.get("expires"),
                        "grace_until": current_record.get("grace_until"),
                    }
                self._reject(
                    "DKIM_KEY_CONFLICT",
                    {"domain": domain, "selector": selector,
                     "generation": generation})

            record = {
                "generation": generation,
                "private_key": key,
                "algorithm": _SUPPORTED_ALGORITHM,
                "created_at": current_time,
                "not_before": not_before_ts,
                "expires": expires_ts,
                "retired_at": None,
                "grace_until": None,
            }
            selector_data["keys"][generation] = record
            self._selectors[selector_key] = selector_data
            return {
                "domain": domain,
                "selector": selector,
                "generation": generation,
                "algorithm": _SUPPORTED_ALGORITHM,
                "not_before": not_before_ts,
                "expires": expires_ts,
                "grace_until": None,
            }

    def rotate_selector(self, domain: str, selector: str, private_key: bytes,
                        *, grace_seconds: int = 86400, now=None) -> dict:
        with self._lock:
            domain = self._require_text(domain, "DKIM_KEY_INVALID")
            selector = self._require_text(selector, "DKIM_KEY_INVALID")
            if not isinstance(grace_seconds, int) or \
                    isinstance(grace_seconds, bool) or grace_seconds < 0:
                self._reject("DKIM_KEY_INVALID", grace_seconds)
            key = self._require_key(private_key)
            current_time = self._now(now)
            selector_key = (domain, selector)
            selector_data = self._selectors.get(selector_key)
            if selector_data is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector})

            generation = selector_data["current_generation"] + 1
            previous = selector_data["keys"].get(
                selector_data["current_generation"])
            grace_until = current_time + grace_seconds
            if previous is not None:
                previous["retired_at"] = current_time
                previous["grace_until"] = grace_until

            self._prune_locked(current_time)

            selector_data["current_generation"] = generation
            selector_data["keys"][generation] = {
                "generation": generation,
                "private_key": key,
                "algorithm": _SUPPORTED_ALGORITHM,
                "created_at": current_time,
                "not_before": current_time,
                "expires": None,
                "retired_at": None,
                "grace_until": None,
            }
            return {
                "domain": domain,
                "selector": selector,
                "generation": generation,
                "grace_until": grace_until,
            }

    def sign(self, *, domain: str, selector: str, headers: list,
             body: bytes, signed_headers=(), canonicalization: str = "relaxed",
             header_modes: dict | None = None, length: int | None = None,
             now=None) -> dict:
        with self._lock:
            domain = self._require_text(domain, "DKIM_KEY_INVALID")
            selector = self._require_text(selector, "DKIM_KEY_INVALID")
            current_time = self._now(now)
            selector_data = self._selectors.get((domain, selector))
            if selector_data is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector})
            generation = selector_data["current_generation"]
            record = selector_data["keys"].get(generation)
            if record is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector})
            self._ensure_key_usable(record, current_time)

            canon = self._canon_mode(canonicalization)
            self._validate_length(length)
            pairs = self._header_pairs(headers)
            names = self._signed_header_names(signed_headers, pairs)
            if "from" not in names:
                self._reject("DKIM_FROM_REQUIRED")
            if not any(name.lower() == "from" for name, _ in pairs):
                self._reject("DKIM_FROM_REQUIRED")
            resolved_modes = self._resolve_modes(names, canon, header_modes)

            body_bytes = self._require_body(body)
            body_digest = hashlib.sha256(
                self._canonical_body(body_bytes, canon, length)).hexdigest()
            canonical_headers = []
            for index, name in enumerate(names):
                found = self._take_header(pairs, name)
                if found is None:
                    self._reject("DKIM_INVALID_SIGNATURE", name)
                actual_name, value = found
                canonical_headers.append(
                    self._canonical_header(
                        actual_name, value, resolved_modes[index]))

            declared_modes = {}
            if header_modes:
                normalized_declarations = {
                    name.strip().lower(): mode.lower()
                    for name, mode in header_modes.items()
                }
                declared_modes = {
                    name: normalized_declarations[name]
                    for name in names
                    if name in normalized_declarations
                }
            metadata = self._signature_metadata(
                domain, selector, generation, canon, names, resolved_modes,
                body_digest, length)
            signature = self._compute_signature(
                record["private_key"], metadata, canonical_headers)

            result = {
                "domain": domain,
                "selector": selector,
                "generation": generation,
                "canonicalization": canon,
                "signed_headers": list(names),
                "body_hash": body_digest,
                "signature": signature,
            }
            if declared_modes:
                result["header_modes"] = declared_modes
            if length is not None:
                result["length"] = length
            self._stats["signed"] += 1
            return result

    def verify_message(self, message: dict, *, now=None) -> dict:
        with self._lock:
            try:
                block = message.get("dkim")
            except AttributeError:
                block = None
            if not isinstance(block, dict):
                self._reject("DKIM_INVALID_SIGNATURE", "missing dkim block")

            domain = block.get("domain")
            selector = block.get("selector")
            if not isinstance(domain, str) or not isinstance(selector, str):
                self._reject("DKIM_INVALID_SIGNATURE", "invalid selector")

            selector_data = self._selectors.get((domain, selector))
            if selector_data is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector})

            generation = block.get("generation")
            if not isinstance(generation, int) or isinstance(generation, bool):
                self._reject("DKIM_INVALID_SIGNATURE", "invalid generation")
            record = selector_data["keys"].get(generation)
            if record is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector,
                              "generation": generation})

            current_time = self._now(now)
            self._ensure_key_usable(record, current_time)
            if generation != selector_data["current_generation"]:
                grace_until = record.get("grace_until")
                if grace_until is None or current_time >= grace_until:
                    self._reject("DKIM_UNKNOWN_SELECTOR",
                                 {"domain": domain, "selector": selector,
                                  "generation": generation,
                                  "reason": "grace expired"})

            canon = self._canon_mode(block.get("canonicalization", "relaxed"))
            names_raw = block.get("signed_headers")
            if not isinstance(names_raw, (list, tuple)) or not names_raw:
                self._reject("DKIM_INVALID_SIGNATURE", "invalid signed_headers")
            names = []
            for name in names_raw:
                if not isinstance(name, str) or not name.strip():
                    self._reject("DKIM_INVALID_SIGNATURE", "invalid signed_headers")
                names.append(name.strip().lower())
            if "from" not in names:
                self._reject("DKIM_FROM_REQUIRED")

            resolved_modes = self._resolve_modes(
                names, canon, block.get("header_modes"))
            length = block.get("length")
            self._validate_length(length)
            body_bytes = self._require_body(message.get("body", b""))
            full_canonical_body = self._canonical_body(body_bytes, canon, None)
            if length is not None and (
                    len(body_bytes) < length or
                    len(full_canonical_body) < length):
                self._reject("DKIM_BODY_HASH_MISMATCH", "body shorter than l")
            expected_digest = hashlib.sha256(
                full_canonical_body[:length] if length is not None
                else full_canonical_body).hexdigest()
            supplied_digest = block.get("body_hash")
            if not isinstance(supplied_digest, str) or \
                    not hmac.compare_digest(supplied_digest, expected_digest):
                self._reject("DKIM_BODY_HASH_MISMATCH")

            pairs = self._header_pairs(message.get("headers", []))
            canonical_headers = []
            for index, name in enumerate(names):
                found = self._take_header(pairs, name)
                if found is None:
                    actual_name, value = name, ""
                else:
                    actual_name, value = found
                canonical_headers.append(
                    self._canonical_header(
                        actual_name, value, resolved_modes[index]))

            metadata = self._signature_metadata(
                domain, selector, generation, canon, names, resolved_modes,
                expected_digest, length)
            expected_signature = self._compute_signature(
                record["private_key"], metadata, canonical_headers)
            supplied_signature = block.get("signature")
            if not isinstance(supplied_signature, str) or \
                    not hmac.compare_digest(supplied_signature,
                                            expected_signature):
                self._reject("DKIM_INVALID_SIGNATURE")

            self._stats["verified"] += 1
            return {
                "status": "ok",
                "domain": domain,
                "selector": selector,
                "generation": generation,
                "signed_headers": list(names),
            }

    def canonicalize_header(self, name: str, value: str, mode: str) -> bytes:
        with self._lock:
            return self._canonical_header(name, value, mode)

    def canonicalize_body(self, body: bytes, *, mode: str = "relaxed",
                          length: int | None = None) -> bytes:
        with self._lock:
            canon = self._canon_mode(mode)
            self._validate_length(length)
            return self._canonical_body(self._require_body(body), canon, length)

    def body_hash(self, body: bytes, *, length: int | None = None,
                  mode: str = "relaxed") -> str:
        with self._lock:
            canon = self._canon_mode(mode)
            self._validate_length(length)
            normalized = self._canonical_body(
                self._require_body(body), canon, length)
            return hashlib.sha256(normalized).hexdigest()

    def selector_state(self, domain: str, selector: str, *,
                       now=None) -> dict:
        with self._lock:
            selector_data = self._selectors.get((domain, selector))
            if selector_data is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector})
            generation = selector_data["current_generation"]
            record = selector_data["keys"].get(generation)
            if record is None:
                self._reject("DKIM_UNKNOWN_SELECTOR",
                             {"domain": domain, "selector": selector})
            current_time = self._now(now)
            return {
                "domain": domain,
                "selector": selector,
                "generation": generation,
                "algorithm": record["algorithm"],
                "not_before": record.get("not_before"),
                "expires": record.get("expires"),
                "grace_until": record.get("grace_until"),
                "active": self._key_is_usable(record, current_time),
            }

    def expire_selectors(self, *, now=None) -> int:
        with self._lock:
            return self._prune_locked(self._now(now))

    def snapshot(self) -> dict:
        with self._lock:
            selectors: dict[str, dict] = {}
            for (domain, selector), selector_data in sorted(
                    self._selectors.items()):
                keys = {}
                for generation in sorted(selector_data["keys"]):
                    keys[generation] = copy.deepcopy(
                        selector_data["keys"][generation])
                selectors.setdefault(domain, {})[selector] = {
                    "current_generation": selector_data["current_generation"],
                    "keys": keys,
                }
            return copy.deepcopy({
                "version": 1,
                "selectors": selectors,
            })

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                candidate = self._parse_snapshot(state)
            except DkimError as exc:
                if exc.code == "DKIM_SNAPSHOT_INVALID":
                    self._reject("DKIM_SNAPSHOT_INVALID", exc.detail)
                raise
            self._selectors = candidate

    def stats(self) -> dict:
        with self._lock:
            return {
                "signed": int(self._stats["signed"]),
                "verified": int(self._stats["verified"]),
                "rejected": int(self._stats["rejected"]),
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {"signed": 0, "verified": 0, "rejected": 0}

    def _reject(self, code: str, detail: object = None):
        self._stats["rejected"] += 1
        raise DkimError(code, detail)

    @staticmethod
    def _require_text(value, code: str) -> str:
        if not isinstance(value, str) or not value:
            raise DkimError(code, value)
        return value

    @staticmethod
    def _require_key(value) -> bytes:
        if isinstance(value, bytearray):
            return bytes(value)
        if not isinstance(value, bytes):
            raise DkimError("DKIM_KEY_INVALID", value)
        return value

    @staticmethod
    def _require_body(value) -> bytes:
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        if not isinstance(value, bytes):
            raise DkimError("DKIM_BODY_HASH_MISMATCH", "invalid body")
        return value

    def _now(self, value=None):
        if value is None:
            clock = self._clock
            if clock is None:
                value = time.time()
            elif callable(clock):
                value = clock()
            elif hasattr(clock, "now"):
                now_attr = clock.now
                value = now_attr() if callable(now_attr) else now_attr
            else:
                value = clock
        return self._time_value(value)

    @staticmethod
    def _time_value(value):
        if isinstance(value, datetime):
            return value.timestamp()
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day).timestamp()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        raise DkimError("DKIM_KEY_INVALID", value)

    def _optional_time(self, value, code: str):
        if value is None:
            return None
        try:
            return self._time_value(value)
        except DkimError:
            raise DkimError(code, value) from None

    def _canon_mode(self, mode: str) -> str:
        if not isinstance(mode, str) or mode.lower() not in _CANON_MODES:
            self._reject("DKIM_CANON_INVALID", mode)
        return mode.lower()

    def _validate_length(self, length) -> None:
        if length is None:
            return
        if not isinstance(length, int) or isinstance(length, bool) or length < 0:
            self._reject("DKIM_CANON_INVALID", length)

    def _resolve_modes(self, names, default: str, header_modes):
        mode_map: dict[str, str] = {}
        if header_modes is None:
            header_modes = {}
        if not isinstance(header_modes, dict):
            self._reject("DKIM_CANON_INVALID", header_modes)
        for name, mode in header_modes.items():
            if not isinstance(name, str) or not name.strip():
                self._reject("DKIM_CANON_INVALID", name)
            normalized_name = name.strip().lower()
            if not isinstance(mode, str) or mode.lower() not in _CANON_MODES:
                self._reject("DKIM_CANON_INVALID", mode)
            mode_map[normalized_name] = mode.lower()
        return [mode_map.get(name, default) for name in names]

    @staticmethod
    def _header_pairs(headers):
        if not isinstance(headers, (list, tuple)):
            raise DkimError("DKIM_INVALID_SIGNATURE", "invalid headers")
        pairs = []
        for item in headers:
            if isinstance(item, dict):
                name = item.get("name")
                value = item.get("value")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                name, value = item[0], item[1]
            else:
                raise DkimError("DKIM_INVALID_SIGNATURE", item)
            if not isinstance(name, str) or not name:
                raise DkimError("DKIM_INVALID_SIGNATURE", item)
            if isinstance(value, bytes):
                value = value.decode("utf-8", "surrogateescape")
            elif not isinstance(value, str):
                value = str(value)
            pairs.append((name, value))
        return pairs

    def _signed_header_names(self, signed_headers, pairs):
        if signed_headers is None:
            signed_headers = ()
        if not isinstance(signed_headers, (list, tuple)):
            self._reject("DKIM_INVALID_SIGNATURE", signed_headers)
        names = []
        for name in signed_headers:
            if not isinstance(name, str) or not name.strip():
                self._reject("DKIM_INVALID_SIGNATURE", signed_headers)
            names.append(name.strip().lower())
        if not names:
            names = [name.lower() for name, _ in pairs]
        return names

    @staticmethod
    def _take_header(pairs, name: str):
        for index, (header_name, value) in enumerate(pairs):
            if header_name.lower() == name:
                pairs.pop(index)
                return header_name, value
        return None

    def _canonical_header(self, name, value, mode) -> bytes:
        if not isinstance(name, str) or not isinstance(value, str):
            self._reject("DKIM_CANON_INVALID", "header must be text")
        canon = self._canon_mode(mode)
        if canon == "simple":
            return f"{name}:{value}\r\n".encode("utf-8")
        normalized_name = name.lower()
        normalized_value = _HEADER_WSP_RE.sub(" ", value).strip(" \t\r\n")
        return f"{normalized_name}:{normalized_value}\r\n".encode("utf-8")

    @staticmethod
    def _canonical_body(body: bytes, mode: str, length: int | None) -> bytes:
        if mode == "simple":
            normalized = b"\r\n" if not body else body
        else:
            if not body:
                normalized = b"\r\n"
            else:
                data = body.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                lines = data.split(b"\n")
                normalized_lines = [
                    _WSP_BYTES_RE.sub(b" ", line).rstrip(b" ")
                    for line in lines
                ]
                while normalized_lines and normalized_lines[-1] == b"":
                    normalized_lines.pop()
                normalized = b"\r\n".join(normalized_lines) + b"\r\n"
        if length is not None:
            return normalized[:length]
        return normalized

    @staticmethod
    def _signature_metadata(domain, selector, generation, canon, names,
                            resolved_modes, body_digest, length) -> bytes:
        metadata = {
            "algorithm": _SUPPORTED_ALGORITHM,
            "body_hash": body_digest,
            "canonicalization": canon,
            "domain": domain,
            "generation": generation,
            "header_modes": {
                name: resolved_modes[index]
                for index, name in enumerate(names)
            },
            "length": length,
            "selector": selector,
            "signed_headers": list(names),
            "version": 1,
        }
        return json.dumps(
            metadata, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")

    @staticmethod
    def _compute_signature(private_key: bytes, metadata: bytes,
                           canonical_headers) -> str:
        digest = hmac.new(
            private_key, b"DKIM-GUARD-V1\x00", hashlib.sha256)
        digest.update(len(metadata).to_bytes(8, "big"))
        digest.update(metadata)
        digest.update(len(canonical_headers).to_bytes(8, "big"))
        for header in canonical_headers:
            digest.update(len(header).to_bytes(8, "big"))
            digest.update(header)
        return digest.hexdigest()

    @staticmethod
    def _key_is_usable(record, current_time) -> bool:
        not_before = record.get("not_before")
        expires = record.get("expires")
        if not_before is not None and current_time < not_before:
            return False
        if expires is not None and current_time >= expires:
            return False
        return True

    def _ensure_key_usable(self, record, current_time) -> None:
        if not self._key_is_usable(record, current_time):
            self._reject(
                "DKIM_UNKNOWN_SELECTOR",
                {"generation": record.get("generation"),
                 "reason": "key not active"})

    def _prune_locked(self, current_time) -> int:
        removed = 0
        for selector_key in list(self._selectors):
            selector_data = self._selectors[selector_key]
            current_generation = selector_data["current_generation"]
            for generation in list(selector_data["keys"]):
                record = selector_data["keys"][generation]
                expires = record.get("expires")
                grace_until = record.get("grace_until")
                expired_current = (
                    generation == current_generation and expires is not None
                    and current_time >= expires)
                expired_old = (
                    generation != current_generation and (
                        (grace_until is not None and
                         current_time >= grace_until) or
                        (expires is not None and current_time >= expires)
                    ))
                if expired_current or expired_old:
                    del selector_data["keys"][generation]
                    removed += 1
            if not selector_data["keys"]:
                del self._selectors[selector_key]
        return removed

    def _parse_snapshot(self, state):
        try:
            if not isinstance(state, dict):
                raise ValueError
            raw_selectors = state["selectors"]
            if not isinstance(raw_selectors, dict):
                raise ValueError
            parsed = {}
            for domain, selectors in raw_selectors.items():
                if not isinstance(domain, str) or not domain or \
                        not isinstance(selectors, dict):
                    raise ValueError
                for selector, selector_data in selectors.items():
                    if not isinstance(selector, str) or not selector or \
                            not isinstance(selector_data, dict):
                        raise ValueError
                    current_generation = selector_data["current_generation"]
                    raw_keys = selector_data["keys"]
                    if not isinstance(current_generation, int) or \
                            isinstance(current_generation, bool) or \
                            current_generation < 1 or \
                            not isinstance(raw_keys, dict) or not raw_keys:
                        raise ValueError
                    keys = {}
                    for raw_generation, record in raw_keys.items():
                        if isinstance(raw_generation, bool):
                            raise ValueError
                        if isinstance(raw_generation, int):
                            generation = raw_generation
                        elif isinstance(raw_generation, str) and \
                                raw_generation.isdigit():
                            generation = int(raw_generation)
                        else:
                            raise ValueError
                        if generation < 1 or not isinstance(record, dict):
                            raise ValueError
                        if record.get("generation", generation) != generation:
                            raise ValueError
                        private_key = self._require_key(
                            record.get("private_key"))
                        algorithm = record.get(
                            "algorithm", _SUPPORTED_ALGORITHM)
                        if not isinstance(algorithm, str) or \
                                algorithm.lower() != _SUPPORTED_ALGORITHM:
                            raise ValueError
                        not_before = self._optional_time(
                            record.get("not_before"), "DKIM_SNAPSHOT_INVALID")
                        expires = self._optional_time(
                            record.get("expires"), "DKIM_SNAPSHOT_INVALID")
                        retired_at = self._optional_time(
                            record.get("retired_at"), "DKIM_SNAPSHOT_INVALID")
                        grace_until = self._optional_time(
                            record.get("grace_until"), "DKIM_SNAPSHOT_INVALID")
                        created_at = self._optional_time(
                            record.get("created_at"), "DKIM_SNAPSHOT_INVALID")
                        keys[generation] = {
                            "generation": generation,
                            "private_key": private_key,
                            "algorithm": _SUPPORTED_ALGORITHM,
                            "created_at": created_at,
                            "not_before": not_before,
                            "expires": expires,
                            "retired_at": retired_at,
                            "grace_until": grace_until,
                        }
                    if current_generation not in keys:
                        raise ValueError
                    parsed[(domain, selector)] = {
                        "current_generation": current_generation,
                        "keys": keys,
                    }
            return copy.deepcopy(parsed)
        except DkimError as exc:
            if exc.code == "DKIM_SNAPSHOT_INVALID":
                raise
            raise DkimError("DKIM_SNAPSHOT_INVALID", str(exc)) from None
        except (KeyError, TypeError, ValueError, OverflowError):
            raise DkimError("DKIM_SNAPSHOT_INVALID", state) from None
