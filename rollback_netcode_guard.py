"""Rollback netcode guard with predictive frames and resimulation."""

from __future__ import annotations

import copy
import hashlib
import math
import threading
from typing import Any


class RollbackError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STATS_KEYS = (
    "predicts",
    "rollbacks",
    "rolled_frames",
    "resyncs",
    "divergences",
    "buffered",
)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _canonical(value: object) -> bytes:
    if value is None:
        return b"n"
    if isinstance(value, bool):
        return b"b1" if value else b"b0"
    if isinstance(value, int):
        return b"i" + str(value).encode("ascii")
    if isinstance(value, float):
        if math.isnan(value):
            tag = "nan"
        elif math.isinf(value):
            tag = "inf" if value > 0 else "-inf"
        else:
            tag = value.hex()
        return b"f" + tag.encode("ascii")
    if isinstance(value, str):
        encoded = value.encode("utf-8", "surrogatepass")
        return b"s" + str(len(encoded)).encode("ascii") + b":" + encoded
    if isinstance(value, (bytes, bytearray, memoryview)):
        encoded = bytes(value)
        return b"y" + str(len(encoded)).encode("ascii") + b":" + encoded
    if isinstance(value, tuple):
        parts = [_canonical(item) for item in value]
        return b"t" + b"".join(
            str(len(part)).encode("ascii") + b":" + part for part in parts
        )
    if isinstance(value, list):
        parts = [_canonical(item) for item in value]
        return b"l" + b"".join(
            str(len(part)).encode("ascii") + b":" + part for part in parts
        )
    if isinstance(value, dict):
        parts = sorted(
            (_canonical(key), _canonical(item)) for key, item in value.items()
        )
        return b"d" + b"".join(
            str(len(key)).encode("ascii")
            + b":"
            + key
            + str(len(item)).encode("ascii")
            + b":"
            + item
            for key, item in parts
        )
    if isinstance(value, (set, frozenset)):
        parts = sorted(_canonical(item) for item in value)
        return b"e" + b"".join(
            str(len(part)).encode("ascii") + b":" + part for part in parts
        )

    cls = type(value)
    name = f"{cls.__module__}.{cls.__qualname__}".encode("utf-8", "replace")
    representation = repr(value).encode("utf-8", "replace")
    return b"r" + str(len(name)).encode("ascii") + b":" + name + representation


def _same(left: object, right: object) -> bool:
    try:
        result = left == right
        if isinstance(result, bool):
            return result
        return bool(result)
    except Exception:
        return _canonical(left) == _canonical(right)


class RollbackNetcode:
    """Predicts remote inputs, rolls back on divergence and verifies digests."""

    def __init__(self, *, window: int = 8) -> None:
        if not _is_int(window) or window < 0:
            raise RollbackError("ROLLBACK_WINDOW_INVALID", window)

        self.window = window
        self._lock = threading.RLock()
        self._reset_timeline()
        self._stats = {key: 0 for key in _STATS_KEYS}

    def _reset_timeline(self) -> None:
        self._frame = 0
        self._pos = 0
        self._local_inputs: dict[int, Any] = {}
        self._confirmed_inputs: dict[int, Any] = {}
        self._frame_positions: dict[int, int] = {}
        self._digests: dict[int, int] = {}
        self._buffered_inputs: dict[int, Any] = {}

    @staticmethod
    def _frame_error(frame: object) -> RollbackError:
        return RollbackError("ROLLBACK_FRAME_GAP", frame)

    def _effective_input(self, frame: int) -> Any:
        if frame in self._confirmed_inputs:
            return self._confirmed_inputs[frame]
        return self._local_inputs[frame]

    def _digest_for(self, frame: int, input_value: object, pos: int) -> int:
        payload = b"|".join(
            (
                b"rollback-digest-v1",
                _canonical(frame),
                _canonical(pos),
                _canonical(input_value),
            )
        )
        return int.from_bytes(hashlib.sha256(payload).digest(), "big")

    def _record_frame(self, frame: int, input_value: object, pos: int) -> None:
        self._frame_positions[frame] = pos
        self._digests[frame] = self._digest_for(frame, input_value, pos)

    def _resimulate(self, start: int) -> None:
        if start < 1:
            raise self._frame_error(start)

        base = self._frame_positions.get(start - 1, 0)
        pos = base
        for frame in range(start, self._frame + 1):
            input_value = self._effective_input(frame)
            pos += input_value
            self._record_frame(frame, input_value, pos)

        self._pos = self._frame_positions[self._frame]

    def predict(self, frame: int, input: object) -> dict:
        with self._lock:
            if not _is_int(frame) or frame != self._frame + 1:
                raise self._frame_error(frame)

            self._local_inputs[frame] = copy.deepcopy(input)
            if frame in self._buffered_inputs:
                self._confirmed_inputs[frame] = self._buffered_inputs.pop(frame)

            input_value = self._effective_input(frame)
            self._pos += input_value
            self._record_frame(frame, input_value, self._pos)
            self._frame = frame
            self._stats["predicts"] += 1

            return {
                "frame": frame,
                "pos": self._pos,
                "confirmed": frame in self._confirmed_inputs,
            }

    def _resync(self, frame: int, old_input: object, new_input: object) -> dict:
        delta = new_input - old_input
        for current in range(frame, self._frame + 1):
            self._frame_positions[current] += delta
            input_value = self._effective_input(current)
            self._digests[current] = self._digest_for(
                current, input_value, self._frame_positions[current]
            )

        self._pos = self._frame_positions[self._frame]
        self._stats["resyncs"] += 1
        return {"status": "resync", "frame": frame}

    def on_remote_input(self, frame: int, input: object) -> dict:
        with self._lock:
            if not _is_int(frame) or frame < 1:
                raise self._frame_error(frame)

            incoming = copy.deepcopy(input)
            if frame > self._frame:
                self._buffered_inputs[frame] = incoming
                self._stats["buffered"] += 1
                return {"status": "buffered", "frame": frame}

            old_input = self._effective_input(frame)
            if _same(old_input, incoming):
                self._confirmed_inputs[frame] = incoming
                return {"status": "none", "frame": frame}

            depth = self._frame - frame
            if depth > self.window:
                self._confirmed_inputs[frame] = incoming
                return self._resync(frame, old_input, incoming)

            self._confirmed_inputs[frame] = incoming
            self._resimulate(frame)
            self._stats["rollbacks"] += 1
            self._stats["rolled_frames"] += depth + 1
            return {
                "status": "rolled_back",
                "from": frame,
                "depth": depth,
                "pos": self._pos,
            }

    def state(self) -> dict:
        with self._lock:
            return {"frame": self._frame, "pos": self._pos}

    def current_frame(self) -> int:
        with self._lock:
            return self._frame

    def confirmed_frame(self) -> int:
        with self._lock:
            frame = 0
            while frame + 1 in self._confirmed_inputs:
                frame += 1
            return frame

    def digest(self, frame: int) -> int:
        with self._lock:
            if not _is_int(frame) or frame < 1 or frame > self._frame:
                raise self._frame_error(frame)
            return self._digests[frame]

    def on_server_digest(self, frame: int, digest: int) -> dict:
        with self._lock:
            if not _is_int(frame) or frame < 1 or frame > self._frame:
                raise self._frame_error(frame)

            local = self._digests[frame]
            if local == digest:
                status = "ok"
            else:
                status = "divergent"
                self._stats["divergences"] += 1

            return {
                "status": status,
                "frame": frame,
                "local": local,
                "remote": digest,
            }

    def window_size(self) -> int:
        with self._lock:
            return self.window

    def reset(self) -> None:
        with self._lock:
            self._reset_timeline()

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "window": self.window,
                "frame": self._frame,
                "pos": self._pos,
                "local_inputs": copy.deepcopy(self._local_inputs),
                "confirmed_inputs": copy.deepcopy(self._confirmed_inputs),
                "frame_positions": copy.deepcopy(self._frame_positions),
                "digests": copy.deepcopy(self._digests),
                "buffered_inputs": copy.deepcopy(self._buffered_inputs),
                "stats": copy.deepcopy(self._stats),
            }

    @staticmethod
    def _snapshot_error(detail: object) -> RollbackError:
        return RollbackError("ROLLBACK_SNAPSHOT_INVALID", detail)

    def _parse_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise self._snapshot_error("snapshot must be a dict")
        if state.get("version") != 1:
            raise self._snapshot_error("unsupported version")

        required = (
            "window",
            "frame",
            "pos",
            "local_inputs",
            "confirmed_inputs",
            "frame_positions",
            "digests",
            "buffered_inputs",
            "stats",
        )
        if any(key not in state for key in required):
            raise self._snapshot_error("missing snapshot fields")

        window = state["window"]
        frame = state["frame"]
        pos = state["pos"]
        local_inputs = state["local_inputs"]
        confirmed_inputs = state["confirmed_inputs"]
        frame_positions = state["frame_positions"]
        digests = state["digests"]
        buffered_inputs = state["buffered_inputs"]
        stats = state["stats"]

        if not _is_int(window) or window < 0:
            raise self._snapshot_error("invalid window")
        if not _is_int(frame) or frame < 0:
            raise self._snapshot_error("invalid frame")
        if not _is_int(pos):
            raise self._snapshot_error("invalid position")
        for value, name in (
            (local_inputs, "local inputs"),
            (confirmed_inputs, "confirmed inputs"),
            (frame_positions, "frame positions"),
            (digests, "digests"),
            (buffered_inputs, "buffered inputs"),
        ):
            if not isinstance(value, dict):
                raise self._snapshot_error(f"invalid {name}")
            if any(not _is_int(key) or key < 1 for key in value):
                raise self._snapshot_error(f"invalid {name} keys")

        expected_frames = set(range(1, frame + 1))
        if set(local_inputs) != expected_frames:
            raise self._snapshot_error("local frame buffer is not contiguous")
        if set(frame_positions) != expected_frames:
            raise self._snapshot_error("position buffer is not contiguous")
        if set(digests) != expected_frames:
            raise self._snapshot_error("digest buffer is not contiguous")
        if not set(confirmed_inputs).issubset(expected_frames):
            raise self._snapshot_error("confirmed input is outside the timeline")
        if any(key <= frame for key in buffered_inputs):
            raise self._snapshot_error("buffered input is not in the future")
        if any(not _is_int(value) for value in frame_positions.values()):
            raise self._snapshot_error("invalid position value")
        if any(not _is_int(value) for value in digests.values()):
            raise self._snapshot_error("invalid digest value")

        if not isinstance(stats, dict) or set(stats) != set(_STATS_KEYS):
            raise self._snapshot_error("invalid stats")
        if any(not _is_int(stats[key]) or stats[key] < 0 for key in _STATS_KEYS):
            raise self._snapshot_error("invalid stats value")

        normalized_local = copy.deepcopy(local_inputs)
        normalized_confirmed = copy.deepcopy(confirmed_inputs)
        normalized_positions = dict(frame_positions)
        normalized_digests = dict(digests)
        normalized_buffered = copy.deepcopy(buffered_inputs)
        normalized_stats = dict(stats)

        running_pos = 0
        for current in range(1, frame + 1):
            if current in normalized_confirmed:
                input_value = normalized_confirmed[current]
            else:
                input_value = normalized_local[current]
            running_pos += input_value
            if normalized_positions[current] != running_pos:
                raise self._snapshot_error("position timeline is inconsistent")
            expected_digest = self._digest_for(
                current, input_value, normalized_positions[current]
            )
            if normalized_digests[current] != expected_digest:
                raise self._snapshot_error("digest timeline is inconsistent")

        if pos != running_pos:
            raise self._snapshot_error("position does not match the timeline")

        return {
            "window": window,
            "frame": frame,
            "pos": pos,
            "local_inputs": normalized_local,
            "confirmed_inputs": normalized_confirmed,
            "frame_positions": normalized_positions,
            "digests": normalized_digests,
            "buffered_inputs": normalized_buffered,
            "stats": normalized_stats,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            parsed = self._parse_snapshot(state)
            self.window = parsed["window"]
            self._frame = parsed["frame"]
            self._pos = parsed["pos"]
            self._local_inputs = parsed["local_inputs"]
            self._confirmed_inputs = parsed["confirmed_inputs"]
            self._frame_positions = parsed["frame_positions"]
            self._digests = parsed["digests"]
            self._buffered_inputs = parsed["buffered_inputs"]
            self._stats = parsed["stats"]
