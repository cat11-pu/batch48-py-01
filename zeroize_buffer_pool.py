"""Fixed-capacity sensitive buffer pool with zeroization on return."""

from __future__ import annotations

import threading


class BufferPoolError(ValueError):
    def __init__(self, code: str, detail: object = None,
                 retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail
        self.retryable = retryable


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class ZeroizeBufferPool:
    """Lease fixed-size buffers, wipe them on release, never grow."""

    _LEASE_KEYS = (
        "lease_id", "owner", "size", "capacity", "buffer", "_pool_id", "_slot",
    )
    _STAT_KEYS = ("acquired", "released", "rejected", "zeroized_bytes")
    _SNAPSHOT_KEYS = ("capacity", "buffer_size") + _STAT_KEYS

    def __init__(self, capacity: int, buffer_size: int, *,
                 retry_after_ms: int = 50) -> None:
        if not _is_positive_int(capacity) or not _is_positive_int(buffer_size):
            raise BufferPoolError(
                "POOL_CAPACITY_INVALID",
                {"capacity": capacity, "buffer_size": buffer_size},
            )
        self.capacity = capacity
        self.buffer_size = buffer_size
        self.retry_after_ms = retry_after_ms
        self._lock = threading.RLock()
        self._pool_id = id(self)
        self._blocks = [bytearray(buffer_size) for _ in range(capacity)]
        self._free_slots = list(range(capacity))
        self._active = {}
        self._lease_seq = 0
        self._stats = {
            "acquired": 0,
            "released": 0,
            "rejected": 0,
            "zeroized_bytes": 0,
        }

    def acquire(self, size=None, *, owner: str = "") -> dict:
        with self._lock:
            if size is None:
                size = self.buffer_size
            if (not isinstance(size, int) or isinstance(size, bool)
                    or size <= 0 or size > self.buffer_size):
                self._stats["rejected"] += 1
                raise BufferPoolError(
                    "POOL_SIZE_INVALID",
                    {"size": size, "buffer_size": self.buffer_size},
                )
            if not self._free_slots:
                self._stats["rejected"] += 1
                raise BufferPoolError(
                    "POOL_EXHAUSTED",
                    {"retry_after_ms": self.retry_after_ms},
                    retryable=True,
                )
            slot = self._free_slots.pop()
            block = self._blocks[slot]
            if len(block) < self.buffer_size:
                block.extend(b"\x00" * (self.buffer_size - len(block)))
            if size < self.buffer_size:
                del block[size:]
            self._lease_seq += 1
            lease = {
                "lease_id": self._lease_seq,
                "owner": owner,
                "size": size,
                "capacity": self.buffer_size,
                "buffer": block,
                "_pool_id": self._pool_id,
                "_slot": slot,
            }
            self._active[slot] = lease
            self._stats["acquired"] += 1
            return lease

    def release(self, lease) -> dict:
        with self._lock:
            slot = self._validate_lease(lease)
            active = self._active.get(slot)
            if active is None or active["lease_id"] != lease["lease_id"]:
                raise BufferPoolError(
                    "POOL_DOUBLE_RELEASE",
                    {"lease_id": lease.get("lease_id")},
                )
            self._recycle(slot, active)
            return {
                "lease_id": active["lease_id"],
                "released": True,
                "free_count": len(self._free_slots),
            }

    def release_owner(self, owner: str) -> int:
        with self._lock:
            slots = [
                slot for slot, lease in self._active.items()
                if lease["owner"] == owner
            ]
            for slot in slots:
                self._recycle(slot, self._active[slot])
            return len(slots)

    def zeroize_all(self) -> int:
        with self._lock:
            zeros = b"\x00" * self.buffer_size
            written = 0
            for slot in self._free_slots:
                self._blocks[slot][:] = zeros
                written += self.buffer_size
            self._stats["zeroized_bytes"] += written
            return written

    def free_count(self) -> int:
        with self._lock:
            return len(self._free_slots)

    def leased_count(self) -> int:
        with self._lock:
            return len(self._active)

    def pool_capacity(self) -> int:
        with self._lock:
            return self.capacity

    def lease_info(self, lease) -> dict:
        with self._lock:
            slot = self._validate_lease(lease)
            active = self._active.get(slot)
            leased = (
                active is not None
                and active["lease_id"] == lease["lease_id"]
            )
            return {
                "owner": lease["owner"],
                "size": lease["size"],
                "capacity": lease["capacity"],
                "leased": leased,
            }

    def snapshot(self) -> dict:
        with self._lock:
            snap = {"capacity": self.capacity, "buffer_size": self.buffer_size}
            for key in self._STAT_KEYS:
                snap[key] = self._stats[key]
            return snap

    def restore(self, state: dict) -> None:
        with self._lock:
            if not isinstance(state, dict):
                raise BufferPoolError("POOL_SNAPSHOT_INVALID", {"state": state})
            missing = [key for key in self._SNAPSHOT_KEYS if key not in state]
            if missing:
                raise BufferPoolError(
                    "POOL_SNAPSHOT_INVALID", {"missing": missing},
                )
            counters = [state[key] for key in self._STAT_KEYS]
            if any(not isinstance(value, int) or isinstance(value, bool)
                   or value < 0 for value in counters):
                raise BufferPoolError(
                    "POOL_SNAPSHOT_INVALID", {"reason": "bad_counter"},
                )
            if state["capacity"] != self.capacity:
                raise BufferPoolError(
                    "POOL_SNAPSHOT_INVALID",
                    {"reason": "capacity_mismatch"},
                )
            if state["buffer_size"] != self.buffer_size:
                raise BufferPoolError(
                    "POOL_SNAPSHOT_INVALID",
                    {"reason": "buffer_size_mismatch"},
                )
            self._blocks = [
                bytearray(self.buffer_size) for _ in range(self.capacity)
            ]
            self._free_slots = list(range(self.capacity))
            self._active = {}
            self._stats = {key: state[key] for key in self._STAT_KEYS}

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._STAT_KEYS:
                self._stats[key] = 0

    def _validate_lease(self, lease: object) -> int:
        if not isinstance(lease, dict):
            raise BufferPoolError("POOL_LEASE_INVALID", {"reason": "not_dict"})
        if any(key not in lease for key in self._LEASE_KEYS):
            raise BufferPoolError(
                "POOL_LEASE_INVALID", {"reason": "missing_fields"},
            )
        if lease["_pool_id"] != self._pool_id:
            raise BufferPoolError(
                "POOL_FOREIGN_LEASE", {"lease_id": lease["lease_id"]},
            )
        slot = lease["_slot"]
        if (not isinstance(slot, int) or isinstance(slot, bool)
                or not 0 <= slot < self.capacity):
            raise BufferPoolError(
                "POOL_LEASE_INVALID", {"reason": "bad_slot"},
            )
        if not isinstance(lease["buffer"], bytearray):
            raise BufferPoolError(
                "POOL_LEASE_INVALID", {"reason": "bad_buffer"},
            )
        return slot

    def _recycle(self, slot: int, lease: dict) -> None:
        block = lease["buffer"]
        block[:] = b"\x00" * len(block)
        if len(block) < self.buffer_size:
            block.extend(b"\x00" * (self.buffer_size - len(block)))
        del self._active[slot]
        self._free_slots.append(slot)
        self._stats["released"] += 1
        self._stats["zeroized_bytes"] += self.buffer_size
