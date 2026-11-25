"""Multi-track timeline placement and render plan composition."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping


class TimelineError(ValueError):
    """Rejection raised by the timeline compositor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STAT_KEYS = (
    "tracks_added",
    "tracks_removed",
    "clips_added",
    "clips_trimmed",
    "clips_moved",
    "clips_removed",
    "transition_clamps",
)

_CLIP_FIELDS = (
    "clip_id",
    "track",
    "start_frame",
    "duration_frames",
    "end_frame",
    "source_in_frame",
    "transition_in_frames",
    "transition_out_frames",
)

_AUTO_CLIP_ID = re.compile(r"clip-(\d+)\Z")


def _fail(code: str, detail: object = None) -> TimelineError:
    return TimelineError(code, detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class TimelineCompositor:
    """Place clips on ordered tracks and build frame-accurate render plans."""

    def __init__(self, *, frame_rate: int = 25) -> None:
        if not _is_int(frame_rate) or frame_rate <= 0:
            raise _fail("TL_FRAME_RATE_INVALID", frame_rate)
        self.frame_rate = frame_rate
        self._lock = threading.RLock()
        self._tracks: dict[str, int] = {}
        self._clips: dict[str, dict] = {}
        self._version = 0
        self._next_clip_number = 1
        self._stats = {name: 0 for name in _STAT_KEYS}

    def add_track(self, name: str, *, order: int) -> dict:
        with self._lock:
            if not isinstance(name, str):
                raise _fail("TL_TRACK_INVALID", name)
            if not _is_int(order):
                raise _fail("TL_TRACK_INVALID", order)
            if name in self._tracks:
                raise _fail("TL_TRACK_EXISTS", name)
            if order in self._tracks.values():
                raise _fail("TL_TRACK_ORDER_CONFLICT", order)

            self._tracks[name] = order
            self._version += 1
            self._stats["tracks_added"] += 1
            return {"track": name, "order": order}

    def remove_track(self, name: str) -> None:
        with self._lock:
            if name not in self._tracks:
                raise _fail("TL_TRACK_UNKNOWN", name)

            del self._tracks[name]
            for clip_id in [
                clip_id
                for clip_id, clip in self._clips.items()
                if clip["track"] == name
            ]:
                del self._clips[clip_id]
            self._version += 1
            self._stats["tracks_removed"] += 1

    def tracks(self) -> list[dict]:
        with self._lock:
            return [
                {"track": name, "order": order}
                for name, order in sorted(
                    self._tracks.items(), key=lambda item: item[1]
                )
            ]

    def add_clip(self, track: str, *, start_frame: int, duration_frames: int,
                 source_in_frame: int = 0, transition_in_frames: int = 0,
                 transition_out_frames: int = 0,
                 clip_id: str | None = None) -> dict:
        with self._lock:
            if track not in self._tracks:
                raise _fail("TL_TRACK_UNKNOWN", track)
            if (
                not _is_int(start_frame)
                or start_frame < 0
                or not _is_int(duration_frames)
                or duration_frames <= 0
                or not _is_int(source_in_frame)
                or source_in_frame < 0
                or not _is_int(transition_in_frames)
                or transition_in_frames < 0
                or not _is_int(transition_out_frames)
                or transition_out_frames < 0
            ):
                raise _fail("TL_CLIP_INVALID", "frame values")
            if transition_in_frames + transition_out_frames > duration_frames:
                raise _fail("TL_TRANSITION_OVERFLOW", {
                    "transition_in_frames": transition_in_frames,
                    "transition_out_frames": transition_out_frames,
                    "duration_frames": duration_frames,
                })

            end_frame = start_frame + duration_frames
            self._check_overlap_locked(track, start_frame, end_frame)

            if clip_id is None:
                clip_id = self._allocate_clip_id_locked()
            else:
                if not isinstance(clip_id, str) or not clip_id:
                    raise _fail("TL_CLIP_INVALID", "clip_id")
                if clip_id in self._clips:
                    raise _fail("TL_CLIP_EXISTS", clip_id)

            clip = {
                "clip_id": clip_id,
                "track": track,
                "start_frame": start_frame,
                "duration_frames": duration_frames,
                "end_frame": end_frame,
                "source_in_frame": source_in_frame,
                "transition_in_frames": transition_in_frames,
                "transition_out_frames": transition_out_frames,
            }
            self._clips[clip_id] = clip
            self._version += 1
            self._stats["clips_added"] += 1
            return dict(clip)

    def trim_clip(self, clip_id: str, *, duration_frames: int) -> dict:
        with self._lock:
            clip = self._clip_locked(clip_id)
            if (
                not _is_int(duration_frames)
                or duration_frames <= 0
                or duration_frames >= clip["duration_frames"]
            ):
                raise _fail("TL_TRIM_INVALID", duration_frames)

            old_in = clip["transition_in_frames"]
            old_out = clip["transition_out_frames"]
            new_in, new_out = self._clamp_transitions(
                old_in, old_out, duration_frames
            )
            if (new_in, new_out) != (old_in, old_out):
                self._stats["transition_clamps"] += 1

            clip["duration_frames"] = duration_frames
            clip["end_frame"] = clip["start_frame"] + duration_frames
            clip["transition_in_frames"] = new_in
            clip["transition_out_frames"] = new_out
            self._version += 1
            self._stats["clips_trimmed"] += 1
            return dict(clip)

    def move_clip(self, clip_id: str, *, start_frame: int) -> dict:
        with self._lock:
            clip = self._clip_locked(clip_id)
            if not _is_int(start_frame) or start_frame < 0:
                raise _fail("TL_CLIP_INVALID", "start_frame")

            end_frame = start_frame + clip["duration_frames"]
            self._check_overlap_locked(
                clip["track"], start_frame, end_frame, exclude=clip_id
            )
            clip["start_frame"] = start_frame
            clip["end_frame"] = end_frame
            self._version += 1
            self._stats["clips_moved"] += 1
            return dict(clip)

    def remove_clip(self, clip_id: str) -> None:
        with self._lock:
            self._clip_locked(clip_id)
            del self._clips[clip_id]
            self._version += 1
            self._stats["clips_removed"] += 1

    def effective_transition(self, clip_id: str, side: str) -> int:
        with self._lock:
            clip = self._clip_locked(clip_id)
            in_frames, out_frames = self._clamp_transitions(
                clip["transition_in_frames"],
                clip["transition_out_frames"],
                clip["duration_frames"],
            )
            if side == "in":
                return in_frames
            if side == "out":
                return out_frames
            raise _fail("TL_TRANSITION_INVALID", side)

    def render_plan(self, *, start_frame: int, end_frame: int) -> dict:
        with self._lock:
            if (
                not _is_int(start_frame)
                or not _is_int(end_frame)
                or start_frame < 0
                or end_frame <= start_frame
            ):
                raise _fail("TL_RANGE_INVALID", {
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                })

            track_plans = []
            has_clip = False
            for track, order in sorted(
                self._tracks.items(), key=lambda item: item[1]
            ):
                clip_list = sorted(
                    (
                        clip
                        for clip in self._clips.values()
                        if clip["track"] == track
                    ),
                    key=lambda clip: (clip["start_frame"], clip["clip_id"]),
                )
                segments, track_has_clip = self._track_segments(
                    clip_list, start_frame, end_frame
                )
                has_clip = has_clip or track_has_clip
                track_plans.append({
                    "track": track,
                    "order": order,
                    "segments": segments,
                })

            return {
                "version": self._version,
                "frame_rate": self.frame_rate,
                "range": {
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "frame_count": end_frame - start_frame,
                },
                "tracks": track_plans,
                "empty": not has_clip,
            }

    def version(self) -> int:
        with self._lock:
            return self._version

    def clips(self, track: str | None = None) -> list[dict]:
        with self._lock:
            if track is not None and track not in self._tracks:
                raise _fail("TL_TRACK_UNKNOWN", track)
            selected = [
                clip
                for clip in self._clips.values()
                if track is None or clip["track"] == track
            ]
            selected.sort(
                key=lambda clip: (clip["start_frame"], clip["clip_id"])
            )
            return [dict(clip) for clip in selected]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "tracks": self.tracks(),
                "clips": self.clips(),
                "version": self._version,
                "stats": dict(self._stats),
                "frame_rate": self.frame_rate,
                "next_clip_number": self._next_clip_number,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                (new_tracks, new_clips, new_version, new_stats,
                 new_frame_rate, new_next_clip_number) = (
                    self._validated_state(state)
                )
            except TimelineError:
                raise
            except Exception as exc:
                raise _fail("TL_SNAPSHOT_INVALID") from exc

            self._tracks = new_tracks
            self._clips = new_clips
            self._version = new_version
            self._stats = new_stats
            self.frame_rate = new_frame_rate
            self._next_clip_number = new_next_clip_number

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {name: 0 for name in _STAT_KEYS}

    def _clip_locked(self, clip_id: str) -> dict:
        clip = self._clips.get(clip_id)
        if clip is None:
            raise _fail("TL_CLIP_UNKNOWN", clip_id)
        return clip

    def _allocate_clip_id_locked(self) -> str:
        number = self._next_clip_number
        while f"clip-{number}" in self._clips:
            number += 1
        self._next_clip_number = number + 1
        return f"clip-{number}"

    def _check_overlap_locked(self, track: str, start_frame: int,
                              end_frame: int, *, exclude: str | None = None
                              ) -> None:
        for clip in self._clips.values():
            if clip["track"] != track or clip["clip_id"] == exclude:
                continue
            if start_frame < clip["end_frame"] and clip["start_frame"] < end_frame:
                raise _fail("TL_CLIP_OVERLAP", {
                    "track": track,
                    "start_frame": start_frame,
                    "end_frame": end_frame,
                    "clip_id": clip["clip_id"],
                })

    @staticmethod
    def _clamp_transitions(transition_in_frames: int,
                           transition_out_frames: int,
                           duration_frames: int) -> tuple[int, int]:
        in_frames = min(transition_in_frames, duration_frames)
        out_frames = min(transition_out_frames, duration_frames - in_frames)
        return in_frames, out_frames

    @staticmethod
    def _track_segments(clip_list: list[dict], start_frame: int,
                        end_frame: int) -> tuple[list[dict], bool]:
        segments: list[dict] = []
        cursor = start_frame
        has_clip = False

        for clip in clip_list:
            clip_start = clip["start_frame"]
            clip_end = clip["end_frame"]
            if clip_end <= start_frame or clip_start >= end_frame:
                continue

            segment_start = max(clip_start, start_frame)
            segment_end = min(clip_end, end_frame)
            if segment_start > cursor:
                segments.append(TimelineCompositor._black_segment(
                    cursor, segment_start
                ))

            source_in_frame = (
                clip["source_in_frame"] + segment_start - clip_start
            )
            segments.append({
                "kind": "clip",
                "start_frame": segment_start,
                "end_frame": segment_end,
                "clip_id": clip["clip_id"],
                "source_in_frame": source_in_frame,
            })
            cursor = segment_end
            has_clip = True

        if cursor < end_frame:
            segments.append(TimelineCompositor._black_segment(
                cursor, end_frame
            ))
        return segments, has_clip

    @staticmethod
    def _black_segment(start_frame: int, end_frame: int) -> dict:
        return {
            "kind": "black",
            "start_frame": start_frame,
            "end_frame": end_frame,
            "clip_id": None,
            "source_in_frame": None,
        }

    def _validated_state(self, state: object) -> tuple[
        dict[str, int], dict[str, dict], int, dict[str, int], int, int
    ]:
        invalid = _fail("TL_SNAPSHOT_INVALID")
        if not isinstance(state, Mapping):
            raise invalid
        if not all(name in state for name in ("tracks", "clips", "version",
                                              "stats")):
            raise invalid

        raw_tracks = state["tracks"]
        raw_clips = state["clips"]
        raw_version = state["version"]
        raw_stats = state["stats"]
        if not isinstance(raw_tracks, list) or not isinstance(raw_clips, list):
            raise invalid
        if not _is_int(raw_version) or raw_version < 0:
            raise invalid
        if not isinstance(raw_stats, Mapping):
            raise invalid

        tracks: dict[str, int] = {}
        orders: set[int] = set()
        for item in raw_tracks:
            if not isinstance(item, Mapping):
                raise invalid
            name = item.get("track")
            order = item.get("order")
            if not isinstance(name, str) or not _is_int(order):
                raise invalid
            if name in tracks or order in orders:
                raise invalid
            tracks[name] = order
            orders.add(order)

        clips: dict[str, dict] = {}
        track_intervals: dict[str, list[tuple[int, int, str]]] = {
            name: [] for name in tracks
        }
        for item in raw_clips:
            if not isinstance(item, Mapping):
                raise invalid
            if any(field not in item for field in _CLIP_FIELDS):
                raise invalid

            clip_id = item["clip_id"]
            track = item["track"]
            start_frame = item["start_frame"]
            duration_frames = item["duration_frames"]
            end_frame = item["end_frame"]
            source_in_frame = item["source_in_frame"]
            transition_in_frames = item["transition_in_frames"]
            transition_out_frames = item["transition_out_frames"]

            if not isinstance(clip_id, str) or not clip_id:
                raise invalid
            if clip_id in clips or track not in tracks:
                raise invalid
            if (
                not _is_int(start_frame)
                or start_frame < 0
                or not _is_int(duration_frames)
                or duration_frames <= 0
                or not _is_int(end_frame)
                or not _is_int(source_in_frame)
                or source_in_frame < 0
                or not _is_int(transition_in_frames)
                or transition_in_frames < 0
                or not _is_int(transition_out_frames)
                or transition_out_frames < 0
            ):
                raise invalid
            if end_frame != start_frame + duration_frames:
                raise invalid
            if transition_in_frames + transition_out_frames > duration_frames:
                raise invalid

            clip = {
                "clip_id": clip_id,
                "track": track,
                "start_frame": start_frame,
                "duration_frames": duration_frames,
                "end_frame": end_frame,
                "source_in_frame": source_in_frame,
                "transition_in_frames": transition_in_frames,
                "transition_out_frames": transition_out_frames,
            }
            clips[clip_id] = clip
            track_intervals[track].append((start_frame, end_frame, clip_id))

        for intervals in track_intervals.values():
            intervals.sort()
            for previous, current in zip(intervals, intervals[1:]):
                if current[0] < previous[1]:
                    raise invalid

        stats = {name: 0 for name in _STAT_KEYS}
        for name, value in raw_stats.items():
            if (
                not isinstance(name, str)
                or not _is_int(value)
                or value < 0
            ):
                raise invalid
            stats[name] = value

        frame_rate = state.get("frame_rate", self.frame_rate)
        if not _is_int(frame_rate) or frame_rate <= 0:
            raise invalid

        if "next_clip_number" in state:
            next_clip_number = state["next_clip_number"]
            if not _is_int(next_clip_number) or next_clip_number < 1:
                raise invalid
        else:
            highest = 0
            for clip_id in clips:
                match = _AUTO_CLIP_ID.fullmatch(clip_id)
                if match is not None:
                    highest = max(highest, int(match.group(1)))
            next_clip_number = highest + 1

        return tracks, clips, raw_version, stats, frame_rate, next_clip_number
