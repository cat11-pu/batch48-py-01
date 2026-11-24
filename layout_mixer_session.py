"""Canvas slot layout, source binding and atomic layout switching for cloud mixing."""

from __future__ import annotations

import threading

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
        self.canvas_width = canvas_width
        self.canvas_height = canvas_height
        self.max_slots = max_slots
        self.max_sources = max_sources
        self._clock = clock
        self._lock = threading.RLock()

    def add_slot(self, slot_id: str, rect, *, z: int = 0) -> dict:
        raise NotImplementedError

    def remove_slot(self, slot_id: str) -> None:
        raise NotImplementedError

    def bind_source(self, slot_id: str, source_id: str) -> dict:
        raise NotImplementedError

    def unbind_source(self, slot_id: str) -> None:
        raise NotImplementedError

    def apply_layout(self, layout: dict) -> dict:
        raise NotImplementedError

    def current_layout(self) -> dict:
        raise NotImplementedError

    def resize_canvas(self, width: int, height: int) -> dict:
        raise NotImplementedError

    def hit_test(self, x: int, y: int) -> str | None:
        raise NotImplementedError

    def slot_info(self, slot_id: str) -> dict:
        raise NotImplementedError

    def source_slot(self, source_id: str) -> str | None:
        raise NotImplementedError

    def revision(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
