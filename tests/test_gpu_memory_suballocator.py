import threading

import pytest

import gpu_memory_suballocator as gms


def make(**kwargs):
    return gms.GpuMemorySuballocator(**kwargs)


def arena_pool(size=1000, alignment=1):
    sub = make()
    sub.create_arena(size, alignment)
    return sub


def test_create_arena_reports_identity_and_free_space():
    sub = make()
    out = sub.create_arena(1000, 256)
    assert out["arena"] == "arena-0"
    assert out["size"] == 1000
    assert out["alignment"] == 256
    assert sub.arenas() == [
        {"arena": "arena-0", "size": 1000, "alignment": 256,
         "free_bytes": 1000, "blocks": 1},
    ]
    second = sub.create_arena(500, 64)
    assert second["arena"] == "arena-1"
    assert [item["arena"] for item in sub.arenas()] == ["arena-0", "arena-1"]


def test_create_arena_rejects_bad_size_alignment_and_count():
    sub = make(max_arenas=2)
    with pytest.raises(gms.GpuMemError) as err:
        sub.create_arena(0)
    assert err.value.code == "GPU_ARENA_INVALID"
    with pytest.raises(gms.GpuMemError) as err:
        sub.create_arena((1 << 32) + 1)
    assert err.value.code == "GPU_ARENA_INVALID"
    with pytest.raises(gms.GpuMemError) as err:
        sub.create_arena(1000, 128 * 1024)
    assert err.value.code == "GPU_ALIGN_INVALID"
    with pytest.raises(gms.GpuMemError) as err:
        sub.create_arena(1000, 300)
    assert err.value.code == "GPU_ALIGN_INVALID"

    sub.create_arena(1000)
    sub.create_arena(1000)
    with pytest.raises(gms.GpuMemError) as err:
        sub.create_arena(1000)
    assert err.value.code == "GPU_ARENA_LIMIT"
    assert len(sub.arenas()) == 2


def test_alloc_uses_first_fit_across_arenas_in_creation_order():
    sub = make()
    sub.create_arena(1000, 1)
    sub.create_arena(1000, 1)
    first = sub.alloc(600, "albedo")
    second = sub.alloc(600, "normal")
    assert first["arena"] == "arena-0"
    assert first["offset"] == 0
    assert second["arena"] == "arena-1"
    assert second["offset"] == 0
    assert [item["free_bytes"] for item in sub.arenas()] == [400, 400]


def test_alloc_rounds_offset_up_to_requested_alignment():
    sub = arena_pool()
    first = sub.alloc(100, "albedo")
    assert first["offset"] == 0
    second = sub.alloc(50, "constants", alignment=64)
    assert second["offset"] == 128
    assert second["offset"] % 64 == 0


def test_alloc_rejects_oversized_and_misaligned_requests():
    sub = make(max_alloc_bytes=256 << 20)
    sub.create_arena(1 << 32, 256)
    with pytest.raises(gms.GpuMemError) as err:
        sub.alloc((256 << 20) + 1, "giant")
    assert err.value.code == "GPU_ALLOC_TOO_LARGE"
    with pytest.raises(gms.GpuMemError) as err:
        sub.alloc(100, "bad-align", alignment=3)
    assert err.value.code == "GPU_ALIGN_INVALID"
    with pytest.raises(gms.GpuMemError) as err:
        sub.alloc(100, "bad-life", lifetime="forever")
    assert err.value.code == "GPU_LIFETIME_INVALID"
    with pytest.raises(gms.GpuMemError) as err:
        sub.alloc(100, "   ")
    assert err.value.code == "GPU_TAG_INVALID"
    assert sub.stats()["allocated_bytes"] == 0


def test_alloc_out_of_memory_leaves_arena_untouched():
    sub = arena_pool()
    sub.alloc(600, "albedo")
    before = sub.stats()
    with pytest.raises(gms.GpuMemError) as err:
        sub.alloc(500, "normal")
    assert err.value.code == "GPU_ADDRESS_SPACE"
    assert sub.stats() == before
    assert sub.free_blocks() == [{"offset": 600, "size": 400}]


def test_free_merges_adjacent_blocks():
    sub = arena_pool()
    first = sub.alloc(100, "a")
    second = sub.alloc(100, "b")
    sub.alloc(100, "c")
    assert sub.free(first["handle"]) == 100
    assert sub.free(second["handle"]) == 100
    assert sub.free_blocks() == [
        {"offset": 0, "size": 200},
        {"offset": 300, "size": 700},
    ]


def test_double_free_and_check_rejected_handle():
    sub = arena_pool()
    first = sub.alloc(100, "a")
    second = sub.alloc(100, "b")
    sub.free(first["handle"])
    with pytest.raises(gms.GpuMemError) as err:
        sub.free(first["handle"])
    assert err.value.code == "GPU_HANDLE_INVALID"
    with pytest.raises(gms.GpuMemError) as err:
        sub.check(first["handle"])
    assert err.value.code == "GPU_HANDLE_INVALID"

    info = sub.check(second["handle"])
    assert info == {"handle": second["handle"], "arena": "arena-0",
                    "offset": 100, "size": 100, "tag": "b",
                    "lifetime": "frame", "state": "live"}


def test_end_frame_reclaims_fenced_transient_but_not_persistent():
    sub = arena_pool()
    keep = sub.alloc(100, "target", lifetime="persistent")
    temp = sub.alloc(100, "gbuffer", lifetime="frame")
    sub.signal_fence(0)
    out = sub.end_frame(1)
    assert out["frame"] == 1
    assert out["freed"] == [temp["handle"]]
    assert out["retained"] == []
    assert sub.check(keep["handle"])["offset"] == 0
    with pytest.raises(gms.GpuMemError) as err:
        sub.check(temp["handle"])
    assert err.value.code == "GPU_HANDLE_INVALID"
    assert sub.stats()["allocated_bytes"] == 100


def test_end_frame_and_fence_reject_bad_order():
    sub = arena_pool()
    sub.end_frame(1)
    with pytest.raises(gms.GpuMemError) as err:
        sub.end_frame(1)
    assert err.value.code == "GPU_FRAME_ORDER"
    with pytest.raises(gms.GpuMemError) as err:
        sub.end_frame(0)
    assert err.value.code == "GPU_FRAME_ORDER"
    with pytest.raises(gms.GpuMemError) as err:
        sub.signal_fence(-1)
    assert err.value.code == "GPU_FENCE_INVALID"


def test_stats_reports_free_bytes_largest_block_and_fragmentation():
    sub = arena_pool()
    first = sub.alloc(200, "a")
    sub.alloc(300, "b")
    sub.free(first["handle"])
    stats = sub.stats("arena-0")
    assert stats["allocated_bytes"] == 300
    assert stats["free_bytes"] == 700
    assert stats["largest_free"] == 500
    assert stats["blocks"] == 2
    assert stats["fragmentation"] == pytest.approx(0.285714, abs=1e-6)


def test_defrag_plan_moves_blocks_to_close_gaps():
    sub = arena_pool()
    sub.alloc(100, "a")
    middle = sub.alloc(100, "b")
    tail = sub.alloc(100, "c")
    sub.free(middle["handle"])
    plan = sub.defrag_plan("arena-0")
    assert plan == [{"handle": tail["handle"], "from": 200, "to": 100,
                     "tag": "c"}]
    assert sub.apply_defrag(plan) == 1
    assert sub.check(tail["handle"])["offset"] == 100
    assert sub.free_blocks() == [{"offset": 200, "size": 800}]
    assert sub.stats()["largest_free"] == 800


def test_apply_defrag_rejects_unknown_plan_and_keeps_state():
    sub = arena_pool()
    sub.alloc(100, "a")
    middle = sub.alloc(100, "b")
    tail = sub.alloc(100, "c")
    sub.free(middle["handle"])
    with pytest.raises(gms.GpuMemError) as err:
        sub.apply_defrag([{"handle": 9999, "from": 0, "to": 0, "tag": "x"}])
    assert err.value.code == "GPU_PLAN_INVALID"
    assert sub.check(tail["handle"])["offset"] == 200
    assert sub.free_blocks() == [{"offset": 100, "size": 100},
                                 {"offset": 300, "size": 700}]


def test_snapshot_restore_keeps_blocks_and_arena_state():
    sub = arena_pool()
    first = sub.alloc(100, "a")
    keep = sub.alloc(200, "b")
    sub.free(first["handle"])
    state = sub.snapshot()
    before = sub.stats()

    scratch = sub.alloc(50, "tmp")
    assert scratch["offset"] == 0
    sub.restore(state)

    assert sub.stats() == before
    assert sub.check(keep["handle"])["offset"] == 100
    assert sub.free_blocks() == [{"offset": 0, "size": 100},
                                 {"offset": 300, "size": 700}]
    again = sub.alloc(50, "tmp")
    assert again["offset"] == 0

    with pytest.raises(gms.GpuMemError) as err:
        sub.restore({"arenas": 42})
    assert err.value.code == "GPU_SNAPSHOT_INVALID"
    assert sub.stats() == before


def test_concurrent_alloc_free_leaves_no_residue():
    sub = make()
    sub.create_arena(1_000_000, 1)
    errors = []

    def worker(index):
        try:
            for step in range(50):
                handle = sub.alloc(1000, f"t{index}-{step}")
                assert sub.free(handle["handle"]) == 1000
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stats = sub.stats("arena-0")
    assert stats["allocated_bytes"] == 0
    assert stats["free_bytes"] == 1_000_000
    assert stats["blocks"] == 1
    assert stats["fragmentation"] == 0.0
