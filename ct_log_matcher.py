"""Certificate transparency log entry merger with revocation matching."""

import re
import threading


_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_FIELDS = (
    "log",
    "fingerprint",
    "serial",
    "not_before",
    "not_after",
    "timestamp",
)
_INFO_FIELDS = _ENTRY_FIELDS + ("sources",)
_STATS_KEYS = ("ingested", "duplicates", "revoked", "ignored", "cursor")


class CtError(ValueError):
    """Raised for every rejected ingestion or revocation operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _normalized_fingerprint(value):
    if not isinstance(value, str):
        return None
    lowered = value.lower()
    if _FINGERPRINT_RE.fullmatch(lowered) is None:
        return None
    return lowered


def _validate_entry(raw):
    if not isinstance(raw, dict):
        raise CtError("CT_INVALID_ENTRY", "entry must be an object")
    for field in _ENTRY_FIELDS:
        if field not in raw:
            raise CtError("CT_INVALID_ENTRY", "missing field: %s" % field)

    log = raw["log"]
    if not isinstance(log, str) or not log:
        raise CtError("CT_INVALID_ENTRY", "log must be a non-empty string")

    fingerprint = _normalized_fingerprint(raw["fingerprint"])
    if fingerprint is None:
        raise CtError(
            "CT_INVALID_ENTRY", "fingerprint must be 64 lowercase hex chars"
        )

    serial = raw["serial"]
    if not isinstance(serial, str):
        raise CtError("CT_INVALID_ENTRY", "serial must be a string")

    timestamp = raw["timestamp"]
    not_before = raw["not_before"]
    not_after = raw["not_after"]
    for name, value in (
        ("timestamp", timestamp),
        ("not_before", not_before),
        ("not_after", not_after),
    ):
        if not _is_number(value):
            raise CtError("CT_INVALID_ENTRY", "%s must be a number" % name)

    if not_before > not_after:
        raise CtError(
            "CT_INVALID_ENTRY", "not_before must not be later than not_after"
        )

    return {
        "log": log,
        "fingerprint": fingerprint,
        "serial": serial,
        "not_before": not_before,
        "not_after": not_after,
        "timestamp": timestamp,
    }


def _validate_snapshot(state):
    if not isinstance(state, dict):
        raise CtError("CT_SNAPSHOT_INVALID", "snapshot must be an object")
    if state.get("version") != 1:
        raise CtError("CT_SNAPSHOT_INVALID", "unsupported snapshot version")

    records = state.get("records")
    unique = state.get("unique")
    stats = state.get("stats")
    if not isinstance(records, list):
        raise CtError("CT_SNAPSHOT_INVALID", "records must be a list")
    if not isinstance(unique, dict):
        raise CtError("CT_SNAPSHOT_INVALID", "unique must be a dict")
    if not isinstance(stats, dict):
        raise CtError("CT_SNAPSHOT_INVALID", "stats must be a dict")

    if set(stats.keys()) != set(_STATS_KEYS):
        raise CtError("CT_SNAPSHOT_INVALID", "stats has an unexpected shape")
    clean_stats = {}
    for key in _STATS_KEYS:
        value = stats[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise CtError("CT_SNAPSHOT_INVALID", "stats.%s must be an int" % key)
        clean_stats[key] = value

    clean_records = []
    for record in records:
        try:
            clean_records.append(_validate_entry(record))
        except CtError as exc:
            raise CtError(
                "CT_SNAPSHOT_INVALID", "bad record: %s" % exc.detail
            ) from exc

    clean_unique = {}
    for fingerprint, info in unique.items():
        normalized = _normalized_fingerprint(fingerprint)
        if normalized is None or normalized != fingerprint:
            raise CtError("CT_SNAPSHOT_INVALID", "bad unique fingerprint")
        if not isinstance(info, dict):
            raise CtError("CT_SNAPSHOT_INVALID", "unique entry must be an object")
        for field in _INFO_FIELDS:
            if field not in info:
                raise CtError(
                    "CT_SNAPSHOT_INVALID", "unique entry missing %s" % field
                )
        if info["fingerprint"] != fingerprint:
            raise CtError("CT_SNAPSHOT_INVALID", "fingerprint key mismatch")
        if not isinstance(info["log"], str) or not info["log"]:
            raise CtError("CT_SNAPSHOT_INVALID", "bad log in unique entry")
        if not isinstance(info["serial"], str):
            raise CtError("CT_SNAPSHOT_INVALID", "bad serial in unique entry")
        for name in ("timestamp", "not_before", "not_after"):
            if not _is_number(info[name]):
                raise CtError(
                    "CT_SNAPSHOT_INVALID", "bad %s in unique entry" % name
                )
        if info["not_before"] > info["not_after"]:
            raise CtError("CT_SNAPSHOT_INVALID", "bad validity window")
        sources = info["sources"]
        if not isinstance(sources, list) or not all(
            isinstance(source, str) and source for source in sources
        ):
            raise CtError("CT_SNAPSHOT_INVALID", "bad sources in unique entry")

        clean_unique[fingerprint] = {
            "fingerprint": fingerprint,
            "log": info["log"],
            "serial": info["serial"],
            "not_before": info["not_before"],
            "not_after": info["not_after"],
            "timestamp": info["timestamp"],
            "sources": sorted(set(sources)),
        }

    record_fingerprints = {record["fingerprint"] for record in clean_records}
    if record_fingerprints != set(clean_unique.keys()):
        raise CtError(
            "CT_SNAPSHOT_INVALID", "records and unique fingerprints disagree"
        )

    return {
        "records": clean_records,
        "unique": clean_unique,
        "stats": clean_stats,
    }


class CtLogMatcher:
    """Merge CT log entries by fingerprint and match revocation records."""

    def __init__(self, *, max_entries=100000):
        self.max_entries = max_entries
        self._lock = threading.RLock()
        self._records = []
        self._unique = {}
        self._stats = {
            "ingested": 0,
            "duplicates": 0,
            "revoked": 0,
            "ignored": 0,
            "cursor": 0,
        }

    def ingest(self, entries):
        if not isinstance(entries, list):
            raise CtError("CT_INVALID_ENTRY", "entries must be a list")
        normalized = [_validate_entry(item) for item in entries]

        with self._lock:
            new_fingerprints = {
                item["fingerprint"]
                for item in normalized
                if item["fingerprint"] not in self._unique
            }
            if len(self._unique) + len(new_fingerprints) > self.max_entries:
                raise CtError(
                    "CT_CAPACITY_EXCEEDED",
                    "capacity %d reached" % self.max_entries,
                )

            added = 0
            max_timestamp = self._stats["cursor"]
            for item in normalized:
                fingerprint = item["fingerprint"]
                self._records.append(dict(item))

                info = self._unique.get(fingerprint)
                if info is None:
                    self._unique[fingerprint] = {
                        "fingerprint": fingerprint,
                        "log": item["log"],
                        "serial": item["serial"],
                        "not_before": item["not_before"],
                        "not_after": item["not_after"],
                        "timestamp": item["timestamp"],
                        "sources": [item["log"]],
                    }
                    added += 1
                    self._stats["ingested"] += 1
                else:
                    self._stats["duplicates"] += 1
                    if item["log"] not in info["sources"]:
                        info["sources"].append(item["log"])
                    if item["timestamp"] < info["timestamp"]:
                        info["log"] = item["log"]
                        info["serial"] = item["serial"]
                        info["not_before"] = item["not_before"]
                        info["not_after"] = item["not_after"]
                        info["timestamp"] = item["timestamp"]

                if item["timestamp"] > max_timestamp:
                    max_timestamp = item["timestamp"]

            self._stats["cursor"] = int(max_timestamp)
            return added

    def dedup(self):
        with self._lock:
            chosen = {}
            for index, record in enumerate(self._records):
                fingerprint = record["fingerprint"]
                if fingerprint not in chosen:
                    chosen[fingerprint] = index
                elif record["timestamp"] < self._records[chosen[fingerprint]][
                    "timestamp"
                ]:
                    chosen[fingerprint] = index

            kept = [
                dict(self._records[index])
                for index in sorted(chosen.values())
            ]
            removed = len(self._records) - len(kept)
            self._records = kept
            return removed

    def match_revocation(self, cert, reason, at):
        if not isinstance(cert, dict):
            raise CtError("CT_INVALID_REVOCATION", "cert must be an object")
        fingerprint = _normalized_fingerprint(cert.get("fingerprint"))
        if fingerprint is None:
            raise CtError("CT_INVALID_REVOCATION", "invalid cert fingerprint")
        if not isinstance(reason, str) or not reason:
            raise CtError(
                "CT_INVALID_REVOCATION", "reason must be a non-empty string"
            )
        if not _is_number(at):
            raise CtError("CT_INVALID_REVOCATION", "at must be a number")

        with self._lock:
            info = self._unique.get(fingerprint)
            if info is None:
                return {"status": "unknown"}

            result = {
                "status": None,
                "fingerprint": fingerprint,
                "reason": reason,
                "at": at,
            }
            if at < info["not_before"]:
                result["status"] = "invalid"
                self._stats["ignored"] += 1
            elif at > info["not_after"]:
                result["status"] = "expired"
                self._stats["ignored"] += 1
            else:
                result["status"] = "revoked"
                self._stats["revoked"] += 1
            return result

    def cursor(self):
        with self._lock:
            return self._stats["cursor"]

    def advance_cursor(self, value):
        if not isinstance(value, int) or isinstance(value, bool):
            raise CtError("CT_INVALID_CURSOR", "cursor must be an integer")
        with self._lock:
            if value <= self._stats["cursor"]:
                raise CtError(
                    "CT_CURSOR_REGRESSION",
                    "cursor %d is not above %d"
                    % (value, self._stats["cursor"]),
                )
            self._stats["cursor"] = value
            return value

    def processed(self):
        with self._lock:
            return len(self._unique)

    def lookup(self, fingerprint):
        normalized = _normalized_fingerprint(fingerprint)
        with self._lock:
            info = self._unique.get(normalized) if normalized else None
            if info is None:
                raise CtError("CT_UNKNOWN_CERT", "unknown fingerprint")
            return {
                "fingerprint": info["fingerprint"],
                "log": info["log"],
                "serial": info["serial"],
                "not_before": info["not_before"],
                "not_after": info["not_after"],
                "timestamp": info["timestamp"],
                "sources": sorted(info["sources"]),
            }

    def entries(self):
        with self._lock:
            ordered = sorted(
                self._unique.values(),
                key=lambda item: (item["timestamp"], item["fingerprint"]),
            )
            return [
                {
                    "log": item["log"],
                    "fingerprint": item["fingerprint"],
                    "serial": item["serial"],
                    "not_before": item["not_before"],
                    "not_after": item["not_after"],
                    "timestamp": item["timestamp"],
                }
                for item in ordered
            ]

    def stats(self):
        with self._lock:
            return {key: self._stats[key] for key in _STATS_KEYS}

    def snapshot(self):
        with self._lock:
            unique_copy = {}
            for fingerprint, info in self._unique.items():
                unique_copy[fingerprint] = {
                    "fingerprint": info["fingerprint"],
                    "log": info["log"],
                    "serial": info["serial"],
                    "not_before": info["not_before"],
                    "not_after": info["not_after"],
                    "timestamp": info["timestamp"],
                    "sources": sorted(info["sources"]),
                }
            return {
                "version": 1,
                "records": [dict(record) for record in self._records],
                "unique": unique_copy,
                "stats": {key: self._stats[key] for key in _STATS_KEYS},
            }

    def restore(self, state):
        clean = _validate_snapshot(state)
        with self._lock:
            self._records = clean["records"]
            self._unique = clean["unique"]
            self._stats = clean["stats"]
