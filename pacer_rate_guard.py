"""Sender side AIMD rate control with token bucket pacing and overload dropping."""

from __future__ import annotations

import copy
import math
import threading
import time

STAT_KEYS = (
    "enqueued",
    "duplicates",
    "rejected_frames",
    "dropped_frames",
    "sent_frames",
    "bytes_sent",
    "cycles_up",
    "cycles_down",
    "loss_reports",
)

FRAME_KEYS = ("seq", "bytes", "keyframe", "droppable")
MODES = ("steady", "probe")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_frame(frame: object) -> dict:
    if not isinstance(frame, dict):
        raise PacerError("PACER_INVALID_FRAME", "frame must be a dict")
    for key in FRAME_KEYS:
        if key not in frame:
            raise PacerError("PACER_INVALID_FRAME", f"missing key: {key}")
    seq = frame["seq"]
    if not _is_int(seq) or seq < 0:
        raise PacerError("PACER_INVALID_FRAME", "seq must be a non-negative integer")
    size = frame["bytes"]
    if not _is_int(size) or size <= 0:
        raise PacerError("PACER_INVALID_FRAME", "bytes must be a positive integer")
    return frame


class PacerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PacerRateGuard:
    """Adjust the target bitrate and pace queued frames through a token bucket."""

    def __init__(self, *, min_bps: float = 200_000.0, max_bps: float = 4_000_000.0,
                 start_bps: float = 800_000.0, capacity_ms: float = 100.0,
                 max_queue_bytes: int = 120_000, clock=None) -> None:
        self.min_bps = min_bps
        self.max_bps = max_bps
        self.start_bps = start_bps
        self.capacity_ms = capacity_ms
        self.max_queue_bytes = max_queue_bytes
        self._clock = clock
        self._lock = threading.RLock()
        self._target_bps = self._clamp_bps(start_bps)
        self._tokens = self._capacity_locked()
        self._mode = "steady"
        self._queue: list[dict] = []
        self._queued_seqs: set[int] = set()
        self._queue_bytes = 0
        self._clean_cycles = 0
        self._probe_cycles = 0
        self._last_refill_ms: float | None = None
        self._stats = {key: 0 for key in STAT_KEYS}

    def _capacity_locked(self) -> float:
        return self._target_bps * self.capacity_ms / 8000.0

    def _clamp_bps(self, value: float) -> float:
        return min(self.max_bps, max(self.min_bps, value))

    def _now_ms(self, now_ms: float | None) -> float:
        if now_ms is not None:
            return now_ms
        if callable(self._clock):
            return self._clock()
        return time_monotonic_ms()

    def _refill_locked(self, now_ms: float) -> None:
        if self._last_refill_ms is None:
            self._last_refill_ms = now_ms
            return
        elapsed_ms = now_ms - self._last_refill_ms
        if elapsed_ms > 0:
            bytes_per_ms = self._target_bps / 8000.0
            capacity = self._capacity_locked()
            self._tokens = min(capacity, self._tokens + bytes_per_ms * elapsed_ms)
        self._last_refill_ms = now_ms

    def _apply_downshift_locked(self) -> tuple[float, bool]:
        new_bps = self._clamp_bps(self._target_bps * 0.85)
        changed = new_bps != self._target_bps
        self._target_bps = new_bps
        self._tokens = min(self._tokens, self._capacity_locked())
        self._mode = "probe"
        self._clean_cycles = 0
        self._probe_cycles = 0
        return new_bps, changed

    def _drop_oldest_droppable_locked(self) -> dict | None:
        for index, queued in enumerate(self._queue):
            if queued["droppable"] and not queued["keyframe"]:
                victim = self._queue.pop(index)
                self._queue_bytes -= victim["bytes"]
                self._queued_seqs.discard(victim["seq"])
                self._stats["dropped_frames"] += 1
                return victim
        return None

    def enqueue(self, frame: dict, *, now_ms: float) -> dict:
        _validate_frame(frame)
        with self._lock:
            self._refill_locked(self._now_ms(now_ms))
            seq = frame["seq"]
            size = frame["bytes"]
            if seq in self._queued_seqs:
                self._stats["duplicates"] += 1
                return {
                    "accepted": False,
                    "reason": "duplicate",
                    "dropped": 0,
                    "queue_bytes": self._queue_bytes,
                }

            dropped = 0
            while self._queue_bytes + size > self.max_queue_bytes:
                victim = self._drop_oldest_droppable_locked()
                if victim is None:
                    self._stats["rejected_frames"] += 1
                    return {
                        "accepted": False,
                        "reason": "queue_full",
                        "dropped": dropped,
                        "queue_bytes": self._queue_bytes,
                    }
                dropped += 1

            self._queue.append(frame)
            self._queued_seqs.add(seq)
            self._queue_bytes += size
            self._stats["enqueued"] += 1
            return {
                "accepted": True,
                "reason": None,
                "dropped": dropped,
                "queue_bytes": self._queue_bytes,
            }

    def tick(self, now_ms: float) -> dict:
        with self._lock:
            self._refill_locked(self._now_ms(now_ms))
            sent_seqs: list[int] = []
            bytes_sent = 0
            while self._queue and self._tokens >= self._queue[0]["bytes"]:
                frame = self._queue.pop(0)
                self._tokens -= frame["bytes"]
                bytes_sent += frame["bytes"]
                sent_seqs.append(frame["seq"])
                self._queued_seqs.discard(frame["seq"])
            self._queue_bytes -= bytes_sent
            self._stats["sent_frames"] += len(sent_seqs)
            self._stats["bytes_sent"] += bytes_sent
            return {
                "sent": sent_seqs,
                "bytes_sent": bytes_sent,
                "tokens": self._tokens,
                "target_bps": self._target_bps,
                "mode": self._mode,
            }

    def on_cycle(self, no_congestion: bool) -> dict:
        with self._lock:
            if not no_congestion:
                target_bps, changed = self._apply_downshift_locked()
                self._stats["cycles_down"] += 1
                return {
                    "changed": changed,
                    "target_bps": target_bps,
                    "mode": self._mode,
                }

            if self._mode == "probe":
                self._probe_cycles += 1
                if self._probe_cycles >= 2:
                    self._mode = "steady"
                    self._probe_cycles = 0
                    self._clean_cycles = 0
                return {
                    "changed": False,
                    "target_bps": self._target_bps,
                    "mode": self._mode,
                }

            self._clean_cycles += 1
            if self._clean_cycles >= 2:
                new_bps = self._clamp_bps(self._target_bps * 1.05)
                changed = new_bps != self._target_bps
                self._target_bps = new_bps
                self._tokens = min(self._tokens, self._capacity_locked())
                self._clean_cycles = 0
                self._stats["cycles_up"] += 1
                return {
                    "changed": changed,
                    "target_bps": new_bps,
                    "mode": self._mode,
                }
            return {
                "changed": False,
                "target_bps": self._target_bps,
                "mode": self._mode,
            }

    def report_loss(self, loss_ratio: float, *, now_ms: float | None = None) -> dict:
        if (not _is_number(loss_ratio) or math.isnan(loss_ratio)
                or loss_ratio < 0 or loss_ratio > 1):
            raise PacerError("PACER_INVALID_LOSS", "loss_ratio must be within [0, 1]")
        with self._lock:
            if now_ms is not None:
                self._refill_locked(self._now_ms(now_ms))
            if loss_ratio > 0.10:
                target_bps, _ = self._apply_downshift_locked()
                self._stats["loss_reports"] += 1
                return {
                    "adjusted": True,
                    "target_bps": target_bps,
                    "mode": self._mode,
                }
            return {
                "adjusted": False,
                "target_bps": self._target_bps,
                "mode": self._mode,
            }

    def target_bps(self) -> float:
        with self._lock:
            return self._target_bps

    def tokens_bytes(self) -> float:
        with self._lock:
            return self._tokens

    def mode(self) -> str:
        with self._lock:
            return self._mode

    def queue_bytes(self) -> int:
        with self._lock:
            return self._queue_bytes

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "target_bps": self._target_bps,
                "tokens": self._tokens,
                "mode": self._mode,
                "queue": copy.deepcopy(self._queue),
            }

    def restore(self, state: dict) -> None:
        restored = self._validate_snapshot(state)
        with self._lock:
            self._target_bps = restored["target_bps"]
            self._tokens = min(restored["tokens"], self._capacity_locked())
            self._mode = restored["mode"]
            self._queue = restored["queue"]
            self._queued_seqs = {frame["seq"] for frame in self._queue}
            self._queue_bytes = sum(frame["bytes"] for frame in self._queue)
            self._clean_cycles = 0
            self._probe_cycles = 0
            self._last_refill_ms = None

    def _validate_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise PacerError("PACER_SNAPSHOT_INVALID", "state must be a dict")
        for key in ("target_bps", "tokens", "mode", "queue"):
            if key not in state:
                raise PacerError("PACER_SNAPSHOT_INVALID", f"missing key: {key}")
        target_bps = state["target_bps"]
        tokens = state["tokens"]
        mode = state["mode"]
        queue = state["queue"]
        if (not _is_number(target_bps) or not math.isfinite(target_bps)
                or target_bps < 0):
            raise PacerError("PACER_SNAPSHOT_INVALID", "target_bps must be a number")
        if not _is_number(tokens) or not math.isfinite(tokens) or tokens < 0:
            raise PacerError("PACER_SNAPSHOT_INVALID", "tokens must be a number")
        if mode not in MODES:
            raise PacerError("PACER_SNAPSHOT_INVALID", "unknown mode")
        if not isinstance(queue, list):
            raise PacerError("PACER_SNAPSHOT_INVALID", "queue must be a list")

        restored_queue: list[dict] = []
        seen_seqs: set[int] = set()
        total_bytes = 0
        for frame in queue:
            try:
                _validate_frame(frame)
            except PacerError as exc:
                raise PacerError("PACER_SNAPSHOT_INVALID", exc.detail) from exc
            if frame["seq"] in seen_seqs:
                raise PacerError("PACER_SNAPSHOT_INVALID", "duplicate seq in queue")
            seen_seqs.add(frame["seq"])
            total_bytes += frame["bytes"]
            restored_queue.append(copy.deepcopy(frame))
        if total_bytes > self.max_queue_bytes:
            raise PacerError("PACER_SNAPSHOT_INVALID", "queue exceeds capacity")
        return {
            "target_bps": self._clamp_bps(target_bps),
            "tokens": tokens,
            "mode": mode,
            "queue": restored_queue,
        }

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0


def time_monotonic_ms() -> float:
    return time.monotonic() * 1000.0
