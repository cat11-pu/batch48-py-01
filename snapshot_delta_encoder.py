"""Per-connection incremental snapshot encoder with dirty masks and budget."""

from __future__ import annotations

import threading


class DeltaError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SnapshotDeltaEncoder:
    """Encodes per-connection deltas against the last acknowledged baseline."""

    def __init__(self, *, keyframe_interval: int = 8, keyframe_age: int = 32,
                 field_cost: int = 1, event_cost: int = 1) -> None:
        self.keyframe_interval = int(keyframe_interval)
        self.keyframe_age = int(keyframe_age)
        self.field_cost = int(field_cost)
        self.event_cost = int(event_cost)
        self._lock = threading.RLock()

    def register_entity(self, entity_id: str, fields: dict, *,
                        local: bool = False) -> dict:
        raise NotImplementedError

    def remove_entity(self, entity_id: str) -> dict:
        raise NotImplementedError

    def update_field(self, entity_id: str, name: str, value: object) -> dict:
        raise NotImplementedError

    def update_fields(self, entity_id: str, values: dict) -> dict:
        raise NotImplementedError

    def set_local_player(self, entity_id: str, *, local: bool = True) -> dict:
        raise NotImplementedError

    def set_distance(self, entity_id: str, distance: float) -> dict:
        raise NotImplementedError

    def encode(self, *, tick: int, budget: int) -> dict:
        raise NotImplementedError

    def ack(self, *, tick: int) -> dict:
        raise NotImplementedError

    def baseline_of(self, entity_id: str) -> dict | None:
        raise NotImplementedError

    def dirty_of(self, entity_id: str) -> list:
        raise NotImplementedError

    def entities(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
