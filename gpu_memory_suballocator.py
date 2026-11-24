"""GPU memory arena sub-allocation, fencing and defragmentation."""

from __future__ import annotations


class GpuMemError(ValueError):
    """Raised when an arena request or a defragmentation plan is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class GpuMemorySuballocator:
    """Sub-allocate GPU arena memory and plan compaction per frame."""

    def __init__(self, *, max_arena_bytes: int = 1 << 32,
                 max_align: int = 65536,
                 max_alloc_bytes: int = 256 << 20,
                 max_arenas: int = 8) -> None:
        self.max_arena_bytes = max_arena_bytes
        self.max_align = max_align
        self.max_alloc_bytes = max_alloc_bytes
        self.max_arenas = max_arenas

    def create_arena(self, size: int, alignment: int = 256) -> dict:
        raise NotImplementedError

    def arenas(self) -> list:
        raise NotImplementedError

    def alloc(self, size: int, tag: str, lifetime: str = "frame",
              alignment: int = 1) -> dict:
        raise NotImplementedError

    def free(self, handle: int) -> int:
        raise NotImplementedError

    def signal_fence(self, frame: int) -> None:
        raise NotImplementedError

    def end_frame(self, frame: int) -> dict:
        raise NotImplementedError

    def check(self, handle: int) -> dict:
        raise NotImplementedError

    def free_blocks(self, arena: str | None = None) -> list:
        raise NotImplementedError

    def defrag_plan(self, arena: str | None = None) -> list:
        raise NotImplementedError

    def apply_defrag(self, plan) -> int:
        raise NotImplementedError

    def stats(self, arena: str | None = None) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError
