"""QUIC connection-ID routing guard with retire table and stateless reset."""

from __future__ import annotations

import copy
import hashlib
import hmac
import threading


_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")
_STAT_KEYS = ("allocated", "forwarded", "resets", "retired", "rejected")


class CidError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CidRouteGuard:
    """Allocate, route and retire destination connection IDs per worker."""

    def __init__(self, *, cid_len: int = 8, worker_capacity: int = 64,
                 retire_grace_ms: int = 5000, reset_key: bytes = b"") -> None:
        if (not isinstance(cid_len, int) or isinstance(cid_len, bool)
                or cid_len < 2):
            raise CidError("CID_INVALID_ARGUMENT")
        if (not isinstance(worker_capacity, int)
                or isinstance(worker_capacity, bool)
                or worker_capacity < 1):
            raise CidError("CID_INVALID_ARGUMENT")
        if (not isinstance(retire_grace_ms, int)
                or isinstance(retire_grace_ms, bool)
                or retire_grace_ms < 0):
            raise CidError("CID_INVALID_ARGUMENT")
        if not isinstance(reset_key, (bytes, bytearray, memoryview)):
            raise CidError("CID_INVALID_ARGUMENT")

        self.cid_len = cid_len
        self.worker_capacity = worker_capacity
        self.retire_grace_ms = retire_grace_ms
        self.reset_key = bytes(reset_key)
        self._lock = threading.RLock()

        self._active: dict[str, dict] = {}
        self._retired: dict[str, dict] = {}
        self._serial = 0
        self._stats = {key: 0 for key in _STAT_KEYS}
        self._active_counts: dict[str, int] = {}
        self._retired_counts: dict[str, int] = {}

    # ------------------------------------------------------------------
    # Public API

    def allocate(self, worker: str, *, now_ms: int = 0,
                 client_cid: str | None = None) -> dict:
        with self._lock:
            try:
                self._check_worker(worker)
                self._check_now(now_ms)
                normalized_client = None
                if client_cid is not None:
                    normalized_client = self._check_client_cid(client_cid)

                self._prune_retired(now_ms)

                if normalized_client is not None:
                    cid = normalized_client
                    if cid in self._active:
                        raise CidError("CID_IN_USE")
                    if cid in self._retired:
                        raise CidError("CID_RETIRED_CONFLICT")
                    if (self._active_counts.get(worker, 0)
                            >= self.worker_capacity):
                        raise CidError("CID_WORKER_FULL")
                else:
                    if (self._active_counts.get(worker, 0)
                            >= self.worker_capacity):
                        raise CidError("CID_WORKER_FULL")
                    cid = self._next_generated()

                self._active[cid] = {
                    "worker": worker,
                    "created_ms": now_ms,
                }
                self._add_count(self._active_counts, worker)
                self._stats["allocated"] += 1
                return {
                    "cid": cid,
                    "worker": worker,
                    "token": self._make_token(cid),
                    "created_ms": now_ms,
                }
            except CidError:
                self._stats["rejected"] += 1
                raise

    def route(self, dcid: str, *, now_ms: int = 0) -> dict:
        with self._lock:
            try:
                if not isinstance(dcid, str):
                    raise CidError("CID_INVALID_ARGUMENT")
                self._check_now(now_ms)
                self._prune_retired(now_ms)

                active = self._active.get(dcid)
                if active is not None:
                    self._stats["forwarded"] += 1
                    return {
                        "action": "forward",
                        "cid": dcid,
                        "worker": active["worker"],
                    }

                retired = self._retired.get(dcid)
                if retired is not None:
                    self._stats["resets"] += 1
                    return {
                        "action": "reset",
                        "cid": dcid,
                        "token": self._make_token(dcid),
                    }

                return {"action": "drop", "cid": dcid}
            except CidError:
                self._stats["rejected"] += 1
                raise

    def retire(self, cid: str, *, now_ms: int = 0) -> dict:
        with self._lock:
            try:
                if not isinstance(cid, str):
                    raise CidError("CID_INVALID_ARGUMENT")
                self._check_now(now_ms)
                self._prune_retired(now_ms)

                active = self._active.get(cid)
                if active is None:
                    raise CidError("CID_UNKNOWN")

                del self._active[cid]
                self._drop_count(self._active_counts, active["worker"])

                retire_until_ms = now_ms + self.retire_grace_ms
                self._retired[cid] = {
                    "worker": active["worker"],
                    "created_ms": active["created_ms"],
                    "retire_until_ms": retire_until_ms,
                }
                self._add_count(self._retired_counts, active["worker"])
                self._stats["retired"] += 1
                return {
                    "cid": cid,
                    "worker": active["worker"],
                    "retire_until_ms": retire_until_ms,
                }
            except CidError:
                self._stats["rejected"] += 1
                raise

    def reset_token(self, cid: str) -> str:
        with self._lock:
            try:
                if not isinstance(cid, str):
                    raise CidError("CID_INVALID_ARGUMENT")
                return self._make_token(cid)
            except CidError:
                self._stats["rejected"] += 1
                raise

    def verify_token(self, cid: str, token: str) -> bool:
        with self._lock:
            if not isinstance(cid, str) or not isinstance(token, str):
                return False
            try:
                supplied = token.lower().encode("ascii")
            except UnicodeEncodeError:
                return False
            expected = self._make_token(cid).encode("ascii")
            return hmac.compare_digest(expected, supplied)

    def parse_cid(self, cid: str) -> dict:
        with self._lock:
            try:
                _, serial = self._decode_cid(cid)
                return {"version": 1, "serial": serial, "valid": True}
            except CidError:
                self._stats["rejected"] += 1
                raise

    def worker_stats(self, worker: str) -> dict:
        with self._lock:
            try:
                self._check_worker(worker)
                return {
                    "worker": worker,
                    "active": self._active_counts.get(worker, 0),
                    "retired": self._retired_counts.get(worker, 0),
                    "capacity": self.worker_capacity,
                }
            except CidError:
                self._stats["rejected"] += 1
                raise

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": 1,
                "active": self._active,
                "retired": self._retired,
                "serial": self._serial,
                "stats": self._stats,
            })

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                active, retired, serial, stats = self._validate_snapshot(state)
            except CidError:
                self._stats["rejected"] += 1
                raise

            self._active = active
            self._retired = retired
            self._serial = serial
            self._stats = stats
            self._active_counts = {}
            self._retired_counts = {}
            for meta in self._active.values():
                self._add_count(self._active_counts, meta["worker"])
            for meta in self._retired.values():
                self._add_count(self._retired_counts, meta["worker"])

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}

    # ------------------------------------------------------------------
    # Internal helpers (all called with the guard lock held)

    @staticmethod
    def _check_worker(worker: str) -> None:
        if not isinstance(worker, str) or not worker.strip():
            raise CidError("CID_INVALID_ARGUMENT")

    @staticmethod
    def _check_now(now_ms: int) -> None:
        if not isinstance(now_ms, int) or isinstance(now_ms, bool):
            raise CidError("CID_INVALID_ARGUMENT")

    def _check_client_cid(self, client_cid: str) -> str:
        if not isinstance(client_cid, str):
            raise CidError("CID_INVALID_ARGUMENT")
        if (len(client_cid) % 2 != 0
                or any(ch not in _HEX_DIGITS for ch in client_cid)):
            raise CidError("CID_INVALID_ARGUMENT")
        if not 8 <= len(client_cid) // 2 <= 20:
            raise CidError("CID_CLIENT_LEN")
        return client_cid.lower()

    def _next_generated(self) -> str:
        max_serial = (1 << (8 * (self.cid_len - 1))) - 1
        serial = self._serial
        while serial < max_serial:
            serial += 1
            cid = self._encode_cid(serial)
            if cid not in self._active and cid not in self._retired:
                self._serial = serial
                return cid
        raise CidError("CID_INVALID_ARGUMENT")

    def _encode_cid(self, serial: int) -> str:
        payload = serial.to_bytes(self.cid_len - 1, "big")
        check = 0xA5
        for byte in payload:
            check ^= byte
        return (bytes((check,)) + payload).hex()

    def _decode_cid(self, cid: str) -> tuple[bytes, int]:
        if (not isinstance(cid, str)
                or len(cid) != self.cid_len * 2
                or any(ch not in _HEX_DIGITS for ch in cid)):
            raise CidError("CID_BAD_PREFIX")

        raw = bytes.fromhex(cid)
        check = 0xA5
        for byte in raw[1:]:
            check ^= byte
        if raw[0] != check:
            raise CidError("CID_BAD_PREFIX")
        return raw, int.from_bytes(raw[1:], "big")

    def _make_token(self, cid: str) -> str:
        digest = hmac.new(
            self.reset_key, cid.encode("utf-8"), hashlib.sha256
        ).digest()
        return digest[:16].hex()

    def _prune_retired(self, now_ms: int) -> None:
        expired = [
            cid for cid, meta in self._retired.items()
            if now_ms >= meta["retire_until_ms"]
        ]
        for cid in expired:
            meta = self._retired.pop(cid)
            self._drop_count(self._retired_counts, meta["worker"])

    @staticmethod
    def _add_count(counts: dict[str, int], worker: str) -> None:
        counts[worker] = counts.get(worker, 0) + 1

    @staticmethod
    def _drop_count(counts: dict[str, int], worker: str) -> None:
        current = counts.get(worker, 0)
        if current <= 1:
            counts.pop(worker, None)
        else:
            counts[worker] = current - 1

    def _validate_snapshot(self, state: dict) -> tuple[dict, dict, int, dict]:
        invalid = CidError("CID_SNAPSHOT_INVALID")
        if not isinstance(state, dict):
            raise invalid
        if set(state) != {"version", "active", "retired", "serial", "stats"}:
            raise invalid
        if (not isinstance(state["version"], int)
                or isinstance(state["version"], bool)
                or state["version"] != 1):
            raise invalid

        serial = state["serial"]
        if (not isinstance(serial, int) or isinstance(serial, bool)
                or serial < 0):
            raise invalid

        raw_active = state["active"]
        raw_retired = state["retired"]
        raw_stats = state["stats"]
        if not isinstance(raw_active, dict):
            raise invalid
        if not isinstance(raw_retired, dict):
            raise invalid
        if not isinstance(raw_stats, dict):
            raise invalid

        if set(raw_stats) != set(_STAT_KEYS):
            raise invalid
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if (not isinstance(value, int) or isinstance(value, bool)
                    or value < 0):
                raise invalid
            stats[key] = value

        active: dict[str, dict] = {}
        for cid, meta in raw_active.items():
            if not self._valid_snapshot_cid(cid):
                raise invalid
            if cid in raw_retired or not isinstance(meta, dict):
                raise invalid
            worker = meta.get("worker")
            if not isinstance(worker, str) or not worker.strip():
                raise invalid
            created_ms = meta.get("created_ms", 0)
            if (not isinstance(created_ms, int)
                    or isinstance(created_ms, bool)):
                raise invalid
            active[cid] = {
                "worker": worker,
                "created_ms": created_ms,
            }

        retired: dict[str, dict] = {}
        for cid, meta in raw_retired.items():
            if not self._valid_snapshot_cid(cid):
                raise invalid
            if not isinstance(meta, dict):
                raise invalid
            worker = meta.get("worker")
            if not isinstance(worker, str) or not worker.strip():
                raise invalid
            retire_until_ms = meta.get("retire_until_ms")
            if (not isinstance(retire_until_ms, int)
                    or isinstance(retire_until_ms, bool)):
                raise invalid
            created_ms = meta.get("created_ms", 0)
            if (not isinstance(created_ms, int)
                    or isinstance(created_ms, bool)):
                raise invalid
            retired[cid] = {
                "worker": worker,
                "created_ms": created_ms,
                "retire_until_ms": retire_until_ms,
            }

        return active, retired, serial, stats

    @staticmethod
    def _valid_snapshot_cid(cid: object) -> bool:
        return (isinstance(cid, str) and bool(cid)
                and len(cid) % 2 == 0
                and all(ch in _HEX_DIGITS for ch in cid))
