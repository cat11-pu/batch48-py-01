"""Canvas slot layout, source binding and atomic layout switching for cloud mixing."""

from __future__ import annotations

import copy
import threading
from contextlib import contextmanager

STAT_KEYS = (
    "slots_added",
    "slots_removed",
    "binds",
    "unbinds",
    "layouts_applied",
    "resizes",
    "hit_tests",
    "rejected",
)


class LayoutError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class LayoutMixerSession:
    """Manage canvas slots, bound sources and whole layout replacements."""

    def __init__(self, *, canvas_width: int = 1280, canvas_height: int = 720,
                 max_slots: int = 16, max_sources: int = 16,
                 clock=None) -> None:
        if not _is_positive_int(canvas_width) or not _is_positive_int(canvas_height):
            raise LayoutError(
                "LAYOUT_INVALID_CANVAS",
                {"width": canvas_width, "height": canvas_height},
            )

        self.canvas_width = canvas_width
        self.canvas_height = canvas_height
        self.max_slots = max_slots
        self.max_sources = max_sources
        self._clock = clock
        self._lock = threading.RLock()
        self._slots: dict[object, dict] = {}
        self._source_to_slot: dict[str, object] = {}
        self._revision = 0
        self._stats = {key: 0 for key in STAT_KEYS}

    @contextmanager
    def _guard(self):
        with self._lock:
            try:
                yield
            except LayoutError:
                self._stats["rejected"] += 1
                raise

    def _parse_rect(self, rect, canvas_width: int, canvas_height: int) -> list[int]:
        if not isinstance(rect, (list, tuple)) or len(rect) != 4:
            raise LayoutError("LAYOUT_INVALID_RECT", {"rect": rect})

        left, top, width, height = rect
        if not all(_is_strict_int(value) for value in rect):
            raise LayoutError("LAYOUT_INVALID_RECT", {"rect": list(rect)})
        if width <= 0 or height <= 0:
            raise LayoutError("LAYOUT_INVALID_RECT", {"rect": list(rect)})

        if left < 0 or top < 0:
            raise LayoutError("LAYOUT_RECT_OUT_OF_BOUNDS", {"rect": list(rect)})
        if left + width > canvas_width or top + height > canvas_height:
            raise LayoutError("LAYOUT_RECT_OUT_OF_BOUNDS", {"rect": list(rect)})

        return [left, top, width, height]

    @staticmethod
    def _parse_z(z) -> int:
        if not _is_strict_int(z) or z < 0 or z > 1000:
            raise LayoutError("LAYOUT_INVALID_Z", {"z": z})
        return z

    @staticmethod
    def _parse_source(source_id) -> str:
        if not isinstance(source_id, str) or source_id == "":
            raise LayoutError("LAYOUT_INVALID_SOURCE", {"source": source_id})
        return source_id

    def _build_slots(self, slots, canvas_width: int, canvas_height: int):
        if not isinstance(slots, dict):
            raise LayoutError("LAYOUT_LAYOUT_INVALID", {"slots": slots})

        built_slots: dict[object, dict] = {}
        source_map: dict[str, object] = {}

        for slot_id, slot_data in slots.items():
            if not isinstance(slot_data, dict):
                raise LayoutError("LAYOUT_INVALID_RECT", {"slot_id": slot_id})

            rect = self._parse_rect(
                slot_data.get("rect"), canvas_width, canvas_height
            )
            z = self._parse_z(slot_data.get("z", 0))
            raw_source = slot_data.get("source")
            source = None if raw_source is None else self._parse_source(raw_source)

            if source is not None:
                existing_slot = source_map.get(source)
                if existing_slot is not None and existing_slot != slot_id:
                    raise LayoutError(
                        "LAYOUT_SOURCE_BOUND",
                        {"source": source, "slot_id": existing_slot},
                    )
                source_map[source] = slot_id

            built_slots[slot_id] = {
                "slot_id": slot_id,
                "rect": rect,
                "z": z,
                "source": source,
            }

        if len(built_slots) > self.max_slots:
            raise LayoutError(
                "LAYOUT_SLOT_LIMIT",
                {"max_slots": self.max_slots, "slots": len(built_slots)},
            )
        if len(source_map) > self.max_sources:
            raise LayoutError(
                "LAYOUT_SOURCE_LIMIT",
                {"max_sources": self.max_sources, "sources": len(source_map)},
            )

        return built_slots, source_map

    def add_slot(self, slot_id: str, rect, *, z: int = 0) -> dict:
        with self._guard():
            parsed_rect = self._parse_rect(rect, self.canvas_width, self.canvas_height)
            parsed_z = self._parse_z(z)

            if slot_id in self._slots:
                raise LayoutError("LAYOUT_DUPLICATE_SLOT", {"slot_id": slot_id})
            if len(self._slots) >= self.max_slots:
                raise LayoutError("LAYOUT_SLOT_LIMIT", {"max_slots": self.max_slots})

            record = {
                "slot_id": slot_id,
                "rect": parsed_rect,
                "z": parsed_z,
                "source": None,
            }
            self._slots[slot_id] = record
            self._revision += 1
            self._stats["slots_added"] += 1
            return copy.deepcopy(record)

    def remove_slot(self, slot_id: str) -> None:
        with self._guard():
            if slot_id not in self._slots:
                raise LayoutError("LAYOUT_UNKNOWN_SLOT", {"slot_id": slot_id})

            record = self._slots.pop(slot_id)
            source_id = record.get("source")
            if source_id is not None:
                self._source_to_slot.pop(source_id, None)

            self._revision += 1
            self._stats["slots_removed"] += 1

    def bind_source(self, slot_id: str, source_id: str) -> dict:
        with self._guard():
            if slot_id not in self._slots:
                raise LayoutError("LAYOUT_UNKNOWN_SLOT", {"slot_id": slot_id})

            source = self._parse_source(source_id)
            existing_slot = self._source_to_slot.get(source)
            if existing_slot is not None and existing_slot != slot_id:
                raise LayoutError(
                    "LAYOUT_SOURCE_BOUND",
                    {"source": source, "slot_id": existing_slot},
                )

            record = self._slots[slot_id]
            current_source = record["source"]
            if source not in self._source_to_slot:
                final_source_count = len(self._source_to_slot)
                if current_source is not None and current_source != source:
                    final_source_count -= 1
                final_source_count += 1
                if final_source_count > self.max_sources:
                    raise LayoutError(
                        "LAYOUT_SOURCE_LIMIT",
                        {"max_sources": self.max_sources},
                    )

            if current_source is not None and current_source != source:
                self._source_to_slot.pop(current_source, None)

            record["source"] = source
            self._source_to_slot[source] = slot_id
            self._stats["binds"] += 1
            return {"slot_id": slot_id, "source": source}

    def unbind_source(self, slot_id: str) -> None:
        with self._guard():
            if slot_id not in self._slots:
                raise LayoutError("LAYOUT_UNKNOWN_SLOT", {"slot_id": slot_id})

            record = self._slots[slot_id]
            source = record["source"]
            if source is not None:
                self._source_to_slot.pop(source, None)
                record["source"] = None

            self._stats["unbinds"] += 1

    def apply_layout(self, layout: dict) -> dict:
        with self._guard():
            if not isinstance(layout, dict) or "slots" not in layout:
                raise LayoutError("LAYOUT_LAYOUT_INVALID", {"layout": layout})

            new_slots, new_sources = self._build_slots(
                layout["slots"], self.canvas_width, self.canvas_height
            )

            self._slots = new_slots
            self._source_to_slot = new_sources
            self._revision += 1
            self._stats["layouts_applied"] += 1
            return {"revision": self._revision, "slots": len(new_slots)}

    def current_layout(self) -> dict:
        with self._lock:
            return copy.deepcopy(
                {
                    "revision": self._revision,
                    "canvas": {
                        "width": self.canvas_width,
                        "height": self.canvas_height,
                    },
                    "slots": self._slots,
                }
            )

    def resize_canvas(self, width: int, height: int) -> dict:
        with self._guard():
            if not _is_positive_int(width) or not _is_positive_int(height):
                raise LayoutError(
                    "LAYOUT_INVALID_CANVAS", {"width": width, "height": height}
                )

            scale_x = width / self.canvas_width
            scale_y = height / self.canvas_height

            for record in self._slots.values():
                left, top, slot_width, slot_height = record["rect"]
                scaled = (
                    _floor_even(left * scale_x),
                    _floor_even(top * scale_y),
                    max(2, _floor_even(slot_width * scale_x)),
                    max(2, _floor_even(slot_height * scale_y)),
                )

                aligned_left, aligned_top, aligned_width, aligned_height = scaled
                aligned_width = min(
                    aligned_width, width - aligned_left - (width - aligned_left) % 2
                )
                aligned_height = min(
                    aligned_height,
                    height - aligned_top - (height - aligned_top) % 2,
                )
                record["rect"] = [
                    aligned_left,
                    aligned_top,
                    aligned_width,
                    aligned_height,
                ]

            self.canvas_width = width
            self.canvas_height = height
            self._revision += 1
            self._stats["resizes"] += 1
            return {
                "width": width,
                "height": height,
                "revision": self._revision,
                "slots": len(self._slots),
            }

    def hit_test(self, x: int, y: int) -> str | None:
        with self._guard():
            self._stats["hit_tests"] += 1
            hit_slot = None
            hit_z = None

            for slot_id, record in self._slots.items():
                left, top, width, height = record["rect"]
                contains = (
                    left <= x < left + width and top <= y < top + height
                )
                if contains and (hit_z is None or record["z"] >= hit_z):
                    hit_slot = slot_id
                    hit_z = record["z"]

            return hit_slot

    def slot_info(self, slot_id: str) -> dict:
        with self._guard():
            record = self._slots.get(slot_id)
            if record is None:
                raise LayoutError("LAYOUT_UNKNOWN_SLOT", {"slot_id": slot_id})
            return copy.deepcopy(record)

    def source_slot(self, source_id: str) -> str | None:
        with self._lock:
            return self._source_to_slot.get(source_id)

    def revision(self) -> int:
        with self._lock:
            return self._revision

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(
                {
                    "revision": self._revision,
                    "canvas": {
                        "width": self.canvas_width,
                        "height": self.canvas_height,
                    },
                    "slots": self._slots,
                }
            )

    def restore(self, state: dict) -> None:
        with self._guard():
            try:
                if not isinstance(state, dict):
                    raise LayoutError("LAYOUT_SNAPSHOT_INVALID", {"state": state})

                revision = state.get("revision")
                if not _is_strict_int(revision) or revision < 0:
                    raise LayoutError("LAYOUT_SNAPSHOT_INVALID", {"field": "revision"})

                canvas = state.get("canvas")
                if not isinstance(canvas, dict):
                    raise LayoutError("LAYOUT_SNAPSHOT_INVALID", {"field": "canvas"})
                width = canvas.get("width")
                height = canvas.get("height")
                if not _is_positive_int(width) or not _is_positive_int(height):
                    raise LayoutError("LAYOUT_SNAPSHOT_INVALID", {"field": "canvas"})

                slots = state.get("slots")
                new_slots, new_sources = self._build_slots(slots, width, height)
            except LayoutError as exc:
                raise LayoutError(
                    "LAYOUT_SNAPSHOT_INVALID", {"cause": exc.code}
                ) from exc

            self._revision = revision
            self.canvas_width = width
            self.canvas_height = height
            self._slots = new_slots
            self._source_to_slot = new_sources

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STAT_KEYS:
                self._stats[key] = 0


def _is_strict_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value) -> bool:
    return _is_strict_int(value) and value > 0


def _floor_even(value: float) -> int:
    return int(value // 2) * 2
