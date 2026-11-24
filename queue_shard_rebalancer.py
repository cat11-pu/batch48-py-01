"""Match queue shard splitting, merging and ticket migration."""

from __future__ import annotations

import threading

STATS_KEYS = (
    "added",
    "removed",
    "splits",
    "merges",
    "migrations",
    "rebalances",
    "matches_started",
    "rejected",
)


class ShardError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class QueueShardRebalancer:
    """Keep match queue tickets in shards without losing or duplicating them."""

    def __init__(self, *, max_shards: int = 6, shard_capacity: int = 4,
                 clock=None) -> None:
        self.max_shards = max_shards
        self.shard_capacity = shard_capacity
        self._clock = clock
        self._lock = threading.RLock()

    def add_ticket(self, player: str, *, score: int, at: int = 0) -> dict:
        raise NotImplementedError

    def remove_ticket(self, player: str) -> dict:
        raise NotImplementedError

    def shard_of(self, player: str) -> str:
        raise NotImplementedError

    def ticket_info(self, player: str) -> dict:
        raise NotImplementedError

    def locate(self, player: str) -> dict:
        raise NotImplementedError

    def shard_state(self, shard_id: str) -> dict:
        raise NotImplementedError

    def split_shard(self, shard_id: str) -> dict:
        raise NotImplementedError

    def merge_shards(self, left_id: str, right_id: str) -> dict:
        raise NotImplementedError

    def migrate_ticket(self, player: str, to_shard: str) -> dict:
        raise NotImplementedError

    def begin_match(self, shard_id: str) -> dict:
        raise NotImplementedError

    def end_match(self, shard_id: str) -> dict:
        raise NotImplementedError

    def rebalance(self) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state) -> None:
        raise NotImplementedError
