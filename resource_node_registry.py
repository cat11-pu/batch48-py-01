"""Resource node claims, harvest settlement and respawn anchors.

A node can have a single active claim; harvest draws down the stored amount in
one atomic step and depletion starts the respawn clock once.
"""

from __future__ import annotations

import re
import threading
from collections import OrderedDict


class NodeError(ValueError):
    """Gathering failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_CLAIM_ID_RE = re.compile(r"^clm-(\d+)$")


class NodeRegistry:
    """Tracks map nodes, claims, harvest output and respawn timing."""

    # Released/expired claims are remembered only so that later operations can
    # distinguish CLAIM_INVALID/CLAIM_EXPIRED from an unknown claim id.
    _MAX_CLAIM_HISTORY = 10_000

    def __init__(self, *, claim_lease: int = 30, default_respawn: int = 60) -> None:
        self.claim_lease = int(claim_lease)
        self.default_respawn = int(default_respawn)
        self._lock = threading.RLock()
        self._nodes: dict[str, dict] = {}
        self._claims: dict[str, dict] = {}
        self._inventories: dict[str, int] = {}
        self._claim_history: OrderedDict[str, str] = OrderedDict()
        self._next_claim_id = 1

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _require_node(self, node_id: str) -> dict:
        node = self._nodes.get(node_id)
        if node is None:
            raise NodeError("NODE_UNKNOWN", node_id)
        return node

    @staticmethod
    def _node_view(node: dict) -> dict:
        return {
            "node_id": node["node_id"],
            "capacity": node["capacity"],
            "remaining": node["remaining"],
            "respawn_seconds": node["respawn_seconds"],
            "respawn_at": node["respawn_at"],
            "depleted": node["depleted"],
            "occupier": node["occupier"],
        }

    @staticmethod
    def _claim_view(claim: dict, node: dict) -> dict:
        return {
            "claim_id": claim["claim_id"],
            "node_id": claim["node_id"],
            "player": claim["player"],
            "remaining": node["remaining"],
            "expires_at": claim["expires_at"],
            "produced": claim["produced"],
        }

    def _remember_claim(self, claim_id: str, status: str) -> None:
        self._claim_history[claim_id] = status
        self._claim_history.move_to_end(claim_id)
        while len(self._claim_history) > self._MAX_CLAIM_HISTORY:
            self._claim_history.popitem(last=False)

    def _claim_status(self, claim_id: str) -> str | None:
        return self._claim_history.get(claim_id)

    def _expire_claim(self, claim_id: str) -> None:
        claim = self._claims.pop(claim_id, None)
        if claim is None:
            return
        node = self._nodes.get(claim["node_id"])
        if node is not None and node["occupier"] == claim_id:
            node["occupier"] = None
        self._remember_claim(claim_id, "expired")

    def _invalidate_claim(self, claim_id: str) -> None:
        claim = self._claims.pop(claim_id, None)
        if claim is not None:
            node = self._nodes.get(claim["node_id"])
            if node is not None and node["occupier"] == claim_id:
                node["occupier"] = None
        self._remember_claim(claim_id, "invalid")

    def _touch(self, node: dict, now: int) -> None:
        """Apply time-based respawn and lease reclamation to one node."""
        if (node["depleted"] and node["respawn_at"] is not None
                and now >= node["respawn_at"]):
            node["remaining"] = node["capacity"]
            node["respawn_at"] = None
            node["depleted"] = False

        claim_id = node["occupier"]
        if claim_id is None:
            return
        claim = self._claims.get(claim_id)
        if claim is None:
            node["occupier"] = None
        elif now >= claim["expires_at"]:
            self._expire_claim(claim_id)

    def _new_claim_id(self) -> str:
        while True:
            claim_id = f"clm-{self._next_claim_id}"
            self._next_claim_id += 1
            if (claim_id not in self._claims
                    and claim_id not in self._claim_history):
                return claim_id

    def register_node(self, node_id: str, *, capacity: int = 100,
                      respawn_seconds: int | None = None, now: int = 0) -> dict:
        with self._lock:
            if node_id in self._nodes:
                raise NodeError("NODE_DUPLICATE", node_id)
            if not self._is_int(capacity) or capacity <= 0:
                raise NodeError("NODE_INVALID", "capacity")

            if respawn_seconds is None:
                respawn_seconds = self.default_respawn
            if not self._is_int(respawn_seconds) or respawn_seconds < 0:
                raise NodeError("NODE_INVALID", "respawn_seconds")

            node = {
                "node_id": node_id,
                "capacity": capacity,
                "remaining": capacity,
                "respawn_seconds": respawn_seconds,
                "respawn_at": None,
                "depleted": False,
                "occupier": None,
            }
            self._nodes[node_id] = node
            return self._node_view(node)

    def remove_node(self, node_id: str) -> dict:
        with self._lock:
            node = self._require_node(node_id)
            claim_id = node["occupier"]
            if claim_id is not None:
                self._invalidate_claim(claim_id)
            del self._nodes[node_id]
            return {"node_id": node_id, "removed": True}

    def node(self, node_id: str) -> dict:
        with self._lock:
            return self._node_view(self._require_node(node_id))

    def claim(self, node_id: str, player: str, now: int = 0) -> dict:
        with self._lock:
            node = self._require_node(node_id)
            self._touch(node, now)

            if node["depleted"] or node["remaining"] <= 0:
                raise NodeError("NODE_DEPLETED", node_id)
            if node["occupier"] is not None:
                raise NodeError("NODE_OCCUPIED", node_id)

            claim_id = self._new_claim_id()
            claim = {
                "claim_id": claim_id,
                "node_id": node_id,
                "player": player,
                "expires_at": now + self.claim_lease,
                "produced": 0,
            }
            self._claims[claim_id] = claim
            node["occupier"] = claim_id
            return self._claim_view(claim, node)

    def harvest(self, claim_id: str, amount: int, now: int = 0) -> dict:
        if not self._is_int(amount) or amount <= 0:
            raise NodeError("CLAIM_INVALID_AMOUNT", amount)

        with self._lock:
            claim = self._claims.get(claim_id)
            if claim is None:
                status = self._claim_status(claim_id)
                if status == "expired":
                    raise NodeError("CLAIM_EXPIRED", claim_id)
                if status in ("released", "invalid"):
                    raise NodeError("CLAIM_INVALID", claim_id)
                raise NodeError("CLAIM_UNKNOWN", claim_id)

            node = self._nodes.get(claim["node_id"])
            if node is None:
                self._invalidate_claim(claim_id)
                raise NodeError("CLAIM_INVALID", claim_id)

            self._touch(node, now)

            # Touching may have reclaimed this claim's lease.
            if claim_id not in self._claims:
                status = self._claim_status(claim_id)
                if status == "expired":
                    raise NodeError("CLAIM_EXPIRED", claim_id)
                raise NodeError("CLAIM_INVALID", claim_id)

            if node["remaining"] <= 0:
                raise NodeError("CLAIM_EMPTY", claim_id)

            harvested = min(amount, node["remaining"])
            node["remaining"] -= harvested
            if node["remaining"] == 0:
                node["depleted"] = True
                node["respawn_at"] = now + node["respawn_seconds"]

            claim["produced"] += harvested
            self._inventories[claim["player"]] = (
                self._inventories.get(claim["player"], 0) + harvested
            )
            return {
                "claim_id": claim_id,
                "harvested": harvested,
                "remaining": node["remaining"],
                "depleted": node["depleted"],
            }

    def release(self, claim_id: str) -> dict:
        with self._lock:
            claim = self._claims.pop(claim_id, None)
            if claim is None:
                status = self._claim_status(claim_id)
                if status == "expired":
                    raise NodeError("CLAIM_EXPIRED", claim_id)
                if status in ("released", "invalid"):
                    raise NodeError("CLAIM_INVALID", claim_id)
                raise NodeError("CLAIM_UNKNOWN", claim_id)

            node = self._nodes.get(claim["node_id"])
            if node is not None and node["occupier"] == claim_id:
                node["occupier"] = None
            self._remember_claim(claim_id, "released")
            return {"claim_id": claim_id, "released": True}

    def respawn(self, node_id: str, now: int = 0) -> dict:
        with self._lock:
            node = self._require_node(node_id)
            self._touch(node, now)
            node["remaining"] = node["capacity"]
            node["respawn_at"] = None
            node["depleted"] = False
            return self._node_view(node)

    def available(self, node_id: str, now: int = 0) -> bool:
        with self._lock:
            node = self._require_node(node_id)
            self._touch(node, now)
            return bool(
                not node["depleted"]
                and node["remaining"] > 0
                and node["occupier"] is None
            )

    def inventory(self, player: str) -> int:
        with self._lock:
            return self._inventories.get(player, 0)

    def claims(self, player: str | None = None) -> list:
        with self._lock:
            result = []
            for claim in self._claims.values():
                if player is not None and claim["player"] != player:
                    continue
                node = self._nodes.get(claim["node_id"])
                if node is None:
                    continue
                result.append(self._claim_view(claim, node))
            return result

    def snapshot(self) -> dict:
        with self._lock:
            nodes = {
                node_id: self._node_view(node)
                for node_id, node in self._nodes.items()
            }
            claims = {}
            for claim_id, claim in self._claims.items():
                node = self._nodes.get(claim["node_id"])
                if node is None:
                    continue
                claims[claim_id] = self._claim_view(claim, node)
            return {
                "version": 1,
                "nodes": nodes,
                "claims": claims,
                "inventories": dict(self._inventories),
            }

    @staticmethod
    def _claim_suffix(claim_id: object) -> int | None:
        if not isinstance(claim_id, str):
            return None
        match = _CLAIM_ID_RE.match(claim_id)
        return int(match.group(1)) if match else None

    def _validate_snapshot(self, state: object) -> tuple[dict, dict, dict, int]:
        def invalid(detail: object) -> NodeError:
            return NodeError("NODE_SNAPSHOT_INVALID", detail)

        if not isinstance(state, dict):
            raise invalid("state")
        version = state.get("version")
        if not self._is_int(version) or version != 1:
            raise invalid("version")

        raw_nodes = state.get("nodes")
        raw_claims = state.get("claims")
        raw_inventories = state.get("inventories")
        if (not isinstance(raw_nodes, dict)
                or not isinstance(raw_claims, dict)
                or not isinstance(raw_inventories, dict)):
            raise invalid("sections")

        nodes: dict = {}
        for node_id, record in raw_nodes.items():
            if not isinstance(record, dict):
                raise invalid("node")
            if "node_id" in record and record["node_id"] != node_id:
                raise invalid("node_id")

            capacity = record.get("capacity")
            remaining = record.get("remaining")
            respawn_seconds = record.get("respawn_seconds")
            respawn_at = record.get("respawn_at")
            depleted = record.get("depleted")
            occupier = record.get("occupier")

            if not self._is_int(capacity) or capacity <= 0:
                raise invalid("capacity")
            if (not self._is_int(remaining)
                    or remaining < 0 or remaining > capacity):
                raise invalid("remaining")
            if not self._is_int(respawn_seconds) or respawn_seconds < 0:
                raise invalid("respawn_seconds")
            if respawn_at is not None and not self._is_int(respawn_at):
                raise invalid("respawn_at")
            if not isinstance(depleted, bool) or depleted != (remaining == 0):
                raise invalid("depleted")
            if depleted and respawn_at is None:
                raise invalid("respawn_at")
            if not depleted and respawn_at is not None:
                raise invalid("respawn_at")
            if occupier is not None and not isinstance(occupier, str):
                raise invalid("occupier")

            nodes[node_id] = {
                "node_id": node_id,
                "capacity": capacity,
                "remaining": remaining,
                "respawn_seconds": respawn_seconds,
                "respawn_at": respawn_at,
                "depleted": depleted,
                "occupier": occupier,
            }

        claims: dict = {}
        for claim_id, record in raw_claims.items():
            if not isinstance(record, dict):
                raise invalid("claim")
            if "claim_id" in record and record["claim_id"] != claim_id:
                raise invalid("claim_id")

            node_id = record.get("node_id")
            player = record.get("player")
            expires_at = record.get("expires_at")
            produced = record.get("produced", 0)
            if node_id not in nodes:
                raise invalid("claim_node")
            if not isinstance(player, str):
                raise invalid("player")
            if not self._is_int(expires_at):
                raise invalid("expires_at")
            if not self._is_int(produced) or produced < 0:
                raise invalid("produced")
            if nodes[node_id]["occupier"] != claim_id:
                raise invalid("occupier")

            claims[claim_id] = {
                "claim_id": claim_id,
                "node_id": node_id,
                "player": player,
                "expires_at": expires_at,
                "produced": produced,
            }

        for node in nodes.values():
            occupier = node["occupier"]
            if occupier is None:
                continue
            claim = claims.get(occupier)
            if claim is None or claim["node_id"] != node["node_id"]:
                raise invalid("occupier")

        inventories: dict = {}
        for player, amount in raw_inventories.items():
            if not self._is_int(amount) or amount < 0:
                raise invalid("inventory")
            inventories[player] = amount

        next_claim_id = 1
        for claim_id in claims:
            suffix = self._claim_suffix(claim_id)
            if suffix is not None and suffix >= next_claim_id:
                next_claim_id = suffix + 1
        while f"clm-{next_claim_id}" in claims:
            next_claim_id += 1

        return nodes, claims, inventories, next_claim_id

    def restore(self, state: dict) -> None:
        try:
            validated = self._validate_snapshot(state)
        except NodeError:
            raise
        except Exception as exc:
            raise NodeError("NODE_SNAPSHOT_INVALID", str(exc)) from None
        nodes, claims, inventories, next_claim_id = validated
        with self._lock:
            self._nodes = nodes
            self._claims = claims
            self._inventories = inventories
            self._claim_history = OrderedDict()
            self._next_claim_id = next_claim_id
