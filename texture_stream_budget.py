"""Texture mip streaming with per-frame bandwidth budget and residency."""

from __future__ import annotations

import math
import threading


class TextureStreamError(ValueError):
    """Raised when a streaming request or plan cannot be satisfied."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STATS_KEYS = ("requests", "merged", "hits", "uploads", "evictions", "deferred")
_SNAPSHOT_VERSION = 1


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_error_value(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


class TextureStreamBudget:
    """Schedule mip uploads, residency changes and pins under a byte budget."""

    def __init__(self, *, max_textures: int = 50000, max_mip: int = 15,
                 max_frame_bytes: int = 64 * 1024 * 1024) -> None:
        self.max_textures = max_textures
        self.max_mip = max_mip
        self.max_frame_bytes = max_frame_bytes
        self._lock = threading.RLock()
        self._textures = {}
        self._resident = {}
        self._pins = set()
        self._pending = {}
        self._last_error = {}
        self._unpinned_frame = {}
        self._frame = 0
        self._stats = {key: 0 for key in _STATS_KEYS}

    def _mip_bytes(self, texture_id: str, mip: int) -> int:
        return max(1, self._textures[texture_id] >> mip)

    def _resident_bytes(self) -> int:
        return sum(
            self._mip_bytes(texture_id, mip)
            for texture_id, mip in self._resident.items()
        )

    def register(self, texture_id: str, base_bytes: int) -> dict:
        with self._lock:
            if not _is_int(base_bytes) or base_bytes <= 0:
                raise TextureStreamError(
                    "TEXTURE_BASE_INVALID",
                    {"texture_id": texture_id, "base_bytes": base_bytes},
                )
            existing = self._textures.get(texture_id)
            if existing is not None:
                if existing != base_bytes:
                    raise TextureStreamError(
                        "TEXTURE_BASE_CONFLICT",
                        {"texture_id": texture_id, "base_bytes": base_bytes,
                         "registered": existing},
                    )
                return {"texture_id": texture_id, "base_bytes": existing,
                        "mips": self.max_mip}
            if len(self._textures) >= self.max_textures:
                raise TextureStreamError(
                    "TEXTURE_TEXTURE_LIMIT",
                    {"texture_id": texture_id, "max_textures": self.max_textures},
                )
            self._textures[texture_id] = base_bytes
            return {"texture_id": texture_id, "base_bytes": base_bytes,
                    "mips": self.max_mip}

    def request(self, texture_id: str, mip: int, screen_error: float) -> dict:
        with self._lock:
            if texture_id not in self._textures:
                raise TextureStreamError(
                    "TEXTURE_UNKNOWN_TEXTURE", {"texture_id": texture_id})
            if not _is_int(mip) or not 0 <= mip < self.max_mip:
                raise TextureStreamError(
                    "TEXTURE_MIP_RANGE",
                    {"texture_id": texture_id, "mip": mip,
                     "max_mip": self.max_mip},
                )
            if not _is_error_value(screen_error):
                raise TextureStreamError(
                    "TEXTURE_ERROR_INVALID",
                    {"texture_id": texture_id, "screen_error": screen_error},
                )
            self._stats["requests"] += 1
            self._last_error[texture_id] = float(screen_error)
            resident_mip = self._resident.get(texture_id, -1)
            if 0 <= resident_mip <= mip:
                self._stats["hits"] += 1
                return {"texture_id": texture_id, "mip": mip,
                        "screen_error": screen_error, "merged": False}
            entry = self._pending.get(texture_id)
            if entry is not None:
                entry["mip"] = min(entry["mip"], mip)
                entry["screen_error"] = max(entry["screen_error"],
                                            float(screen_error))
                self._stats["merged"] += 1
                return {"texture_id": texture_id, "mip": entry["mip"],
                        "screen_error": entry["screen_error"], "merged": True}
            self._pending[texture_id] = {"mip": mip,
                                         "screen_error": float(screen_error)}
            return {"texture_id": texture_id, "mip": mip,
                    "screen_error": screen_error, "merged": False}

    def plan(self, budget_bytes: int | None = None) -> dict:
        with self._lock:
            if budget_bytes is None:
                budget_bytes = self.max_frame_bytes
            if not _is_int(budget_bytes) or budget_bytes <= 0:
                raise TextureStreamError(
                    "TEXTURE_BUDGET_INVALID", {"budget_bytes": budget_bytes})
            if budget_bytes > self.max_frame_bytes:
                raise TextureStreamError(
                    "TEXTURE_BUDGET_EXCEEDED",
                    {"budget_bytes": budget_bytes,
                     "max_frame_bytes": self.max_frame_bytes},
                )
            self._frame += 1

            uploads = []
            used_bytes = 0
            uploaded = set()
            ordered = sorted(
                self._pending.items(),
                key=lambda item: (-item[1]["screen_error"], item[0]),
            )
            for texture_id, entry in ordered:
                cost = self._mip_bytes(texture_id, entry["mip"])
                if used_bytes + cost > budget_bytes:
                    continue
                from_mip = self._resident.get(texture_id, -1)
                to_mip = entry["mip"]
                uploads.append({"texture_id": texture_id, "from_mip": from_mip,
                                "to_mip": to_mip, "bytes": cost})
                used_bytes += cost
                self._resident[texture_id] = to_mip
                uploaded.add(texture_id)
                del self._pending[texture_id]

            deferred = [
                texture_id for texture_id, _ in sorted(
                    self._pending.items(),
                    key=lambda item: (-item[1]["screen_error"], item[0]),
                )
            ]

            evictions = []
            resident_bytes = self._resident_bytes()
            if resident_bytes > budget_bytes:
                candidates = [
                    texture_id
                    for texture_id, mip in self._resident.items()
                    if texture_id not in self._pins
                    and texture_id not in uploaded
                    and texture_id not in self._pending
                    and mip < self.max_mip - 1
                    and self._unpinned_frame.get(texture_id, 0) < self._frame
                ]
                candidates.sort(
                    key=lambda texture_id: (
                        self._last_error.get(texture_id, 0.0), texture_id)
                )
                for texture_id in candidates:
                    if resident_bytes <= budget_bytes:
                        break
                    from_mip = self._resident[texture_id]
                    to_mip = from_mip + 1
                    resident_bytes -= self._mip_bytes(texture_id, from_mip)
                    resident_bytes += self._mip_bytes(texture_id, to_mip)
                    self._resident[texture_id] = to_mip
                    evictions.append({
                        "texture_id": texture_id,
                        "from_mip": from_mip,
                        "to_mip": to_mip,
                        "bytes": self._mip_bytes(texture_id, to_mip),
                    })

            resident_bytes = self._resident_bytes()
            self._stats["uploads"] += len(uploads)
            self._stats["evictions"] += len(evictions)
            self._stats["deferred"] += len(deferred)
            return {
                "uploads": uploads,
                "evictions": evictions,
                "deferred": deferred,
                "used_bytes": used_bytes,
                "resident_bytes": resident_bytes,
            }

    def resident(self, texture_id: str) -> int:
        with self._lock:
            return self._resident.get(texture_id, -1)

    def pin(self, texture_id: str) -> bool:
        with self._lock:
            if texture_id not in self._textures:
                raise TextureStreamError(
                    "TEXTURE_UNKNOWN_TEXTURE", {"texture_id": texture_id})
            self._pins.add(texture_id)
            self._unpinned_frame.pop(texture_id, None)
            return True

    def unpin(self, texture_id: str) -> bool:
        with self._lock:
            if texture_id not in self._textures:
                raise TextureStreamError(
                    "TEXTURE_UNKNOWN_TEXTURE", {"texture_id": texture_id})
            if texture_id not in self._pins:
                return False
            self._pins.discard(texture_id)
            self._unpinned_frame[texture_id] = self._frame + 1
            return True

    def pinned(self) -> list:
        with self._lock:
            return sorted(self._pins)

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
                "version": _SNAPSHOT_VERSION,
                "textures": {
                    texture_id: {"base_bytes": base_bytes}
                    for texture_id, base_bytes in self._textures.items()
                },
                "resident": dict(self._resident),
                "pins": sorted(self._pins),
                "pending": {
                    texture_id: dict(entry)
                    for texture_id, entry in self._pending.items()
                },
                "stats": dict(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            textures, resident, pins, pending, stats = self._validate_snapshot(
                state)
            self._textures = textures
            self._resident = resident
            self._pins = pins
            self._pending = pending
            self._stats = stats
            self._last_error = {}
            self._unpinned_frame = {}
            self._frame = 0

    def _validate_snapshot(self, state: object) -> tuple:
        def invalid(detail: object) -> None:
            raise TextureStreamError("TEXTURE_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            invalid({"reason": "state_not_mapping", "state": state})
        for key in ("version", "textures", "resident", "pins", "pending",
                    "stats"):
            if key not in state:
                invalid({"reason": "missing_key", "key": key})
        if state["version"] != _SNAPSHOT_VERSION:
            invalid({"reason": "bad_version", "version": state["version"]})

        raw_textures = state["textures"]
        if not isinstance(raw_textures, dict):
            invalid({"reason": "textures_not_mapping"})
        textures = {}
        for texture_id, entry in raw_textures.items():
            if not isinstance(texture_id, str) or not isinstance(entry, dict):
                invalid({"reason": "bad_texture_entry",
                         "texture_id": texture_id})
            base_bytes = entry.get("base_bytes")
            if not _is_int(base_bytes) or base_bytes <= 0:
                invalid({"reason": "bad_base_bytes",
                         "texture_id": texture_id})
            textures[texture_id] = base_bytes

        raw_resident = state["resident"]
        if not isinstance(raw_resident, dict):
            invalid({"reason": "resident_not_mapping"})
        resident = {}
        for texture_id, mip in raw_resident.items():
            if (texture_id not in textures or not _is_int(mip)
                    or not 0 <= mip < self.max_mip):
                invalid({"reason": "bad_resident_entry",
                         "texture_id": texture_id})
            resident[texture_id] = mip

        raw_pins = state["pins"]
        if not isinstance(raw_pins, (list, tuple)):
            invalid({"reason": "pins_not_list"})
        pins = set()
        for texture_id in raw_pins:
            if not isinstance(texture_id, str) or texture_id not in textures:
                invalid({"reason": "bad_pin", "texture_id": texture_id})
            pins.add(texture_id)

        raw_pending = state["pending"]
        if not isinstance(raw_pending, dict):
            invalid({"reason": "pending_not_mapping"})
        pending = {}
        for texture_id, entry in raw_pending.items():
            if texture_id not in textures or not isinstance(entry, dict):
                invalid({"reason": "bad_pending_entry",
                         "texture_id": texture_id})
            mip = entry.get("mip")
            screen_error = entry.get("screen_error")
            if not _is_int(mip) or not 0 <= mip < self.max_mip:
                invalid({"reason": "bad_pending_mip",
                         "texture_id": texture_id})
            if not _is_error_value(screen_error):
                invalid({"reason": "bad_pending_error",
                         "texture_id": texture_id})
            pending[texture_id] = {"mip": mip,
                                   "screen_error": float(screen_error)}

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict):
            invalid({"reason": "stats_not_mapping"})
        stats = {}
        for key in _STATS_KEYS:
            value = raw_stats.get(key, 0)
            if not _is_int(value) or value < 0:
                invalid({"reason": "bad_stat", "key": key})
            stats[key] = value

        return textures, resident, pins, pending, stats
