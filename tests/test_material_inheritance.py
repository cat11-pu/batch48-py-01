import copy
import threading

import pytest

import material_inheritance as mi


def build_three_level():
    resolver = mi.MaterialInheritanceResolver()
    resolver.define(
        "root",
        None,
        {
            "albedo": {"type": "vec3", "value": [0.5, 0.5, 0.5]},
            "rough": {"type": "scalar", "value": 0.9},
            "metal": {"type": "scalar", "value": 0.0},
        },
    )
    resolver.define("mid", "root", {"rough": {"value": 0.6}})
    resolver.define("leaf", "mid", {"albedo": {"value": [0.2, 0.2, 0.2]}})
    return resolver


def test_resolve_merges_child_over_parent_values():
    resolver = build_three_level()
    out = resolver.resolve("leaf")
    assert out["material"] == "leaf"
    assert out["params"]["albedo"] == (0.2, 0.2, 0.2)
    assert out["params"]["rough"] == 0.6
    assert out["params"]["metal"] == 0.0


def test_resolve_sorts_params_and_tracks_origins():
    resolver = build_three_level()
    resolver.override("leaf", "metal", 1.0)
    out = resolver.resolve("leaf")
    assert list(out["params"]) == ["albedo", "metal", "rough"]
    assert out["origins"] == {"albedo": "leaf", "metal": "leaf", "rough": "mid"}
    assert resolver.resolve("leaf") == out


def test_chain_runs_root_to_self_and_descendants_sort():
    resolver = build_three_level()
    assert resolver.chain("leaf") == ["root", "mid", "leaf"]
    assert resolver.chain("root") == ["root"]
    assert resolver.descendants("root") == ["leaf", "mid"]


def test_override_unknown_param_rejected():
    resolver = build_three_level()
    with pytest.raises(mi.MaterialError) as exc:
        resolver.override("leaf", "gloss", 0.5)
    assert exc.value.code == "MATERIAL_PARAM_UNKNOWN"
    with pytest.raises(mi.MaterialError) as exc:
        resolver.override("ghost", "rough", 0.5)
    assert exc.value.code == "MATERIAL_UNKNOWN"
    assert resolver.resolve("leaf")["params"]["rough"] == 0.6


def test_override_many_is_all_or_nothing():
    resolver = build_three_level()
    with pytest.raises(mi.MaterialError) as exc:
        resolver.override_many("leaf", {"rough": 0.1, "gloss": 2.0})
    assert exc.value.code == "MATERIAL_PARAM_UNKNOWN"
    assert resolver.resolve("leaf")["params"]["rough"] == 0.6

    resolver.override_many("leaf", {"rough": 0.1, "metal": 0.25})
    out = resolver.resolve("leaf")
    assert out["params"]["rough"] == 0.1
    assert out["params"]["metal"] == 0.25
    assert out["origins"]["rough"] == "leaf"
    assert out["origins"]["metal"] == "leaf"


def test_clear_override_restores_inherited_value():
    resolver = build_three_level()
    resolver.override("leaf", "rough", 0.15)
    assert resolver.resolve("leaf")["origins"]["rough"] == "leaf"

    resolver.clear_override("leaf", "rough")
    out = resolver.resolve("leaf")
    assert out["params"]["rough"] == 0.6
    assert out["origins"]["rough"] == "mid"

    resolver.clear_override("leaf", "rough")
    assert resolver.resolve("leaf")["params"]["rough"] == 0.6


def test_scalar_value_expands_to_declared_vector_width():
    resolver = mi.MaterialInheritanceResolver()
    resolver.define(
        "root",
        None,
        {
            "tint": {"type": "vec4", "value": 2.0},
            "uv": {"type": "vec2", "value": 1.0},
        },
    )
    assert resolver.resolve("root")["params"]["tint"] == (2.0, 2.0, 2.0, 2.0)
    assert resolver.coerce("root", "uv", 3.0) == (3.0, 3.0)

    resolver.override("root", "tint", 0.5)
    assert resolver.resolve("root")["params"]["tint"] == (0.5, 0.5, 0.5, 0.5)


def test_vector_width_mismatch_rejected():
    resolver = mi.MaterialInheritanceResolver()
    resolver.define("root", None, {"color": {"type": "vec3", "value": [1.0, 0.0, 0.0]}})
    with pytest.raises(mi.MaterialError) as exc:
        resolver.override("root", "color", [1.0, 0.0])
    assert exc.value.code == "MATERIAL_TYPE_MISMATCH"
    with pytest.raises(mi.MaterialError) as exc:
        resolver.define("bad", None, {"color": {"type": "vec3", "value": [1.0, 0.0]}})
    assert exc.value.code == "MATERIAL_TYPE_MISMATCH"
    assert resolver.resolve("root")["params"]["color"] == (1.0, 0.0, 0.0)


def test_duplicate_definition_and_unknown_material_rejected():
    resolver = build_three_level()
    with pytest.raises(mi.MaterialError) as exc:
        resolver.define("root", None, {"rough": {"type": "scalar", "value": 0.1}})
    assert exc.value.code == "MATERIAL_DUPLICATE"
    assert resolver.resolve("root")["params"]["rough"] == 0.9

    for call in (lambda: resolver.resolve("ghost"),
                 lambda: resolver.chain("ghost"),
                 lambda: resolver.detach("ghost")):
        with pytest.raises(mi.MaterialError) as exc:
            call()
        assert exc.value.code == "MATERIAL_UNKNOWN"


def test_reparent_and_detach_rewire_chain_layout():
    resolver = build_three_level()
    resolver.define(
        "other",
        None,
        {
            "albedo": {"type": "vec3", "value": [0.9, 0.9, 0.9]},
            "rough": {"type": "scalar", "value": 0.3},
            "metal": {"type": "scalar", "value": 0.1},
        },
    )
    resolver.reparent("leaf", "other")
    assert resolver.chain("leaf") == ["other", "leaf"]
    assert resolver.resolve("leaf")["params"]["rough"] == 0.3
    assert resolver.descendants("mid") == []

    resolver.detach("leaf")
    assert resolver.chain("leaf") == ["leaf"]
    assert resolver.descendants("other") == []
    assert resolver.type_of("leaf", "rough") == "scalar"
    assert resolver.type_of("leaf", "albedo") == "vec3"


def test_cycle_reparent_rejected_with_ring_path():
    resolver = build_three_level()
    with pytest.raises(mi.MaterialError) as exc:
        resolver.reparent("root", "leaf")
    err = exc.value
    assert err.code == "MATERIAL_CYCLE"
    path = list(err.detail)
    assert len(path) >= 4
    assert path[0] == "root"
    assert path[-1] == "root"
    assert set(path) == {"root", "mid", "leaf"}


def test_depth_limit_rejects_thirty_third_level():
    resolver = mi.MaterialInheritanceResolver()
    resolver.define("m00", None, {})
    for index in range(1, 32):
        resolver.define(f"m{index:02d}", f"m{index - 1:02d}", {})
    assert len(resolver.chain("m31")) == 32
    with pytest.raises(mi.MaterialError) as exc:
        resolver.define("m32", "m31", {})
    assert exc.value.code == "MATERIAL_DEPTH_EXCEEDED"
    with pytest.raises(mi.MaterialError) as exc:
        resolver.chain("m32")
    assert exc.value.code == "MATERIAL_UNKNOWN"


def test_param_name_and_material_count_limits():
    resolver = mi.MaterialInheritanceResolver()
    resolver.define("root", None, {"p" * 256: {"type": "scalar", "value": 1.0}})
    with pytest.raises(mi.MaterialError) as exc:
        resolver.override("root", "p" * 257, 2.0)
    assert exc.value.code == "MATERIAL_PARAM_NAME_TOO_LONG"
    with pytest.raises(mi.MaterialError) as exc:
        resolver.define("bad", None, {"q" * 257: {"type": "scalar", "value": 1.0}})
    assert exc.value.code == "MATERIAL_PARAM_NAME_TOO_LONG"
    with pytest.raises(mi.MaterialError) as exc:
        resolver.resolve("bad")
    assert exc.value.code == "MATERIAL_UNKNOWN"

    big = mi.MaterialInheritanceResolver()
    for index in range(20000):
        big.define(f"mat-{index}", None, {})
    with pytest.raises(mi.MaterialError) as exc:
        big.define("mat-overflow", None, {})
    assert exc.value.code == "MATERIAL_TOO_MANY_MATERIALS"
    assert big.chain("mat-19999") == ["mat-19999"]


def test_snapshot_restore_preserves_chain_and_params():
    resolver = build_three_level()
    resolver.override("leaf", "metal", 0.75)
    state = resolver.snapshot()
    assert state["version"] == 1
    pristine = copy.deepcopy(state)

    state["materials"].clear()
    assert resolver.resolve("leaf")["params"]["metal"] == 0.75

    restored = mi.MaterialInheritanceResolver()
    restored.restore(pristine)
    assert restored.chain("leaf") == ["root", "mid", "leaf"]
    assert restored.resolve("leaf") == resolver.resolve("leaf")

    with pytest.raises(mi.MaterialError) as exc:
        restored.restore({"version": 99})
    assert exc.value.code == "MATERIAL_SNAPSHOT_INVALID"
    assert restored.resolve("leaf") == resolver.resolve("leaf")


def test_concurrent_override_many_and_resolve_stay_consistent():
    resolver = build_three_level()
    observed = []
    errors = []

    def writer(offset):
        try:
            for step in range(120):
                value = float((step + offset) % 2)
                resolver.override_many("leaf", {"rough": value, "metal": value})
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(120):
                params = resolver.resolve("leaf")["params"]
                observed.append((params["rough"], params["metal"]))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [
        threading.Thread(target=writer, args=(0,)),
        threading.Thread(target=writer, args=(1,)),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
        threading.Thread(target=reader),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(observed) == 360
    assert all(rough == metal for rough, metal in observed)
