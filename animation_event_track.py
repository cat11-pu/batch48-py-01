"""Animation event track sampling, ordering and root motion extraction."""

from __future__ import annotations

import copy
import math
import threading
from bisect import bisect_right


ANIM_CLIP_INVALID = "ANIM_CLIP_INVALID"
ANIM_CLIP_DUPLICATE = "ANIM_CLIP_DUPLICATE"
ANIM_UNKNOWN_CLIP = "ANIM_UNKNOWN_CLIP"
ANIM_EVENT_INVALID = "ANIM_EVENT_INVALID"
ANIM_EVENT_OUT_OF_RANGE = "ANIM_EVENT_OUT_OF_RANGE"
ANIM_EVENT_LIMIT = "ANIM_EVENT_LIMIT"
ANIM_SNAPSHOT_INVALID = "ANIM_SNAPSHOT_INVALID"

SNAPSHOT_VERSION = 1


class AnimationError(ValueError):
    """Raised for rejected clips, events and snapshots."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _vector3(value: object) -> tuple[float, float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise AnimationError(ANIM_CLIP_INVALID)

    components = []
    for component in value:
        if not _finite_number(component):
            raise AnimationError(ANIM_CLIP_INVALID)
        components.append(float(component))
    return components[0], components[1], components[2]


def _validate_clip(clip: object) -> dict:
    if not isinstance(clip, dict):
        raise AnimationError(ANIM_CLIP_INVALID)

    name = clip.get("name")
    if not isinstance(name, str) or not name:
        raise AnimationError(ANIM_CLIP_INVALID)

    duration_ms = clip.get("duration_ms")
    if not _finite_number(duration_ms) or duration_ms <= 0:
        raise AnimationError(ANIM_CLIP_INVALID)

    loop = clip.get("loop", False)
    if not isinstance(loop, bool):
        raise AnimationError(ANIM_CLIP_INVALID)

    bones_input = clip.get("bones")
    if not isinstance(bones_input, dict):
        raise AnimationError(ANIM_CLIP_INVALID)

    bones: dict[str, dict] = {}
    for bone_name, frames in bones_input.items():
        if not isinstance(bone_name, str) or not bone_name:
            raise AnimationError(ANIM_CLIP_INVALID)
        if not isinstance(frames, (list, tuple)) or not frames:
            raise AnimationError(ANIM_CLIP_INVALID)

        times = []
        positions = []
        scales = []
        previous_time = None

        for frame in frames:
            if not isinstance(frame, dict):
                raise AnimationError(ANIM_CLIP_INVALID)

            time_ms = frame.get("t")
            if not _finite_number(time_ms):
                raise AnimationError(ANIM_CLIP_INVALID)
            if time_ms < 0 or time_ms > duration_ms:
                raise AnimationError(ANIM_CLIP_INVALID)
            if previous_time is not None and time_ms <= previous_time:
                raise AnimationError(ANIM_CLIP_INVALID)

            position = _vector3(frame.get("pos"))
            scale = _vector3(frame.get("scale"))

            times.append(time_ms)
            positions.append(position)
            scales.append(scale)
            previous_time = time_ms

        bones[bone_name] = {
            "times": times,
            "pos": positions,
            "scale": scales,
        }

    return {
        "name": name,
        "duration_ms": duration_ms,
        "loop": loop,
        "bones": bones,
    }


def _canonical_event(event: object, duration_ms: float) -> dict:
    if not isinstance(event, dict):
        raise AnimationError(ANIM_EVENT_INVALID)

    try:
        data = copy.deepcopy(event)
    except Exception as exc:
        raise AnimationError(ANIM_EVENT_INVALID) from exc

    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise AnimationError(ANIM_EVENT_INVALID)

    time_ms = data.get("time_ms")
    if not _is_number(time_ms):
        raise AnimationError(ANIM_EVENT_INVALID)
    if isinstance(time_ms, float) and math.isnan(time_ms):
        raise AnimationError(ANIM_EVENT_INVALID)
    if time_ms < 0 or time_ms > duration_ms:
        raise AnimationError(ANIM_EVENT_OUT_OF_RANGE)

    priority = data.get("priority", 0)
    if not _finite_number(priority):
        raise AnimationError(ANIM_EVENT_INVALID)

    data["name"] = name
    data["time_ms"] = time_ms
    data["priority"] = priority
    return data


def _event_sort_key(event: dict) -> tuple:
    return event["time_ms"], -event["priority"], event["event_id"]


def _interpolate_values(values: list[tuple[float, float, float]],
                        index: int, alpha: float) -> list[float]:
    start = values[index]
    end = values[index + 1]
    return [start[axis] + (end[axis] - start[axis]) * alpha for axis in range(3)]


def _sample_bone(bone: dict, time_ms: float) -> dict:
    times = bone["times"]
    index = bisect_right(times, time_ms) - 1

    if index < 0:
        position_index = 0
        position_alpha = 0.0
        scale_index = 0
        scale_alpha = 0.0
    elif index >= len(times) - 1:
        position_index = len(times) - 2 if len(times) > 1 else 0
        position_alpha = 1.0 if len(times) > 1 else 0.0
        scale_index = position_index
        scale_alpha = position_alpha
    else:
        span = times[index + 1] - times[index]
        alpha = 0.0 if span == 0 else (time_ms - times[index]) / span
        position_index = index
        position_alpha = alpha
        scale_index = index
        scale_alpha = alpha

    if len(times) == 1:
        position = list(bone["pos"][0])
        scale = list(bone["scale"][0])
    else:
        position = _interpolate_values(bone["pos"], position_index, position_alpha)
        scale = _interpolate_values(bone["scale"], scale_index, scale_alpha)

    return {"pos": position, "scale": scale}


def _effective_time(clip: dict, time_ms: object) -> tuple[float, int]:
    if not _finite_number(time_ms):
        raise AnimationError(ANIM_EVENT_INVALID)

    duration_ms = clip["duration_ms"]
    if time_ms < 0:
        return 0, 0

    if not clip["loop"]:
        if time_ms > duration_ms:
            return duration_ms, 0
        return time_ms, 0

    if isinstance(time_ms, int) and isinstance(duration_ms, int):
        loop_index = time_ms // duration_ms
    else:
        loop_index = math.floor(time_ms / duration_ms)
    local_time = time_ms - loop_index * duration_ms
    return local_time, loop_index


class AnimationEventTrack:
    """Track animation clips, events and root motion in milliseconds."""

    MAX_EVENTS = 256

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._clips: dict[str, dict] = {}

    def _record(self, clip: object) -> dict:
        if not isinstance(clip, str) or clip not in self._clips:
            raise AnimationError(ANIM_UNKNOWN_CLIP)
        return self._clips[clip]

    def load_clip(self, clip: dict) -> dict:
        try:
            raw_clip = copy.deepcopy(clip)
        except Exception as exc:
            raise AnimationError(ANIM_CLIP_INVALID) from exc

        normalized = _validate_clip(raw_clip)
        name = normalized["name"]

        with self._lock:
            if name in self._clips:
                raise AnimationError(ANIM_CLIP_DUPLICATE)
            self._clips[name] = {
                "raw_clip": raw_clip,
                "clip": normalized,
                "events": [],
                "next_event_seq": 1,
            }
            return self.clip_info(name)

    def clip_info(self, clip: str) -> dict:
        with self._lock:
            record = self._record(clip)
            clip_data = record["clip"]
            return {
                "name": clip_data["name"],
                "duration_ms": clip_data["duration_ms"],
                "loop": clip_data["loop"],
                "bone_count": len(clip_data["bones"]),
                "event_count": len(record["events"]),
            }

    def list_clips(self) -> list[str]:
        with self._lock:
            return list(self._clips.keys())

    def add_event(self, clip: str, event: dict) -> str:
        with self._lock:
            record = self._record(clip)
            if len(record["events"]) >= self.MAX_EVENTS:
                raise AnimationError(ANIM_EVENT_LIMIT)

            clip_data = record["clip"]
            normalized = _canonical_event(event, clip_data["duration_ms"])
            sequence = record["next_event_seq"]
            event_id = f"{clip_data['name']}-{sequence}"
            normalized["event_id"] = event_id
            record["next_event_seq"] = sequence + 1
            record["events"].append(normalized)
            return event_id

    def events(self, clip: str) -> list[dict]:
        with self._lock:
            record = self._record(clip)
            ordered = sorted(record["events"], key=_event_sort_key)
            return copy.deepcopy(ordered)

    def events_between(self, clip: str, from_ms, to_ms) -> list[dict]:
        with self._lock:
            record = self._record(clip)
            clip_data = record["clip"]

            if not _finite_number(from_ms) or not _finite_number(to_ms):
                raise AnimationError(ANIM_EVENT_INVALID)
            if from_ms >= to_ms:
                return []

            if not clip_data["loop"]:
                items = []
                for event in record["events"]:
                    time_ms = event["time_ms"]
                    if from_ms <= time_ms < to_ms:
                        items.append(self._public_event(event, time_ms, 0))
                items.sort(key=_event_sort_key)
                return copy.deepcopy(items)

            return self._loop_events_between_locked(record, from_ms, to_ms)

    def _loop_events_between_locked(self, record: dict, from_ms, to_ms) -> list[dict]:
        clip_data = record["clip"]
        duration_ms = clip_data["duration_ms"]

        if from_ms > 0:
            first_loop = max(0, math.floor(from_ms / duration_ms) - 1)
        else:
            first_loop = 0
        last_loop = max(first_loop, math.ceil(to_ms / duration_ms) + 1)

        collected = []
        seam_starts = set()

        for loop_index in range(first_loop, last_loop + 1):
            for event in record["events"]:
                local_time = event["time_ms"]
                is_end_seam = local_time == duration_ms

                if is_end_seam:
                    absolute_time = local_time + loop_index * duration_ms
                    output_loop_index = loop_index + 1
                else:
                    absolute_time = local_time + loop_index * duration_ms
                    output_loop_index = loop_index

                if from_ms <= absolute_time < to_ms:
                    if local_time == 0:
                        seam_starts.add(absolute_time)
                    collected.append((
                        self._public_event(event, absolute_time, output_loop_index),
                        is_end_seam,
                    ))

        items = [
            item for item, is_end_seam in collected
            if not is_end_seam or item["time_ms"] not in seam_starts
        ]
        items.sort(key=_event_sort_key)

        seen = set()
        deduped = []
        for item in items:
            identity = (item["event_id"], item["loop_index"], item["time_ms"])
            if identity in seen:
                continue
            seen.add(identity)
            deduped.append(item)

        return copy.deepcopy(deduped)

    @staticmethod
    def _public_event(event: dict, time_ms, loop_index: int) -> dict:
        return {
            "event_id": event["event_id"],
            "name": event["name"],
            "time_ms": time_ms,
            "loop_index": loop_index,
            "priority": event["priority"],
        }

    def remove_event(self, clip: str, event_id: str) -> bool:
        with self._lock:
            record = self._record(clip)
            events = record["events"]
            for index, event in enumerate(events):
                if event.get("event_id") == event_id:
                    del events[index]
                    return True
            return False

    def clear_events(self, clip: str) -> int:
        with self._lock:
            record = self._record(clip)
            removed = len(record["events"])
            record["events"].clear()
            return removed

    def event_count(self, clip: str) -> int:
        with self._lock:
            return len(self._record(clip)["events"])

    def sample(self, clip: str, time_ms) -> dict:
        with self._lock:
            record = self._record(clip)
            clip_data = record["clip"]
            effective_time, loop_index = _effective_time(clip_data, time_ms)
            bones = {
                bone_name: _sample_bone(bone, effective_time)
                for bone_name, bone in clip_data["bones"].items()
            }
            return {
                "time_ms": effective_time,
                "loop_index": loop_index,
                "bones": bones,
            }

    def extract_root_motion(self, clip: str, from_ms, to_ms) -> dict:
        with self._lock:
            record = self._record(clip)
            root = record["clip"]["bones"].get("root")
            if root is None:
                return {"delta": [0.0, 0.0, 0.0]}

            if not _finite_number(from_ms) or not _finite_number(to_ms):
                raise AnimationError(ANIM_EVENT_INVALID)

            start = _sample_bone(root, from_ms)["pos"]
            end = _sample_bone(root, to_ms)["pos"]
            return {"delta": [end[axis] - start[axis] for axis in range(3)]}

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": SNAPSHOT_VERSION,
                "clips": {
                    name: {
                        "clip": record["raw_clip"],
                        "events": record["events"],
                        "next_event_seq": record["next_event_seq"],
                    }
                    for name, record in self._clips.items()
                },
            }
            return copy.deepcopy(state)

    def restore(self, state: dict) -> None:
        candidate = self._state_from_snapshot(state)
        with self._lock:
            self._clips = candidate

    @staticmethod
    def _state_from_snapshot(state: object) -> dict:
        try:
            data = copy.deepcopy(state)
        except Exception as exc:
            raise AnimationError(ANIM_SNAPSHOT_INVALID) from exc

        if not isinstance(data, dict):
            raise AnimationError(ANIM_SNAPSHOT_INVALID)
        if type(data.get("version")) is not int or data.get("version") != SNAPSHOT_VERSION:
            raise AnimationError(ANIM_SNAPSHOT_INVALID)

        clips_input = data.get("clips")
        if not isinstance(clips_input, dict):
            raise AnimationError(ANIM_SNAPSHOT_INVALID)

        try:
            clips = {}
            for clip_name, record in clips_input.items():
                if not isinstance(clip_name, str) or not isinstance(record, dict):
                    raise AnimationError(ANIM_SNAPSHOT_INVALID)

                clip_data = _validate_clip(record.get("clip"))
                if clip_data["name"] != clip_name:
                    raise AnimationError(ANIM_SNAPSHOT_INVALID)

                events_input = record.get("events", [])
                if not isinstance(events_input, list):
                    raise AnimationError(ANIM_SNAPSHOT_INVALID)
                if len(events_input) > AnimationEventTrack.MAX_EVENTS:
                    raise AnimationError(ANIM_SNAPSHOT_INVALID)

                events = []
                event_ids = set()
                for event_data in events_input:
                    event = _canonical_event(event_data, clip_data["duration_ms"])
                    event_id = event.get("event_id")
                    if not isinstance(event_id, str) or not event_id or event_id in event_ids:
                        raise AnimationError(ANIM_SNAPSHOT_INVALID)
                    event_ids.add(event_id)
                    events.append(event)

                next_event_seq = record.get("next_event_seq", 1)
                if type(next_event_seq) is not int or next_event_seq < 1:
                    raise AnimationError(ANIM_SNAPSHOT_INVALID)

                id_prefix = f"{clip_name}-"
                for event in events:
                    event_id = event["event_id"]
                    if event_id.startswith(id_prefix):
                        suffix = event_id[len(id_prefix):]
                        if suffix.isdigit():
                            next_event_seq = max(next_event_seq, int(suffix) + 1)

                clips[clip_name] = {
                    "raw_clip": record["clip"],
                    "clip": clip_data,
                    "events": events,
                    "next_event_seq": next_event_seq,
                }
        except AnimationError as exc:
            if exc.code == ANIM_SNAPSHOT_INVALID:
                raise
            raise AnimationError(ANIM_SNAPSHOT_INVALID) from exc

        return clips
