"""Instance slot and draw batch guard for instanced rendering."""

from __future__ import annotations

import threading


class InstanceBatchError(ValueError):
    """Raised when the instance slot or batch bookkeeping is violated."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class InstanceBatchGuard:
    """Track instance slots, dirty attribute ranges and draw batches."""

    def __init__(self, *, max_slots: int = 256, max_batches: int = 64,
                 merge_gap: int = 0) -> None:
        self.max_slots = max_slots
        self.max_batches = max_batches
        self.merge_gap = merge_gap
        self._lock = threading.RLock()

    def create(self, instance_id: str, *, mesh: str, material: str,
               attrs: dict | None = None) -> dict:
        raise NotImplementedError

    def update(self, instance_id: str, attrs: dict) -> int:
        raise NotImplementedError

    def delete(self, instance_id: str) -> int:
        raise NotImplementedError

    def slot_of(self, instance_id: str) -> int:
        raise NotImplementedError

    def instance_at(self, slot: int) -> str | None:
        raise NotImplementedError

    def dirty_ranges(self) -> list[tuple[int, int]]:
        raise NotImplementedError

    def commit(self) -> list[tuple[int, int]]:
        raise NotImplementedError

    def batches(self) -> list[dict]:
        raise NotImplementedError

    def fragmentation(self) -> float:
        raise NotImplementedError

    def compact(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
