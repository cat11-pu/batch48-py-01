import copy
import json
import threading

import pytest

import mrt_attachment_planner as mrt


def base_planner(**kwargs):
    planner = mrt.MrtAttachmentPlanner(**kwargs)
    planner.declare_attachment("albedo", format="rgba8", width=640, height=480)
    planner.declare_attachment("normal", format="rgb10a2", width=640, height=480)
    planner.declare_attachment("depth", format="depth24", width=640, height=480)
    planner.declare_attachment("hdr", format="rgba16f", width=640, height=480)
    return planner


def test_declare_attachment_reports_byte_cost():
    planner = mrt.MrtAttachmentPlanner()
    info = planner.declare_attachment("albedo", format="rgba8", width=640, height=480)
    assert info == {
        "name": "albedo",
        "format": "rgba8",
        "width": 640,
        "height": 480,
        "samples": 1,
        "bytes_per_texel": 4,
        "bytes": 640 * 480 * 4,
    }
    assert planner.attachment_info("albedo") == info
    assert planner.declare_attachment("albedo", format="rgba8", width=640, height=480) == info
    assert planner.stats()["attachments_declared"] == 1

    msaa = planner.declare_attachment("albedo_msaa", format="rgba16f", width=100,
                                      height=50, samples=4)
    assert msaa["bytes_per_texel"] == 8
    assert msaa["bytes"] == 100 * 50 * 8 * 4


def test_declare_attachment_rejects_unknown_format_and_bad_dimensions():
    planner = mrt.MrtAttachmentPlanner()
    bad_calls = [
        {"name": "", "format": "rgba8", "width": 8, "height": 8},
        {"name": "a", "format": "rgb24", "width": 8, "height": 8},
        {"name": "b", "format": "rgba8", "width": 0, "height": 8},
        {"name": "c", "format": "rgba8", "width": 8, "height": -1},
        {"name": "d", "format": "rgba8", "width": 8, "height": 8, "samples": 0},
        {"name": "e", "format": "rgba8", "width": 8, "height": 8, "samples": 9},
        {"name": "f", "format": "rgba8", "width": 20000, "height": 8},
    ]
    for call in bad_calls:
        name = call.pop("name")
        with pytest.raises(mrt.MrtPlannerError) as exc:
            planner.declare_attachment(name, **call)
        assert exc.value.code == "MRT_INVALID_ATTACHMENT"
    assert planner.stats()["attachments_declared"] == 0
    assert planner.stats()["rejected"] == len(bad_calls)


def test_declare_attachment_rejects_conflict_and_attachment_limit():
    planner = mrt.MrtAttachmentPlanner(max_attachments=2)
    planner.declare_attachment("a", format="r8", width=8, height=8)
    planner.declare_attachment("b", format="r8", width=8, height=8)
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_attachment("c", format="r8", width=8, height=8)
    assert exc.value.code == "MRT_ATTACHMENT_LIMIT"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_attachment("a", format="rgba8", width=8, height=8)
    assert exc.value.code == "MRT_DUPLICATE_ATTACHMENT"
    assert planner.attachment_info("a")["format"] == "r8"


def test_declare_pass_binds_color_slots_in_declared_order():
    planner = base_planner()
    info = planner.declare_pass(
        "gbuffer",
        order=10,
        color=[("albedo", "base_color"), ("normal", "world_normal")],
        depth="depth",
    )
    assert info["name"] == "gbuffer"
    assert info["order"] == 10
    assert info["color"] == [["albedo", "base_color"], ["normal", "world_normal"]]
    assert info["depth"] == "depth"
    assert info["blend"] is False
    assert info["samples"] == 1
    assert info["reads"] == []
    assert planner.pass_info("gbuffer") == info
    assert planner.stats()["passes_declared"] == 1


def test_declare_pass_rejects_unknown_attachment_and_empty_binding():
    planner = base_planner()
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("p1", order=1, color=[("missing", "base_color")])
    assert exc.value.code == "MRT_UNKNOWN_ATTACHMENT"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("p2", order=1, color=[("albedo", "base_color")], depth="missing")
    assert exc.value.code == "MRT_UNKNOWN_ATTACHMENT"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("p3", order=1, color=[("albedo", "base_color")], reads=["missing"])
    assert exc.value.code == "MRT_UNKNOWN_ATTACHMENT"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("p4", order=1)
    assert exc.value.code == "MRT_INVALID_PASS"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("p5", order=1, color=[("albedo", "a"), ("albedo", "b")])
    assert exc.value.code == "MRT_INVALID_PASS"
    assert planner.stats()["passes_declared"] == 0


def test_declare_pass_enforces_slot_limit_and_unique_names():
    planner = mrt.MrtAttachmentPlanner(max_color_slots=2)
    for name in ("a", "b", "c"):
        planner.declare_attachment(name, format="rgba8", width=8, height=8)
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("wide", order=1,
                             color=[("a", "x"), ("b", "y"), ("c", "z")])
    assert exc.value.code == "MRT_SLOT_LIMIT"
    assert exc.value.detail["color_slots"] == 3
    ok = planner.declare_pass("narrow", order=1, color=[("a", "x")])
    assert ok["color"] == [["a", "x"]]
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("narrow", order=2, color=[("b", "y")])
    assert exc.value.code == "MRT_DUPLICATE_PASS"
    assert planner.stats()["passes_declared"] == 1


def test_declare_pass_rejects_sample_mismatch_and_wrong_depth_format():
    planner = base_planner()
    planner.declare_attachment("single", format="rgba8", width=64, height=64)
    planner.declare_attachment("quad", format="rgba8", width=64, height=64, samples=4)
    planner.declare_attachment("depth4", format="depth32f", width=64, height=64, samples=4)
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("msaa", order=1, color=[("single", "x")], samples=4)
    assert exc.value.code == "MRT_SAMPLE_MISMATCH"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("mixed", order=1,
                             color=[("single", "x"), ("quad", "y")], samples=1)
    assert exc.value.code == "MRT_SAMPLE_MISMATCH"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.declare_pass("bad-depth", order=1, color=[("albedo", "x")], depth="hdr")
    assert exc.value.code == "MRT_FORMAT_UNSUPPORTED"
    ok = planner.declare_pass("multi", order=1, color=[("quad", "y")],
                              depth="depth4", samples=4)
    assert ok["samples"] == 4
    assert ok["depth"] == "depth4"


def test_plan_sorts_passes_and_counts_unique_bytes():
    planner = base_planner()
    planner.declare_pass("late", order=20,
                         color=[("albedo", "base_color"), ("normal", "world_normal")],
                         depth="depth")
    planner.declare_pass("early", order=10,
                         color=[("albedo", "base_color"), ("hdr", "lit")])
    plan = planner.plan()
    assert [item["name"] for item in plan["passes"]] == ["early", "late"]
    assert plan["passes"][0]["bytes"] == 640 * 480 * 4 + 640 * 480 * 8
    assert plan["passes"][1]["bytes"] == 640 * 480 * 4 + 640 * 480 * 4 + 640 * 480 * 4
    assert plan["total_bytes"] == 640 * 480 * (4 + 4 + 4 + 8)
    assert plan["color_slots_used"] == 2
    assert [item["name"] for item in plan["attachments"]] == ["albedo", "depth", "hdr", "normal"]
    assert planner.stats()["plans_computed"] == 1


def test_plan_is_independent_of_declaration_order():
    first = mrt.MrtAttachmentPlanner()
    first.declare_attachment("albedo", format="rgba8", width=32, height=32)
    first.declare_attachment("hdr", format="rgba16f", width=32, height=32)
    first.declare_pass("p2", order=2, color=[("hdr", "lit")])
    first.declare_pass("p1", order=1, color=[("albedo", "base")])

    second = mrt.MrtAttachmentPlanner()
    second.declare_attachment("hdr", format="rgba16f", width=32, height=32)
    second.declare_attachment("albedo", format="rgba8", width=32, height=32)
    second.declare_pass("p1", order=1, color=[("albedo", "base")])
    second.declare_pass("p2", order=2, color=[("hdr", "lit")])

    assert json.dumps(first.plan(), sort_keys=True) == json.dumps(second.plan(), sort_keys=True)


def test_plan_rejects_total_byte_budget_exceeded():
    planner = mrt.MrtAttachmentPlanner(byte_budget=1000)
    for name in ("a", "b", "c"):
        planner.declare_attachment(name, format="rgba8", width=10, height=10)
    planner.declare_pass("p1", order=1, color=[("a", "x")])
    planner.declare_pass("p2", order=2, color=[("b", "y")])
    assert planner.plan()["total_bytes"] == 800
    planner.declare_pass("p3", order=3, color=[("c", "z")])
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.plan()
    assert exc.value.code == "MRT_BUDGET_EXCEEDED"
    assert exc.value.detail["total_bytes"] == 1200
    assert exc.value.detail["byte_budget"] == 1000
    assert planner.stats()["plans_computed"] == 1


def test_compatibility_reports_matching_and_mismatched_pairs():
    planner = base_planner()
    planner.declare_attachment("albedo_copy", format="rgba8", width=640, height=480)
    planner.declare_attachment("small", format="rgba8", width=320, height=240)
    planner.declare_attachment("msaa", format="rgba8", width=640, height=480, samples=2)

    assert planner.compatibility("albedo", "albedo") == {"compatible": True, "reasons": []}
    assert planner.compatibility("albedo", "albedo_copy") == {"compatible": True, "reasons": []}
    assert planner.compatibility("albedo", "normal") == {
        "compatible": False, "reasons": ["format_mismatch"]}
    assert planner.compatibility("albedo", "small") == {
        "compatible": False, "reasons": ["size_mismatch"]}
    assert planner.compatibility("albedo", "msaa") == {
        "compatible": False, "reasons": ["sample_mismatch"]}
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.compatibility("albedo", "missing")
    assert exc.value.code == "MRT_UNKNOWN_ATTACHMENT"


def test_alias_candidates_only_pair_disjoint_lifetimes_of_same_layout():
    planner = mrt.MrtAttachmentPlanner()
    for name in ("a", "b", "c", "d"):
        planner.declare_attachment(name, format="rgba8", width=16, height=16)
    planner.declare_attachment("f", format="r8", width=16, height=16)
    planner.declare_pass("early", order=1, color=[("a", "x"), ("c", "y")])
    planner.declare_pass("late", order=5, color=[("b", "x"), ("f", "z")])

    candidates = planner.alias_candidates()
    assert candidates == [
        {"earlier": "a", "later": "b", "bytes": 16 * 16 * 4},
        {"earlier": "c", "later": "b", "bytes": 16 * 16 * 4},
    ]
    assert planner.plan()["alias_candidates"] == candidates


def test_stats_snapshot_restore_and_corrupt_state():
    planner = base_planner()
    planner.declare_pass("gbuffer", order=1, color=[("albedo", "base_color")], depth="depth")
    planner.plan()
    state = planner.snapshot()
    pristine = copy.deepcopy(state)
    assert pristine["version"] == 1
    assert set(pristine["attachments"]) == {"albedo", "normal", "depth", "hdr"}
    assert set(pristine["passes"]) == {"gbuffer"}

    state["attachments"]["albedo"]["width"] = 1
    assert planner.attachment_info("albedo")["width"] == 640

    restored = mrt.MrtAttachmentPlanner()
    restored.restore(pristine)
    assert restored.stats() == planner.stats()
    assert restored.attachment_info("albedo") == planner.attachment_info("albedo")
    assert restored.plan()["total_bytes"] == planner.plan()["total_bytes"]

    bad_states = [
        None,
        {},
        {"version": 2, "attachments": {}, "passes": {}, "stats": {}},
        {"version": 1, "attachments": [], "passes": {}, "stats": {}},
        {"version": 1, "attachments": {}, "passes": {}, "stats": {"attachments_declared": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(mrt.MrtPlannerError) as exc:
            restored.restore(bad)
        assert exc.value.code == "MRT_SNAPSHOT_INVALID"
    assert restored.attachment_info("albedo")["width"] == 640


def test_concurrent_declare_pass_and_plan_stay_consistent():
    planner = base_planner()
    errors = []

    def worker(index):
        try:
            planner.declare_pass(f"pass-{index}", order=index,
                                 color=[("albedo", f"sem-{index}")])
            planner.plan()
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    plan = planner.plan()
    assert [item["name"] for item in plan["passes"]] == [f"pass-{i}" for i in range(8)]
    assert len({item["name"] for item in plan["passes"]}) == 8
    assert planner.stats()["passes_declared"] == 8
    assert planner.stats()["plans_computed"] == 9


def test_drop_attachment_is_refused_while_referenced():
    planner = base_planner()
    planner.declare_pass("gbuffer", order=1, color=[("albedo", "base_color")])
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.drop_attachment("albedo")
    assert exc.value.code == "MRT_IN_USE"

    planner.drop_attachment("normal")
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.attachment_info("normal")
    assert exc.value.code == "MRT_UNKNOWN_ATTACHMENT"
    with pytest.raises(mrt.MrtPlannerError) as exc:
        planner.drop_attachment("normal")
    assert exc.value.code == "MRT_UNKNOWN_ATTACHMENT"
    assert planner.stats()["rejected"] == 3
    assert [item["name"] for item in planner.plan()["attachments"]] == ["albedo", "depth", "hdr"]
