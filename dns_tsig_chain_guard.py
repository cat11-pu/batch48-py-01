"""TSIG chain guard for a secondary DNS server."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time


class DnsTsigError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_TSIG_FIELDS = ("key_name", "algorithm", "generation", "timestamp", "fudge", "mac")
_STAT_NAMES = ("signed", "verified", "rejected", "replayed")
_SIGNABLE_ERRORS = ("BADKEY", "BADSIG", "BADTIME")


class DnsTsigChainGuard:
    """Normalise TSIG records, rotate signing keys and reject replays."""

    def __init__(self, *, clock_skew: int = 300, replay_window: int = 600,
                 max_keys: int = 32, max_replay: int = 4096,
                 clock=None) -> None:
        self.clock_skew = clock_skew
        self.replay_window = replay_window
        self.max_keys = max_keys
        self.max_replay = max_replay
        self._clock = clock
        self._lock = threading.RLock()
        self._keys = {}
        self._replay = {}
        self._stats = {name: 0 for name in _STAT_NAMES}

    def _now(self, now):
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return time.time()

    @staticmethod
    def _canonical(key_name, algorithm, generation, timestamp, fudge):
        fields = (key_name, algorithm, generation, timestamp, fudge)
        return "|".join(str(field) for field in fields).encode("utf-8")

    @staticmethod
    def _mac(secret, message, canonical):
        return hmac.new(secret, message + canonical, hashlib.sha256).hexdigest()

    @staticmethod
    def _is_active(record, now) -> bool:
        if record["retired"]:
            return False
        until = record["overlap_until"]
        return until is None or now <= until

    def _find_generation(self, key_name, generation):
        for record in self._keys.get(key_name, ()):  # noqa: B007
            if record["generation"] == generation:
                return record
        return None

    def _latest_active(self, key_name, now):
        candidates = [
            record for record in self._keys.get(key_name, ())
            if self._is_active(record, now)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record["generation"])

    def register_key(self, key_name: str, secret: bytes, *,
                     algorithm: str = "hmac-sha256", now=None) -> dict:
        with self._lock:
            records = self._keys.get(key_name)
            if records is not None:
                for record in records:
                    if (not record["retired"]
                            and record["secret"] == bytes(secret)
                            and record["algorithm"] == algorithm):
                        return {"key_name": key_name,
                                "generation": record["generation"]}
                raise DnsTsigError("KEYCONFLICT", {"key_name": key_name})
            if len(self._keys) >= self.max_keys:
                raise DnsTsigError("KEYLIMIT", {
                    "key_name": key_name, "max_keys": self.max_keys,
                })
            self._keys[key_name] = [{
                "generation": 1,
                "secret": bytes(secret),
                "algorithm": algorithm,
                "overlap_until": None,
                "retired": False,
            }]
            return {"key_name": key_name, "generation": 1}

    def begin_rotation(self, key_name: str, new_secret: bytes, *,
                       algorithm: str = "hmac-sha256",
                       overlap_seconds: int = 600, now=None) -> dict:
        with self._lock:
            now = self._now(now)
            records = self._keys.get(key_name)
            if not records:
                raise DnsTsigError("BADKEY", {"key_name": key_name})
            overlap_until = now + overlap_seconds
            latest = max(records, key=lambda record: record["generation"])
            latest["overlap_until"] = overlap_until
            generation = latest["generation"] + 1
            records.append({
                "generation": generation,
                "secret": bytes(new_secret),
                "algorithm": algorithm,
                "overlap_until": None,
                "retired": False,
            })
            return {
                "key_name": key_name,
                "generation": generation,
                "algorithm": algorithm,
                "overlap_until": overlap_until,
            }

    def retire_key(self, key_name: str, generation: int, *,
                   now=None) -> None:
        with self._lock:
            record = self._find_generation(key_name, generation)
            if record is None:
                raise DnsTsigError("BADKEY", {
                    "key_name": key_name, "generation": generation,
                })
            record["retired"] = True

    def active_keys(self, key_name: str, *, now=None) -> list[dict]:
        with self._lock:
            now = self._now(now)
            if key_name not in self._keys:
                raise DnsTsigError("BADKEY", {"key_name": key_name})
            return [
                {"generation": record["generation"],
                 "algorithm": record["algorithm"]}
                for record in sorted(self._keys[key_name],
                                     key=lambda item: item["generation"])
                if self._is_active(record, now)
            ]

    def sign_message(self, message: bytes, *, key_name: str,
                     generation: int | None = None, fudge: int = 300,
                     now=None) -> dict:
        with self._lock:
            now = self._now(now)
            if generation is None:
                record = self._latest_active(key_name, now)
            else:
                record = self._find_generation(key_name, generation)
                if record is not None and not self._is_active(record, now):
                    record = None
            if record is None:
                raise DnsTsigError("BADKEY", {
                    "key_name": key_name, "generation": generation,
                })
            canonical = self._canonical(key_name, record["algorithm"],
                                        record["generation"], now, fudge)
            mac = self._mac(record["secret"], message, canonical)
            self._stats["signed"] += 1
            return {
                "key_name": key_name,
                "algorithm": record["algorithm"],
                "generation": record["generation"],
                "timestamp": now,
                "fudge": fudge,
                "mac": mac,
            }

    def _verify(self, message: bytes, tsig: dict, now) -> dict:
        if not isinstance(tsig, dict) or any(
                field not in tsig for field in _TSIG_FIELDS):
            raise DnsTsigError("BADFORM", {
                "missing": [field for field in _TSIG_FIELDS
                            if not isinstance(tsig, dict) or field not in tsig],
            })
        key_name = tsig["key_name"]
        record = self._find_generation(key_name, tsig["generation"])
        if (record is None
                or not self._is_active(record, now)
                or record["algorithm"] != tsig["algorithm"]):
            raise DnsTsigError("BADKEY", {
                "key_name": key_name, "generation": tsig["generation"],
            })
        canonical = self._canonical(key_name, record["algorithm"],
                                    record["generation"], tsig["timestamp"],
                                    tsig["fudge"])
        expected = self._mac(record["secret"], message, canonical)
        mac = tsig["mac"]
        if not isinstance(mac, str) or not hmac.compare_digest(mac, expected):
            raise DnsTsigError("BADSIG", {"key_name": key_name})
        timestamp = tsig["timestamp"]
        if (not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
                or abs(now - timestamp) > self.clock_skew):
            raise DnsTsigError("BADTIME", {
                "key_name": key_name, "timestamp": timestamp,
            })
        self._purge_replay(now)
        marker = (key_name, timestamp, mac)
        if marker in self._replay:
            raise DnsTsigError("REPLAY", {
                "key_name": key_name, "timestamp": timestamp,
            })
        self._replay[marker] = now
        while len(self._replay) > self.max_replay:
            oldest = min(self._replay, key=self._replay.get)
            del self._replay[oldest]
        self._stats["verified"] += 1
        return {
            "status": "ok",
            "key_name": key_name,
            "generation": record["generation"],
            "signed": True,
        }

    def _track_failure(self, err: DnsTsigError) -> None:
        self._stats["rejected"] += 1
        if err.code == "REPLAY":
            self._stats["replayed"] += 1

    def verify_tsig(self, message: bytes, tsig: dict, *, now=None) -> dict:
        with self._lock:
            now = self._now(now)
            try:
                return self._verify(message, tsig, now)
            except DnsTsigError as err:
                self._track_failure(err)
                raise

    def verify_and_sign_error(self, message: bytes, tsig: dict, *,
                              now=None) -> dict:
        with self._lock:
            now = self._now(now)
            try:
                return self._verify(message, tsig, now)
            except DnsTsigError as err:
                self._track_failure(err)
                if err.code not in _SIGNABLE_ERRORS:
                    raise
                key_name = tsig.get("key_name") if isinstance(tsig, dict) else None
                payload = message + b"|" + err.code.encode("utf-8")
                error_tsig = self._sign_error(payload, key_name, now)
                if error_tsig is None:
                    raise
                return {
                    "status": "error",
                    "code": err.code,
                    "key_name": error_tsig["key_name"],
                    "tsig": error_tsig,
                }

    def _sign_error(self, payload: bytes, key_name, now):
        candidates = []
        if key_name is not None and self._latest_active(key_name, now):
            candidates.append(key_name)
        for name in sorted(self._keys):
            if name not in candidates and self._latest_active(name, now):
                candidates.append(name)
        if not candidates:
            return None
        return self.sign_message(payload, key_name=candidates[0], now=now)

    def _purge_replay(self, now) -> int:
        stale = [marker for marker, seen in self._replay.items()
                 if now - seen >= self.replay_window]
        for marker in stale:
            del self._replay[marker]
        return len(stale)

    def purge_replay(self, *, now=None) -> int:
        with self._lock:
            return self._purge_replay(self._now(now))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "keys": {
                    name: [{
                        "generation": record["generation"],
                        "secret": record["secret"].hex(),
                        "algorithm": record["algorithm"],
                        "overlap_until": record["overlap_until"],
                        "retired": record["retired"],
                    } for record in records]
                    for name, records in self._keys.items()
                },
                "replay": [
                    [key_name, timestamp, mac, seen]
                    for (key_name, timestamp, mac), seen in self._replay.items()
                ],
                "stats": dict(self._stats),
            }

    @staticmethod
    def _parse_state(state):
        if not isinstance(state, dict):
            raise DnsTsigError("STATE_INVALID", {"reason": "not-a-mapping"})
        raw_keys = state.get("keys")
        if not isinstance(raw_keys, dict):
            raise DnsTsigError("STATE_INVALID", {"reason": "keys-not-a-mapping"})
        keys = {}
        for name, records in raw_keys.items():
            if not isinstance(name, str) or not isinstance(records, list):
                raise DnsTsigError("STATE_INVALID", {"reason": "bad-key-entry"})
            parsed_records = []
            for record in records:
                if not isinstance(record, dict):
                    raise DnsTsigError("STATE_INVALID",
                                       {"reason": "bad-generation-entry"})
                generation = record.get("generation")
                secret = record.get("secret")
                algorithm = record.get("algorithm")
                overlap_until = record.get("overlap_until")
                retired = record.get("retired")
                if (not isinstance(generation, int)
                        or isinstance(generation, bool)
                        or not isinstance(secret, str)
                        or not isinstance(algorithm, str)
                        or not (overlap_until is None
                                or isinstance(overlap_until, (int, float)))
                        or not isinstance(retired, bool)):
                    raise DnsTsigError("STATE_INVALID",
                                       {"reason": "bad-generation-fields"})
                try:
                    secret_bytes = bytes.fromhex(secret)
                except ValueError:
                    raise DnsTsigError("STATE_INVALID",
                                       {"reason": "bad-secret-encoding"})
                parsed_records.append({
                    "generation": generation,
                    "secret": secret_bytes,
                    "algorithm": algorithm,
                    "overlap_until": overlap_until,
                    "retired": retired,
                })
            keys[name] = parsed_records
        replay = {}
        raw_replay = state.get("replay", [])
        if not isinstance(raw_replay, list):
            raise DnsTsigError("STATE_INVALID", {"reason": "bad-replay-entry"})
        for entry in raw_replay:
            if (not isinstance(entry, (list, tuple)) or len(entry) != 4
                    or not isinstance(entry[0], str)
                    or not isinstance(entry[1], (int, float))
                    or not isinstance(entry[2], str)
                    or not isinstance(entry[3], (int, float))):
                raise DnsTsigError("STATE_INVALID", {"reason": "bad-replay-entry"})
            replay[(entry[0], entry[1], entry[2])] = entry[3]
        stats = None
        raw_stats = state.get("stats")
        if raw_stats is not None:
            if (not isinstance(raw_stats, dict)
                    or any(not isinstance(raw_stats.get(name), int)
                           for name in _STAT_NAMES)):
                raise DnsTsigError("STATE_INVALID", {"reason": "bad-stats"})
            stats = {name: raw_stats[name] for name in _STAT_NAMES}
        return keys, replay, stats

    def restore(self, state: dict) -> None:
        with self._lock:
            keys, replay, stats = self._parse_state(state)
            self._keys = keys
            self._replay = replay
            if stats is not None:
                self._stats = stats

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {name: 0 for name in _STAT_NAMES}
