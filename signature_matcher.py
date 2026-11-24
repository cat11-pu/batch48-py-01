"""Streaming multi-pattern signature matcher with masked bytes."""

from __future__ import annotations

import threading

MIN_PATTERN_BYTES = 4
MAX_PATTERN_BYTES = 4096
MAX_PATTERNS = 20000
MAX_CHUNK_BYTES = 8 * 1024 * 1024


class SignatureError(ValueError):
    """Refusal raised by the matcher; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class SignatureMatcher:
    """Compile byte signatures and match them over chunked streams."""

    def __init__(self, *, max_patterns: int = MAX_PATTERNS,
                 max_chunk: int = MAX_CHUNK_BYTES) -> None:
        self._max_patterns = max_patterns
        self._max_chunk = max_chunk
        self._lock = threading.RLock()

    def build(self, patterns: dict) -> dict:
        raise NotImplementedError

    def build_errors(self) -> list[dict]:
        raise NotImplementedError

    def pattern_ids(self) -> list[str]:
        raise NotImplementedError

    def pattern_count(self) -> int:
        raise NotImplementedError

    def feed(self, chunk: bytes, eof: bool = False) -> list[dict]:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def stats(self) -> dict[str, int]:
        raise NotImplementedError

    def hits_for(self, pattern_id: str) -> int:
        raise NotImplementedError

    def state(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def pending_bytes(self) -> int:
        raise NotImplementedError
