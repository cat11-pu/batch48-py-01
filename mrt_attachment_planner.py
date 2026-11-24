"""Multi render target attachment planning and compatibility grouping."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "attachments_declared",
    "passes_declared",
    "plans_computed",
    "rejected",
)


class MrtPlannerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MrtAttachmentPlanner:
    """Plan colour and depth attachments for multi render target passes."""

    def __init__(self, *, max_color_slots: int = 8, byte_budget: int = 67108864,
                 max_attachments: int = 16, clock=None) -> None:
        self.max_color_slots = max_color_slots
        self.byte_budget = byte_budget
        self.max_attachments = max_attachments
        self._clock = clock
        self._lock = threading.RLock()

    def declare_attachment(self, name: str, *, format: str, width: int,
                           height: int, samples: int = 1) -> dict:
        raise NotImplementedError

    def attachment_info(self, name: str) -> dict:
        raise NotImplementedError

    def drop_attachment(self, name: str) -> None:
        raise NotImplementedError

    def declare_pass(self, name: str, *, order: int, color=(), depth=None,
                     blend: bool = False, reads=(), samples: int = 1) -> dict:
        raise NotImplementedError

    def pass_info(self, name: str) -> dict:
        raise NotImplementedError

    def plan(self) -> dict:
        raise NotImplementedError

    def compatibility(self, name_a: str, name_b: str) -> dict:
        raise NotImplementedError

    def alias_candidates(self) -> list:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
