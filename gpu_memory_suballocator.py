"""GPU memory arena sub-allocation, fencing and defragmentation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field


_MAX_ARENA_BYTES = 1 << 32
_MAX_ALIGN = 1 << 16
_MAX_ALLOC_BYTES = 256 << 20
_MAX_ARENAS = 8


class GpuMemError(ValueError):
    """Raised when an arena request or a defragmentation plan is invalid."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _Allocation:
    handle: int
    arena: str
    offset: int
    size: int
    tag: str
    lifetime: str
    frame: int
    alignment: int


@dataclass
class _Arena:
    name: str
    size: int
    alignment: int
    free: list[dict] = field(default_factory=list)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_power_of_two(value: object) -> bool:
    return _is_int(value) and value > 0 and (value & (value - 1)) == 0


def _align_up(value: int, alignment: int) -> int:
    return ((value + alignment - 1) // alignment) * alignment


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

        self._lock = threading.RLock()
        self._arenas: list[_Arena] = []
        self._by_name: dict[str, _Arena] = {}
        self._allocations: dict[int, _Allocation] = {}
        self._current_frame = 0
        self._fences: set[int] = set()
        self._next_handle = 1
        self._next_arena = 0
        self._stats_cache: dict[object, dict] = {}

    @staticmethod
    def _bounded(value: object, hard_limit: int) -> int:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return 0
        return max(0, min(number, hard_limit))

    def _arena_size_limit(self) -> int:
        return self._bounded(self.max_arena_bytes, _MAX_ARENA_BYTES)

    def _align_limit(self) -> int:
        return self._bounded(self.max_align, _MAX_ALIGN)

    def _alloc_limit(self) -> int:
        return self._bounded(self.max_alloc_bytes, _MAX_ALLOC_BYTES)

    def _arena_limit(self) -> int:
        return self._bounded(self.max_arenas, _MAX_ARENAS)

    def _validate_alignment(self, alignment: object, limit: int) -> int:
        if not _is_power_of_two(alignment) or alignment > limit:
            raise GpuMemError(
                "GPU_ALIGN_INVALID",
                {"alignment": alignment, "max_align": limit},
            )
        return alignment

    def _arena_for(self, name: object) -> _Arena:
        if not isinstance(name, str) or name not in self._by_name:
            raise GpuMemError("GPU_ARENA_INVALID", name)
        return self._by_name[name]

    @staticmethod
    def _copy_free_blocks(blocks: list[dict]) -> list[dict]:
        return [{"offset": block["offset"], "size": block["size"]}
                for block in blocks]

    def _insert_free(self, arena: _Arena, offset: int, size: int) -> None:
        blocks = arena.free
        index = 0
        while index < len(blocks) and blocks[index]["offset"] < offset:
            index += 1

        previous = blocks[index - 1] if index > 0 else None
        following = blocks[index] if index < len(blocks) else None

        if previous is not None and previous["offset"] + previous["size"] == offset:
            previous["size"] += size
            if (following is not None and
                    previous["offset"] + previous["size"] == following["offset"]):
                previous["size"] += following["size"]
                blocks.pop(index)
            return

        if following is not None and offset + size == following["offset"]:
            following["offset"] = offset
            following["size"] += size
            return

        blocks.insert(index, {"offset": offset, "size": size})

    def _free_locked(self, handle: int) -> int:
        allocation = self._allocations.pop(handle)
        arena = self._by_name[allocation.arena]
        self._insert_free(arena, allocation.offset, allocation.size)
        return allocation.size

    def _allocation_for_handle(self, handle: object) -> _Allocation:
        if not _is_int(handle):
            raise GpuMemError("GPU_HANDLE_INVALID", handle)
        allocation = self._allocations.get(handle)
        if allocation is None:
            raise GpuMemError("GPU_HANDLE_INVALID", handle)
        return allocation

    def create_arena(self, size: int, alignment: int = 256) -> dict:
        with self._lock:
            if not _is_int(size) or not 1 <= size <= self._arena_size_limit():
                raise GpuMemError(
                    "GPU_ARENA_INVALID",
                    {"size": size, "max_bytes": self._arena_size_limit()},
                )
            alignment = self._validate_alignment(
                alignment, self._align_limit())
            if len(self._arenas) >= self._arena_limit():
                raise GpuMemError(
                    "GPU_ARENA_LIMIT",
                    {"max_arenas": self._arena_limit()},
                )

            name = f"arena-{self._next_arena}"
            self._next_arena += 1
            arena = _Arena(
                name=name,
                size=size,
                alignment=alignment,
                free=[{"offset": 0, "size": size}],
            )
            self._arenas.append(arena)
            self._by_name[name] = arena
            return {"arena": name, "size": size, "alignment": alignment}

    def arenas(self) -> list:
        with self._lock:
            result = []
            for arena in self._arenas:
                result.append({
                    "arena": arena.name,
                    "size": arena.size,
                    "alignment": arena.alignment,
                    "free_bytes": sum(block["size"] for block in arena.free),
                    "blocks": len(arena.free),
                })
            return result

    def alloc(self, size: int, tag: str, lifetime: str = "frame",
              alignment: int = 1) -> dict:
        with self._lock:
            if not _is_int(size) or not 1 <= size <= self._alloc_limit():
                raise GpuMemError(
                    "GPU_ALLOC_TOO_LARGE",
                    {"size": size, "max_bytes": self._alloc_limit()},
                )
            alignment = self._validate_alignment(
                alignment, self._align_limit())
            if lifetime not in ("frame", "persistent"):
                raise GpuMemError("GPU_LIFETIME_INVALID", lifetime)
            if not isinstance(tag, str):
                raise GpuMemError("GPU_TAG_INVALID", tag)
            tag = tag.strip()
            if not tag:
                raise GpuMemError("GPU_TAG_INVALID", tag)
            if not self._arenas:
                raise GpuMemError("GPU_ARENA_INVALID", None)

            for arena in self._arenas:
                for index, block in enumerate(arena.free):
                    start = block["offset"]
                    block_end = start + block["size"]
                    offset = _align_up(start, alignment)
                    end = offset + size
                    if end > block_end:
                        continue

                    replacement = []
                    if offset > start:
                        replacement.append(
                            {"offset": start, "size": offset - start})
                    if end < block_end:
                        replacement.append(
                            {"offset": end, "size": block_end - end})
                    arena.free[index:index + 1] = replacement

                    handle = self._next_handle
                    self._next_handle += 1
                    self._allocations[handle] = _Allocation(
                        handle=handle,
                        arena=arena.name,
                        offset=offset,
                        size=size,
                        tag=tag,
                        lifetime=lifetime,
                        frame=self._current_frame,
                        alignment=alignment,
                    )
                    return {
                        "handle": handle,
                        "arena": arena.name,
                        "offset": offset,
                        "size": size,
                        "tag": tag,
                        "lifetime": lifetime,
                    }

            raise GpuMemError(
                "GPU_ADDRESS_SPACE",
                {"size": size, "alignment": alignment},
            )

    def free(self, handle: int) -> int:
        with self._lock:
            allocation = self._allocation_for_handle(handle)
            return self._free_locked(allocation.handle)

    def signal_fence(self, frame: int) -> None:
        with self._lock:
            if not _is_int(frame) or frame < 0:
                raise GpuMemError("GPU_FENCE_INVALID", frame)
            self._fences.add(frame)

    def end_frame(self, frame: int) -> dict:
        with self._lock:
            if not _is_int(frame) or frame <= self._current_frame:
                raise GpuMemError(
                    "GPU_FRAME_ORDER",
                    {"frame": frame, "current_frame": self._current_frame},
                )

            self._current_frame = frame
            freed = []
            retained = []
            for handle, allocation in list(self._allocations.items()):
                if allocation.lifetime != "frame":
                    continue
                if allocation.frame in self._fences:
                    self._free_locked(handle)
                    freed.append(handle)
                else:
                    retained.append(handle)
            return {"frame": frame, "freed": freed, "retained": retained}

    def check(self, handle: int) -> dict:
        with self._lock:
            allocation = self._allocation_for_handle(handle)
            return {
                "handle": allocation.handle,
                "arena": allocation.arena,
                "offset": allocation.offset,
                "size": allocation.size,
                "tag": allocation.tag,
                "lifetime": allocation.lifetime,
                "state": "live",
            }

    def free_blocks(self, arena: str | None = None) -> list:
        with self._lock:
            if arena is not None:
                selected = [self._arena_for(arena)]
            else:
                selected = self._arenas

            result = []
            for item in selected:
                result.extend(self._copy_free_blocks(item.free))
            result.sort(key=lambda block: block["offset"])
            return result

    def defrag_plan(self, arena: str | None = None) -> list:
        with self._lock:
            if arena is not None:
                selected = [self._arena_for(arena)]
            else:
                selected = self._arenas

            plan = []
            for item in selected:
                allocations = sorted(
                    (allocation for allocation in self._allocations.values()
                     if allocation.arena == item.name),
                    key=lambda allocation: allocation.offset,
                )
                cursor = 0
                for allocation in allocations:
                    if allocation.lifetime == "persistent":
                        cursor = max(
                            cursor, allocation.offset + allocation.size)
                        continue

                    target = _align_up(cursor, allocation.alignment)
                    if target > allocation.offset:
                        target = allocation.offset
                    if target != allocation.offset:
                        plan.append({
                            "handle": allocation.handle,
                            "from": allocation.offset,
                            "to": target,
                            "tag": allocation.tag,
                        })
                    cursor = target + allocation.size

            plan.sort(key=lambda move: move["from"])
            return plan

    def _parse_plan(self, plan: object) -> list[dict]:
        if isinstance(plan, (str, bytes, dict)):
            raise GpuMemError("GPU_PLAN_INVALID", plan)
        try:
            items = list(plan)
        except Exception as exc:
            raise GpuMemError("GPU_PLAN_INVALID", plan) from exc

        parsed = []
        seen = set()
        for item in items:
            if not isinstance(item, dict):
                raise GpuMemError("GPU_PLAN_INVALID", item)
            if not {"handle", "from", "to"} <= item.keys():
                raise GpuMemError("GPU_PLAN_INVALID", item)

            handle = item["handle"]
            source = item["from"]
            target = item["to"]
            if not _is_int(handle) or not _is_int(source) or not _is_int(target):
                raise GpuMemError("GPU_PLAN_INVALID", item)
            if handle in seen:
                raise GpuMemError("GPU_PLAN_INVALID", item)
            seen.add(handle)

            allocation = self._allocations.get(handle)
            if allocation is None:
                raise GpuMemError("GPU_PLAN_INVALID", item)
            if allocation.lifetime == "persistent":
                raise GpuMemError("GPU_PLAN_INVALID", item)
            if source != allocation.offset:
                raise GpuMemError("GPU_PLAN_INVALID", item)
            if "tag" in item and item["tag"] != allocation.tag:
                raise GpuMemError("GPU_PLAN_INVALID", item)

            arena = self._by_name[allocation.arena]
            if target < 0 or target + allocation.size > arena.size:
                raise GpuMemError("GPU_PLAN_INVALID", item)
            parsed.append({
                "handle": handle,
                "offset": target,
                "allocation": allocation,
            })
        return parsed

    @staticmethod
    def _build_free_blocks(size: int,
                           spans: list[tuple[int, int]]) -> list[dict]:
        free = []
        cursor = 0
        for offset, allocation_size in spans:
            if offset > cursor:
                free.append({
                    "offset": cursor,
                    "size": offset - cursor,
                })
            cursor = offset + allocation_size
        if cursor < size:
            free.append({"offset": cursor, "size": size - cursor})
        return free

    def apply_defrag(self, plan) -> int:
        with self._lock:
            moves = self._parse_plan(plan)
            if not moves:
                return 0

            new_offsets = {
                move["handle"]: move["offset"] for move in moves}

            final_by_arena: dict[str, list[_Allocation]] = {
                arena.name: [] for arena in self._arenas}
            for allocation in self._allocations.values():
                final_by_arena[allocation.arena].append(allocation)

            rebuilt_free: dict[str, list[dict]] = {}
            for arena in self._arenas:
                original = sorted(
                    final_by_arena[arena.name],
                    key=lambda allocation: allocation.offset,
                )
                original_order = [allocation.handle for allocation in original]

                final = [
                    (new_offsets.get(allocation.handle, allocation.offset),
                     allocation)
                    for allocation in original
                ]
                final.sort(key=lambda pair: pair[0])

                if [allocation.handle for _, allocation in final] != original_order:
                    raise GpuMemError(
                        "GPU_PLAN_INVALID", {"arena": arena.name})

                cursor = 0
                for offset, allocation in final:
                    if offset < cursor:
                        raise GpuMemError(
                            "GPU_PLAN_INVALID", {"arena": arena.name})
                    if offset + allocation.size > arena.size:
                        raise GpuMemError(
                            "GPU_PLAN_INVALID",
                            {"handle": allocation.handle, "to": offset},
                        )
                    cursor = offset + allocation.size

                spans = [
                    (offset, allocation.size)
                    for offset, allocation in final
                ]
                rebuilt_free[arena.name] = self._build_free_blocks(
                    arena.size, spans)

            for move in moves:
                move["allocation"].offset = move["offset"]
            for arena in self._arenas:
                arena.free = rebuilt_free[arena.name]
            return len(moves)

    def stats(self, arena: str | None = None) -> dict:
        with self._lock:
            if arena is not None:
                selected = [self._arena_for(arena)]
            else:
                selected = self._arenas

            names = {item.name for item in selected}
            allocated_bytes = sum(
                allocation.size
                for allocation in self._allocations.values()
                if allocation.arena in names
            )
            free_bytes = sum(
                block["size"]
                for item in selected
                for block in item.free
            )
            largest_free = max(
                (block["size"]
                 for item in selected
                 for block in item.free),
                default=0,
            )
            blocks = sum(len(item.free) for item in selected)
            fragmentation = (
                0.0 if free_bytes == 0
                else round(1.0 - (largest_free / free_bytes), 6)
            )
            result = {
                "arenas": len(selected),
                "allocated_bytes": allocated_bytes,
                "free_bytes": free_bytes,
                "largest_free": largest_free,
                "blocks": blocks,
                "fragmentation": fragmentation,
            }
            cached = self._stats_cache.get(arena)
            if cached is None:
                cached = {}
                self._stats_cache[arena] = cached
            cached.clear()
            cached.update(result)
            return cached

    def snapshot(self) -> dict:
        with self._lock:
            arenas = []
            for arena in self._arenas:
                arenas.append({
                    "arena": arena.name,
                    "size": arena.size,
                    "alignment": arena.alignment,
                    "free_blocks": self._copy_free_blocks(arena.free),
                })

            allocations = []
            for allocation in self._allocations.values():
                allocations.append({
                    "handle": allocation.handle,
                    "arena": allocation.arena,
                    "offset": allocation.offset,
                    "size": allocation.size,
                    "tag": allocation.tag,
                    "lifetime": allocation.lifetime,
                    "frame": allocation.frame,
                    "alignment": allocation.alignment,
                })

            return {
                "version": 1,
                "arenas": arenas,
                "allocations": allocations,
                "current_frame": self._current_frame,
                "fences": set(self._fences),
                "next_handle": self._next_handle,
                "next_arena": self._next_arena,
            }

    def _snapshot_error(self, detail: object) -> GpuMemError:
        return GpuMemError("GPU_SNAPSHOT_INVALID", detail)

    def _parse_snapshot(self, state: object):
        if not isinstance(state, dict):
            raise self._snapshot_error(state)

        try:
            arena_states = state["arenas"]
            allocation_states = state["allocations"]
            current_frame = state["current_frame"]
            fences_state = state["fences"]
            next_handle = state["next_handle"]
            next_arena = state["next_arena"]
        except (KeyError, TypeError) as exc:
            raise self._snapshot_error("missing fields") from exc

        if state.get("version", 1) != 1:
            raise self._snapshot_error("version")
        if not isinstance(arena_states, list):
            raise self._snapshot_error("arenas")
        if not isinstance(allocation_states, list):
            raise self._snapshot_error("allocations")
        if not _is_int(current_frame) or current_frame < 0:
            raise self._snapshot_error("current_frame")
        if not _is_int(next_handle) or next_handle < 1:
            raise self._snapshot_error("next_handle")
        if not _is_int(next_arena) or next_arena < 0:
            raise self._snapshot_error("next_arena")
        if not isinstance(fences_state, (set, list, tuple)):
            raise self._snapshot_error("fences")

        fences = set()
        try:
            for frame in fences_state:
                if not _is_int(frame) or frame < 0:
                    raise self._snapshot_error("fences")
                fences.add(frame)
        except TypeError as exc:
            raise self._snapshot_error("fences") from exc

        if len(arena_states) > self._arena_limit():
            raise self._snapshot_error("arena limit")

        arenas = []
        by_name = {}
        arena_indices = []
        for arena_state in arena_states:
            if not isinstance(arena_state, dict):
                raise self._snapshot_error(arena_state)
            try:
                name = arena_state["arena"]
                size = arena_state["size"]
                alignment = arena_state["alignment"]
                free_states = arena_state["free_blocks"]
            except (KeyError, TypeError) as exc:
                raise self._snapshot_error("arena fields") from exc

            if not isinstance(name, str) or not name.startswith("arena-"):
                raise self._snapshot_error(name)
            suffix = name[len("arena-"):]
            if not suffix.isdigit():
                raise self._snapshot_error(name)
            arena_index = int(suffix)
            if name in by_name:
                raise self._snapshot_error(name)
            if (not _is_int(size) or
                    not 1 <= size <= self._arena_size_limit()):
                raise self._snapshot_error(name)
            if (not _is_power_of_two(alignment) or
                    alignment > self._align_limit()):
                raise self._snapshot_error(name)
            if not isinstance(free_states, list):
                raise self._snapshot_error(name)

            free = []
            previous_end = 0
            for block in free_states:
                if not isinstance(block, dict):
                    raise self._snapshot_error(name)
                try:
                    offset = block["offset"]
                    block_size = block["size"]
                except KeyError as exc:
                    raise self._snapshot_error(name) from exc
                if (not _is_int(offset) or not _is_int(block_size) or
                        offset < previous_end or block_size <= 0 or
                        offset + block_size > size):
                    raise self._snapshot_error(name)
                free.append({"offset": offset, "size": block_size})
                previous_end = offset + block_size

            arena = _Arena(
                name=name,
                size=size,
                alignment=alignment,
                free=free,
            )
            arenas.append(arena)
            by_name[name] = arena
            arena_indices.append(arena_index)

        arena_indices.sort()
        if arena_indices != list(range(len(arenas))):
            raise self._snapshot_error("arena ids")
        if next_arena != len(arenas):
            raise self._snapshot_error("next_arena")

        allocations = {}
        intervals: dict[str, list[tuple[int, int, str]]] = {
            arena.name: [] for arena in arenas}
        max_handle = 0
        for allocation_state in allocation_states:
            if not isinstance(allocation_state, dict):
                raise self._snapshot_error(allocation_state)
            try:
                handle = allocation_state["handle"]
                arena_name = allocation_state["arena"]
                offset = allocation_state["offset"]
                size = allocation_state["size"]
                tag = allocation_state["tag"]
                lifetime = allocation_state["lifetime"]
                allocation_frame = allocation_state["frame"]
                alignment = allocation_state["alignment"]
            except (KeyError, TypeError) as exc:
                raise self._snapshot_error("allocation fields") from exc

            if not _is_int(handle) or handle < 1 or handle in allocations:
                raise self._snapshot_error(handle)
            if not isinstance(arena_name, str) or arena_name not in by_name:
                raise self._snapshot_error(arena_name)
            arena = by_name[arena_name]
            if (not _is_int(offset) or not _is_int(size) or
                    offset < 0 or size < 1 or
                    size > self._alloc_limit() or
                    offset + size > arena.size):
                raise self._snapshot_error(handle)
            if not isinstance(tag, str) or not tag.strip():
                raise self._snapshot_error(handle)
            if lifetime not in ("frame", "persistent"):
                raise self._snapshot_error(handle)
            if not _is_int(allocation_frame) or allocation_frame < 0:
                raise self._snapshot_error(handle)
            if allocation_frame > current_frame:
                raise self._snapshot_error(handle)
            if (not _is_power_of_two(alignment) or
                    alignment > self._align_limit()):
                raise self._snapshot_error(handle)

            allocation = _Allocation(
                handle=handle,
                arena=arena_name,
                offset=offset,
                size=size,
                tag=tag,
                lifetime=lifetime,
                frame=allocation_frame,
                alignment=alignment,
            )
            allocations[handle] = allocation
            intervals[arena_name].append((offset, offset + size, "allocated"))
            max_handle = max(max_handle, handle)

        if next_handle <= max_handle:
            raise self._snapshot_error("next_handle")

        for arena in arenas:
            for block in arena.free:
                intervals[arena.name].append((
                    block["offset"],
                    block["offset"] + block["size"],
                    "free",
                ))
            intervals[arena.name].sort(key=lambda item: item[0])
            cursor = 0
            for start, end, _ in intervals[arena.name]:
                if start != cursor:
                    raise self._snapshot_error(arena.name)
                cursor = end
            if cursor != arena.size:
                raise self._snapshot_error(arena.name)

        return {
            "arenas": arenas,
            "by_name": by_name,
            "allocations": allocations,
            "current_frame": current_frame,
            "fences": fences,
            "next_handle": next_handle,
            "next_arena": next_arena,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            parsed = self._parse_snapshot(state)
            self._arenas = parsed["arenas"]
            self._by_name = parsed["by_name"]
            self._allocations = parsed["allocations"]
            self._current_frame = parsed["current_frame"]
            self._fences = parsed["fences"]
            self._next_handle = parsed["next_handle"]
            self._next_arena = parsed["next_arena"]

    def reset(self) -> None:
        with self._lock:
            self._arenas = []
            self._by_name = {}
            self._allocations = {}
            self._current_frame = 0
            self._fences = set()
            self._next_handle = 1
            self._next_arena = 0
