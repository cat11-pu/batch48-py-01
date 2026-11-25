"""Auction listing, bidding and settlement with escrowed guarantees.

Every auction freezes its seller guarantee exactly once; settlement moves the
winning price and refunds the guarantee in a single atomic step.
"""

from __future__ import annotations

import copy
import threading


class AuctionError(ValueError):
    """Auction failure carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class AuctionSettlementGuard:
    """Tracks balances, listings, bids and settlement for one shard."""

    def __init__(self, *, guarantee_rate: float = 0.1,
                 extension_seconds: int = 30, max_duration: int = 86400) -> None:
        self.guarantee_rate = float(guarantee_rate)
        self.extension_seconds = int(extension_seconds)
        self.max_duration = int(max_duration)
        self._lock = threading.RLock()
        self._balances: dict[str, int] = {}
        self._auctions: dict[str, dict] = {}
        self._next_auction = 1

    def deposit(self, player: str, amount: int) -> dict:
        self._require_positive(amount)
        with self._lock:
            balance = self._balances.get(player, 0) + amount
            self._balances[player] = balance
            return {"player": player, "amount": amount, "balance": balance}

    def withdraw(self, player: str, amount: int) -> dict:
        self._require_positive(amount)
        with self._lock:
            balance = self._balances.get(player, 0)
            if balance < amount:
                raise AuctionError(
                    "AUCTION_INSUFFICIENT_FUNDS",
                    {"player": player, "balance": balance, "amount": amount},
                )
            balance -= amount
            self._balances[player] = balance
            return {"player": player, "amount": amount, "balance": balance}

    def balance(self, player: str) -> int:
        with self._lock:
            return self._balances.get(player, 0)

    def list_item(self, seller: str, item: str, reserve: int, duration: int,
                  now: int = 0) -> dict:
        if not self._is_int(reserve) or reserve < 0:
            raise AuctionError("AUCTION_INVALID_AMOUNT", reserve)
        if not self._is_int(duration) or duration <= 0:
            raise AuctionError("AUCTION_INVALID_AMOUNT", duration)
        duration = min(duration, self.max_duration)
        guarantee = int(reserve * self.guarantee_rate)
        if guarantee < 0:
            raise AuctionError("AUCTION_INVALID_AMOUNT", reserve)

        with self._lock:
            if self._balances.get(seller, 0) < guarantee:
                raise AuctionError(
                    "AUCTION_INSUFFICIENT_FUNDS",
                    {"player": seller, "required": guarantee},
                )
            auction_id = f"auc-{self._next_auction}"
            self._next_auction += 1
            self._balances[seller] = self._balances.get(seller, 0) - guarantee
            record = {
                "auction_id": auction_id,
                "seller": seller,
                "item": item,
                "reserve": reserve,
                "close_at": now + duration,
                "status": "open",
                "guarantee": guarantee,
                "guarantee_held": True,
                "extended": False,
                "bids": [],
            }
            self._auctions[auction_id] = record
            return {
                "auction_id": auction_id,
                "guarantee": guarantee,
                "status": "open",
                "close_at": record["close_at"],
            }

    def bid(self, bidder: str, auction_id: str, amount: int,
            now: int = 0) -> dict:
        with self._lock:
            record = self._get_auction(auction_id)
            if record["status"] != "open" or now >= record["close_at"]:
                raise AuctionError("AUCTION_CLOSED", auction_id)

            if record["bids"]:
                highest = max(row["amount"] for row in record["bids"])
                if amount <= highest:
                    raise AuctionError(
                        "AUCTION_BID_TOO_LOW",
                        {"auction_id": auction_id, "highest": highest},
                    )
            if amount < record["reserve"]:
                raise AuctionError(
                    "AUCTION_BELOW_RESERVE",
                    {"auction_id": auction_id, "reserve": record["reserve"]},
                )

            if not record["extended"] and record["close_at"] - now <= 1:
                record["close_at"] += self.extension_seconds
                record["extended"] = True

            sequence = max(
                (row["sequence"] for row in record["bids"]), default=0
            ) + 1
            record["bids"].append({
                "bidder": bidder,
                "amount": amount,
                "timestamp": now,
                "sequence": sequence,
            })
            return {
                "auction_id": auction_id,
                "count": len(record["bids"]),
            }

    def close(self, auction_id: str, now: int = 0) -> dict:
        with self._lock:
            record = self._get_auction(auction_id)
            if record["status"] == "open":
                record["status"] = "closed"
            return {"auction_id": auction_id, "status": record["status"]}

    def settle(self, auction_id: str, now: int | None = None) -> dict:
        with self._lock:
            record = self._get_auction(auction_id)
            status = record["status"]
            if status == "settled":
                raise AuctionError("AUCTION_ALREADY_SETTLED", auction_id)
            if status == "cancelled":
                raise AuctionError("AUCTION_CANCELLED", auction_id)

            due = status == "closed" or (
                now is not None and now >= record["close_at"]
            )
            if not due:
                raise AuctionError(
                    "AUCTION_NOT_DUE",
                    {"auction_id": auction_id, "close_at": record["close_at"]},
                )

            winner = None
            price = 0
            ordered = self._ordered_bids(record)
            if ordered and ordered[0]["amount"] >= record["reserve"]:
                winner = ordered[0]["bidder"]
                price = ordered[0]["amount"]
                if self._balances.get(winner, 0) < price:
                    raise AuctionError(
                        "AUCTION_INSUFFICIENT_FUNDS",
                        {"player": winner, "price": price},
                    )

            seller = record["seller"]
            if winner is not None:
                self._balances[winner] = self._balances.get(winner, 0) - price
                self._balances[seller] = self._balances.get(seller, 0) + price
            if record["guarantee_held"]:
                self._balances[seller] = (
                    self._balances.get(seller, 0) + record["guarantee"]
                )
                record["guarantee_held"] = False
            record["status"] = "settled"
            return {
                "auction_id": auction_id,
                "winner": winner,
                "price": price,
                "status": "settled",
            }

    def cancel(self, seller: str, auction_id: str) -> dict:
        with self._lock:
            record = self._get_auction(auction_id)
            if record["seller"] != seller:
                raise AuctionError("AUCTION_NOT_OWNER", auction_id)
            if record["status"] == "cancelled":
                raise AuctionError("AUCTION_CANCELLED", auction_id)
            if record["status"] == "settled":
                raise AuctionError("AUCTION_ALREADY_SETTLED", auction_id)
            if record["bids"]:
                raise AuctionError("AUCTION_HAS_BIDS", auction_id)

            refund = int(record["guarantee"]) if record["guarantee_held"] else 0
            if refund:
                self._balances[seller] = self._balances.get(seller, 0) + refund
            record["guarantee_held"] = False
            record["status"] = "cancelled"
            return {
                "auction_id": auction_id,
                "refund": refund,
                "status": "cancelled",
            }

    def auction_state(self, auction_id: str) -> dict:
        with self._lock:
            record = self._get_auction(auction_id)
            ordered = self._ordered_bids(record)
            highest = ordered[0] if ordered else None
            return {
                "auction_id": auction_id,
                "seller": record["seller"],
                "item": record["item"],
                "reserve": record["reserve"],
                "close_at": record["close_at"],
                "status": record["status"],
                "guarantee": record["guarantee"],
                "bid_count": len(record["bids"]),
                "highest_amount": highest["amount"] if highest else 0,
                "highest_bidder": highest["bidder"] if highest else None,
            }

    def bids(self, auction_id: str) -> list:
        with self._lock:
            record = self._get_auction(auction_id)
            return [
                {
                    "bidder": row["bidder"],
                    "amount": row["amount"],
                    "timestamp": row["timestamp"],
                    "sequence": row["sequence"],
                }
                for row in self._ordered_bids(record)
            ]

    def active(self, now: int = 0) -> list:
        with self._lock:
            return [
                auction_id
                for auction_id, record in self._auctions.items()
                if record["status"] == "open" and now < record["close_at"]
            ]

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": 1,
                "balances": dict(self._balances),
                "auctions": dict(self._auctions),
            })

    def restore(self, state: dict) -> None:
        with self._lock:
            balances, auctions = self._parse_snapshot(state)
            next_auction = 1
            for auction_id in auctions:
                prefix, _, suffix = auction_id.partition("-")
                if prefix == "auc" and suffix.isdigit():
                    next_auction = max(next_auction, int(suffix) + 1)
            while f"auc-{next_auction}" in auctions:
                next_auction += 1
            self._balances = balances
            self._auctions = auctions
            self._next_auction = next_auction

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _require_positive(cls, amount: object) -> None:
        if not cls._is_int(amount) or amount <= 0:
            raise AuctionError("AUCTION_INVALID_AMOUNT", amount)

    def _get_auction(self, auction_id: str) -> dict:
        try:
            return self._auctions[auction_id]
        except (KeyError, TypeError):
            raise AuctionError("AUCTION_UNKNOWN", auction_id) from None

    @staticmethod
    def _ordered_bids(record: dict) -> list:
        return sorted(
            record["bids"],
            key=lambda row: (-row["amount"], row["timestamp"], row["sequence"]),
        )

    @classmethod
    def _parse_snapshot(cls, state: object) -> tuple[dict, dict]:
        if not isinstance(state, dict):
            raise AuctionError("AUCTION_SNAPSHOT_INVALID", "state")
        if not cls._is_int(state.get("version")) or state["version"] != 1:
            raise AuctionError("AUCTION_SNAPSHOT_INVALID", "version")
        raw_balances = state.get("balances")
        raw_auctions = state.get("auctions")
        if not isinstance(raw_balances, dict) or not isinstance(raw_auctions, dict):
            raise AuctionError("AUCTION_SNAPSHOT_INVALID", "sections")

        balances: dict[str, int] = {}
        for player, amount in raw_balances.items():
            if not isinstance(player, str):
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "balances")
            if not cls._is_int(amount) or amount < 0:
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "balances")
            balances[player] = amount

        auctions: dict[str, dict] = {}
        for auction_id, raw in raw_auctions.items():
            if not isinstance(auction_id, str) or not isinstance(raw, dict):
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "auctions")
            if raw.get("auction_id", auction_id) != auction_id:
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "auction_id")
            seller = raw.get("seller")
            item = raw.get("item")
            reserve = raw.get("reserve")
            close_at = raw.get("close_at")
            status = raw.get("status")
            guarantee = raw.get("guarantee")
            extended = raw.get("extended", False)
            raw_bids = raw.get("bids", [])
            if not isinstance(seller, str) or not isinstance(item, str):
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "auction")
            if (
                not cls._is_int(reserve) or reserve < 0
                or not cls._is_int(close_at)
                or status not in {"open", "closed", "settled", "cancelled"}
                or not cls._is_int(guarantee) or guarantee < 0
                or not isinstance(extended, bool)
                or not isinstance(raw_bids, list)
            ):
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "auction")

            bids: list[dict] = []
            for row in raw_bids:
                if not isinstance(row, dict):
                    raise AuctionError("AUCTION_SNAPSHOT_INVALID", "bids")
                bidder = row.get("bidder")
                amount = row.get("amount")
                timestamp = row.get("timestamp")
                sequence = row.get("sequence")
                if (
                    not isinstance(bidder, str)
                    or not cls._is_int(amount)
                    or not cls._is_int(timestamp)
                    or not cls._is_int(sequence)
                    or sequence <= 0
                ):
                    raise AuctionError("AUCTION_SNAPSHOT_INVALID", "bids")
                bids.append({
                    "bidder": bidder,
                    "amount": amount,
                    "timestamp": timestamp,
                    "sequence": sequence,
                })
            if len({row["sequence"] for row in bids}) != len(bids):
                raise AuctionError("AUCTION_SNAPSHOT_INVALID", "bids")

            held = status in {"open", "closed"}
            if "guarantee_held" in raw:
                if not isinstance(raw["guarantee_held"], bool):
                    raise AuctionError("AUCTION_SNAPSHOT_INVALID", "guarantee")
                if raw["guarantee_held"] != held:
                    raise AuctionError("AUCTION_SNAPSHOT_INVALID", "guarantee")

            auctions[auction_id] = {
                "auction_id": auction_id,
                "seller": seller,
                "item": item,
                "reserve": reserve,
                "close_at": close_at,
                "status": status,
                "guarantee": guarantee,
                "guarantee_held": held,
                "extended": extended,
                "bids": bids,
            }
        return balances, auctions
