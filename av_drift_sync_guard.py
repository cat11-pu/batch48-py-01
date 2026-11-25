"""Clock drift estimation and watermark compensation based on RTCP sender reports."""

from __future__ import annotations

import copy
import math
import threading
from collections import deque
from typing import Any


STAT_KEYS = (
    "streams_registered",
    "streams_removed",
    "sr_ingested",
    "sr_rejected",
    "samples_dropped",
    "ratio_clamped",
    "drift_warnings",
    "audio_padded_ms",
    "audio_dropped_ms",
)


class SyncError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class AvDriftSyncGuard:
    """Map RTP to NTP, estimate audio drift and schedule video on the main clock."""

    def __init__(self, *, max_streams: int = 8, max_samples: int = 32,
                 clamp_low: float = 0.98, clamp_high: float = 1.02,
                 warn_ms: float = 200.0, early_ms: float = 40.0,
                 late_ms: float = 120.0, low_watermark_ms: float = 60.0,
                 high_watermark_ms: float = 240.0, clock=None) -> None:
        self.max_streams = max_streams
        self.max_samples = max_samples
        self.clamp_low = clamp_low
        self.clamp_high = clamp_high
        self.warn_ms = warn_ms
        self.early_ms = early_ms
        self.late_ms = late_ms
        self.low_watermark_ms = low_watermark_ms
        self.high_watermark_ms = high_watermark_ms
        self._clock = clock
        self._lock = threading.RLock()
        self._streams: dict[str, dict[str, Any]] = {}
        self._primary: str | None = None
        self._pending: str | None = None
        self._primary_election_used = False
        self._stats: dict[str, float] = {key: 0 for key in STAT_KEYS}

    def register_stream(self, stream_id: str, *, kind: str,
                        clock_rate: int) -> dict:
        if not isinstance(stream_id, str):
            raise SyncError(
                "SYNC_INVALID_STREAM",
                {"field": "stream_id", "value": stream_id},
            )
        if kind not in ("audio", "video"):
            raise SyncError(
                "SYNC_INVALID_STREAM",
                {"stream_id": stream_id, "field": "kind", "value": kind},
            )
        if not _is_positive_int(clock_rate):
            raise SyncError(
                "SYNC_INVALID_STREAM",
                {"stream_id": stream_id, "field": "clock_rate",
                 "value": clock_rate},
            )

        with self._lock:
            if stream_id in self._streams:
                raise SyncError("SYNC_DUPLICATE_STREAM", stream_id)
            if len(self._streams) >= self.max_streams:
                raise SyncError("SYNC_STREAM_LIMIT", {
                    "stream_id": stream_id,
                    "max_streams": self.max_streams,
                })

            record = {
                "stream_id": stream_id,
                "kind": kind,
                "clock_rate": clock_rate,
                "samples": deque(),
                "baseline_ntp_ms": None,
                "baseline_rtp_ts": None,
                "drift_ms": 0.0,
                "ratio": 1.0,
                "buffered_ms": 0.0,
                "confirmations": 0,
                "remap_required": False,
                "last_now_ms": None,
            }
            self._streams[stream_id] = record
            self._stats["streams_registered"] += 1

            if not self._primary_election_used:
                self._primary = stream_id
                self._primary_election_used = True

            return self._stream_info_locked(record)

    def unregister_stream(self, stream_id: str) -> None:
        with self._lock:
            self._require_stream_locked(stream_id)
            del self._streams[stream_id]
            self._stats["streams_removed"] += 1
            if self._primary == stream_id:
                self._primary = None
            if self._pending == stream_id:
                self._pending = None

    def ingest_sr(self, stream_id: str, *, ntp_ms: float,
                  rtp_ts: int) -> dict:
        with self._lock:
            record = self._require_stream_locked(stream_id)

            try:
                if not _is_finite_number(ntp_ms):
                    raise SyncError("SYNC_INVALID_SR", {
                        "stream_id": stream_id,
                        "field": "ntp_ms",
                        "value": ntp_ms,
                    })
                ntp_value = float(ntp_ms)

                if not _is_integer(rtp_ts):
                    raise SyncError("SYNC_INVALID_SR", {
                        "stream_id": stream_id,
                        "field": "rtp_ts",
                        "value": rtp_ts,
                    })

                samples = record["samples"]
                if not record["remap_required"] and samples:
                    last_sample = samples[-1]
                    if (ntp_value <= last_sample["ntp_ms"]
                            or rtp_ts < last_sample["rtp_ts"]):
                        raise SyncError("SYNC_INVALID_SR", {
                            "stream_id": stream_id,
                            "ntp_ms": ntp_ms,
                            "rtp_ts": rtp_ts,
                            "last_ntp_ms": last_sample["ntp_ms"],
                            "last_rtp_ts": last_sample["rtp_ts"],
                        })
            except SyncError as exc:
                if exc.code == "SYNC_INVALID_SR":
                    self._stats["sr_rejected"] += 1
                    if self._pending == stream_id:
                        record["confirmations"] = 0
                raise

            self._stats["sr_ingested"] += 1

            max_samples = self.max_samples
            sample_limit_valid = _is_positive_int(max_samples) or max_samples == 0
            if sample_limit_valid and len(samples) >= max_samples:
                self._stats["samples_dropped"] += 1
            samples.append({"ntp_ms": ntp_value, "rtp_ts": rtp_ts})
            if sample_limit_valid:
                while len(samples) > max_samples:
                    samples.popleft()

            baseline_created = (
                record["baseline_ntp_ms"] is None
                or record["baseline_rtp_ts"] is None
                or record["remap_required"]
            )
            switched = False

            if baseline_created:
                record["baseline_ntp_ms"] = ntp_value
                record["baseline_rtp_ts"] = rtp_ts
                record["drift_ms"] = 0.0
                record["ratio"] = 1.0
                record["remap_required"] = False
                drift = 0.0
                ratio = 1.0
            else:
                baseline_ntp = float(record["baseline_ntp_ms"])
                baseline_rtp = int(record["baseline_rtp_ts"])
                rtp_delta_ms = (
                    (rtp_ts - baseline_rtp) * 1000.0
                    / float(record["clock_rate"])
                )
                ntp_span = ntp_value - baseline_ntp
                drift = rtp_delta_ms - ntp_span
                raw_ratio = 1.0 + drift / ntp_span if ntp_span else 1.0

                if raw_ratio < self.clamp_low or raw_ratio > self.clamp_high:
                    self._stats["ratio_clamped"] += 1
                ratio = min(self.clamp_high,
                            max(self.clamp_low, raw_ratio))

                record["drift_ms"] = drift
                record["ratio"] = ratio

                if abs(drift) > self.warn_ms:
                    self._stats["drift_warnings"] += 1

            if self._pending == stream_id:
                record["confirmations"] += 1
                if record["confirmations"] >= 2:
                    self._primary = stream_id
                    self._pending = None
                    record["confirmations"] = 0
                    switched = True

            return {
                "baseline": baseline_created,
                "drift_ms": drift,
                "ratio": ratio,
                "switched": switched,
                "primary": self._primary,
            }

    def drift_ms(self, stream_id: str) -> float:
        with self._lock:
            record = self._require_stream_locked(stream_id)
            return float(record["drift_ms"])

    def drift_ratio(self, stream_id: str) -> float:
        with self._lock:
            record = self._require_stream_locked(stream_id)
            return float(record["ratio"])

    def stream_info(self, stream_id: str) -> dict:
        with self._lock:
            record = self._require_stream_locked(stream_id)
            return self._stream_info_locked(record)

    def primary_stream(self) -> str | None:
        with self._lock:
            return self._primary

    def request_primary(self, stream_id: str) -> dict:
        with self._lock:
            record = self._require_stream_locked(stream_id)
            self._pending = stream_id
            record["confirmations"] = 0
            return {"pending": stream_id, "switched": False}

    def enqueue_audio(self, stream_id: str, watermark_ms: float, *,
                      now_ms: float | None = None) -> dict:
        with self._lock:
            record = self._require_stream_locked(stream_id)
            if not _is_finite_number(watermark_ms):
                raise SyncError("SYNC_INVALID_STREAM", {
                    "stream_id": stream_id,
                    "field": "watermark_ms",
                    "value": watermark_ms,
                })

            current_depth = float(watermark_ms)
            if now_ms is None and callable(self._clock):
                now_ms = self._clock()
            record["last_now_ms"] = now_ms

            padded_ms = 0.0
            dropped_ms = 0.0

            if current_depth < self.low_watermark_ms:
                action = "pad"
                padded_ms = self.low_watermark_ms - current_depth
                buffered_ms = float(self.low_watermark_ms)
                self._stats["audio_padded_ms"] += max(0.0, padded_ms)
            elif current_depth > self.high_watermark_ms:
                action = "drop"
                dropped_ms = current_depth - self.high_watermark_ms
                buffered_ms = float(self.high_watermark_ms)
                self._stats["audio_dropped_ms"] += max(0.0, dropped_ms)
            else:
                action = "ok"
                buffered_ms = current_depth

            record["buffered_ms"] = buffered_ms
            return {
                "action": action,
                "padded_ms": padded_ms,
                "dropped_ms": dropped_ms,
                "buffered_ms": buffered_ms,
            }

    def schedule_video(self, stream_id: str, pts_ms: float, *,
                       now_ms: float) -> dict:
        with self._lock:
            record = self._require_stream_locked(stream_id)
            ratio = float(record["ratio"])
            corrected_ms = float(pts_ms) * ratio
            clock_delta = float(now_ms) - corrected_ms

            if clock_delta < -float(self.late_ms):
                action = "wait"
            elif clock_delta > float(self.early_ms):
                action = "drop"
            else:
                action = "render"

            return {"action": action, "corrected_ms": corrected_ms}

    def snapshot(self) -> dict:
        with self._lock:
            streams = copy.deepcopy(self._streams)
            for record in streams.values():
                record["samples"] = list(record["samples"])
            return {
                "primary": self._primary,
                "pending": self._pending,
                "streams": streams,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            restored = self._validate_snapshot(state)
            self._primary = restored["primary"]
            self._pending = restored["pending"]
            self._streams = restored["streams"]
            self._primary_election_used = (
                self._primary_election_used
                or self._primary is not None
                or bool(self._streams)
            )

    def stats(self) -> dict:
        with self._lock:
            return {key: self._stats[key] for key in STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0

    def _require_stream_locked(self, stream_id: str) -> dict[str, Any]:
        try:
            record = self._streams[stream_id]
        except (KeyError, TypeError):
            raise SyncError("SYNC_UNKNOWN_STREAM", stream_id) from None
        return record

    def _stream_info_locked(self, record: dict[str, Any]) -> dict:
        stream_id = record["stream_id"]
        return {
            "stream_id": stream_id,
            "kind": record["kind"],
            "clock_rate": record["clock_rate"],
            "primary": self._primary == stream_id,
            "samples": len(record["samples"]),
            "drift_ms": float(record["drift_ms"]),
            "ratio": float(record["ratio"]),
            "buffered_ms": float(record["buffered_ms"]),
        }

    def _validate_snapshot(self, state: object) -> dict[str, Any]:
        def invalid(detail: object = None) -> SyncError:
            return SyncError("SYNC_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            raise invalid("snapshot must be an object")
        if any(key not in state for key in ("primary", "pending", "streams")):
            raise invalid("snapshot is missing a required field")

        primary = state["primary"]
        pending = state["pending"]
        streams_obj = state["streams"]

        if primary is not None and not isinstance(primary, str):
            raise invalid("primary must be a stream id or None")
        if pending is not None and not isinstance(pending, str):
            raise invalid("pending must be a stream id or None")
        if not isinstance(streams_obj, dict):
            raise invalid("streams must be an object")
        if primary is not None and primary not in streams_obj:
            raise invalid("primary stream is missing")
        if pending is not None and pending not in streams_obj:
            raise invalid("pending stream is missing")

        max_samples = self.max_samples
        sample_maxlen = max_samples if (
            _is_positive_int(max_samples) or max_samples == 0
        ) else None
        streams: dict[str, dict[str, Any]] = {}

        for stream_id, source in streams_obj.items():
            if not isinstance(stream_id, str) or not isinstance(source, dict):
                raise invalid("invalid stream record")

            kind = source.get("kind")
            clock_rate = source.get("clock_rate")
            if kind not in ("audio", "video"):
                raise invalid({"stream_id": stream_id, "field": "kind"})
            if not _is_positive_int(clock_rate):
                raise invalid({
                    "stream_id": stream_id,
                    "field": "clock_rate",
                })

            samples_obj = source.get("samples")
            if not isinstance(samples_obj, list):
                raise invalid({
                    "stream_id": stream_id,
                    "field": "samples",
                })

            samples: deque[dict[str, Any]] = deque(maxlen=sample_maxlen)
            for sample in samples_obj:
                if not isinstance(sample, dict):
                    raise invalid("invalid SR sample")
                ntp_ms = sample.get("ntp_ms")
                rtp_ts = sample.get("rtp_ts")
                if not _is_finite_number(ntp_ms):
                    raise invalid("invalid sample ntp_ms")
                if not _is_integer(rtp_ts):
                    raise invalid("invalid sample rtp_ts")
                samples.append({
                    "ntp_ms": float(ntp_ms),
                    "rtp_ts": int(rtp_ts),
                })

            baseline_ntp = source.get("baseline_ntp_ms")
            baseline_rtp = source.get("baseline_rtp_ts")
            if baseline_ntp is not None and not _is_finite_number(baseline_ntp):
                raise invalid("invalid baseline ntp_ms")
            if baseline_rtp is not None and not _is_integer(baseline_rtp):
                raise invalid("invalid baseline rtp_ts")
            if baseline_ntp is None and samples:
                baseline_ntp = samples[0]["ntp_ms"]
                if baseline_rtp is None:
                    baseline_rtp = samples[0]["rtp_ts"]

            drift_ms = source.get("drift_ms", 0.0)
            ratio = source.get("ratio", 1.0)
            buffered_ms = source.get("buffered_ms", 0.0)
            confirmations = source.get("confirmations", 0)
            if not _is_finite_number(drift_ms):
                raise invalid("invalid drift_ms")
            if not _is_finite_number(ratio):
                raise invalid("invalid ratio")
            if not _is_finite_number(buffered_ms) or buffered_ms < 0:
                raise invalid("invalid buffered_ms")
            if (not isinstance(confirmations, int)
                    or isinstance(confirmations, bool)
                    or confirmations < 0):
                raise invalid("invalid confirmation count")

            streams[stream_id] = {
                "stream_id": stream_id,
                "kind": kind,
                "clock_rate": clock_rate,
                "samples": samples,
                "baseline_ntp_ms": (
                    float(baseline_ntp) if baseline_ntp is not None else None
                ),
                "baseline_rtp_ts": (
                    int(baseline_rtp) if baseline_rtp is not None else None
                ),
                "drift_ms": float(drift_ms),
                "ratio": float(ratio),
                "buffered_ms": float(buffered_ms),
                "confirmations": confirmations,
                "remap_required": True,
                "last_now_ms": None,
            }

        return {
            "primary": primary,
            "pending": pending,
            "streams": streams,
        }
