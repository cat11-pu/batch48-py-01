"""Resource node claims, harvest settlement and respawn anchors.

A node can have a single active claim; harvest draws down the stored amount in
one atomic step and depletion starts the respawn clock once.
"""

from __future__ import annotations

import threading


class NodeError(ValueError):
    """Gathering failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class NodeRegistry:
    """Tracks map nodes, claims, harvest output and respawn timing."""

    def __init__(self, *, claim_lease: int = 30, default_respawn: int = 60) -> None:
        self.claim_lease = int(claim_lease)
        self.default_respawn = int(default_respawn)
        self._lock = threading.RLock()

    def register_node(self, node_id: str, *, capacity: int = 100,
                      respawn_seconds: int | None = None, now: int = 0) -> dict:
        raise NotImplementedError

    def remove_node(self, node_id: str) -> dict:
        raise NotImplementedError

    def node(self, node_id: str) -> dict:
        raise NotImplementedError

    def claim(self, node_id: str, player: str, now: int = 0) -> dict:
        raise NotImplementedError

    def harvest(self, claim_id: str, amount: int, now: int = 0) -> dict:
        raise NotImplementedError

    def release(self, claim_id: str) -> dict:
        raise NotImplementedError

    def respawn(self, node_id: str, now: int = 0) -> dict:
        raise NotImplementedError

    def available(self, node_id: str, now: int = 0) -> bool:
        raise NotImplementedError

    def inventory(self, player: str) -> int:
        raise NotImplementedError

    def claims(self, player: str | None = None) -> list:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
