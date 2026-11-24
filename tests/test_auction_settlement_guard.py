import copy
import threading

import pytest

import auction_settlement_guard as ag


def test_deposit_withdraw_and_balance():
    guard = ag.AuctionSettlementGuard()
    assert guard.balance("ann") == 0
    out = guard.deposit("ann", 500)
    assert out["player"] == "ann"
    assert out["balance"] == 500
    assert guard.deposit("ann", 100)["balance"] == 600
    assert guard.withdraw("ann", 200)["balance"] == 400
    with pytest.raises(ag.AuctionError) as exc:
        guard.withdraw("ann", 401)
    assert exc.value.code == "AUCTION_INSUFFICIENT_FUNDS"
    assert guard.balance("ann") == 400
    with pytest.raises(ag.AuctionError) as exc:
        guard.deposit("ann", 0)
    assert exc.value.code == "AUCTION_INVALID_AMOUNT"
    with pytest.raises(ag.AuctionError) as exc:
        guard.withdraw("ann", -5)
    assert exc.value.code == "AUCTION_INVALID_AMOUNT"


def test_listing_freezes_guarantee_and_cancel_refunds_once():
    guard = ag.AuctionSettlementGuard(guarantee_rate=0.1)
    guard.deposit("ann", 1000)
    auction = guard.list_item("ann", "sword", 100, 100, now=0)
    assert auction["auction_id"] == "auc-1"
    assert auction["guarantee"] == 10
    assert auction["status"] == "open"
    assert auction["close_at"] == 100
    assert guard.balance("ann") == 990
    assert guard.active(now=50) == ["auc-1"]
    assert guard.active(now=100) == []
    cancelled = guard.cancel("ann", "auc-1")
    assert cancelled["refund"] == 10
    assert guard.balance("ann") == 1000
    with pytest.raises(ag.AuctionError) as exc:
        guard.cancel("ann", "auc-1")
    assert exc.value.code == "AUCTION_CANCELLED"
    assert guard.balance("ann") == 1000


def test_cancel_with_bids_is_refused():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.deposit("bob", 500)
    guard.list_item("ann", "sword", 100, 50, now=0)
    guard.bid("bob", "auc-1", 120, now=1)
    with pytest.raises(ag.AuctionError) as exc:
        guard.cancel("ann", "auc-1")
    assert exc.value.code == "AUCTION_HAS_BIDS"
    assert guard.balance("ann") == 990
    assert guard.auction_state("auc-1")["bid_count"] == 1
    with pytest.raises(ag.AuctionError) as exc:
        guard.cancel("bob", "auc-1")
    assert exc.value.code == "AUCTION_NOT_OWNER"
    with pytest.raises(ag.AuctionError) as exc:
        guard.cancel("ann", "auc-9")
    assert exc.value.code == "AUCTION_UNKNOWN"


def test_bid_floor_and_duplicate_bids():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.deposit("bob", 1000)
    guard.list_item("ann", "sword", 100, 50, now=0)
    with pytest.raises(ag.AuctionError) as exc:
        guard.bid("bob", "auc-1", 99, now=1)
    assert exc.value.code == "AUCTION_BELOW_RESERVE"
    assert guard.bids("auc-1") == []
    out = guard.bid("bob", "auc-1", 100, now=1)
    assert out["count"] == 1
    with pytest.raises(ag.AuctionError) as exc:
        guard.bid("bob", "auc-1", 100, now=2)
    assert exc.value.code == "AUCTION_BID_TOO_LOW"
    with pytest.raises(ag.AuctionError) as exc:
        guard.bid("bob", "auc-1", 90, now=3)
    assert exc.value.code == "AUCTION_BID_TOO_LOW"
    assert len(guard.bids("auc-1")) == 1
    assert guard.balance("bob") == 1000


def test_bid_after_close_is_refused():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.deposit("bob", 1000)
    guard.list_item("ann", "sword", 100, 10, now=0)
    with pytest.raises(ag.AuctionError) as exc:
        guard.bid("bob", "auc-1", 150, now=10)
    assert exc.value.code == "AUCTION_CLOSED"
    assert guard.bids("auc-1") == []
    assert guard.auction_state("auc-1")["close_at"] == 10
    assert guard.balance("bob") == 1000


def test_highest_bid_wins_and_settlement_transfers():
    guard = ag.AuctionSettlementGuard(guarantee_rate=0.1)
    guard.deposit("ann", 1000)
    guard.deposit("bob", 500)
    guard.deposit("cid", 400)
    guard.list_item("ann", "sword", 100, 10, now=0)
    guard.bid("bob", "auc-1", 120, now=1)
    guard.bid("cid", "auc-1", 150, now=2)
    out = guard.settle("auc-1", now=10)
    assert out["winner"] == "cid"
    assert out["price"] == 150
    assert out["status"] == "settled"
    assert guard.balance("cid") == 250
    assert guard.balance("ann") == 1150
    assert guard.balance("bob") == 500
    state = guard.auction_state("auc-1")
    assert state["status"] == "settled"
    assert state["highest_amount"] == 150
    assert state["highest_bidder"] == "cid"


def test_bid_board_is_ordered_by_amount():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.deposit("bob", 1000)
    guard.deposit("cid", 1000)
    guard.list_item("ann", "sword", 100, 100, now=0)
    guard.bid("bob", "auc-1", 120, now=1)
    guard.bid("cid", "auc-1", 250, now=2)
    guard.bid("bob", "auc-1", 300, now=3)
    rows = guard.bids("auc-1")
    assert [row["amount"] for row in rows] == [300, 250, 120]
    assert [row["bidder"] for row in rows] == ["bob", "cid", "bob"]
    out = guard.settle("auc-1", now=100)
    assert out["winner"] == "bob"
    assert out["price"] == 300
    assert guard.balance("bob") == 700


def test_settle_twice_does_not_move_money_again():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.deposit("cid", 400)
    guard.list_item("ann", "sword", 100, 10, now=0)
    guard.bid("cid", "auc-1", 150, now=2)
    first = guard.settle("auc-1", now=10)
    assert first["price"] == 150
    balances = (guard.balance("ann"), guard.balance("cid"))
    with pytest.raises(ag.AuctionError) as exc:
        guard.settle("auc-1", now=20)
    assert exc.value.code == "AUCTION_ALREADY_SETTLED"
    assert (guard.balance("ann"), guard.balance("cid")) == balances
    assert balances == (1150, 250)


def test_settle_without_bids_refunds_guarantee():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 100)
    guard.list_item("ann", "sword", 100, 10, now=0)
    assert guard.balance("ann") == 90
    out = guard.settle("auc-1", now=10)
    assert out["winner"] is None
    assert out["price"] == 0
    assert out["status"] == "settled"
    assert guard.balance("ann") == 100


def test_close_ends_bidding_early():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.deposit("bob", 1000)
    guard.list_item("ann", "sword", 100, 100, now=0)
    closed = guard.close("auc-1", now=5)
    assert closed["status"] == "closed"
    with pytest.raises(ag.AuctionError) as exc:
        guard.bid("bob", "auc-1", 150, now=6)
    assert exc.value.code == "AUCTION_CLOSED"
    out = guard.settle("auc-1")
    assert out["status"] == "settled"
    assert out["winner"] is None
    assert guard.balance("ann") == 1000


def test_settle_with_insufficient_winner_balance_rolls_back():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 100)
    guard.deposit("bob", 150)
    guard.list_item("ann", "sword", 100, 10, now=0)
    guard.bid("bob", "auc-1", 150, now=1)
    guard.withdraw("bob", 149)
    with pytest.raises(ag.AuctionError) as exc:
        guard.settle("auc-1", now=10)
    assert exc.value.code == "AUCTION_INSUFFICIENT_FUNDS"
    assert guard.auction_state("auc-1")["status"] == "open"
    assert guard.balance("ann") == 90
    assert guard.balance("bob") == 1
    guard.deposit("bob", 149)
    out = guard.settle("auc-1", now=10)
    assert out["winner"] == "bob"
    assert guard.balance("bob") == 0
    assert guard.balance("ann") == 250


def test_settle_before_close_is_not_due():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 1000)
    guard.list_item("ann", "sword", 100, 10, now=0)
    with pytest.raises(ag.AuctionError) as exc:
        guard.settle("auc-1", now=9)
    assert exc.value.code == "AUCTION_NOT_DUE"
    with pytest.raises(ag.AuctionError) as exc:
        guard.settle("auc-1")
    assert exc.value.code == "AUCTION_NOT_DUE"
    assert guard.auction_state("auc-1")["status"] == "open"


def test_snapshot_restore_keeps_bids_and_frozen_funds():
    guard = ag.AuctionSettlementGuard()
    for player, amount in (("ann", 1000), ("bob", 800), ("cid", 600)):
        guard.deposit(player, amount)
    guard.list_item("ann", "sword", 200, 100, now=0)
    guard.bid("bob", "auc-1", 220, now=5)
    guard.bid("cid", "auc-1", 260, now=6)
    state = guard.snapshot()
    pristine = copy.deepcopy(state)
    state["balances"]["ann"] = 0
    assert guard.balance("ann") == 980
    restored = ag.AuctionSettlementGuard()
    restored.restore(pristine)
    assert restored.balance("ann") == 980
    assert restored.balance("bob") == 800
    assert restored.bids("auc-1") == guard.bids("auc-1")
    assert restored.auction_state("auc-1") == guard.auction_state("auc-1")
    out = restored.settle("auc-1", now=100)
    assert out["winner"] == "cid"
    assert out["price"] == 260
    assert restored.balance("ann") == 1260
    assert restored.balance("cid") == 340


def test_invalid_snapshot_and_unknown_auction():
    guard = ag.AuctionSettlementGuard()
    guard.deposit("ann", 500)
    bad_states = [
        None,
        {},
        {"version": 2, "balances": {}, "auctions": {}},
        {"version": 1, "balances": "nope", "auctions": {}},
        {"version": 1, "balances": {}, "auctions": {"auc-1": "bad"}},
    ]
    for bad in bad_states:
        with pytest.raises(ag.AuctionError) as exc:
            guard.restore(bad)
        assert exc.value.code == "AUCTION_SNAPSHOT_INVALID"
    assert guard.balance("ann") == 500
    with pytest.raises(ag.AuctionError) as exc:
        guard.bid("ann", "auc-9", 100, now=0)
    assert exc.value.code == "AUCTION_UNKNOWN"
    with pytest.raises(ag.AuctionError) as exc:
        guard.auction_state("auc-9")
    assert exc.value.code == "AUCTION_UNKNOWN"
    with pytest.raises(ag.AuctionError) as exc:
        guard.settle("auc-9", now=10)
    assert exc.value.code == "AUCTION_UNKNOWN"
    with pytest.raises(ag.AuctionError) as exc:
        guard.close("auc-9", now=0)
    assert exc.value.code == "AUCTION_UNKNOWN"
    with pytest.raises(ag.AuctionError) as exc:
        guard.bids("auc-9")
    assert exc.value.code == "AUCTION_UNKNOWN"


def test_concurrent_bids_keep_one_winner_and_consistent_board():
    guard = ag.AuctionSettlementGuard()
    for index in range(8):
        guard.deposit(f"p{index}", 1000)
    guard.deposit("seller", 100)
    guard.list_item("seller", "sword", 100, 100, now=0)
    barrier = threading.Barrier(8)
    accepted = []
    rejected = []

    def worker(index):
        barrier.wait()
        try:
            accepted.append(guard.bid(f"p{index}", "auc-1", 100 + index,
                                      now=1))
        except ag.AuctionError as exc:
            rejected.append(exc.code)

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(accepted) + len(rejected) == 8
    assert set(rejected) <= {"AUCTION_BID_TOO_LOW"}
    rows = guard.bids("auc-1")
    assert len(rows) == guard.auction_state("auc-1")["bid_count"]
    assert rows[0]["amount"] == 107
    for higher, lower in zip(rows, rows[1:]):
        assert higher["amount"] > lower["amount"]
    out = guard.settle("auc-1", now=100)
    assert out["winner"] == "p7"
    assert out["price"] == 107
