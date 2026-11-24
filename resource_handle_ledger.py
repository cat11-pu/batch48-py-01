"""GPU resource handle ledger with generations and deferred destruction."""

from __future__ import annotations

import copy
import threading


KINDS = ("buffer", "texture", "sampler", "pipeline")
MAX_RESOURCE_SIZE = 2**31 - 1
SNAPSHOT_VERSION = 1

STATS_KEYS = (
    "resources_created",
    "handles_retained",
    "handles_released",
    "frames_begun",
    "frames_completed",
    "reclaimed",
    "device_lost_events",
    "rejected",
)


class LedgerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail if detail is not None else {}


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(value: object) -> bool:
    return _is_int(value) and value > 0


class ResourceHandleLedger:
    """Track GPU resources by generation tagged handles and frame lifetime."""

    def __init__(self, *, max_resources: int = 64, max_open_frames: int = 3,
                 max_rebuild_queue: int = 8, clock=None) -> None:
        self.max_resources = max_resources
        self.max_open_frames = max_open_frames
        self.max_rebuild_queue = max_rebuild_queue
        self._clock = clock
        self._lock = threading.RLock()
        self._reset_state()

    def _reset_state(self) -> None:
        self._slots: list[dict] = []
        self._frame_number = 0
        self._open_frames: list[int] = []
        self._completed_frames: set[int] = set()
        self._rebuild_queue: list[str] = []
        self._stats = {key: 0 for key in STATS_KEYS}

    def _now(self):
        if self._clock is None:
            return None
        return self._clock()

    def _reject(self, code: str, detail: object = None) -> LedgerError:
        self._stats["rejected"] += 1
        return LedgerError(code, detail)

    def _validate_resource_fields(self, name: object, kind: object,
                                  size: object) -> bool:
        if not isinstance(name, str) or not name or len(name) > 64:
            return False
        if kind not in KINDS:
            return False
        if not _is_int(size) or size < 0 or size > MAX_RESOURCE_SIZE:
            return False
        return True

    def _occupied_slots(self) -> list[dict]:
        return [slot for slot in self._slots
                if slot["state"] in ("live", "pending")]

    def _locate(self, handle: object):
        if not isinstance(handle, dict):
            return "HANDLE_UNKNOWN", None, None

        handle_id = handle.get("id")
        generation = handle.get("generation")
        if not _positive_int(handle_id):
            return "HANDLE_UNKNOWN", None, None
        if handle_id > len(self._slots):
            return "HANDLE_UNKNOWN", None, None

        slot = self._slots[handle_id - 1]
        if not isinstance(slot, dict):
            return "HANDLE_UNKNOWN", None, None
        if not _positive_int(generation) or generation != slot["generation"]:
            return "HANDLE_STALE", slot, generation
        return None, slot, generation

    def _require_slot(self, handle: object) -> dict:
        reason, slot, generation = self._locate(handle)
        if reason is not None or slot is None:
            handle_id = handle.get("id") if isinstance(handle, dict) else None
            raise self._reject("LEDGER_INVALID_HANDLE", {
                "reason": reason or "HANDLE_UNKNOWN",
                "id": handle_id,
                "generation": generation,
            })
        return slot

    def _is_lost_generation(self, slot: dict, generation: object) -> bool:
        return (_positive_int(generation)
                and generation in slot.get("lost_generations", []))

    def _public_info(self, slot: dict) -> dict:
        release_frames = list(slot.get("release_frames", []))
        release_frame = slot.get("release_frame")
        if release_frame is None and release_frames:
            release_frame = release_frames[0]

        state = slot["state"]
        device_lost = bool(slot.get("device_lost", False)) or state == "device_lost"
        return {
            "id": slot["id"],
            "generation": slot["generation"],
            "handle": {"id": slot["id"], "generation": slot["generation"]},
            "name": slot["name"],
            "kind": slot["kind"],
            "size": slot["size"],
            "state": state,
            "refcount": slot["refcount"],
            "created_frame": slot.get("created_frame"),
            "release_frame": release_frame,
            "release_frames": release_frames,
            "device_lost": device_lost,
            "lost": device_lost,
            "created_at": slot.get("created_at"),
            "released_at": slot.get("released_at"),
            "destroyed_at": slot.get("destroyed_at"),
            "lost_at": slot.get("lost_at"),
        }

    def create(self, name: str, *, kind: str = "buffer", size: int = 0) -> dict:
        with self._lock:
            if not self._validate_resource_fields(name, kind, size):
                raise self._reject("LEDGER_INVALID_RESOURCE", {
                    "name": name if isinstance(name, (str, type(None))) else None,
                    "kind": kind,
                    "size": size,
                    "min_size": 0,
                    "max_size": MAX_RESOURCE_SIZE,
                })

            for slot in self._slots:
                if slot["state"] == "live" and slot["name"] == name:
                    raise self._reject("LEDGER_DUPLICATE_NAME", {"name": name})

            occupied = self._occupied_slots()
            if len(occupied) >= self.max_resources:
                raise self._reject("LEDGER_RESOURCE_LIMIT", {
                    "live": len(occupied),
                    "max_resources": self.max_resources,
                })

            reusable_index = None
            for index, slot in enumerate(self._slots):
                if slot["state"] in ("destroyed", "device_lost"):
                    reusable_index = index
                    break

            timestamp = self._now()
            if reusable_index is None:
                resource_id = len(self._slots) + 1
                generation = 1
                slot = {
                    "id": resource_id,
                    "generation": generation,
                    "lost_generations": [],
                    "name": name,
                    "kind": kind,
                    "size": size,
                    "state": "live",
                    "refcount": 0,
                    "created_frame": self._frame_number,
                    "release_frame": None,
                    "release_frames": [],
                    "device_lost": False,
                    "created_at": timestamp,
                    "released_at": None,
                    "destroyed_at": None,
                    "lost_at": None,
                }
                self._slots.append(slot)
            else:
                old_slot = self._slots[reusable_index]
                resource_id = reusable_index + 1
                generation = old_slot["generation"] + 1
                slot = {
                    "id": resource_id,
                    "generation": generation,
                    "lost_generations": list(old_slot.get("lost_generations", [])),
                    "name": name,
                    "kind": kind,
                    "size": size,
                    "state": "live",
                    "refcount": 0,
                    "created_frame": self._frame_number,
                    "release_frame": None,
                    "release_frames": [],
                    "device_lost": False,
                    "created_at": timestamp,
                    "released_at": None,
                    "destroyed_at": None,
                    "lost_at": None,
                }
                self._slots[reusable_index] = slot

            self._rebuild_queue = [item for item in self._rebuild_queue
                                   if item != name]
            self._stats["resources_created"] += 1
            return self._public_info(slot)

    def resource_info(self, handle: dict) -> dict:
        with self._lock:
            return self._public_info(self._require_slot(handle))

    def retain(self, handle: dict, *, count: int = 1) -> dict:
        with self._lock:
            reason, slot, generation = self._locate(handle)
            if reason is not None or slot is None:
                if (reason == "HANDLE_STALE" and slot is not None
                        and self._is_lost_generation(slot, generation)):
                    raise self._reject("LEDGER_INVALID_HANDLE", {
                        "reason": "DEVICE_LOST",
                        "id": slot["id"],
                        "generation": generation,
                    })
                handle_id = handle.get("id") if isinstance(handle, dict) else None
                raise self._reject("LEDGER_INVALID_HANDLE", {
                    "reason": reason or "HANDLE_UNKNOWN",
                    "id": handle_id,
                    "generation": generation,
                })

            if slot.get("device_lost") or slot["state"] == "device_lost":
                raise self._reject("LEDGER_INVALID_HANDLE", {
                    "reason": "DEVICE_LOST",
                    "id": slot["id"],
                    "generation": slot["generation"],
                })

            if slot["state"] in ("pending", "destroyed"):
                raise self._reject("LEDGER_RELEASED_HANDLE", {
                    "id": slot["id"],
                    "generation": slot["generation"],
                    "state": slot["state"],
                })

            if not _positive_int(count):
                raise self._reject("LEDGER_INVALID_COUNT", {"count": count})

            slot["refcount"] += count
            self._stats["handles_retained"] += count
            return self._public_info(slot)

    def _lost_release_result(self, slot: dict, handle: object, *,
                             stale: bool) -> dict:
        requested_id = slot["id"]
        requested_generation = slot["generation"]
        if isinstance(handle, dict):
            if _positive_int(handle.get("id")):
                requested_id = handle["id"]
            if _positive_int(handle.get("generation")):
                requested_generation = handle["generation"]

        released_reference = False
        if not stale and slot["refcount"] > 0:
            slot["refcount"] -= 1
            self._stats["handles_released"] += 1
            released_reference = True

        return {
            "id": requested_id,
            "generation": requested_generation,
            "handle": {"id": requested_id, "generation": requested_generation},
            "name": slot["name"],
            "state": "device_lost",
            "refcount": slot["refcount"],
            "released": True,
            "idempotent": not released_reference,
            "released_reference": released_reference,
            "stale_lost_generation": stale,
            "device_lost": True,
        }

    def release(self, handle: dict) -> dict:
        with self._lock:
            reason, slot, generation = self._locate(handle)
            if reason == "HANDLE_UNKNOWN" or slot is None:
                handle_id = handle.get("id") if isinstance(handle, dict) else None
                raise self._reject("LEDGER_INVALID_HANDLE", {
                    "reason": "HANDLE_UNKNOWN",
                    "id": handle_id,
                    "generation": generation,
                })

            if reason == "HANDLE_STALE":
                if self._is_lost_generation(slot, generation):
                    return self._lost_release_result(slot, handle, stale=True)
                raise self._reject("LEDGER_INVALID_HANDLE", {
                    "reason": "HANDLE_STALE",
                    "id": slot["id"],
                    "generation": generation,
                })

            if slot.get("device_lost") or slot["state"] == "device_lost":
                return self._lost_release_result(slot, handle, stale=False)

            if slot["state"] != "live" or slot["refcount"] <= 0:
                raise self._reject("LEDGER_DOUBLE_RELEASE", {
                    "id": slot["id"],
                    "generation": slot["generation"],
                    "state": slot["state"],
                    "refcount": slot["refcount"],
                })

            slot["refcount"] -= 1
            self._stats["handles_released"] += 1

            if slot["refcount"] == 0:
                slot["state"] = "pending"
                slot["release_frames"] = list(self._open_frames)
                slot["release_frame"] = (
                    self._open_frames[0] if self._open_frames else None
                )
                slot["released_at"] = self._now()

            return self._public_info(slot)

    def begin_frame(self) -> dict:
        with self._lock:
            open_count = len(self._open_frames)
            if open_count >= self.max_open_frames:
                raise self._reject("LEDGER_FRAME_LIMIT", {
                    "open_frames": open_count,
                    "max_open_frames": self.max_open_frames,
                    "frame_number": self._frame_number,
                })

            self._frame_number += 1
            frame_id = self._frame_number
            self._open_frames.append(frame_id)
            self._stats["frames_begun"] += 1
            return self._frame_info(frame_id)

    def _frame_info(self, frame_id: int) -> dict:
        open_count = len(self._open_frames)
        completed_frame = None
        if frame_id in self._completed_frames:
            completed_frame = frame_id
        return {
            "frame": frame_id,
            "frame_id": frame_id,
            "completed_frame": completed_frame,
            "current_frame": self._frame_number,
            "open_frames": open_count,
            "open_frame_count": open_count,
            "in_flight_frames": open_count,
            "max_open_frames": self.max_open_frames,
            "open_frame_ids": list(self._open_frames),
        }

    def end_frame(self) -> dict:
        with self._lock:
            if not self._open_frames:
                raise self._reject("LEDGER_NO_OPEN_FRAME", {
                    "frame_number": self._frame_number,
                })

            frame_id = self._open_frames.pop(0)
            self._completed_frames.add(frame_id)
            self._stats["frames_completed"] += 1
            result = self._frame_info(frame_id)
            result["completed_frame"] = frame_id
            return result

    def reclaim(self) -> dict:
        with self._lock:
            reclaimed: list[str] = []
            reclaimed_ids: list[int] = []

            for slot in self._slots:
                if slot["state"] != "pending":
                    continue

                release_frames = list(slot.get("release_frames", []))
                if not release_frames and slot.get("release_frame") is not None:
                    release_frames = [slot["release_frame"]]

                frame_ready = all(
                    frame_id in self._completed_frames
                    for frame_id in release_frames
                )
                if not frame_ready:
                    continue

                slot["state"] = "destroyed"
                slot["destroyed_at"] = self._now()
                reclaimed.append(slot["name"])
                reclaimed_ids.append(slot["id"])

            self._stats["reclaimed"] += len(reclaimed)
            return {
                "count": len(reclaimed),
                "reclaimed": reclaimed,
                "names": list(reclaimed),
                "ids": reclaimed_ids,
                "reclaimed_ids": reclaimed_ids,
            }

    def validate(self, handle: dict) -> dict:
        with self._lock:
            reason, slot, generation = self._locate(handle)
            if reason is not None:
                result = {"valid": False, "reason": reason}
                if slot is not None:
                    result["id"] = slot["id"]
                    result["slot_generation"] = slot["generation"]
                    result["requested_generation"] = generation
                return result

            assert slot is not None
            state = slot["state"]
            if slot.get("device_lost") or state == "device_lost":
                return {
                    "valid": False,
                    "reason": "DEVICE_LOST",
                    "name": slot["name"],
                    "id": slot["id"],
                    "generation": slot["generation"],
                    "state": state,
                }

            if state in ("pending", "destroyed"):
                return {
                    "valid": False,
                    "reason": "HANDLE_RELEASED",
                    "name": slot["name"],
                    "id": slot["id"],
                    "generation": slot["generation"],
                    "state": state,
                }

            return {
                "valid": True,
                "reason": None,
                "name": slot["name"],
                "id": slot["id"],
                "generation": slot["generation"],
                "state": state,
                "refcount": slot["refcount"],
            }

    def leak_report(self) -> dict:
        with self._lock:
            entries = []
            for slot in self._slots:
                if slot["state"] not in ("live", "pending"):
                    continue
                info = self._public_info(slot)
                entries.append({
                    "id": info["id"],
                    "generation": info["generation"],
                    "handle": info["handle"],
                    "name": info["name"],
                    "kind": info["kind"],
                    "size": info["size"],
                    "state": info["state"],
                    "refcount": info["refcount"],
                })

            entries.sort(key=lambda entry: (
                entry["name"], entry["id"], entry["generation"]
            ))
            live_count = sum(entry["state"] == "live" for entry in entries)
            pending_count = sum(entry["state"] == "pending" for entry in entries)
            refcount_total = sum(entry["refcount"] for entry in entries)

            lost_entries = [
                {
                    "id": slot["id"],
                    "generation": slot["generation"],
                    "name": slot["name"],
                    "state": slot["state"],
                    "refcount": slot["refcount"],
                }
                for slot in self._slots
                if slot.get("device_lost") or slot["state"] == "device_lost"
            ]

            return {
                "entries": entries,
                "total": len(entries),
                "total_count": len(entries),
                "live": live_count,
                "live_count": live_count,
                "active": live_count,
                "active_count": live_count,
                "referenced": live_count,
                "referenced_count": live_count,
                "pending": pending_count,
                "pending_count": pending_count,
                "pending_destroy_count": pending_count,
                "refcount_total": refcount_total,
                "total_refcount": refcount_total,
                "lost_entries": lost_entries,
                "device_lost_count": len(lost_entries),
                "counts": {
                    "live": live_count,
                    "pending": pending_count,
                    "total": len(entries),
                    "refcount_total": refcount_total,
                    "device_lost": len(lost_entries),
                },
            }

    def rebuild_queue(self) -> list:
        with self._lock:
            return list(self._rebuild_queue)

    def device_lost(self) -> dict:
        with self._lock:
            candidate_names: list[str] = []
            for slot in self._slots:
                if slot["state"] != "live":
                    continue
                name = slot["name"]
                if name not in self._rebuild_queue and name not in candidate_names:
                    candidate_names.append(name)

            queue_size_after = len(self._rebuild_queue) + len(candidate_names)
            if queue_size_after > self.max_rebuild_queue:
                raise self._reject("LEDGER_REBUILD_QUEUE_LIMIT", {
                    "rebuild_queue": list(self._rebuild_queue),
                    "queued": len(self._rebuild_queue),
                    "adding": len(candidate_names),
                    "queue_size": queue_size_after,
                    "max_rebuild_queue": self.max_rebuild_queue,
                })

            invalidated = 0
            timestamp = self._now()
            for slot in self._slots:
                if slot["state"] not in ("live", "pending"):
                    continue
                slot["state"] = "device_lost"
                slot["device_lost"] = True
                slot["lost_at"] = timestamp
                generation = slot["generation"]
                if generation not in slot.setdefault("lost_generations", []):
                    slot["lost_generations"].append(generation)
                invalidated += 1

            self._rebuild_queue.extend(candidate_names)
            self._stats["device_lost_events"] += 1
            return {
                "invalidated": invalidated,
                "queued": len(candidate_names),
                "added": len(candidate_names),
                "rebuild_queue": list(self._rebuild_queue),
                "queue": list(self._rebuild_queue),
                "names": list(self._rebuild_queue),
                "queue_size": len(self._rebuild_queue),
                "device_lost_events": self._stats["device_lost_events"],
            }

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": SNAPSHOT_VERSION,
                "slots": copy.deepcopy(self._slots),
                "frame": self._frame_number,
                "frame_number": self._frame_number,
                "current_frame": self._frame_number,
                "frame_id": self._frame_number,
                "next_frame": self._frame_number + 1,
                "open_frames": list(self._open_frames),
                "in_flight_frames": list(self._open_frames),
                "completed_frames": sorted(self._completed_frames),
                "rebuild_queue": list(self._rebuild_queue),
                "stats": dict(self._stats),
                "max_resources": self.max_resources,
                "max_open_frames": self.max_open_frames,
                "max_rebuild_queue": self.max_rebuild_queue,
                "config": {
                    "max_resources": self.max_resources,
                    "max_open_frames": self.max_open_frames,
                    "max_rebuild_queue": self.max_rebuild_queue,
                },
            }
            return copy.deepcopy(state)

    def _parse_restore_state(self, state: object) -> dict:
        try:
            candidate = copy.deepcopy(state)
        except Exception as exc:
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "UNCOPYABLE_STATE",
            }) from exc

        if not isinstance(candidate, dict):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "STATE_NOT_OBJECT",
            })

        version = candidate.get("version", SNAPSHOT_VERSION)
        if version != SNAPSHOT_VERSION:
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "UNSUPPORTED_VERSION",
                "version": version,
            })

        raw_slots = candidate.get("slots")
        if not isinstance(raw_slots, list):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "SLOTS_NOT_LIST",
            })

        slots: list[dict] = []
        for index, raw_slot in enumerate(raw_slots):
            if not isinstance(raw_slot, dict):
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "SLOT_NOT_OBJECT",
                    "index": index,
                })

            slot_id = raw_slot.get("id", index + 1)
            generation = raw_slot.get("generation", 1)
            name = raw_slot.get("name")
            kind = raw_slot.get("kind", "buffer")
            size = raw_slot.get("size", 0)
            state_name = raw_slot.get("state", "destroyed")
            refcount = raw_slot.get("refcount", 0)

            if not _positive_int(slot_id) or slot_id != index + 1:
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_SLOT_ID",
                    "index": index,
                    "id": slot_id,
                })
            if not _positive_int(generation):
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_GENERATION",
                    "id": slot_id,
                })
            if not isinstance(name, str) or not name or len(name) > 64:
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_NAME",
                    "id": slot_id,
                })
            if kind not in KINDS:
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_KIND",
                    "id": slot_id,
                })
            if not _is_int(size) or size < 0 or size > MAX_RESOURCE_SIZE:
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_SIZE",
                    "id": slot_id,
                })
            if state_name not in ("live", "pending", "destroyed", "device_lost"):
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_STATE",
                    "id": slot_id,
                    "state": state_name,
                })
            if not _is_int(refcount) or refcount < 0:
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_REFCOUNT",
                    "id": slot_id,
                    "refcount": refcount,
                })

            release_frames = raw_slot.get("release_frames", [])
            if release_frames is None:
                release_frames = []
            if not isinstance(release_frames, list) or not all(
                    _positive_int(frame) for frame in release_frames):
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_RELEASE_FRAMES",
                    "id": slot_id,
                })

            lost_generations = raw_slot.get("lost_generations", [])
            if lost_generations is None:
                lost_generations = []
            if not isinstance(lost_generations, list) or not all(
                    _positive_int(item) for item in lost_generations):
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_LOST_GENERATIONS",
                    "id": slot_id,
                })

            device_lost = bool(raw_slot.get("device_lost", False))
            if state_name == "device_lost":
                device_lost = True

            slot = {
                "id": slot_id,
                "generation": generation,
                "lost_generations": list(dict.fromkeys(lost_generations)),
                "name": name,
                "kind": kind,
                "size": size,
                "state": state_name,
                "refcount": refcount,
                "created_frame": raw_slot.get("created_frame"),
                "release_frame": raw_slot.get("release_frame"),
                "release_frames": list(release_frames),
                "device_lost": device_lost,
                "created_at": raw_slot.get("created_at"),
                "released_at": raw_slot.get("released_at"),
                "destroyed_at": raw_slot.get("destroyed_at"),
                "lost_at": raw_slot.get("lost_at"),
            }
            slots.append(slot)

        frame_number = candidate.get(
            "frame_number",
            candidate.get("frame", candidate.get("current_frame", 0)),
        )
        if not _is_int(frame_number) or frame_number < 0:
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "INVALID_FRAME_NUMBER",
            })

        open_frames = candidate.get("open_frames",
                                    candidate.get("in_flight_frames", []))
        if not isinstance(open_frames, list) or not all(
                _positive_int(frame) for frame in open_frames):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "INVALID_OPEN_FRAMES",
            })
        if len(open_frames) != len(set(open_frames)):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "DUPLICATE_OPEN_FRAMES",
            })
        if any(frame > frame_number for frame in open_frames):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "OPEN_FRAME_AFTER_CURRENT",
            })

        completed_frames = candidate.get("completed_frames", [])
        if not isinstance(completed_frames, list) or not all(
                _positive_int(frame) for frame in completed_frames):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "INVALID_COMPLETED_FRAMES",
            })

        rebuild_queue = candidate.get("rebuild_queue", [])
        if not isinstance(rebuild_queue, list) or not all(
                isinstance(item, str) and item for item in rebuild_queue):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "INVALID_REBUILD_QUEUE",
            })
        rebuild_queue = list(dict.fromkeys(rebuild_queue))

        raw_stats = candidate.get("stats", {})
        if not isinstance(raw_stats, dict):
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "INVALID_STATS",
            })
        stats = {key: 0 for key in STATS_KEYS}
        for key in STATS_KEYS:
            value = raw_stats.get(key, 0)
            if not _is_int(value) or value < 0:
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_STAT",
                    "stat": key,
                })
            stats[key] = value

        config = candidate.get("config", candidate)
        if isinstance(config, dict):
            max_resources = config.get("max_resources", self.max_resources)
            max_open_frames = config.get("max_open_frames", self.max_open_frames)
            max_rebuild_queue = config.get(
                "max_rebuild_queue", self.max_rebuild_queue
            )
        else:
            max_resources = self.max_resources
            max_open_frames = self.max_open_frames
            max_rebuild_queue = self.max_rebuild_queue

        for field_name, value in (
            ("max_resources", max_resources),
            ("max_open_frames", max_open_frames),
            ("max_rebuild_queue", max_rebuild_queue),
        ):
            if not _positive_int(value):
                raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                    "reason": "INVALID_LIMIT",
                    "field": field_name,
                })

        if len(open_frames) > max_open_frames:
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "FRAME_LIMIT_EXCEEDED",
            })
        if len(rebuild_queue) > max_rebuild_queue:
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "REBUILD_QUEUE_LIMIT_EXCEEDED",
            })
        if len([slot for slot in slots
                if slot["state"] in ("live", "pending")]) > max_resources:
            raise self._reject("LEDGER_SNAPSHOT_INVALID", {
                "reason": "RESOURCE_LIMIT_EXCEEDED",
            })

        return {
            "slots": slots,
            "frame_number": frame_number,
            "open_frames": list(open_frames),
            "completed_frames": set(completed_frames),
            "rebuild_queue": rebuild_queue,
            "stats": stats,
            "max_resources": max_resources,
            "max_open_frames": max_open_frames,
            "max_rebuild_queue": max_rebuild_queue,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            restored = self._parse_restore_state(state)
            self._slots = restored["slots"]
            self._frame_number = restored["frame_number"]
            self._open_frames = restored["open_frames"]
            self._completed_frames = restored["completed_frames"]
            self._rebuild_queue = restored["rebuild_queue"]
            self._stats = restored["stats"]
            self.max_resources = restored["max_resources"]
            self.max_open_frames = restored["max_open_frames"]
            self.max_rebuild_queue = restored["max_rebuild_queue"]
