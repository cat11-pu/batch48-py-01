import threading

import pytest

import backpressure_guard as bg


def make(**kwargs):
    base = {
        "high_watermark": 100,
        "low_watermark": 30,
        "hard_cap": 1000,
        "grace": 10.0,
        "quantum": 1000,
        "tenant_quota": 100000,
    }
    base.update(kwargs)
    return bg.BackpressureGuard(**base)


def test_register_conn_returns_descriptor_and_lists_in_order():
    guard = make()
    descriptor = guard.register_conn("conn-a", "tenant-1")
    assert descriptor["conn_id"] == "conn-a"
    assert descriptor["tenant"] == "tenant-1"
    assert descriptor["pending"] == 0
    assert descriptor["paused"] is False
    guard.register_conn("conn-b", "tenant-1")
    assert guard.list_conns() == ["conn-a", "conn-b"]


def test_read_accumulates_below_high_watermark():
    guard = make()
    guard.register_conn("conn-a", "tenant-1")
    assert guard.on_read("conn-a", 60, now=0.0) is False
    assert guard.pending_bytes("conn-a") == 60
    assert guard.read_paused("conn-a") is False
    assert guard.tenant_usage("tenant-1") == 60
    assert guard.stats()["accepted_bytes"] == 60
    assert guard.stats()["pauses"] == 0


def test_reaching_high_watermark_pauses_reading():
    guard = make()
    guard.register_conn("conn-a", "tenant-1")
    assert guard.on_read("conn-a", 60, now=0.0) is False
    assert guard.on_read("conn-a", 40, now=0.5) is True
    assert guard.read_paused("conn-a") is True
    assert guard.pending_bytes("conn-a") == 100
    assert guard.stats()["pauses"] == 1


def test_reading_resumes_only_below_low_watermark():
    guard = make()
    guard.register_conn("conn-a", "tenant-1")
    assert guard.on_read("conn-a", 120, now=0.0) is True
    assert guard.drain("conn-a", 80, now=1.0) == 80
    assert guard.pending_bytes("conn-a") == 40
    assert guard.read_paused("conn-a") is True
    assert guard.drain("conn-a", 10, now=2.0) == 10
    assert guard.pending_bytes("conn-a") == 30
    assert guard.read_paused("conn-a") is False
    assert guard.stats()["resumes"] == 1
    assert guard.stats()["drained_bytes"] == 90


def test_constructor_rejects_invalid_watermarks():
    bad_configs = [
        {"low_watermark": 100, "high_watermark": 100},
        {"low_watermark": 120},
        {"high_watermark": 2000, "hard_cap": 1000},
        {"grace": 0.0},
        {"quantum": 0},
        {"tenant_quota": 0},
    ]
    for config in bad_configs:
        with pytest.raises(bg.BackpressureError) as exc:
            make(**config)
        assert exc.value.code == "BP_INVALID_ARGUMENT"


def test_invalid_registration_and_connection_arguments():
    guard = make()
    with pytest.raises(bg.BackpressureError) as exc:
        guard.register_conn("", "tenant-1")
    assert exc.value.code == "BP_INVALID_ARGUMENT"
    with pytest.raises(bg.BackpressureError) as exc:
        guard.register_conn("conn-a", "")
    assert exc.value.code == "BP_INVALID_ARGUMENT"
    guard.register_conn("conn-a", "tenant-1")
    with pytest.raises(bg.BackpressureError) as exc:
        guard.register_conn("conn-a", "tenant-1")
    assert exc.value.code == "BP_DUPLICATE_CONN"
    with pytest.raises(bg.BackpressureError) as exc:
        guard.on_read("conn-missing", 10, now=0.0)
    assert exc.value.code == "BP_UNKNOWN_CONN"
    with pytest.raises(bg.BackpressureError) as exc:
        guard.on_read("conn-a", 0, now=0.0)
    assert exc.value.code == "BP_INVALID_ARGUMENT"
    with pytest.raises(bg.BackpressureError) as exc:
        guard.drain("conn-a", -5, now=0.0)
    assert exc.value.code == "BP_INVALID_ARGUMENT"


def test_tenant_quota_rejection_leaves_buffer_untouched():
    guard = make(tenant_quota=500, high_watermark=800, low_watermark=200)
    guard.register_conn("conn-a", "tenant-1")
    guard.register_conn("conn-b", "tenant-2")
    assert guard.on_read("conn-a", 400, now=0.0) is False
    with pytest.raises(bg.BackpressureError) as exc:
        guard.on_read("conn-a", 200, now=0.5)
    assert exc.value.code == "BP_TENANT_QUOTA"
    assert guard.pending_bytes("conn-a") == 400
    assert guard.tenant_usage("tenant-1") == 400
    assert guard.on_read("conn-b", 500, now=0.6) is False


def test_drain_releases_tenant_quota_for_later_reads():
    guard = make(tenant_quota=500, high_watermark=800, low_watermark=200)
    guard.register_conn("conn-a", "tenant-1")
    guard.on_read("conn-a", 400, now=0.0)
    assert guard.drain("conn-a", 300, now=1.0) == 300
    assert guard.tenant_usage("tenant-1") == 100
    assert guard.on_read("conn-a", 400, now=2.0) is False
    assert guard.tenant_usage("tenant-1") == 500


def test_drr_serves_connections_round_robin():
    guard = make(high_watermark=8192, low_watermark=2048, quantum=1000)
    for conn_id in ("conn-a", "conn-b", "conn-c"):
        guard.register_conn(conn_id, "tenant-1")
        guard.on_read(conn_id, 5000, now=0.0)
    served = guard.schedule(6000, now=1.0)
    assert served == [
        ("conn-a", 1000), ("conn-b", 1000), ("conn-c", 1000),
        ("conn-a", 1000), ("conn-b", 1000), ("conn-c", 1000),
    ]
    assert guard.pending_bytes("conn-a") == 3000
    assert guard.pending_bytes("conn-b") == 3000
    assert guard.pending_bytes("conn-c") == 3000
    assert guard.stats()["drained_bytes"] == 6000


def test_drr_quantum_keeps_small_flow_from_starving():
    guard = make(high_watermark=8192, low_watermark=2048, quantum=1000)
    guard.register_conn("conn-a", "tenant-1")
    guard.register_conn("conn-b", "tenant-1")
    guard.on_read("conn-a", 5000, now=0.0)
    guard.on_read("conn-b", 500, now=0.0)
    served = guard.schedule(3000, now=1.0)
    assert served == [
        ("conn-a", 1000), ("conn-b", 500), ("conn-a", 1000), ("conn-a", 500),
    ]
    assert guard.pending_bytes("conn-b") == 0
    assert guard.pending_bytes("conn-a") == 2500


def test_drr_budget_stops_and_leaves_remaining_pending():
    guard = make(high_watermark=8192, low_watermark=2048, quantum=1000)
    guard.register_conn("conn-a", "tenant-1")
    guard.on_read("conn-a", 2500, now=0.0)
    assert guard.schedule(1200, now=1.0) == [("conn-a", 1000), ("conn-a", 200)]
    assert guard.pending_bytes("conn-a") == 1300
    assert guard.schedule(5000, now=2.0) == [("conn-a", 1000), ("conn-a", 300)]
    assert guard.pending_bytes("conn-a") == 0


def test_expire_slow_closes_only_grace_expired_connections():
    guard = make(grace=10.0)
    guard.register_conn("conn-a", "tenant-1")
    guard.register_conn("conn-b", "tenant-1")
    guard.on_read("conn-a", 150, now=0.0)
    guard.on_read("conn-b", 150, now=5.0)
    assert guard.read_paused("conn-a") is True
    assert guard.expire_slow(now=10.0) == ["conn-a"]
    assert guard.list_conns() == ["conn-b"]
    assert guard.expire_slow(now=15.0) == ["conn-b"]
    assert guard.list_conns() == []
    assert guard.stats()["closed"] == 2


def test_close_conn_returns_reclaimed_bytes():
    guard = make()
    guard.register_conn("conn-a", "tenant-1")
    guard.on_read("conn-a", 300, now=0.0)
    assert guard.close_conn("conn-a") == 300
    assert guard.list_conns() == []
    assert guard.stats()["closed"] == 1
    with pytest.raises(bg.BackpressureError) as exc:
        guard.close_conn("conn-a")
    assert exc.value.code == "BP_UNKNOWN_CONN"


def test_snapshot_restore_preserves_queue_and_paused_flag():
    guard = make()
    guard.register_conn("conn-a", "tenant-1")
    guard.on_read("conn-a", 300, now=0.0)
    state = guard.snapshot()
    assert state["version"] == 1
    restored = make()
    restored.restore(state)
    assert restored.stats() == guard.stats()
    assert restored.list_conns() == ["conn-a"]
    assert restored.pending_bytes("conn-a") == 300
    assert restored.read_paused("conn-a") is True
    assert restored.drain("conn-a", 280, now=1.0) == 280
    assert restored.read_paused("conn-a") is False
    with pytest.raises(bg.BackpressureError) as exc:
        restored.restore({"version": 2})
    assert exc.value.code == "BP_SNAPSHOT_INVALID"
    assert restored.pending_bytes("conn-a") == 20


def test_concurrent_reads_respect_tenant_quota():
    guard = make(high_watermark=8192, tenant_quota=1000, hard_cap=10000)
    conns = ["conn-%d" % index for index in range(5)]
    for conn_id in conns:
        guard.register_conn(conn_id, "tenant-1")
    accepted = []
    rejected = []
    guard_lock = threading.Lock()

    def worker(conn_id):
        for _ in range(20):
            try:
                guard.on_read(conn_id, 100, now=0.0)
            except bg.BackpressureError as exc:
                with guard_lock:
                    rejected.append(exc.code)
            else:
                with guard_lock:
                    accepted.append(conn_id)

    threads = [threading.Thread(target=worker, args=(conn_id,)) for conn_id in conns]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(accepted) == 10
    assert rejected.count("BP_TENANT_QUOTA") == 90
    assert guard.tenant_usage("tenant-1") == 1000
    assert sum(guard.pending_bytes(conn_id) for conn_id in conns) == 1000
    assert guard.stats()["accepted_bytes"] == 1000
