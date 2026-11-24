import threading

import pytest

import zeroize_buffer_pool as zp


def make(capacity=4, buffer_size=32, **kwargs):
    return zp.ZeroizeBufferPool(capacity, buffer_size, **kwargs)


def test_acquire_returns_zeroed_buffer_and_lease_metadata():
    pool = make()
    lease = pool.acquire(16, owner="analytics")
    assert isinstance(lease["buffer"], bytearray)
    assert len(lease["buffer"]) == 16
    assert bytes(lease["buffer"]) == b"\x00" * 16
    assert lease["owner"] == "analytics"
    assert lease["size"] == 16
    assert lease["capacity"] == 32
    assert pool.lease_info(lease)["leased"] is True
    assert pool.free_count() == 3
    assert pool.leased_count() == 1
    assert pool.stats()["acquired"] == 1


def test_acquire_without_size_uses_full_buffer():
    pool = make(capacity=2, buffer_size=24)
    lease = pool.acquire(owner="batch")
    assert lease["size"] == 24
    assert len(lease["buffer"]) == 24
    assert pool.stats()["acquired"] == 1


def test_size_above_buffer_capacity_is_rejected():
    pool = make(capacity=2, buffer_size=8)
    with pytest.raises(zp.BufferPoolError) as exc:
        pool.acquire(9)
    assert exc.value.code == "POOL_SIZE_INVALID"
    assert pool.stats()["rejected"] == 1


def test_non_positive_size_is_rejected():
    pool = make(capacity=2, buffer_size=8)
    for bad in (0, -3):
        with pytest.raises(zp.BufferPoolError) as exc:
            pool.acquire(bad)
        assert exc.value.code == "POOL_SIZE_INVALID"
    assert pool.stats()["rejected"] == 2


def test_exhaustion_is_retryable_and_release_restores_capacity():
    pool = make(capacity=2, buffer_size=8)
    first = pool.acquire(owner="a")
    pool.acquire(owner="b")
    with pytest.raises(zp.BufferPoolError) as exc:
        pool.acquire(owner="c")
    assert exc.value.code == "POOL_EXHAUSTED"
    assert exc.value.retryable is True
    assert pool.stats()["rejected"] == 1
    pool.release(first)
    third = pool.acquire(owner="c")
    assert third["size"] == 8
    assert pool.leased_count() == 2


def test_release_zeroes_bytes_and_updates_counters():
    pool = make(capacity=4, buffer_size=32)
    lease = pool.acquire(12)
    pool.release(lease)
    assert pool.free_count() == 4
    assert pool.leased_count() == 0
    assert pool.stats() == {
        "acquired": 1,
        "released": 1,
        "rejected": 0,
        "zeroized_bytes": 32,
    }


def test_double_release_is_an_error():
    pool = make(capacity=2, buffer_size=8)
    lease = pool.acquire()
    pool.release(lease)
    with pytest.raises(zp.BufferPoolError) as exc:
        pool.release(lease)
    assert exc.value.code == "POOL_DOUBLE_RELEASE"


def test_lease_from_another_pool_is_rejected():
    left = make(capacity=2, buffer_size=8)
    right = make(capacity=2, buffer_size=8)
    lease = right.acquire()
    with pytest.raises(zp.BufferPoolError) as exc:
        left.release(lease)
    assert exc.value.code == "POOL_FOREIGN_LEASE"
    assert left.free_count() == 2


def test_unknown_lease_object_is_rejected():
    pool = make(capacity=2, buffer_size=8)
    for bad in (None, {"lease_id": 99}, object()):
        with pytest.raises(zp.BufferPoolError) as exc:
            pool.release(bad)
        assert exc.value.code == "POOL_LEASE_INVALID"


def test_release_owner_returns_all_of_its_leases():
    pool = make(capacity=4, buffer_size=8)
    for _ in range(3):
        pool.acquire(4, owner="svc-a")
    held = pool.acquire(4, owner="svc-b")
    assert pool.release_owner("svc-a") == 3
    assert pool.free_count() == 3
    assert pool.lease_info(held)["leased"] is True
    assert pool.stats()["released"] == 3


def test_zeroize_all_wipes_free_buffers_again():
    pool = make(capacity=2, buffer_size=8)
    left = pool.acquire(8, owner="a")
    left["buffer"][:] = b"AAAAAAAA"
    right = pool.acquire(8, owner="b")
    right["buffer"][:] = b"BBBBBBBB"
    pool.release(left)
    pool.release(right)
    written = pool.zeroize_all()
    assert written == 16
    assert pool.stats()["zeroized_bytes"] == 32
    assert pool.free_count() == 2


def test_snapshot_restores_config_and_counters():
    pool = make(capacity=3, buffer_size=16)
    first = pool.acquire(16, owner="a")
    pool.release(first)
    pool.acquire(8, owner="b")
    snap = pool.snapshot()
    assert snap["capacity"] == 3
    assert snap["buffer_size"] == 16

    other = make(capacity=3, buffer_size=16)
    other.restore(snap)
    assert other.stats() == pool.stats()
    assert other.pool_capacity() == 3
    fresh = other.acquire(4, owner="c")
    assert fresh["size"] == 4


def test_restore_rejects_malformed_state_without_resetting_counters():
    pool = make(capacity=2, buffer_size=8)
    lease = pool.acquire(8)
    before = pool.stats()
    with pytest.raises(zp.BufferPoolError) as exc:
        pool.restore({"capacity": 2})
    assert exc.value.code == "POOL_SNAPSHOT_INVALID"
    assert pool.stats() == before
    pool.release(lease)
    assert pool.stats()["released"] == 1


def test_restore_rejects_capacity_mismatch():
    source = make(capacity=2, buffer_size=8)
    snap = source.snapshot()
    target = make(capacity=4, buffer_size=8)
    with pytest.raises(zp.BufferPoolError) as exc:
        target.restore(snap)
    assert exc.value.code == "POOL_SNAPSHOT_INVALID"


def test_concurrent_lease_and_release_never_leaks_slots():
    pool = make(capacity=3, buffer_size=8)
    errors = []

    def worker():
        done = 0
        attempts = 0
        while done < 15 and attempts < 500:
            attempts += 1
            try:
                lease = pool.acquire(8, owner="worker")
            except zp.BufferPoolError as exc:
                if exc.code != "POOL_EXHAUSTED":
                    errors.append(exc)
                    return
                continue
            except Exception as exc:  # pragma: no cover - defensive
                errors.append(exc)
                return
            lease["buffer"][:] = b"12345678"
            pool.release(lease)
            done += 1
        if done != 15:
            errors.append(RuntimeError("worker stalled"))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert pool.stats()["acquired"] == 120
    assert pool.stats()["released"] == 120
    assert pool.free_count() == 3
