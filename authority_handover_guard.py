"""Entity authority handover between processes with an invisible window."""

from __future__ import annotations

import threading


class HandoverError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuthorityHandoverGuard:
    """Moves entity authority across processes with export, receipt and rollback."""

    def __init__(self, *, receipt_timeout_ticks: int = 3,
                 max_buffered: int = 64) -> None:
        self.receipt_timeout_ticks = int(receipt_timeout_ticks)
        self.max_buffered = int(max_buffered)
        self._lock = threading.RLock()

    def register_entity(self, entity_id: str, *, process: str, fields: dict | None = None,
                        children: tuple = ()) -> dict:
        raise NotImplementedError

    def destroy_entity(self, entity_id: str) -> dict:
        raise NotImplementedError

    def begin_handover(self, entity_id: str, *, source: str, target: str,
                       tick: int = 0) -> dict:
        raise NotImplementedError

    def export_state(self, entity_id: str) -> dict:
        raise NotImplementedError

    def import_state(self, target: str, state: dict) -> dict:
        raise NotImplementedError

    def deliver_receipt(self, ticket: str, receipt: str) -> dict:
        raise NotImplementedError

    def expire(self, tick: int) -> list:
        raise NotImplementedError

    def buffer_input(self, entity_id: str, seq: int, payload: object = None) -> dict:
        raise NotImplementedError

    def forward_inputs(self, ticket: str) -> dict:
        raise NotImplementedError

    def apply_input(self, process: str, entity_id: str, seq: int,
                    payload: object = None) -> dict:
        raise NotImplementedError

    def query(self, entity_id: str) -> dict:
        raise NotImplementedError

    def invisible(self, entity_id: str) -> bool:
        raise NotImplementedError

    def authority(self, entity_id: str) -> str:
        raise NotImplementedError

    def handover(self, ticket: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
