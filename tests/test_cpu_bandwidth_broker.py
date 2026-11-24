import threading

import pytest

import cpu_bandwidth_broker as cb


def make(**kwargs):
    return cb.CpuBandwidthBroker(**kwargs)


def test_set_quota_reports_epoch_and_updates_in_place():
    broker = make(global_shares=400)
    out = broker.set_quota("g1", 100, 100)
    assert out == {"group": "g1", "quota": 100, "period": 100, "epoch": 1}
    again = broker.set_quota("g1", 120, 100)
    assert again["epoch"] == 2
    assert again["quota"] == 120
    assert broker.set_quota("g2", 100, 200)["epoch"] == 1


def test_quota_and_period_validation():
    broker = make()
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("g1", 5, 100)
    assert exc.value.code == "CPU_QUOTA_TOO_SMALL"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("g1", 105, 100)
    assert exc.value.code == "CPU_QUOTA_INVALID"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("g1", "100", 100)
    assert exc.value.code == "CPU_QUOTA_INVALID"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("g1", 100, 0)
    assert exc.value.code == "CPU_PERIOD_INVALID"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("g1", 100, 1001)
    assert exc.value.code == "CPU_PERIOD_INVALID"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("", 100, 100)
    assert exc.value.code == "CPU_GROUP_INVALID"


def test_group_cap_and_global_commitment_are_enforced():
    broker = make(global_shares=200, max_groups=2)
    broker.set_quota("g1", 30, 100)
    broker.set_quota("g2", 30, 100)
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.set_quota("g3", 30, 100)
    assert exc.value.code == "CPU_GROUP_LIMIT"

    tight = make(global_shares=100)
    tight.set_quota("g1", 60, 100)
    with pytest.raises(cb.CpuBrokerError) as exc:
        tight.set_quota("g2", 60, 100)
    assert exc.value.code == "CPU_QUOTA_OVERCOMMIT"


def test_borrow_creates_loan_expiring_at_the_period_edge():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    loan = broker.borrow("g1", 40, 250)
    assert loan["loan_id"] == "loan-1"
    assert loan["group"] == "g1"
    assert loan["shares"] == 40
    assert loan["start"] == 250
    assert loan["expire"] == 300
    assert loan["status"] == "open"
    assert broker.global_free(250) == 260


def test_borrow_cannot_cross_the_global_remainder():
    broker = make(global_shares=200)
    broker.set_quota("g1", 100, 100)
    broker.set_quota("g2", 50, 100)
    assert broker.global_free(0) == 50
    assert broker.borrow("g1", 50, 10)["shares"] == 50
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.borrow("g2", 10, 20)
    assert exc.value.code == "CPU_GLOBAL_EXHAUSTED"


def test_borrow_validates_granularity_and_per_group_ceiling():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    for bad in (5, 15, -10, "30"):
        with pytest.raises(cb.CpuBrokerError) as exc:
            broker.borrow("g1", bad, 10)
        assert exc.value.code == "CPU_SHARES_INVALID"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.borrow("nope", 30, 10)
    assert exc.value.code == "CPU_GROUP_UNKNOWN"
    assert broker.borrow("g1", 100, 10)["shares"] == 100
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.borrow("g1", 10, 20)
    assert exc.value.code == "CPU_LOAN_LIMIT"


def test_returned_shares_go_back_to_the_borrowers_own_group():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    broker.set_quota("g2", 100, 100)
    broker.borrow("g1", 60, 10)
    broker.borrow("g2", 50, 20)
    assert broker.global_free(20) == 90

    out = broker.return_loan("loan-2", now=30)
    assert out == {"loan_id": "loan-2", "group": "g2", "shares": 50, "status": "returned"}
    assert broker.global_free(30) == 140
    assert broker.loan_status("loan-1")["status"] == "open"
    assert broker.loan_status("loan-2")["group"] == "g2"
    assert broker.loan_status("loan-2")["status"] == "returned"


def test_return_loan_rejects_unknown_and_closed_loans():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    broker.borrow("g1", 30, 10)
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.return_loan("loan-99")
    assert exc.value.code == "CPU_LOAN_UNKNOWN"
    broker.return_loan("loan-1")
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.return_loan("loan-1")
    assert exc.value.code == "CPU_LOAN_CLOSED"


def test_expired_loans_are_reclaimed_at_the_period_boundary():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    broker.borrow("g1", 100, 50)
    assert broker.global_free(99) == 200
    assert broker.global_free(100) == 300
    assert broker.loan_status("loan-1")["status"] == "expired"
    assert broker.reclaim("g1", 100) == []


def test_reclaim_walks_loans_by_expiry_not_creation_order():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    broker.borrow("g1", 40, 120)
    broker.borrow("g1", 30, 20)
    rows = broker.reclaim("g1", 1000)
    assert [row["loan_id"] for row in rows] == ["loan-2", "loan-1"]
    assert [row["status"] for row in rows] == ["expired", "expired"]
    assert broker.reclaim("g1", 1000) == []


def test_charge_cannot_exceed_quota_plus_open_loan():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 1000)
    broker.borrow("g1", 50, 10)
    first = broker.charge("g1", 120, 100)
    assert first["used"] == 120
    assert first["borrowed"] == 50
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.charge("g1", 40, 200)
    assert exc.value.code == "CPU_USAGE_EXCEEDED"
    assert broker.usage("g1", (0, 1000))["used"] == 120


def test_usage_windows_stay_inside_a_single_period():
    broker = make(global_shares=400)
    broker.set_quota("g1", 200, 1000)
    broker.charge("g1", 50, 100)
    broker.charge("g1", 30, 300)
    whole = broker.usage("g1", (0, 1000))
    assert whole["group"] == "g1"
    assert whole["start"] == 0
    assert whole["end"] == 1000
    assert whole["used"] == 80
    assert whole["quota"] == 200
    assert broker.usage("g1", (200, 1000))["used"] == 30
    for bad in ((900, 1100), (300, 200), (1000, 1000)):
        with pytest.raises(cb.CpuBrokerError) as exc:
            broker.usage("g1", bad)
        assert exc.value.code == "CPU_WINDOW_INVALID"
    with pytest.raises(cb.CpuBrokerError) as exc:
        broker.charge("g1", 0, 400)
    assert exc.value.code == "CPU_SHARES_INVALID"


def test_snapshot_restore_keeps_quotas_loans_and_charges():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    broker.set_quota("g2", 100, 100)
    broker.borrow("g1", 60, 10)
    broker.charge("g1", 40, 20)
    state = broker.snapshot()
    assert state["version"] == 1
    assert state["groups"]["g1"]["quota"] == 100
    state["loans"].clear()
    assert broker.global_free(20) == 140

    restored = make(global_shares=400)
    restored.restore(broker.snapshot())
    assert restored.global_free(20) == 140
    assert restored.loan_status("loan-1")["status"] == "open"
    assert restored.usage("g1", (0, 100))["used"] == 40
    assert restored.stats() == {"groups": 2, "open_loans": 1, "borrowed": 60,
                                "used": 40, "returned": 0, "expired": 0}
    assert restored.borrow("g1", 40, 30)["shares"] == 40
    with pytest.raises(cb.CpuBrokerError) as exc:
        restored.borrow("g1", 10, 40)
    assert exc.value.code == "CPU_LOAN_LIMIT"
    assert restored.global_free(100) == 200
    assert restored.stats()["expired"] == 2


def test_restore_rejects_corrupt_state_and_keeps_live_ledger():
    broker = make(global_shares=400)
    broker.set_quota("g1", 100, 100)
    broker.borrow("g1", 40, 10)
    for bad in (None, {}, {"version": 2, "groups": {}, "loans": [], "charges": []},
                {"version": 1, "groups": [], "loans": [], "charges": []},
                {"version": 1, "groups": {}, "loans": "x", "charges": []}):
        with pytest.raises(cb.CpuBrokerError) as exc:
            broker.restore(bad)
        assert exc.value.code == "CPU_SNAPSHOT_INVALID"
    assert broker.global_free(10) == 260
    assert broker.loan_status("loan-1")["status"] == "open"


def test_concurrent_borrowers_never_oversell_the_remainder():
    broker = make(global_shares=300, max_groups=8)
    for index in range(1, 5):
        broker.set_quota(f"g{index}", 50, 1000)

    loans = []
    errors = []
    lock = threading.Lock()

    def worker(index):
        try:
            loan = broker.borrow(f"g{index % 4 + 1}", 30, 10 + index)
            with lock:
                loans.append(loan)
        except cb.CpuBrokerError as exc:
            with lock:
                errors.append(exc.code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(loans) == 3
    assert sum(loan["shares"] for loan in loans) == 90
    assert len(errors) == 7
    assert set(errors) <= {"CPU_GLOBAL_EXHAUSTED", "CPU_LOAN_LIMIT"}
    assert broker.global_free(60) == 10
