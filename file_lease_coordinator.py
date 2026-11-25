"""File lease coordination: exclusive/shared leases with FIFO waiters."""

from __future__ import annotations

import threading
from dataclasses import dataclass, replace


class LeaseError(ValueError):
    """Raised when a lease operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Lease:
    lease_id: str
    key: str
    owner: str
    mode: str
    acquired_at: int
    expires_at: int


@dataclass(frozen=True)
class _Waiter:
    owner: str
    mode: str
    ttl: int
    seq: int
    requested_at: int


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _lease_order(lease_id: str) -> tuple:
    prefix, separator, suffix = lease_id.rpartition("-")
    if separator and prefix == "lease" and suffix.isdigit():
        return (0, int(suffix), lease_id)
    return (1, 0, lease_id)


class LeaseCoordinator:
    """Grant, renew, release and expire leases on resource keys."""

    def __init__(self, *, max_leases: int = 1024) -> None:
        if not _is_int(max_leases) or max_leases < 1:
            raise ValueError("max_leases must be a positive integer")
        self.max_leases = max_leases
        self._lock = threading.RLock()
        self._leases: dict[str, Lease] = {}
        self._leases_by_key: dict[str, list[str]] = {}
        self._lease_ttls: dict[str, int] = {}
        self._waiters: dict[str, list[_Waiter]] = {}
        self._next_lease_seq = 1
        self._next_wait_seq = 1
        self._observed_at = 0
        self._last_wall_now: int | None = None
        self._stats = {
            "granted": 0,
            "denied": 0,
            "renewed": 0,
            "expired": 0,
            "released": 0,
            "waited": 0,
        }

    @staticmethod
    def _validate_key_owner(key: object, owner: object) -> None:
        if not isinstance(key, str) or not key:
            raise LeaseError("LEASE_INVALID_REQUEST", {"field": "key"})
        if not isinstance(owner, str) or not owner:
            raise LeaseError("LEASE_INVALID_REQUEST", {"field": "owner"})

    @staticmethod
    def _validate_mode(mode: object) -> None:
        if mode not in ("shared", "exclusive"):
            raise LeaseError("LEASE_INVALID_REQUEST", {"field": "mode"})

    @staticmethod
    def _validate_ttl(ttl: object) -> None:
        if not _is_int(ttl) or ttl <= 0:
            raise LeaseError("LEASE_INVALID_TTL", {"field": "ttl"})

    @staticmethod
    def _validate_now(now: object) -> None:
        if not _is_int(now) or now < 0:
            raise LeaseError("LEASE_INVALID_REQUEST", {"field": "now"})

    def _observe(self, now: int) -> int:
        if self._last_wall_now is None:
            self._observed_at += now
        else:
            advancement = now - self._last_wall_now
            if advancement > 0:
                self._observed_at += advancement
        self._last_wall_now = now
        return self._observed_at

    @staticmethod
    def _is_active(lease: Lease, now: int) -> bool:
        return lease.expires_at > now

    def _active_leases(self, key: str, now: int) -> list[Lease]:
        return [
            self._leases[lease_id]
            for lease_id in self._leases_by_key.get(key, [])
            if self._is_active(self._leases[lease_id], now)
        ]

    def _at_capacity(self, now: int) -> bool:
        active = sum(
            1 for lease in self._leases.values() if self._is_active(lease, now)
        )
        return active >= self.max_leases

    def _can_grant(self, key: str, mode: str, now: int) -> bool:
        active = self._active_leases(key, now)
        if mode == "exclusive":
            return not active
        return all(lease.mode != "exclusive" for lease in active)

    def _new_lease_id(self) -> str:
        while True:
            lease_id = "lease-%d" % self._next_lease_seq
            self._next_lease_seq += 1
            if lease_id not in self._leases:
                return lease_id

    def _grant(self, key: str, owner: str, mode: str, ttl: int, now: int) -> Lease:
        lease = Lease(
            lease_id=self._new_lease_id(),
            key=key,
            owner=owner,
            mode=mode,
            acquired_at=now,
            expires_at=now + ttl,
        )
        self._leases[lease.lease_id] = lease
        self._leases_by_key.setdefault(key, []).append(lease.lease_id)
        self._lease_ttls[lease.lease_id] = ttl
        self._stats["granted"] += 1
        return lease

    def _queue_waiter(
        self, key: str, owner: str, mode: str, ttl: int, now: int
    ) -> None:
        queue = self._waiters.setdefault(key, [])
        if any(waiter.owner == owner for waiter in queue):
            self._stats["denied"] += 1
            raise LeaseError(
                "LEASE_BUSY", {"key": key, "owner": owner, "queued": True}
            )
        queue.append(
            _Waiter(
                owner=owner,
                mode=mode,
                ttl=ttl,
                seq=self._next_wait_seq,
                requested_at=now,
            )
        )
        self._next_wait_seq += 1
        self._stats["denied"] += 1
        self._stats["waited"] += 1
        raise LeaseError(
            "LEASE_BUSY", {"key": key, "owner": owner, "queued": True}
        )

    def _remove_lease(self, lease_id: str) -> Lease:
        lease = self._leases.pop(lease_id)
        self._lease_ttls.pop(lease_id, None)
        key_ids = self._leases_by_key.get(lease.key, [])
        if lease_id in key_ids:
            key_ids.remove(lease_id)
        if not key_ids:
            self._leases_by_key.pop(lease.key, None)
        return lease

    def _promote_head(self, key: str, now: int) -> Lease | None:
        queue = self._waiters.get(key)
        if not queue:
            return None
        waiter = queue[0]
        if not self._can_grant(key, waiter.mode, now):
            return None
        if self._at_capacity(now):
            return None
        queue.pop(0)
        if not queue:
            self._waiters.pop(key, None)
        return self._grant(key, waiter.owner, waiter.mode, waiter.ttl, now)

    def _promote_one(self, now: int, preferred_key: str | None = None) -> Lease | None:
        if preferred_key is not None:
            promoted = self._promote_head(preferred_key, now)
            if promoted is not None:
                return promoted
        candidates = sorted(
            (
                (queue[0].seq, key)
                for key, queue in self._waiters.items()
                if key != preferred_key
                and self._can_grant(key, queue[0].mode, now)
            ),
            key=lambda item: item[0],
        )
        for _, key in candidates:
            promoted = self._promote_head(key, now)
            if promoted is not None:
                return promoted
        return None

    def acquire(self, key: str, owner: str, mode: str, ttl: int, now: int) -> Lease:
        with self._lock:
            try:
                self._validate_key_owner(key, owner)
                self._validate_mode(mode)
                self._validate_ttl(ttl)
                self._validate_now(now)
            except LeaseError:
                self._stats["denied"] += 1
                raise

            effective_now = self._observe(now)
            for lease in self._active_leases(key, effective_now):
                if lease.owner == owner and lease.mode == mode:
                    return lease

            if self._waiters.get(key):
                self._queue_waiter(key, owner, mode, ttl, effective_now)

            if self._can_grant(key, mode, effective_now) and not self._at_capacity(
                effective_now
            ):
                return self._grant(key, owner, mode, ttl, effective_now)

            self._queue_waiter(key, owner, mode, ttl, effective_now)

    def renew(self, lease_id: str, now: int, ttl: int | None = None) -> Lease:
        with self._lock:
            self._validate_now(now)
            if ttl is not None:
                self._validate_ttl(ttl)
            lease = (
                self._leases.get(lease_id)
                if isinstance(lease_id, str)
                else None
            )
            if lease is None:
                raise LeaseError("LEASE_UNKNOWN_LEASE", {"lease_id": lease_id})

            effective_now = self._observe(now)
            if lease.expires_at <= effective_now:
                raise LeaseError(
                    "LEASE_EXPIRED",
                    {"lease_id": lease_id, "expires_at": lease.expires_at},
                )

            duration = ttl
            if duration is None:
                duration = self._lease_ttls.get(lease_id)
                if duration is None:
                    duration = lease.expires_at - lease.acquired_at
                if duration <= 0:
                    duration = 1

            renewed = replace(
                lease, expires_at=effective_now + duration
            )
            self._leases[lease_id] = renewed
            self._lease_ttls[lease_id] = duration
            self._stats["renewed"] += 1
            return renewed

    def release(self, lease_id: str, owner: str) -> bool:
        with self._lock:
            if not isinstance(owner, str) or not owner:
                raise LeaseError("LEASE_INVALID_REQUEST", {"field": "owner"})
            lease = (
                self._leases.get(lease_id)
                if isinstance(lease_id, str)
                else None
            )
            if lease is None:
                raise LeaseError("LEASE_UNKNOWN_LEASE", {"lease_id": lease_id})
            if lease.owner != owner:
                raise LeaseError(
                    "LEASE_OWNER_MISMATCH",
                    {"lease_id": lease_id, "owner": owner},
                )

            self._remove_lease(lease_id)
            self._stats["released"] += 1
            self._promote_one(self._observed_at, lease.key)
            return True

    def holder(self, key: str) -> list:
        with self._lock:
            if not isinstance(key, str):
                return []
            lease_ids = self._leases_by_key.get(key, [])
            leases = sorted(
                (self._leases[lease_id] for lease_id in lease_ids),
                key=lambda lease: _lease_order(lease.lease_id),
            )
            return [
                {
                    "lease_id": lease.lease_id,
                    "owner": lease.owner,
                    "mode": lease.mode,
                    "expires_at": lease.expires_at,
                }
                for lease in leases
            ]

    def waiters(self, key: str) -> list:
        with self._lock:
            if not isinstance(key, str):
                return []
            return [
                {"owner": waiter.owner, "mode": waiter.mode, "seq": waiter.seq}
                for waiter in self._waiters.get(key, [])
            ]

    def cancel_wait(self, key: str, owner: str) -> bool:
        with self._lock:
            if not isinstance(key, str) or not isinstance(owner, str):
                return False
            queue = self._waiters.get(key)
            if not queue:
                return False
            for index, waiter in enumerate(queue):
                if waiter.owner == owner:
                    queue.pop(index)
                    if not queue:
                        self._waiters.pop(key, None)
                    self._promote_head(key, self._observed_at)
                    return True
            return False

    def expire(self, now: int) -> list:
        with self._lock:
            self._validate_now(now)
            effective_now = self._observe(now)
            expired = sorted(
                (
                    lease_id
                    for lease_id, lease in self._leases.items()
                    if lease.expires_at <= effective_now
                ),
                key=_lease_order,
            )
            if not expired:
                return []

            affected_keys = set()
            for lease_id in expired:
                lease = self._remove_lease(lease_id)
                affected_keys.add(lease.key)
            self._stats["expired"] += len(expired)
            promoted = False
            for key in sorted(affected_keys):
                if self._promote_head(key, effective_now) is not None:
                    promoted = True
            if not promoted:
                self._promote_one(effective_now)
            return expired

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def snapshot(self) -> dict:
        with self._lock:
            leases = {
                lease_id: {
                    "lease_id": lease.lease_id,
                    "key": lease.key,
                    "owner": lease.owner,
                    "mode": lease.mode,
                    "acquired_at": lease.acquired_at,
                    "expires_at": lease.expires_at,
                    "ttl": self._lease_ttls.get(lease_id, 1),
                }
                for lease_id, lease in self._leases.items()
            }
            waiters = {
                key: [
                    {
                        "owner": waiter.owner,
                        "mode": waiter.mode,
                        "ttl": waiter.ttl,
                        "seq": waiter.seq,
                        "requested_at": waiter.requested_at,
                    }
                    for waiter in queue
                ]
                for key, queue in self._waiters.items()
            }
            return {
                "leases": leases,
                "waiters": waiters,
                "next_lease_seq": self._next_lease_seq,
                "next_wait_seq": self._next_wait_seq,
                "observed_at": self._observed_at,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                (
                    leases,
                    leases_by_key,
                    lease_ttls,
                    waiters,
                    next_lease_seq,
                    next_wait_seq,
                    observed_at,
                ) = self._parse_snapshot(state)
            except (KeyError, TypeError, ValueError):
                raise LeaseError("LEASE_SNAPSHOT_INVALID", {"state": state}) from None

            self._leases = leases
            self._leases_by_key = leases_by_key
            self._lease_ttls = lease_ttls
            self._waiters = waiters
            self._next_lease_seq = next_lease_seq
            self._next_wait_seq = next_wait_seq
            self._observed_at = observed_at
            self._last_wall_now = observed_at

    def _parse_snapshot(self, state: object) -> tuple:
        if not isinstance(state, dict):
            raise TypeError("snapshot must be a mapping")
        raw_leases = state["leases"]
        raw_waiters = state["waiters"]
        if not isinstance(raw_leases, dict) or not isinstance(raw_waiters, dict):
            raise TypeError("leases and waiters must be mappings")

        observed_at = state.get("observed_at", state.get("now", 0))
        if not _is_int(observed_at) or observed_at < 0:
            raise ValueError("invalid observation time")

        leases: dict[str, Lease] = {}
        leases_by_key: dict[str, list[str]] = {}
        lease_ttls: dict[str, int] = {}
        max_lease_seq = 0
        for lease_id, raw in raw_leases.items():
            if not isinstance(lease_id, str) or not lease_id:
                raise ValueError("invalid lease id")
            if not isinstance(raw, dict):
                raise TypeError("invalid lease record")
            if "lease_id" in raw and raw["lease_id"] != lease_id:
                raise ValueError("lease id mismatch")
            key = raw["key"]
            owner = raw["owner"]
            mode = raw["mode"]
            acquired_at = raw["acquired_at"]
            expires_at = raw["expires_at"]
            self._validate_key_owner(key, owner)
            self._validate_mode(mode)
            if (
                not _is_int(acquired_at)
                or acquired_at < 0
                or not _is_int(expires_at)
                or expires_at < acquired_at
            ):
                raise ValueError("invalid lease timestamps")
            ttl = raw.get("ttl", expires_at - acquired_at)
            if not _is_int(ttl) or ttl <= 0:
                raise ValueError("invalid lease ttl")
            lease = Lease(
                lease_id=lease_id,
                key=key,
                owner=owner,
                mode=mode,
                acquired_at=acquired_at,
                expires_at=expires_at,
            )
            leases[lease_id] = lease
            leases_by_key.setdefault(key, []).append(lease_id)
            lease_ttls[lease_id] = ttl
            order = _lease_order(lease_id)
            if order[0] == 0:
                max_lease_seq = max(max_lease_seq, order[1])

        expired = [
            lease_id
            for lease_id, lease in leases.items()
            if lease.expires_at <= observed_at
        ]
        for lease_id in expired:
            lease = leases.pop(lease_id)
            lease_ttls.pop(lease_id, None)
            key_ids = leases_by_key[lease.key]
            key_ids.remove(lease_id)
            if not key_ids:
                leases_by_key.pop(lease.key, None)

        for key, key_ids in leases_by_key.items():
            active = [leases[lease_id] for lease_id in key_ids]
            if any(lease.mode == "exclusive" for lease in active) and len(active) > 1:
                raise ValueError("conflicting leases")

        waiters: dict[str, list[_Waiter]] = {}
        max_wait_seq = 0
        for key, raw_queue in raw_waiters.items():
            if not isinstance(key, str) or not key or not isinstance(raw_queue, list):
                raise TypeError("invalid waiter queue")
            owners = set()
            queue = []
            for raw in raw_queue:
                if not isinstance(raw, dict):
                    raise TypeError("invalid waiter")
                owner = raw["owner"]
                mode = raw["mode"]
                seq = raw["seq"]
                self._validate_key_owner("waiter", owner)
                self._validate_mode(mode)
                if not _is_int(seq) or seq < 1:
                    raise ValueError("invalid waiter sequence")
                if owner in owners:
                    raise ValueError("duplicate waiter owner")
                ttl = raw.get("ttl", 1)
                if not _is_int(ttl) or ttl <= 0:
                    raise ValueError("invalid waiter ttl")
                requested_at = raw.get("requested_at", observed_at)
                if not _is_int(requested_at) or requested_at < 0:
                    raise ValueError("invalid waiter timestamp")
                owners.add(owner)
                queue.append(
                    _Waiter(
                        owner=owner,
                        mode=mode,
                        ttl=ttl,
                        seq=seq,
                        requested_at=requested_at,
                    )
                )
                max_wait_seq = max(max_wait_seq, seq)
            if queue:
                waiters[key] = queue

        next_lease_seq = state.get("next_lease_seq", max_lease_seq + 1)
        next_wait_seq = state.get("next_wait_seq", max_wait_seq + 1)
        if not _is_int(next_lease_seq) or next_lease_seq < 1:
            raise ValueError("invalid lease sequence")
        if not _is_int(next_wait_seq) or next_wait_seq < 1:
            raise ValueError("invalid waiter sequence")
        next_lease_seq = max(next_lease_seq, max_lease_seq + 1)
        next_wait_seq = max(next_wait_seq, max_wait_seq + 1)
        if len(leases) > self.max_leases:
            raise ValueError("too many leases")

        return (
            leases,
            leases_by_key,
            lease_ttls,
            waiters,
            next_lease_seq,
            next_wait_seq,
            observed_at,
        )
