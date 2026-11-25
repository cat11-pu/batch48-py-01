"""Fragmented MP4 moof/mdat bookkeeping for a muxing pipeline."""

from __future__ import annotations

import copy
import threading

STAT_KEYS = (
    "samples_added",
    "fragments_flushed",
    "samples_flushed",
    "bytes_flushed",
    "rejected",
    "flushes_requested",
)

SYNC_SAMPLE_FLAGS = 0x02000000
DELTA_SAMPLE_FLAGS = 0x01010000

SNAPSHOT_VERSION = 1

# Deterministic synthetic box sizes used for moof/mdat offset bookkeeping.
_MOOF_HEADER_SIZE = 8
_MFHD_SIZE = 16
_TRAF_HEADER_SIZE = 8
_TFHD_SIZE = 16
_TFDT_SIZE = 20
_TRUN_BASE_SIZE = 20
_TRUN_SAMPLE_SIZE = 12
_MDAT_HEADER_SIZE = 8


class Fmp4Error(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class Fmp4MuxGuard:
    """Queue samples and emit fragmented MP4 fragments with stable offsets."""

    def __init__(self, *, stream_id: str = "default",
                 max_fragment_bytes: int = 524288,
                 timescale: int = 1000) -> None:
        if not _is_int(timescale) or timescale <= 0:
            raise Fmp4Error("FMP4_TIMESCALE_INVALID",
                            {"timescale": timescale})
        self.stream_id = stream_id
        self.max_fragment_bytes = max_fragment_bytes
        self.timescale = timescale
        self._lock = threading.RLock()
        self._tracks: dict[str, dict] = {}
        self._queue: list[dict] = []
        self._fragments: list[dict] = []
        self._sequence = 0
        self._sample_index = 0
        self._tfdt: dict[str, int] = {}
        self._last_dts: dict[str, int | None] = {}
        self._stats = {key: 0 for key in STAT_KEYS}

    def _reject(self, code: str, detail: object = None) -> None:
        self._stats["rejected"] += 1
        raise Fmp4Error(code, detail)

    def set_track(self, track: str, *, timescale: int | None = None) -> dict:
        with self._lock:
            if not isinstance(track, str) or not track:
                self._reject("FMP4_INVALID_SAMPLE", {"track": track})
            if timescale is None:
                timescale = self.timescale
            if not _is_int(timescale) or timescale <= 0:
                self._reject("FMP4_TIMESCALE_INVALID",
                             {"track": track, "timescale": timescale})
            if track not in self._tracks:
                self._tfdt[track] = 0
                self._last_dts[track] = None
            self._tracks[track] = {"track": track, "timescale": timescale}
            return dict(self._tracks[track])

    def tracks(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._tracks)

    def add_sample(self, track: str, *, dts: int, duration: int, size: int,
                   keyframe: bool = False) -> dict:
        with self._lock:
            if not isinstance(track, str) or not track:
                self._reject("FMP4_INVALID_SAMPLE", {"track": track})
            if track not in self._tracks:
                self._reject("FMP4_UNKNOWN_TRACK", {"track": track})
            if not _is_int(dts) or not _is_int(duration) or not _is_int(size):
                self._reject("FMP4_INVALID_SAMPLE",
                             {"dts": dts, "duration": duration, "size": size})
            if dts < 0 or duration <= 0 or size <= 0:
                self._reject("FMP4_INVALID_SAMPLE",
                             {"dts": dts, "duration": duration, "size": size})
            if size > self.max_fragment_bytes:
                self._reject("FMP4_INVALID_SAMPLE",
                             {"size": size,
                              "max_fragment_bytes": self.max_fragment_bytes})
            last_dts = self._last_dts.get(track)
            if last_dts is not None and dts < last_dts:
                self._reject("FMP4_INVALID_SAMPLE",
                             {"track": track, "dts": dts,
                              "last_dts": last_dts})
            record = {
                "track": track,
                "dts": dts,
                "duration": duration,
                "size": size,
                "keyframe": bool(keyframe),
                "index": self._sample_index,
            }
            self._sample_index += 1
            self._last_dts[track] = dts
            self._queue.append(record)
            self._stats["samples_added"] += 1
            return dict(record)

    def pending_bytes(self) -> int:
        with self._lock:
            return sum(sample["size"] for sample in self._queue)

    def should_flush(self) -> bool:
        with self._lock:
            return (
                bool(self._queue)
                and self.pending_bytes() >= self.max_fragment_bytes
                and self._queue[-1]["keyframe"]
            )

    def _cut_index(self) -> int:
        """First queue index that starts the next fragment.

        Cuts only happen at keyframes once the accumulated payload would
        exceed max_fragment_bytes; P frames are never stranded at the
        start of a fragment.
        """
        total = 0
        over = False
        for pos, sample in enumerate(self._queue):
            if pos > 0 and (over or total + sample["size"]
                            > self.max_fragment_bytes):
                if sample["keyframe"]:
                    return pos
                over = True
            total += sample["size"]
        return len(self._queue)

    def flush(self) -> dict:
        with self._lock:
            self._stats["flushes_requested"] += 1
            if not self._queue:
                self._reject("FMP4_EMPTY_FRAGMENT",
                             {"stream_id": self.stream_id})
            cut = self._cut_index()
            taken = self._queue[:cut]
            runs: list[list[dict]] = []
            for sample in taken:
                if runs and runs[-1][0]["track"] == sample["track"]:
                    runs[-1].append(sample)
                else:
                    runs.append([sample])
            moof_size = _MOOF_HEADER_SIZE + _MFHD_SIZE + sum(
                _TRAF_HEADER_SIZE + _TFHD_SIZE + _TFDT_SIZE
                + _TRUN_BASE_SIZE + _TRUN_SAMPLE_SIZE * len(run)
                for run in runs
            )
            mdat_size = sum(s["size"] for s in taken) + _MDAT_HEADER_SIZE
            tfdt_map: dict[str, int] = {}
            samples_out: list[dict] = []
            offset = moof_size + _MDAT_HEADER_SIZE
            for run_index, run in enumerate(runs):
                for sample in run:
                    track = sample["track"]
                    if track not in tfdt_map:
                        tfdt_map[track] = self._tfdt[track]
                    samples_out.append({
                        "track": track,
                        "index": sample["index"],
                        "dts": sample["dts"],
                        "duration": sample["duration"],
                        "size": sample["size"],
                        "data_offset": offset,
                        "sample_flags": (SYNC_SAMPLE_FLAGS
                                         if sample["keyframe"]
                                         else DELTA_SAMPLE_FLAGS),
                        "keyframe": sample["keyframe"],
                        "run_index": run_index,
                    })
                    offset += sample["size"]
            self._sequence += 1
            fragment = {
                "seq": self._sequence,
                "stream_id": self.stream_id,
                "moof_size": moof_size,
                "mdat_size": mdat_size,
                "byte_length": moof_size + mdat_size,
                "tfdt": tfdt_map,
                "samples": samples_out,
                "sample_count": len(samples_out),
            }
            for sample in taken:
                track = sample["track"]
                track_timescale = self._tracks[track]["timescale"]
                self._tfdt[track] += (sample["duration"] * track_timescale
                                      // self.timescale)
            del self._queue[:cut]
            self._fragments.append(fragment)
            self._stats["fragments_flushed"] += 1
            self._stats["samples_flushed"] += len(taken)
            self._stats["bytes_flushed"] += fragment["byte_length"]
            return copy.deepcopy(fragment)

    def fragments(self) -> list:
        with self._lock:
            return copy.deepcopy(self._fragments)

    def sequence(self) -> int:
        with self._lock:
            return self._sequence

    def sample_offsets(self, fragment_seq: int) -> list:
        with self._lock:
            for fragment in self._fragments:
                if fragment["seq"] == fragment_seq:
                    return copy.deepcopy(fragment["samples"])
            self._reject("FMP4_FRAGMENT_NOT_FOUND",
                         {"seq": fragment_seq})

    def tfdt_for(self, track: str) -> int:
        with self._lock:
            if track not in self._tracks:
                self._reject("FMP4_UNKNOWN_TRACK", {"track": track})
            return self._tfdt[track]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "stream_id": self.stream_id,
                "sequence": self._sequence,
                "sample_index": self._sample_index,
                "tracks": copy.deepcopy(self._tracks),
                "tfdt": copy.deepcopy(self._tfdt),
                "last_dts": copy.deepcopy(self._last_dts),
                "queue": copy.deepcopy(self._queue),
                "fragments": copy.deepcopy(self._fragments),
            }

    def _parse_snapshot(self, state: object) -> tuple:
        def invalid(detail: object) -> None:
            self._reject("FMP4_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            invalid({"reason": "state is not a mapping"})
        if state.get("version") != SNAPSHOT_VERSION:
            invalid({"reason": "version", "got": state.get("version")})
        sequence = state.get("sequence")
        if not _is_int(sequence) or sequence < 0:
            invalid({"reason": "sequence", "got": sequence})
        sample_index = state.get("sample_index")
        if not _is_int(sample_index) or sample_index < 0:
            invalid({"reason": "sample_index", "got": sample_index})
        tracks = state.get("tracks")
        if not isinstance(tracks, dict):
            invalid({"reason": "tracks"})
        for name, info in tracks.items():
            if not isinstance(name, str) or not name:
                invalid({"reason": "track name", "got": name})
            if not isinstance(info, dict):
                invalid({"reason": "track info", "track": name})
            timescale = info.get("timescale")
            if not _is_int(timescale) or timescale <= 0:
                invalid({"reason": "track timescale", "track": name})
            if info.get("track", name) != name:
                invalid({"reason": "track mismatch", "track": name})
        tfdt = state.get("tfdt")
        if not isinstance(tfdt, dict):
            invalid({"reason": "tfdt"})
        for name, value in tfdt.items():
            if name not in tracks or not _is_int(value) or value < 0:
                invalid({"reason": "tfdt entry", "track": name})
        last_dts = state.get("last_dts")
        if not isinstance(last_dts, dict):
            invalid({"reason": "last_dts"})
        for name, value in last_dts.items():
            if name not in tracks:
                invalid({"reason": "last_dts track", "track": name})
            if value is not None and (not _is_int(value) or value < 0):
                invalid({"reason": "last_dts value", "track": name})
        queue = state.get("queue")
        if not isinstance(queue, list):
            invalid({"reason": "queue"})
        for sample in queue:
            if not isinstance(sample, dict):
                invalid({"reason": "queue sample"})
            track = sample.get("track")
            if track not in tracks:
                invalid({"reason": "queue sample track", "track": track})
            dts = sample.get("dts")
            duration = sample.get("duration")
            size = sample.get("size")
            index = sample.get("index")
            if (not _is_int(dts) or dts < 0
                    or not _is_int(duration) or duration <= 0
                    or not _is_int(size) or size <= 0
                    or not _is_int(index) or index < 0):
                invalid({"reason": "queue sample fields",
                         "sample": sample})
        fragments = state.get("fragments")
        if not isinstance(fragments, list):
            invalid({"reason": "fragments"})
        for fragment in fragments:
            if not isinstance(fragment, dict):
                invalid({"reason": "fragment"})
            seq = fragment.get("seq")
            if not _is_int(seq) or seq < 1:
                invalid({"reason": "fragment seq", "got": seq})
            if not isinstance(fragment.get("samples"), list):
                invalid({"reason": "fragment samples", "seq": seq})
        return (
            copy.deepcopy(tracks),
            {name: int(value) for name, value in tfdt.items()},
            copy.deepcopy(last_dts),
            copy.deepcopy(queue),
            copy.deepcopy(fragments),
            sequence,
            sample_index,
        )

    def restore(self, state: dict) -> None:
        with self._lock:
            (tracks, tfdt, last_dts, queue, fragments,
             sequence, sample_index) = self._parse_snapshot(state)
            self._tracks = tracks
            for name in tracks:
                tfdt.setdefault(name, 0)
                last_dts.setdefault(name, None)
            self._tfdt = tfdt
            self._last_dts = last_dts
            self._queue = queue
            self._fragments = fragments
            self._sequence = sequence
            self._sample_index = sample_index

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0
