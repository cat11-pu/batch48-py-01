"""Adaptive playout delay and sequence handling for an RTP video jitter buffer."""

from __future__ import annotations

import copy
import math
import threading
import time
from collections import deque

STAT_KEYS = (
    "accepted",
    "duplicates",
    "late",
    "rejected",
    "played",
    "fast_forward",
)


class PlayoutError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class RtpPlayoutGuard:
    """Track arrival jitter, the adaptive delay target and the playout clock."""

    def __init__(self, *, min_delay_ms: float = 40.0, max_delay_ms: float = 400.0,
                 jitter_factor: float = 3.0, step_ms: float = 20.0,
                 frame_ms: float = 20.0, capacity: int = 256,
                 jitter_window: int = 10, drop_after_ms: float = 2000.0,
                 clock=None) -> None:
        self.min_delay_ms = min_delay_ms
        self.max_delay_ms = max_delay_ms
        self.jitter_factor = jitter_factor
        self.step_ms = step_ms
        self.frame_ms = frame_ms
        self.capacity = capacity
        self.jitter_window = jitter_window
        self.drop_after_ms = drop_after_ms
        self._clock = clock
        self._lock = threading.RLock()

        self._target_ms = float(min_delay_ms)
        self._playout_ms: float | None = None
        self._last_tick_ms: float | None = None

        self._queue: list[dict] = []
        self._last_seq: int | None = None
        self._seen_seqs: set[int] = set()

        self._played_seqs: set[int] = set()
        self._played_until_ms: float | None = None

        self._jitter_samples: deque[float] = deque(
            maxlen=max(0, int(jitter_window))
        )
        self._stats = {key: 0 for key in STAT_KEYS}

    @staticmethod
    def _is_number(value: object) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
        )

    @staticmethod
    def _is_seq(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _unwrap_seq(self, seq: int) -> int:
        if self._last_seq is None:
            return seq
        epoch = self._last_seq - (self._last_seq % 65536)
        candidate = epoch + seq
        delta = candidate - self._last_seq
        if delta > 32768:
            candidate -= 65536
        elif delta < -32768:
            candidate += 65536
        return candidate

    def _current_jitter(self) -> float:
        if len(self._jitter_samples) < 2:
            return 0.0
        return max(self._jitter_samples) - min(self._jitter_samples)

    def _update_target(self, queue_empty: bool) -> None:
        desired = self._current_jitter() * self.jitter_factor
        desired = max(self.min_delay_ms, min(self.max_delay_ms, desired))
        if desired > self._target_ms:
            self._target_ms = min(desired, self._target_ms + self.step_ms)
        elif desired < self._target_ms and queue_empty:
            self._target_ms = max(desired, self._target_ms - self.step_ms)

    def _speed_for(self, backlog_ms: float) -> float:
        if backlog_ms > self._target_ms + (2.0 * self.frame_ms):
            return 1.05
        if backlog_ms < self.frame_ms:
            return 0.95
        return 1.0

    def _emit_due(self, playout_ms: float, first_tick: bool,
                  advance_ms: float, speed: float) -> tuple[list[int], list[int]]:
        limit = len(self._queue)
        if first_tick and limit:
            limit -= 1

        end = 0
        while (end < limit
               and self._queue[end]["timestamp_ms"] <= playout_ms):
            end += 1
        due = self._queue[:end]
        del self._queue[:end]

        dropped: list[int] = []

        if not first_tick and speed > 1.0 and advance_ms > 0.0:
            if self.frame_ms > 0.0:
                budget = int(advance_ms // self.frame_ms) + 1
            else:
                budget = len(due)
            excess = max(0, len(due) - budget)
            if excess:
                kept = []
                for packet in due:
                    if (excess > 0
                            and not packet["keyframe"]
                            and not packet["ref"]):
                        excess -= 1
                        dropped.append(packet["seq"])
                    else:
                        kept.append(packet)
                due = kept

        played: list[int] = []
        for packet in due:
            seq = packet["seq"]
            if seq in self._played_seqs:
                continue
            played.append(seq)
            self._played_seqs.add(seq)
            timestamp = packet["timestamp_ms"]
            if self._played_until_ms is None or timestamp > self._played_until_ms:
                self._played_until_ms = timestamp
        return played, dropped

    def _invalid_snapshot(self, state: object) -> PlayoutError:
        return PlayoutError(
            "PLAYOUT_SNAPSHOT_INVALID",
            {"state": state},
        )

    def push(self, seq: int, arrival_ms: float, timestamp_ms: float | None = None,
             *, keyframe: bool = False, ref: bool = False) -> dict:
        with self._lock:
            if not self._is_seq(seq) or not (0 <= seq <= 65535):
                self._stats["rejected"] += 1
                raise PlayoutError("PLAYOUT_INVALID_SEQ", seq)

            expanded = self._unwrap_seq(seq)
            if expanded in self._seen_seqs:
                self._stats["duplicates"] += 1
                return {
                    "accepted": False,
                    "reason": "duplicate",
                    "queued": len(self._queue),
                }

            if timestamp_ms is None:
                timestamp = float(seq) * 20.0
            else:
                timestamp = float(timestamp_ms)
            arrival = float(arrival_ms)

            if (self._playout_ms is not None
                    and timestamp <= self._playout_ms):
                self._seen_seqs.add(expanded)
                self._stats["late"] += 1
                return {
                    "accepted": False,
                    "reason": "late",
                    "queued": len(self._queue),
                }

            if len(self._queue) >= self.capacity:
                self._stats["rejected"] += 1
                raise PlayoutError(
                    "PLAYOUT_BUFFER_FULL",
                    {
                        "capacity": self.capacity,
                        "queue_depth": len(self._queue),
                    },
                )

            packet = {
                "seq": expanded,
                "arrival_ms": arrival,
                "timestamp_ms": timestamp,
                "keyframe": bool(keyframe),
                "ref": bool(ref),
            }
            self._queue.append(packet)
            self._queue.sort(
                key=lambda item: (item["timestamp_ms"], item["seq"])
            )

            if self._last_seq is None or expanded > self._last_seq:
                self._last_seq = expanded
            self._seen_seqs.add(expanded)
            self._jitter_samples.append(arrival - timestamp)
            self._stats["accepted"] += 1
            return {
                "accepted": True,
                "reason": None,
                "queued": len(self._queue),
            }

    def tick(self, now_ms: float | None = None) -> dict:
        with self._lock:
            if now_ms is None:
                if self._clock is None:
                    now = time.monotonic() * 1000.0
                else:
                    now = float(self._clock())
            else:
                now = float(now_ms)

            first_tick = self._playout_ms is None
            self._update_target(queue_empty=not self._queue)

            if first_tick:
                if not self._queue:
                    self._last_tick_ms = now
                    return {
                        "played": [],
                        "speed": self._speed_for(0.0),
                        "target_delay_ms": float(self._target_ms),
                        "playout_ms": 0.0,
                        "dropped": [],
                    }
                earliest = self._queue[0]["timestamp_ms"]
                playout = earliest + self._target_ms
                if (self._played_until_ms is not None
                        and playout < self._played_until_ms):
                    playout = self._played_until_ms
                self._playout_ms = playout
                self._last_tick_ms = now
                latest = self._queue[-1]["timestamp_ms"]
                speed = self._speed_for(latest - playout)
                advance = 0.0
            else:
                playout = self._playout_ms
                if self._queue:
                    speed = self._speed_for(
                        self._queue[-1]["timestamp_ms"] - playout
                    )
                else:
                    speed = self._speed_for(0.0)

                if self._last_tick_ms is None:
                    elapsed = 0.0
                else:
                    elapsed = max(0.0, now - self._last_tick_ms)
                advance = elapsed * speed
                self._playout_ms = playout + advance
                if self._last_tick_ms is None or now >= self._last_tick_ms:
                    self._last_tick_ms = now
                playout = self._playout_ms

            played, dropped = self._emit_due(
                playout,
                first_tick=first_tick,
                advance_ms=advance,
                speed=speed,
            )
            self._stats["played"] += len(played)
            if speed > 1.0 and advance > 0.0:
                self._stats["fast_forward"] += 1

            return {
                "played": played,
                "speed": float(speed),
                "target_delay_ms": float(self._target_ms),
                "playout_ms": float(self._playout_ms),
                "dropped": dropped,
            }

    def target_delay_ms(self) -> float:
        with self._lock:
            return float(self._target_ms)

    def jitter_ms(self) -> float:
        with self._lock:
            return float(self._current_jitter())

    def playout_ms(self) -> float:
        with self._lock:
            if self._playout_ms is None:
                return 0.0
            return float(self._playout_ms)

    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    def queued_seqs(self) -> list:
        with self._lock:
            return [packet["seq"] for packet in self._queue]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "target_ms": float(self._target_ms),
                "playout_ms": (
                    None if self._playout_ms is None
                    else float(self._playout_ms)
                ),
                "queue": copy.deepcopy(self._queue),
                "last_seq": self._last_seq,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                if not isinstance(state, dict):
                    raise self._invalid_snapshot(state)
                required = {"target_ms", "playout_ms", "queue", "last_seq"}
                if not required.issubset(state):
                    raise self._invalid_snapshot(state)

                target = state["target_ms"]
                if not self._is_number(target):
                    raise self._invalid_snapshot(state)
                target_value = float(target)

                playout = state["playout_ms"]
                if playout is not None and not self._is_number(playout):
                    raise self._invalid_snapshot(state)
                playout_value = (
                    None if playout is None else float(playout)
                )

                last_seq = state["last_seq"]
                if last_seq is not None and not self._is_seq(last_seq):
                    raise self._invalid_snapshot(state)

                raw_queue = state["queue"]
                if not isinstance(raw_queue, list):
                    raise self._invalid_snapshot(state)
                if len(raw_queue) > self.capacity:
                    raise self._invalid_snapshot(state)

                packets: list[dict] = []
                queue_seqs: set[int] = set()
                for item in raw_queue:
                    if not isinstance(item, dict):
                        raise self._invalid_snapshot(state)
                    packet_seq = item.get("seq")
                    if not self._is_seq(packet_seq):
                        raise self._invalid_snapshot(state)
                    if "timestamp_ms" in item:
                        timestamp = item["timestamp_ms"]
                    elif "timestamp" in item:
                        timestamp = item["timestamp"]
                    elif "ts" in item:
                        timestamp = item["ts"]
                    else:
                        raise self._invalid_snapshot(state)
                    if not self._is_number(timestamp):
                        raise self._invalid_snapshot(state)

                    arrival = item.get("arrival_ms", timestamp)
                    if not self._is_number(arrival):
                        raise self._invalid_snapshot(state)
                    keyframe = item.get("keyframe", False)
                    ref = item.get("ref", False)
                    if not isinstance(keyframe, bool) or not isinstance(ref, bool):
                        raise self._invalid_snapshot(state)
                    if packet_seq in queue_seqs:
                        raise self._invalid_snapshot(state)

                    queue_seqs.add(packet_seq)
                    packets.append({
                        "seq": packet_seq,
                        "arrival_ms": float(arrival),
                        "timestamp_ms": float(timestamp),
                        "keyframe": keyframe,
                        "ref": ref,
                    })
                packets.sort(
                    key=lambda item: (item["timestamp_ms"], item["seq"])
                )

                if playout_value is not None and self._played_until_ms is not None:
                    playout_value = max(playout_value, self._played_until_ms)

                if last_seq is None and queue_seqs:
                    last_seq = max(queue_seqs)
            except PlayoutError:
                raise
            except Exception as exc:
                raise self._invalid_snapshot(state) from exc

            self._target_ms = target_value
            self._playout_ms = playout_value
            self._queue = packets
            self._last_seq = last_seq
            self._last_tick_ms = None
            self._seen_seqs.update(queue_seqs)
            if last_seq is not None:
                self._seen_seqs.add(last_seq)

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0
