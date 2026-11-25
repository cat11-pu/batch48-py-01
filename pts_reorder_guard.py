"""PTS/DTS reorder buffer guard for decode pipelines."""

from __future__ import annotations

import heapq
import threading
import time


class ReorderError(ValueError):
    """Rejection raised by the reorder guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PtsReorderGuard:
    """Buffer frames by PTS and release them in presentation order."""

    _FRAME_FIELDS = ("pts", "dts", "keyframe", "payload_size")
    _STAT_KEYS = ("pushed", "popped", "released", "late_dropped", "flushed")

    def __init__(self, *, max_reorder_depth: int = 16,
                 gap_threshold_ms: int = 500, clock=None) -> None:
        if (isinstance(max_reorder_depth, bool)
                or not isinstance(max_reorder_depth, int)
                or max_reorder_depth < 1):
            raise ReorderError(
                "REORDER_MAX_DEPTH_INVALID", max_reorder_depth
            )
        self.max_reorder_depth = max_reorder_depth
        self.gap_threshold_ms = gap_threshold_ms
        self._clock = clock
        self._lock = threading.RLock()
        self._frames: dict[int, dict] = {}
        self._pts_heap: list[tuple[int, int]] = []
        self._dts_heap: list[tuple[int, int, int]] = []
        self._dts_stale = 0
        self._seq = 0
        self._last_output_pts: int | None = None
        self._last_output_dts: int | None = None
        self._gap_count = 0
        self._eos = False
        self._stats = {key: 0 for key in self._STAT_KEYS}

    def _now_ms(self) -> int:
        if self._clock is not None:
            return int(self._clock())
        return int(time.monotonic() * 1000)

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _public_frame(cls, frame: dict) -> dict:
        return {key: frame[key] for key in cls._FRAME_FIELDS}

    def _push_result(self, *, accepted: bool, dropped: bool,
                     reason: str | None) -> dict:
        return {
            "accepted": accepted,
            "dropped": dropped,
            "buffered": len(self._frames),
            "released": [],
            "reason": reason,
        }

    def _drop_late(self, reason: str = "late") -> dict:
        self._stats["late_dropped"] += 1
        return self._push_result(
            accepted=False, dropped=True, reason=reason
        )

    def _record_output(self, frame: dict) -> None:
        pts = frame["pts"]
        if (self._last_output_pts is not None
                and pts - self._last_output_pts > self.gap_threshold_ms):
            self._gap_count += 1
        self._last_output_pts = pts
        self._last_output_dts = frame["dts"]

    def _rebuild_dts_heap_locked(self) -> None:
        self._dts_heap = [
            (frame["dts"], pts, frame["_seq"])
            for pts, frame in self._frames.items()
        ]
        heapq.heapify(self._dts_heap)
        self._dts_stale = 0

    def _compact_dts_heap_if_needed_locked(self) -> None:
        if self._dts_stale > self.max_reorder_depth:
            self._rebuild_dts_heap_locked()

    def push(self, pts: int, dts: int | None = None, *, payload_size: int = 0,
             keyframe: bool = False) -> dict:
        with self._lock:
            if self._eos:
                raise ReorderError(
                    "REORDER_AFTER_EOS", "cannot push after flush_eos"
                )
            if not self._is_int(pts) or pts < 0:
                raise ReorderError("REORDER_PTS_INVALID", pts)
            if dts is not None and not self._is_int(dts):
                raise ReorderError("REORDER_DTS_INVALID", dts)
            if pts in self._frames:
                raise ReorderError("REORDER_DUPLICATE_PTS", pts)

            # A frame whose PTS was already presented can never be emitted
            # in presentation order again.
            if (self._last_output_pts is not None
                    and pts <= self._last_output_pts):
                return self._drop_late()

            if len(self._frames) >= self.max_reorder_depth:
                return self._drop_late()

            effective_dts = pts if dts is None else dts
            self._seq += 1
            frame = {
                "pts": pts,
                "dts": effective_dts,
                "keyframe": bool(keyframe),
                "payload_size": payload_size,
                "_seq": self._seq,
            }
            self._frames[pts] = frame
            heapq.heappush(self._pts_heap, (pts, frame["_seq"]))
            heapq.heappush(
                self._dts_heap, (effective_dts, pts, frame["_seq"])
            )
            self._stats["pushed"] += 1
            return self._push_result(
                accepted=True, dropped=False, reason=None
            )

    def pop(self, *, count: int = 1) -> list[dict]:
        with self._lock:
            if not self._is_int(count) or count < 0:
                raise ReorderError("REORDER_COUNT_INVALID", count)

            frames: list[dict] = []
            removed = 0
            while count > 0 and self._pts_heap:
                pts, seq = heapq.heappop(self._pts_heap)
                frame = self._frames.get(pts)
                if frame is None or frame["_seq"] != seq:
                    continue
                del self._frames[pts]
                output = self._public_frame(frame)
                self._record_output(output)
                frames.append(output)
                removed += 1
                count -= 1

            if removed:
                self._dts_stale += removed
                self._compact_dts_heap_if_needed_locked()
                self._stats["popped"] += removed
                self._stats["released"] += removed
            return frames

    def flush_eos(self) -> list[dict]:
        with self._lock:
            frames: list[dict] = []
            while self._dts_heap:
                dts, pts, seq = heapq.heappop(self._dts_heap)
                frame = self._frames.get(pts)
                if frame is None or frame["_seq"] != seq:
                    continue
                del self._frames[pts]
                output = self._public_frame(frame)
                self._record_output(output)
                frames.append(output)

            # Every accepted frame is present in the DTS heap, but keep this
            # fallback so an unexpected heap inconsistency cannot lose the
            # tail of a stream.
            if self._frames:
                remaining = sorted(
                    self._frames.values(),
                    key=lambda frame: (frame["dts"], frame["pts"]),
                )
                self._frames.clear()
                for frame in remaining:
                    output = self._public_frame(frame)
                    self._record_output(output)
                    frames.append(output)

            self._pts_heap.clear()
            self._dts_heap.clear()
            self._dts_stale = 0
            self._eos = True

            if frames:
                self._stats["flushed"] += len(frames)
                self._stats["released"] += len(frames)
            return frames

    def buffered(self) -> int:
        with self._lock:
            return len(self._frames)

    def last_output_pts(self) -> int | None:
        with self._lock:
            return self._last_output_pts

    def last_output_dts(self) -> int | None:
        with self._lock:
            return self._last_output_dts

    def late_dropped(self) -> int:
        with self._lock:
            return self._stats["late_dropped"]

    def gap_count(self) -> int:
        with self._lock:
            return self._gap_count

    def snapshot(self) -> dict:
        with self._lock:
            frames = [
                self._public_frame(self._frames[pts])
                for pts in sorted(self._frames)
            ]
            return {
                "version": 1,
                "frames": frames,
                "last_output_pts": self._last_output_pts,
                "last_output_dts": self._last_output_dts,
                "gap_count": self._gap_count,
                "eos": self._eos,
                "stats": dict(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            parsed = self._parse_snapshot(state)
            (frames, pts_heap, dts_heap, last_pts, last_dts, gap_count,
             eos, stats, seq) = parsed
            self._frames = frames
            self._pts_heap = pts_heap
            self._dts_heap = dts_heap
            self._dts_stale = 0
            self._seq = seq
            self._last_output_pts = last_pts
            self._last_output_dts = last_dts
            self._gap_count = gap_count
            self._eos = eos
            self._stats = stats

    @classmethod
    def _snapshot_error(cls, detail: object) -> ReorderError:
        return ReorderError("REORDER_SNAPSHOT_INVALID", detail)

    def _parse_snapshot(self, state: object) -> tuple:
        if not isinstance(state, dict):
            raise self._snapshot_error("state must be a mapping")
        if "version" in state:
            version = state["version"]
            if not self._is_int(version) or version != 1:
                raise self._snapshot_error("unsupported version")

        raw_frames = state.get("frames")
        if not isinstance(raw_frames, list):
            raise self._snapshot_error("frames must be a list")

        frames: dict[int, dict] = {}
        seq = 0
        for index, item in enumerate(raw_frames):
            if not isinstance(item, dict):
                raise self._snapshot_error({"frame": index})
            missing = [
                field for field in self._FRAME_FIELDS
                if field not in item
            ]
            if missing:
                raise self._snapshot_error(
                    {"frame": index, "missing": missing}
                )

            pts = item["pts"]
            if not self._is_int(pts) or pts < 0:
                raise self._snapshot_error({"frame": index, "pts": pts})
            if pts in frames:
                raise self._snapshot_error(
                    {"frame": index, "duplicate_pts": pts}
                )

            dts = item["dts"]
            if dts is not None and not self._is_int(dts):
                raise self._snapshot_error({"frame": index, "dts": dts})

            keyframe = item["keyframe"]
            if not isinstance(keyframe, bool):
                raise self._snapshot_error(
                    {"frame": index, "keyframe": keyframe}
                )

            payload_size = item["payload_size"]
            if (not self._is_int(payload_size)
                    or payload_size < 0):
                raise self._snapshot_error(
                    {"frame": index, "payload_size": payload_size}
                )

            seq += 1
            frames[pts] = {
                "pts": pts,
                "dts": pts if dts is None else dts,
                "keyframe": keyframe,
                "payload_size": payload_size,
                "_seq": seq,
            }

        last_pts = state.get("last_output_pts")
        if last_pts is not None and (
                not self._is_int(last_pts) or last_pts < 0):
            raise self._snapshot_error({"last_output_pts": last_pts})

        last_dts = state.get("last_output_dts")
        if last_dts is not None and not self._is_int(last_dts):
            raise self._snapshot_error({"last_output_dts": last_dts})

        gap_count = state.get("gap_count", 0)
        if not self._is_int(gap_count) or gap_count < 0:
            raise self._snapshot_error({"gap_count": gap_count})

        eos = state.get("eos", False)
        if not isinstance(eos, bool):
            raise self._snapshot_error({"eos": eos})

        raw_stats = state.get("stats", {})
        if not isinstance(raw_stats, dict):
            raise self._snapshot_error("stats must be a mapping")
        stats = {key: 0 for key in self._STAT_KEYS}
        for key in self._STAT_KEYS:
            value = raw_stats.get(key, 0)
            if not self._is_int(value) or value < 0:
                raise self._snapshot_error({"stats": key})
            stats[key] = value

        pts_heap = [
            (frame["pts"], frame["_seq"]) for frame in frames.values()
        ]
        heapq.heapify(pts_heap)
        dts_heap = [
            (frame["dts"], frame["pts"], frame["_seq"])
            for frame in frames.values()
        ]
        heapq.heapify(dts_heap)

        return (frames, pts_heap, dts_heap, last_pts, last_dts,
                gap_count, eos, stats, seq)

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in self._STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._STAT_KEYS:
                self._stats[key] = 0
            self._gap_count = 0
