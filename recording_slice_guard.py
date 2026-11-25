"""Recording slice alignment, manifest indexing and upload idempotency."""

from __future__ import annotations

import copy
import hashlib
import json
import threading

STAT_KEYS = (
    "samples",
    "slices_closed",
    "uploads",
    "retries",
    "cleaned",
    "rejected",
)

MANIFEST_LIMIT = 4096
SNAPSHOT_VERSION = 1
STATUSES = ("open", "closed", "uploaded", "failed")


class RecordingError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RecordingSliceGuard:
    """Cut recordings on keyframes and track the upload manifest."""

    def __init__(self, *, stream_id: str = "stream-0",
                 target_seconds: float = 10.0,
                 max_slice_bytes: int = 8388608,
                 retention_seconds: int = 3600,
                 max_retries: int = 3, backoff_ms: int = 200) -> None:
        self.stream_id = stream_id
        self.target_seconds = target_seconds
        self.max_slice_bytes = max_slice_bytes
        self.retention_seconds = retention_seconds
        self.max_retries = max_retries
        self.backoff_ms = backoff_ms
        self._lock = threading.RLock()
        self._records: list[dict] = []
        self._open: dict | None = None
        self._next_seq = 1
        self._stats = {key: 0 for key in STAT_KEYS}

    def _new_record(self, pts: int, duration: int, size: int) -> dict:
        seq = self._next_seq
        self._next_seq += 1
        record = {
            "slice_id": f"slice-{seq}",
            "seq": seq,
            "start_pts": pts,
            "end_pts": pts + duration,
            "bytes": 0,
            "samples": [],
            "status": "open",
            "attempts": 0,
            "retry_in_ms": 0,
        }
        self._records.append(record)
        self._open = record
        return record

    def _digest(self, record: dict) -> str:
        payload = json.dumps(record["samples"], separators=(",", ":"))
        return hashlib.md5(payload.encode("utf-8")).hexdigest()[:16]

    def _public_view(self, record: dict) -> dict:
        return {
            "slice_id": record["slice_id"],
            "seq": record["seq"],
            "start_pts": record["start_pts"],
            "end_pts": record["end_pts"],
            "bytes": record["bytes"],
            "samples": len(record["samples"]),
            "status": record["status"],
            "digest": self._digest(record),
        }

    def _find(self, slice_id: str) -> dict:
        for record in self._records:
            if record["slice_id"] == slice_id:
                return record
        raise RecordingError(
            "REC_UNKNOWN_SLICE",
            {"slice_id": slice_id},
        )

    def _evict(self) -> None:
        while len(self._records) > MANIFEST_LIMIT:
            victim = next(
                (record for record in self._records
                 if record["status"] == "uploaded"),
                None,
            )
            if victim is None:
                return
            self._records.remove(victim)

    def _finalize(self, record: dict) -> None:
        if record["status"] == "open":
            record["status"] = "closed"
        if self._open is record:
            self._open = None
        self._evict()

    def append(self, pts: int, duration: int, size: int, *,
               keyframe: bool = False) -> dict:
        with self._lock:
            if pts < 0 or duration <= 0 or size <= 0:
                self._stats["rejected"] += 1
                raise RecordingError(
                    "REC_INVALID_SAMPLE",
                    {"pts": pts, "duration": duration, "size": size},
                )

            record = self._open
            if record is None:
                if not keyframe:
                    self._stats["rejected"] += 1
                    raise RecordingError(
                        "REC_INVALID_SAMPLE",
                        {"pts": pts, "reason": "no open slice"},
                    )
                record = self._new_record(pts, duration, size)
            elif keyframe:
                target_ms = int(self.target_seconds * 1000)
                duration_full = pts - record["start_pts"] >= target_ms
                oversized = record["bytes"] >= self.max_slice_bytes
                if duration_full or oversized:
                    self._finalize(record)
                    self._stats["slices_closed"] += 1
                    record = self._new_record(pts, duration, size)

            record["samples"].append([pts, duration, size, bool(keyframe)])
            record["bytes"] += size
            record["end_pts"] = pts + duration
            self._stats["samples"] += 1
            self._evict()

            return {
                "slice_id": record["slice_id"],
                "seq": record["seq"],
                "bytes": record["bytes"],
                "samples": len(record["samples"]),
            }

    def close(self) -> dict | None:
        with self._lock:
            record = self._open
            if record is None:
                return None
            self._finalize(record)
            self._stats["slices_closed"] += 1
            return self._public_view(record)

    def slices(self) -> list:
        with self._lock:
            return [self._public_view(record)
                    for record in self._records]

    def manifest(self) -> dict:
        with self._lock:
            return {
                "stream_id": self.stream_id,
                "slice_count": len(self._records),
                "slices": [self._public_view(record)
                           for record in self._records],
            }

    def slice_digest(self, slice_id: str) -> str:
        with self._lock:
            return self._digest(self._find(slice_id))

    def idempotency_key(self, slice_id: str) -> str:
        with self._lock:
            record = self._find(slice_id)
            return f"{self.stream_id}:{record['seq']}:{self._digest(record)}"

    def mark_uploaded(self, slice_id: str, *, ok: bool) -> dict:
        with self._lock:
            record = self._find(slice_id)

            if ok:
                if record["status"] != "uploaded":
                    record["status"] = "uploaded"
                    record["retry_in_ms"] = 0
                    self._stats["uploads"] += 1
                return {
                    "slice_id": record["slice_id"],
                    "uploaded": True,
                    "status": record["status"],
                    "attempts": record["attempts"],
                }

            if record["status"] == "uploaded":
                return {
                    "slice_id": record["slice_id"],
                    "uploaded": True,
                    "status": record["status"],
                    "attempts": record["attempts"],
                }
            if record["status"] == "failed":
                raise RecordingError(
                    "REC_RETRY_EXHAUSTED",
                    {"slice_id": slice_id, "attempts": record["attempts"]},
                )

            record["attempts"] += 1
            if record["attempts"] > self.max_retries:
                record["status"] = "failed"
                raise RecordingError(
                    "REC_RETRY_EXHAUSTED",
                    {"slice_id": slice_id, "attempts": record["attempts"]},
                )

            record["retry_in_ms"] = (
                self.backoff_ms * (2 ** (record["attempts"] - 1))
            )
            self._stats["retries"] += 1
            return {
                "slice_id": record["slice_id"],
                "uploaded": False,
                "status": record["status"],
                "attempts": record["attempts"],
                "retry_in_ms": record["retry_in_ms"],
            }

    def retry_delay_ms(self, slice_id: str) -> int:
        with self._lock:
            record = self._find(slice_id)
            if record["attempts"] == 0:
                return 0
            return self.backoff_ms * (2 ** (record["attempts"] - 1))

    def pending_upload(self) -> list:
        with self._lock:
            return [
                record["slice_id"]
                for record in self._records
                if record["status"] in ("open", "closed")
            ]

    def cleanup(self, now: int) -> list:
        with self._lock:
            retention_ms = int(self.retention_seconds * 1000)
            removed = []
            survivors = []
            for record in self._records:
                expired = now - record["end_pts"] >= retention_ms
                if record["status"] == "uploaded" and expired:
                    removed.append(record["slice_id"])
                    self._stats["cleaned"] += 1
                else:
                    survivors.append(record)
            self._records = survivors
            return removed

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "stream_id": self.stream_id,
                "target_seconds": self.target_seconds,
                "max_slice_bytes": self.max_slice_bytes,
                "retention_seconds": self.retention_seconds,
                "max_retries": self.max_retries,
                "backoff_ms": self.backoff_ms,
                "next_seq": self._next_seq,
                "open_slice_id": (
                    self._open["slice_id"] if self._open is not None else None
                ),
                "slices": copy.deepcopy(self._records),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            records, next_seq, open_id, stats = self._validate_snapshot(state)
            self._records = copy.deepcopy(records)
            self._next_seq = next_seq
            self._open = (
                next(record for record in self._records
                     if record["slice_id"] == open_id)
                if open_id is not None else None
            )
            self._stats = copy.deepcopy(stats)

    def _validate_snapshot(self, state: dict):
        invalid = RecordingError(
            "REC_SNAPSHOT_INVALID",
            {"reason": "malformed snapshot state"},
        )
        try:
            if not isinstance(state, dict) or \
                    state.get("version") != SNAPSHOT_VERSION:
                raise invalid

            raw_slices = state["slices"]
            if not isinstance(raw_slices, list):
                raise invalid

            records = []
            seen_seqs = set()
            open_id = state.get("open_slice_id")
            open_count = 0
            for raw in raw_slices:
                if not isinstance(raw, dict):
                    raise invalid
                required = (
                    "slice_id", "seq", "start_pts", "end_pts",
                    "bytes", "samples", "status",
                    "attempts", "retry_in_ms",
                )
                if any(key not in raw for key in required):
                    raise invalid
                seq = raw["seq"]
                if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                    raise invalid
                if seq in seen_seqs or raw["slice_id"] != f"slice-{seq}":
                    raise invalid
                if raw["status"] not in STATUSES:
                    raise invalid
                if not isinstance(raw["start_pts"], int) or \
                        isinstance(raw["start_pts"], bool) or \
                        raw["start_pts"] < 0:
                    raise invalid
                if not isinstance(raw["end_pts"], int) or \
                        isinstance(raw["end_pts"], bool) or \
                        raw["end_pts"] < raw["start_pts"]:
                    raise invalid
                if isinstance(raw["bytes"], bool) or \
                        not isinstance(raw["bytes"], int) or raw["bytes"] <= 0:
                    raise invalid
                samples = raw["samples"]
                if not isinstance(samples, list):
                    raise invalid
                total_bytes = 0
                for sample in samples:
                    if not isinstance(sample, (list, tuple)) or len(sample) != 4:
                        raise invalid
                    sample_pts, sample_duration, sample_size, sample_key = sample
                    if isinstance(sample_pts, bool) or \
                            not isinstance(sample_pts, int) or sample_pts < 0:
                        raise invalid
                    if isinstance(sample_duration, bool) or \
                            not isinstance(sample_duration, int) or \
                            sample_duration <= 0:
                        raise invalid
                    if isinstance(sample_size, bool) or \
                            not isinstance(sample_size, int) or sample_size <= 0:
                        raise invalid
                    if not isinstance(sample_key, bool):
                        raise invalid
                    total_bytes += sample_size
                if total_bytes != raw["bytes"]:
                    raise invalid
                if isinstance(raw["attempts"], bool) or \
                        not isinstance(raw["attempts"], int) or \
                        raw["attempts"] < 0:
                    raise invalid
                if isinstance(raw["retry_in_ms"], bool) or \
                        not isinstance(raw["retry_in_ms"], int) or \
                        raw["retry_in_ms"] < 0:
                    raise invalid
                if open_id is not None and raw["slice_id"] == open_id:
                    if raw["status"] != "open":
                        raise invalid
                    open_count += 1
                seen_seqs.add(seq)
                records.append(copy.deepcopy(raw))

            if open_id is not None and open_count != 1:
                raise invalid
            next_seq = state.get("next_seq")
            if isinstance(next_seq, bool) or not isinstance(next_seq, int) or \
                    not next_seq or next_seq <= max(seen_seqs, default=0):
                raise invalid

            stats = state.get("stats", {key: 0 for key in STAT_KEYS})
            if not isinstance(stats, dict) or \
                    any(key not in stats or not isinstance(stats[key], int)
                        or isinstance(stats[key], bool)
                        for key in STAT_KEYS):
                raise invalid
        except RecordingError:
            raise
        except (KeyError, TypeError) as exc:
            raise invalid from exc

        return records, next_seq, open_id, copy.deepcopy(stats)

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STAT_KEYS}
