import pytest

import postprocess_chain_guard as pcg

STAT_KEYS = {"runs", "executed", "cached", "skipped", "trimmed",
             "conversions"}


def make(**kwargs):
    return pcg.PostProcessChainGuard(**kwargs)


def chain():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("blur", reads=("scene",), writes=("blurred",), cost_ms=1.0)
    guard.add_pass("grade", deps=("blur",), reads=("blurred",),
                   writes=("graded",), cost_ms=2.0)
    return guard


def names(plan):
    return [step["name"] for step in plan["steps"]]


def test_build_orders_passes_and_assigns_ping_pong_buffers():
    guard = chain()
    plan = guard.build()
    assert names(plan) == ["blur", "grade"]
    assert plan["buffers"] == {"blurred": 0, "graded": 1}
    assert plan["cost_ms"] == pytest.approx(3.0)
    assert plan["conversions"] == 0
    assert plan["trimmed"] == []
    assert plan["disabled"] == []
    assert plan["over_budget"] is False

    first = plan["steps"][0]
    assert first["kind"] == "pass"
    assert list(first["reads"]) == ["scene"]
    assert list(first["writes"]) == ["blurred"]
    assert first["buffer"] == 0
    assert first["fmt"] == "rgba8"
    assert first["color_space"] == "srgb"
    assert first["cost_ms"] == pytest.approx(1.0)
    assert plan["steps"][1]["reads"] == ["blurred"]
    assert plan["steps"][1]["buffer"] == 1
    assert names(guard.last_plan()) == ["blur", "grade"]


def test_pass_declarations_are_validated_on_add():
    guard = make()
    guard.add_pass("p", reads=("scene",), writes=("t",))
    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("p", reads=("scene",), writes=("u",))
    assert exc.value.code == "CHAIN_DUPLICATE_PASS"

    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("", reads=("scene",), writes=("u",))
    assert exc.value.code == "CHAIN_PASS_INVALID"
    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("q", reads=("scene",), writes=())
    assert exc.value.code == "CHAIN_PASS_INVALID"
    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("r", reads=("scene",), writes=("a", "b"))
    assert exc.value.code == "CHAIN_PASS_INVALID"


def test_unknown_textures_and_duplicate_writers_are_rejected():
    guard = make()
    guard.add_pass("p", reads=("missing",), writes=("o",))
    with pytest.raises(pcg.ChainError) as exc:
        guard.build()
    assert exc.value.code == "CHAIN_UNKNOWN_TEXTURE"

    other = make()
    other.set_source("scene")
    other.add_pass("p1", reads=("scene",), writes=("t",))
    other.add_pass("p2", reads=("scene",), writes=("t",))
    with pytest.raises(pcg.ChainError) as exc:
        other.build()
    assert exc.value.code == "CHAIN_TEXTURE_CONFLICT"
    with pytest.raises(pcg.ChainError) as exc:
        other.set_source("scene")
    assert exc.value.code == "CHAIN_DUPLICATE_TEXTURE"


def test_dependencies_reorder_registered_passes():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("c", deps=("b",), reads=("tb",), writes=("tc",))
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("b", deps=("a",), reads=("ta",), writes=("tb",))
    assert names(guard.build()) == ["a", "b", "c"]

    missing = make()
    missing.set_source("scene")
    missing.add_pass("x", deps=("ghost",), reads=("scene",), writes=("tx",))
    with pytest.raises(pcg.ChainError) as exc:
        missing.build()
    assert exc.value.code == "CHAIN_UNKNOWN_DEP"


def test_reading_a_texture_requires_declaring_its_producer():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("b", reads=("ta",), writes=("tb",))
    with pytest.raises(pcg.ChainError) as exc:
        guard.build()
    assert exc.value.code == "CHAIN_DEPENDENCY_MISSING"


def test_a_pass_cannot_read_what_it_writes():
    guard = make()
    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("p", reads=("t",), writes=("t",))
    assert exc.value.code == "CHAIN_READ_WRITE_HAZARD"


def test_unknown_format_or_color_space_is_refused():
    guard = make()
    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("p", reads=("s",), writes=("t",), fmt="bgra4")
    assert exc.value.code == "CHAIN_FORMAT_UNSUPPORTED"
    with pytest.raises(pcg.ChainError) as exc:
        guard.add_pass("q", reads=("s",), writes=("u",), color_space="display-p3")
    assert exc.value.code == "CHAIN_FORMAT_UNSUPPORTED"


def test_format_mismatch_inserts_one_conversion_pass():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("b", deps=("a",), reads=("ta",), writes=("tb",),
                   fmt="rgba16f")
    plan = guard.build()
    assert names(plan) == ["a", "convert:ta~rgba16f|srgb", "b"]
    assert plan["conversions"] == 1
    conversion = plan["steps"][1]
    assert conversion["kind"] == "convert"
    assert list(conversion["reads"]) == ["ta"]
    assert list(conversion["writes"]) == ["ta~rgba16f|srgb"]
    assert conversion["fmt"] == "rgba16f"
    assert conversion["color_space"] == "srgb"
    assert plan["steps"][2]["reads"] == ["ta~rgba16f|srgb"]


def test_format_and_color_space_mismatch_inserts_two_conversions():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("b", deps=("a",), reads=("ta",), writes=("tb",),
                   fmt="rgba16f", color_space="linear")
    plan = guard.build()
    assert names(plan) == [
        "a", "convert:ta~rgba16f|srgb", "convert:ta~rgba16f|linear", "b",
    ]
    assert plan["conversions"] == 2
    assert plan["steps"][3]["reads"] == ["ta~rgba16f|linear"]


def test_unsupported_format_distance_is_an_explicit_error():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("b", deps=("a",), reads=("ta",), writes=("tb",),
                   fmt="rgba32f")
    with pytest.raises(pcg.ChainError) as exc:
        guard.build()
    assert exc.value.code == "CHAIN_CONVERSION_UNSUPPORTED"


def test_optional_pass_consumed_by_another_pass_cannot_be_trimmed():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("fx", deps=("a",), reads=("ta",), writes=("tfx",),
                   optional=True)
    guard.add_pass("use", deps=("a", "fx"), reads=("tfx",), writes=("tu",))
    with pytest.raises(pcg.ChainError) as exc:
        guard.build()
    assert exc.value.code == "CHAIN_TRIM_CONFLICT"


def test_budget_trims_optional_passes_from_the_tail_forward():
    guard = make(frame_budget_ms=4.0)
    guard.set_source("scene")
    guard.add_pass("p1", reads=("scene",), writes=("t1",), cost_ms=2.0)
    guard.add_pass("p2", deps=("p1",), reads=("t1",), writes=("t2",),
                   cost_ms=1.0)
    guard.add_pass("p3", deps=("p2",), reads=("t2",), writes=("t3",),
                   cost_ms=2.0, optional=True)
    guard.add_pass("p4", deps=("p2",), reads=("t2",), writes=("t4",),
                   cost_ms=1.0, optional=True)
    plan = guard.build()
    assert names(plan) == ["p1", "p2", "p4"]
    assert plan["trimmed"] == ["p3"]
    assert plan["cost_ms"] == pytest.approx(4.0)
    assert plan["over_budget"] is False
    assert plan["buffers"] == {"t1": 0, "t2": 1, "t4": 0}


def test_run_reuses_upstream_and_skips_disabled_downstream():
    guard = make()
    guard.set_source("scene")
    guard.add_pass("a", reads=("scene",), writes=("ta",))
    guard.add_pass("b", deps=("a",), reads=("ta",), writes=("tb",))
    guard.add_pass("c", deps=("b",), reads=("tb",), writes=("tc",))
    guard.add_pass("d", deps=("a",), reads=("ta",), writes=("td",))

    first = guard.run()
    assert first["executed"] == ["a", "b", "c", "d"]
    assert first["cached"] == []

    second = guard.run()
    assert second["executed"] == []
    assert second["cached"] == ["a", "b", "c", "d"]

    assert guard.set_enabled("b", False) is False
    third = guard.run()
    assert third["executed"] == []
    assert third["cached"] == ["a", "d"]
    assert third["skipped"] == ["b", "c"]

    assert guard.set_enabled("b", True) is True
    fourth = guard.run()
    assert fourth["executed"] == ["b", "c"]
    assert fourth["cached"] == ["a", "d"]
    assert fourth["skipped"] == []
    assert guard.stats()["runs"] == 4
    assert guard.stats()["executed"] == 6
    assert guard.stats()["cached"] == 8
    assert guard.stats()["skipped"] == 2


def test_snapshot_restore_keeps_chain_flags_buffers_and_stats():
    guard = chain()
    guard.run()
    guard.set_enabled("grade", False)
    plan = guard.build()
    assert names(plan) == ["blur"]
    assert plan["buffers"] == {"blurred": 0}

    state = guard.snapshot()
    state["passes"].clear()
    state["stats"]["runs"] = 99
    assert guard.stats()["runs"] == 1

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.stats()["runs"] == 1
    rebuilt = restored.build()
    assert names(rebuilt) == ["blur"]
    assert rebuilt["disabled"] == ["grade"]
    assert rebuilt["buffers"] == plan["buffers"]

    assert restored.set_enabled("grade", True) is True
    assert names(restored.build()) == ["blur", "grade"]


def test_corrupt_snapshot_and_unknown_pass_are_rejected():
    guard = chain()
    guard.build()
    good_stats = {key: 0 for key in STAT_KEYS}
    bad_states = [
        None,
        {},
        {"version": 2, "passes": {}, "sources": {}, "stats": dict(good_stats)},
        {"version": 1, "passes": "nope", "sources": {},
         "stats": dict(good_stats)},
        {"version": 1, "passes": {}, "sources": {}, "stats": {"runs": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(pcg.ChainError) as exc:
            guard.restore(bad)
        assert exc.value.code == "CHAIN_SNAPSHOT_INVALID"

    assert names(guard.build()) == ["blur", "grade"]
    with pytest.raises(pcg.ChainError) as exc:
        guard.set_enabled("ghost", True)
    assert exc.value.code == "CHAIN_UNKNOWN_PASS"

    guard.reset_stats()
    assert set(guard.stats()) == STAT_KEYS
    assert all(value == 0 for value in guard.stats().values())
