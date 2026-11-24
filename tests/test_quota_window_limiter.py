import copy
import threading

import pytest

import quota_window_limiter as ql


def make(**kwargs):
    base = {
        "window_seconds": 10,
        "key_limit": 100,
        "tenant_limit": 200,
        "global_limit": 400,
        "lease_ttl": 5.0,
        "max_leases": 50,
    }
    base.update(kwargs)
    return ql.QuotaWindowLimiter(**base)


def test_reserve_deducts_all_three_layers_and_returns_lease_id():
    limiter = make()
    lease_id = limiter.reserve("key-a", "tenant-a", cost=30, now=0.5)
    assert isinstance(lease_id, str) and lease_id
    assert limiter.remaining("key-a", "tenant-a", now=0.5) == {
        "key": 70, "tenant": 170, "global": 370,
    }
    assert limiter.lease_count() == 1
    assert limiter.stats()["reserved"] == 1


def test_key_layer_exhaustion_reports_key_code():
    limiter = make(key_limit=5)
    limiter.reserve("key-a", "tenant-a", cost=5, now=0.5)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.reserve("key-a", "tenant-a", cost=1, now=0.6)
    assert exc.value.code == "QUOTA_EXCEEDED_KEY"
    assert limiter.remaining("key-a", "tenant-a", now=0.6)["key"] == 0
    assert limiter.stats()["denied"] == 1


def test_tenant_layer_exhaustion_reported_before_global():
    limiter = make(tenant_limit=6, key_limit=100, global_limit=400)
    limiter.reserve("key-a", "tenant-a", cost=6, now=0.5)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.reserve("key-b", "tenant-a", cost=1, now=0.6)
    assert exc.value.code == "QUOTA_EXCEEDED_TENANT"
    assert limiter.remaining("key-b", "tenant-a", now=0.6)["tenant"] == 0


def test_global_layer_exhaustion_reported_across_tenants():
    limiter = make(global_limit=7, tenant_limit=100, key_limit=100)
    limiter.reserve("key-a", "tenant-a", cost=7, now=0.5)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.reserve("key-b", "tenant-b", cost=1, now=0.6)
    assert exc.value.code == "QUOTA_EXCEEDED_GLOBAL"
    assert limiter.remaining("key-b", "tenant-b", now=0.6)["global"] == 0


def test_rejected_reservation_leaves_no_partial_counting():
    limiter = make(global_limit=5, tenant_limit=100, key_limit=100)
    limiter.reserve("key-a", "tenant-a", cost=5, now=0.5)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.reserve("key-b", "tenant-b", cost=3, now=0.6)
    assert exc.value.code == "QUOTA_EXCEEDED_GLOBAL"
    assert limiter.remaining("key-b", "tenant-b", now=0.6) == {
        "key": 100, "tenant": 100, "global": 0,
    }
    assert limiter.remaining("key-a", "tenant-a", now=0.6)["key"] == 95
    assert limiter.lease_count() == 1


def test_previous_window_usage_decays_with_remaining_time():
    limiter = make(key_limit=100, tenant_limit=1000, global_limit=1000)
    limiter.reserve("key-a", "tenant-a", cost=30, now=0.5)
    assert limiter.remaining("key-a", "tenant-a", now=5.0)["key"] == 70
    assert limiter.remaining("key-a", "tenant-a", now=10.0)["key"] == 70
    assert limiter.remaining("key-a", "tenant-a", now=15.0)["key"] == 85
    assert limiter.remaining("key-a", "tenant-a", now=20.0)["key"] == 100


def test_settle_clears_lease_and_keeps_usage():
    limiter = make(key_limit=100, tenant_limit=1000, global_limit=1000)
    lease_id = limiter.reserve("key-a", "tenant-a", cost=20, now=0.5)
    before = limiter.remaining("key-a", "tenant-a", now=0.5)
    summary = limiter.settle(lease_id)
    assert summary == {"key": "key-a", "tenant": "tenant-a", "cost": 20}
    assert limiter.remaining("key-a", "tenant-a", now=0.5) == before
    assert limiter.lease_count() == 0
    assert limiter.stats()["settled"] == 1


def test_refund_returns_quota_to_original_window():
    limiter = make(key_limit=100, tenant_limit=1000, global_limit=1000)
    lease_id = limiter.reserve("key-a", "tenant-a", cost=25, now=0.5)
    assert limiter.refund(lease_id) == 25
    assert limiter.remaining("key-a", "tenant-a", now=0.5) == {
        "key": 100, "tenant": 1000, "global": 1000,
    }
    assert limiter.lease_count() == 0
    assert limiter.stats()["refunded"] == 1


def test_unknown_or_finished_leases_are_rejected():
    limiter = make(key_limit=100, tenant_limit=1000, global_limit=1000)
    lease_id = limiter.reserve("key-a", "tenant-a", cost=10, now=0.5)
    limiter.settle(lease_id)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.settle("lease-does-not-exist")
    assert exc.value.code == "QUOTA_UNKNOWN_LEASE"
    with pytest.raises(ql.QuotaError) as exc:
        limiter.refund(lease_id)
    assert exc.value.code == "QUOTA_UNKNOWN_LEASE"
    with pytest.raises(ql.QuotaError) as exc:
        limiter.refund("lease-does-not-exist")
    assert exc.value.code == "QUOTA_UNKNOWN_LEASE"
    assert limiter.remaining("key-a", "tenant-a", now=0.5)["key"] == 90


def test_expire_leases_reclaims_only_due_reservations():
    limiter = make(key_limit=100, tenant_limit=1000, global_limit=1000, lease_ttl=5.0)
    limiter.reserve("key-a", "tenant-a", cost=10, now=0.5)
    limiter.reserve("key-b", "tenant-b", cost=20, now=3.0)
    assert limiter.expire_leases(now=4.0) == 0
    assert limiter.expire_leases(now=5.5) == 1
    assert limiter.remaining("key-a", "tenant-a", now=5.5)["key"] == 80
    assert limiter.lease_count() == 1
    assert limiter.expire_leases(now=8.0) == 1
    assert limiter.remaining("key-b", "tenant-b", now=8.0)["key"] == 100
    assert limiter.stats()["leaked"] == 2


def test_lease_ceiling_rejects_new_reservations():
    limiter = make(key_limit=100, tenant_limit=1000, global_limit=1000, max_leases=3)
    for index in range(3):
        limiter.reserve("key-%d" % index, "tenant-a", cost=1, now=0.5)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.reserve("key-9", "tenant-a", cost=1, now=0.6)
    assert exc.value.code == "QUOTA_LEASE_LIMIT"
    assert limiter.lease_count() == 3


def test_invalid_arguments_are_rejected():
    with pytest.raises(ql.QuotaError) as exc:
        make(window_seconds=0)
    assert exc.value.code == "QUOTA_INVALID_ARGUMENT"
    with pytest.raises(ql.QuotaError) as exc:
        make(key_limit=0)
    assert exc.value.code == "QUOTA_INVALID_ARGUMENT"
    limiter = make()
    bad_calls = [
        {"key": "", "tenant": "tenant-a", "cost": 1},
        {"key": "key-a", "tenant": "", "cost": 1},
        {"key": "key-a", "tenant": "tenant-a", "cost": 0},
        {"key": "key-a", "tenant": "tenant-a", "cost": -2},
        {"key": "key-a", "tenant": "tenant-a", "cost": 1.5},
    ]
    for call in bad_calls:
        with pytest.raises(ql.QuotaError) as exc:
            limiter.reserve(now=0.5, **call)
        assert exc.value.code == "QUOTA_INVALID_ARGUMENT"
    assert limiter.lease_count() == 0
    assert limiter.stats()["reserved"] == 0


def test_stats_track_every_operation_exactly():
    limiter = make(key_limit=10, tenant_limit=100, global_limit=100, lease_ttl=2.0)
    first = limiter.reserve("key-a", "tenant-a", cost=3, now=0.1)
    second = limiter.reserve("key-a", "tenant-a", cost=4, now=0.2)
    with pytest.raises(ql.QuotaError) as exc:
        limiter.reserve("key-a", "tenant-a", cost=4, now=0.3)
    assert exc.value.code == "QUOTA_EXCEEDED_KEY"
    limiter.settle(first)
    limiter.refund(second)
    limiter.reserve("key-a", "tenant-a", cost=6, now=0.4)
    assert limiter.expire_leases(now=2.4) == 1
    assert limiter.stats() == {
        "reserved": 3, "settled": 1, "refunded": 1, "leaked": 1, "denied": 1,
    }


def test_snapshot_is_detached_and_restore_round_trips():
    limiter = make(key_limit=100, tenant_limit=200, global_limit=400, lease_ttl=5.0)
    first = limiter.reserve("key-a", "tenant-a", cost=30, now=0.5)
    limiter.reserve("key-b", "tenant-a", cost=7, now=0.6)
    state = limiter.snapshot()
    assert state["version"] == 1
    assert state["stats"] == limiter.stats()
    pristine = copy.deepcopy(state)
    state["stats"]["reserved"] = 999
    assert limiter.stats()["reserved"] == 2

    restored = make(key_limit=100, tenant_limit=200, global_limit=400, lease_ttl=5.0)
    restored.restore(pristine)
    assert restored.stats() == limiter.stats()
    assert restored.lease_count() == 2
    assert restored.remaining("key-a", "tenant-a", now=0.5) == limiter.remaining(
        "key-a", "tenant-a", now=0.5
    )
    assert restored.settle(first)["cost"] == 30
    with pytest.raises(ql.QuotaError) as exc:
        restored.restore({"version": 2})
    assert exc.value.code == "QUOTA_SNAPSHOT_INVALID"
    assert restored.lease_count() == 1


def test_concurrent_reservations_never_exceed_global_limit():
    limiter = ql.QuotaWindowLimiter(
        window_seconds=10, key_limit=1000, tenant_limit=1000, global_limit=50,
        lease_ttl=100.0, max_leases=200,
    )
    granted = []
    denied = []
    guard = threading.Lock()

    def worker(index):
        for _ in range(5):
            try:
                lease_id = limiter.reserve("key-%d" % index, "tenant-a", cost=1, now=0.5)
            except ql.QuotaError as exc:
                with guard:
                    denied.append(exc.code)
            else:
                with guard:
                    granted.append(lease_id)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(granted) == 50
    assert denied.count("QUOTA_EXCEEDED_GLOBAL") == 50
    assert limiter.lease_count() == 50
    assert limiter.stats()["reserved"] == 50
    assert limiter.stats()["denied"] == 50
