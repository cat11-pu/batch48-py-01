import copy
import threading

import pytest

import texture_compression_planner as tp


def test_format_block_sizes_and_kinds():
    planner = tp.TextureCompressionPlanner()
    assert planner.block_size("BC1_UNORM") == 4
    assert planner.block_size("ASTC_6x6_UNORM") == 6
    assert planner.block_size("ASTC_8x8_UNORM") == 8
    assert planner.block_size("RGBA8_UNORM") == 1

    info = planner.format_info("ASTC_6x6_SRGB")
    assert info["format"] == "ASTC_6x6_SRGB"
    assert info["block"] == 6
    assert info["srgb"] is True
    assert info["kind"] == "block"

    linear = planner.format_info("RGBA8_UNORM")
    assert linear["block"] == 1
    assert linear["srgb"] is False
    assert linear["kind"] == "linear"


def test_unknown_format_is_rejected():
    planner = tp.TextureCompressionPlanner()
    for call in (lambda: planner.plan(8, 8, "DXT9"),
                 lambda: planner.block_size("NOPE"),
                 lambda: planner.cache_key(8, 8, "NOPE")):
        with pytest.raises(tp.TextureError) as exc:
            call()
        assert exc.value.code == "TEXTURE_FORMAT_INVALID"
    assert planner.cache_size() == 0


def test_pad_to_block_for_unaligned_size():
    planner = tp.TextureCompressionPlanner()
    padded = planner.pad_to_block(33, 17, 4)
    assert padded["padded_width"] == 36
    assert padded["padded_height"] == 20
    assert padded["valid_width"] == 33
    assert padded["valid_height"] == 17

    padded = planner.pad_to_block(20, 10, 6)
    assert padded["padded_width"] == 24
    assert padded["padded_height"] == 12

    blocks = planner.level_blocks(33, 17, 4)
    assert blocks["blocks_x"] == 9
    assert blocks["blocks_y"] == 5
    assert blocks["blocks"] == 45


def test_mip_level_count_follows_block_chain():
    planner = tp.TextureCompressionPlanner()
    assert planner.mip_count(64, 64, 4) == 6
    assert planner.mip_count(64, 64, 1) == 7
    assert planner.mip_count(100, 60, 4) == 7


def test_plan_levels_carry_sizes_and_block_counts():
    planner = tp.TextureCompressionPlanner()
    plan = planner.plan(100, 60, "BC1_UNORM")
    assert plan["format"] == "BC1_UNORM"
    assert plan["block"] == 4
    assert plan["level_count"] == 7
    assert len(plan["levels"]) == 7

    dims = [(level["width"], level["height"]) for level in plan["levels"]]
    assert dims == [(100, 60), (50, 30), (25, 15), (12, 7), (6, 3), (3, 1), (1, 1)]

    first = plan["levels"][0]
    assert first["level"] == 0
    assert first["padded_width"] == 100
    assert first["padded_height"] == 60
    assert first["blocks_x"] == 25
    assert first["blocks_y"] == 15
    assert first["blocks"] == 375

    third = plan["levels"][2]
    assert third["blocks_x"] == 7
    assert third["blocks_y"] == 4
    assert third["blocks"] == 28


def test_plan_last_level_is_single_pixel():
    planner = tp.TextureCompressionPlanner()
    for width, height, fmt in ((64, 64, "BC1_UNORM"),
                               (33, 17, "ASTC_8x8_UNORM"),
                               (128, 32, "RGBA8_UNORM")):
        plan = planner.plan(width, height, fmt)
        last = plan["levels"][-1]
        assert (last["width"], last["height"]) == (1, 1)
        assert last["padded_width"] >= last["blocks_x"]
        assert plan["level_count"] == len(plan["levels"])


def test_plan_is_deterministic_and_cached():
    planner = tp.TextureCompressionPlanner()
    first = planner.plan(64, 64, "BC1_UNORM")
    second = planner.plan(64, 64, "BC1_UNORM")
    assert first == second
    assert planner.cache_size() == 1

    planner.plan(64, 64, "BC3_UNORM")
    assert planner.cache_size() == 2
    planner.invalidate_cache()
    assert planner.cache_size() == 0
    assert planner.plan(64, 64, "BC1_UNORM") == first


def test_validate_accepts_planned_chain():
    planner = tp.TextureCompressionPlanner()
    plan = planner.plan(64, 64, "BC1_UNORM")
    assert planner.validate(plan["levels"]) == []
    assert planner.validate(plan["levels"][:-1]) == [plan["level_count"] - 2]


def test_validate_flags_broken_levels():
    planner = tp.TextureCompressionPlanner()
    plan = planner.plan(64, 64, "BC1_UNORM")
    levels = copy.deepcopy(plan["levels"])
    levels[2]["blocks_x"] += 1
    assert planner.validate(levels) == [2]
    assert planner.validate([]) == [0]


def test_validate_flags_missing_final_pixel():
    planner = tp.TextureCompressionPlanner()
    plan = planner.plan(100, 60, "BC1_UNORM")
    levels = copy.deepcopy(plan["levels"])
    levels[-1]["width"] = 2
    problems = planner.validate(levels)
    assert problems == [len(levels) - 1]


def test_zero_and_oversized_dimensions_rejected():
    planner = tp.TextureCompressionPlanner()
    with pytest.raises(tp.TextureError) as exc:
        planner.plan(0, 8, "BC1_UNORM")
    assert exc.value.code == "TEXTURE_SIZE_INVALID"
    with pytest.raises(tp.TextureError) as exc:
        planner.pad_to_block(8, -1, 4)
    assert exc.value.code == "TEXTURE_SIZE_INVALID"
    with pytest.raises(tp.TextureError) as exc:
        planner.pad_to_block(8, 8, 5)
    assert exc.value.code == "TEXTURE_BLOCK_INVALID"
    with pytest.raises(tp.TextureError) as exc:
        planner.plan(16385, 8, "BC1_UNORM")
    assert exc.value.code == "TEXTURE_SIZE_TOO_LARGE"


def test_invalid_plan_does_not_disturb_existing_cache():
    planner = tp.TextureCompressionPlanner()
    original = planner.plan(64, 64, "BC1_UNORM")
    with pytest.raises(tp.TextureError) as exc:
        planner.plan(64, 64, "DXT9")
    assert exc.value.code == "TEXTURE_FORMAT_INVALID"
    assert planner.cache_size() == 1
    assert planner.plan(64, 64, "BC1_UNORM") == original


def test_cache_key_tracks_format_and_size():
    planner = tp.TextureCompressionPlanner()
    assert planner.cache_key(64, 64, "BC1_UNORM") == planner.cache_key(64, 64, "BC1_UNORM")
    assert planner.cache_key(64, 64, "BC1_UNORM") != planner.cache_key(128, 64, "BC1_UNORM")
    assert planner.cache_key(64, 64, "BC1_UNORM") != planner.cache_key(64, 64, "BC3_UNORM")


def test_snapshot_restore_keeps_cache_entries():
    planner = tp.TextureCompressionPlanner()
    planner.plan(64, 64, "BC1_UNORM")
    planner.plan(100, 60, "BC1_UNORM")
    state = planner.snapshot()
    assert state["version"] == 1
    pristine = copy.deepcopy(state)

    state["cache"].clear()
    assert planner.cache_size() == 2

    restored = tp.TextureCompressionPlanner()
    restored.restore(pristine)
    assert restored.cache_size() == 2
    assert restored.plan(64, 64, "BC1_UNORM") == planner.plan(64, 64, "BC1_UNORM")

    with pytest.raises(tp.TextureError) as exc:
        restored.restore({"version": 7})
    assert exc.value.code == "TEXTURE_SNAPSHOT_INVALID"
    assert restored.cache_size() == 2


def test_concurrent_plans_match_serial_results():
    planner = tp.TextureCompressionPlanner()
    cases = [
        (64, 64, "BC1_UNORM", 6),
        (100, 60, "BC1_UNORM", 7),
        (33, 17, "ASTC_8x8_UNORM", 4),
        (64, 64, "RGBA8_UNORM", 7),
    ]
    errors = []

    def worker(offset):
        try:
            for step in range(40):
                width, height, fmt, expected = cases[(step + offset) % len(cases)]
                plan = planner.plan(width, height, fmt)
                assert plan["level_count"] == expected
                assert plan["levels"][-1]["width"] == 1
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert planner.cache_size() == 4
