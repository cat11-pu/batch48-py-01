"""PSI section reassembly and PES rejoining for a transport stream demuxer."""

from __future__ import annotations

import threading

STAT_KEYS = (
    "sections_parsed",
    "crc_errors",
    "rejected",
    "pes_units",
    "overflows",
    "scrambled",
)

PAT_TABLE_ID = 0x00
PMT_TABLE_ID = 0x02


class PsiError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class PsiSectionGuard:
    """Reassemble PAT and PMT sections, guard table versions and rejoin PES."""

    def __init__(self, *, max_section_bytes: int = 4096,
                 max_pes_bytes: int = 1048576) -> None:
        self.max_section_bytes = max_section_bytes
        self.max_pes_bytes = max_pes_bytes
        self._lock = threading.RLock()

    def feed_section(self, pid: int, section: bytes, *,
                     scrambling: int = 0) -> dict:
        raise NotImplementedError

    def feed_payload(self, pid: int, payload: bytes, *,
                     unit_start: bool = False, scrambling: int = 0) -> dict:
        raise NotImplementedError

    def tables(self) -> dict:
        raise NotImplementedError

    def version_of(self, table: str, *, pid: int | None = None) -> int:
        raise NotImplementedError

    def pending_bytes(self, pid: int) -> int:
        raise NotImplementedError

    def discard(self, pid: int) -> int:
        raise NotImplementedError

    def push_pes(self, stream_id: str, chunk: bytes, *, pts: int) -> dict:
        raise NotImplementedError

    def flush_pes(self) -> list:
        raise NotImplementedError

    def drain_pes(self) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
