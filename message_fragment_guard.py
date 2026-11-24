"""Fragmented tunnel message reassembly guard."""

from __future__ import annotations

import threading


class FragmentError(ValueError):
    """Rejection raised by the fragment guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MessageFragmentGuard:
    """Reassemble tunnel messages from fragments with bounded per-stream state."""

    def __init__(self, *, max_inflight_per_stream: int = 4,
                 max_bytes_per_stream: int = 65536,
                 max_fragments_per_stream: int = 64,
                 reassembly_timeout: float = 5.0, clock=None) -> None:
        self.max_inflight_per_stream = max_inflight_per_stream
        self.max_bytes_per_stream = max_bytes_per_stream
        self.max_fragments_per_stream = max_fragments_per_stream
        self.reassembly_timeout = reassembly_timeout
        self._clock = clock
        self._lock = threading.RLock()

    def on_fragment(self, stream: str, msg_id: int, epoch: int, index: int,
                    count: int, total_len: int, data: bytes,
                    now=None) -> bytes | None:
        raise NotImplementedError

    def pending_messages(self, stream: str) -> list:
        raise NotImplementedError

    def stream_state(self, stream: str) -> dict:
        raise NotImplementedError

    def expire(self, now) -> list:
        raise NotImplementedError

    def close_stream(self, stream: str) -> int:
        raise NotImplementedError

    def drop_message(self, stream: str, msg_id: int, epoch: int) -> bool:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
