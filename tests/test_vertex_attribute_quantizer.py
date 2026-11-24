import copy
import json
import threading

import pytest

import vertex_attribute_quantizer as vq

POS_UV = {
    "POSITION": {"kind": "position", "bits": 16},
    "TEXCOORD_0": {"kind": "uv", "bits": 8},
}

MESH = {
    "attributes": {
        "POSITION": [
            [0.0, 0.0, 0.0],
            [0.7, 0.0, 0.1],
            [0.7, 0.5, 0.1],
            [0.0, 0.5, 0.0],
        ],
        "TEXCOORD_0": [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]],
    },
    "indices": [0, 1, 2, 0, 2, 3],
}


def make(**kwargs):
    quantizer = vq.VertexAttributeQuantizer(**kwargs)
    quantizer.declare_layout("std", POS_UV)
    return quantizer


def test_declare_layout_reports_versions_and_semantics():
    quantizer = vq.VertexAttributeQuantizer()
    info = quantizer.declare_layout("std", POS_UV)
    assert info["layout_id"] == "std"
    assert info["version"] == 1
    assert info["total_bits_per_vertex"] == 64
    assert info["semantics"] == [
        {"name": "POSITION", "kind": "position", "bits": 16, "components": 3},
        {"name": "TEXCOORD_0", "kind": "uv", "bits": 8, "components": 2},
    ]
    assert quantizer.layout_info("std") == info
    assert quantizer.stats()["layouts_declared"] == 1


def test_declare_layout_rejects_unknown_kind_and_bad_bits():
    quantizer = vq.VertexAttributeQuantizer()
    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.declare_layout("bad-kind", {"A": {"kind": "normalmap", "bits": 8}})
    assert exc.value.code == "QUANT_INVALID_LAYOUT"
    for bits in (2, 26, 9):
        with pytest.raises(vq.QuantizerError) as exc:
            quantizer.declare_layout("bad-bits", {"A": {"kind": "uv", "bits": bits}})
        assert exc.value.code == "QUANT_INVALID_LAYOUT"
    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.declare_layout("", {"A": {"kind": "uv", "bits": 8}})
    assert exc.value.code == "QUANT_INVALID_LAYOUT"
    assert quantizer.stats()["layouts_declared"] == 0
    assert quantizer.stats()["rejected"] == 5


def test_declare_layout_enforces_semantic_and_bit_budgets():
    quantizer = vq.VertexAttributeQuantizer(max_semantics=2, max_bits_per_vertex=96)
    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.declare_layout("wide", {
            "P": {"kind": "position", "bits": 24},
            "N": {"kind": "normal", "bits": 24},
        })
    assert exc.value.code == "QUANT_LAYOUT_BUDGET"
    assert exc.value.detail["total_bits_per_vertex"] == 144
    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.declare_layout("many", {
            "P": {"kind": "position", "bits": 16},
            "U": {"kind": "uv", "bits": 8},
            "C": {"kind": "color", "bits": 8},
        })
    assert exc.value.code == "QUANT_LAYOUT_BUDGET"
    assert exc.value.detail["semantics"] == 3
    fit = quantizer.declare_layout("fit", {"P": {"kind": "position", "bits": 24}})
    assert fit["total_bits_per_vertex"] == 72


def test_declare_layout_conflicts_and_identical_redeclare_is_idempotent():
    quantizer = vq.VertexAttributeQuantizer()
    first = quantizer.declare_layout("std", POS_UV)
    again = quantizer.declare_layout("std", copy.deepcopy(POS_UV))
    assert again == first
    assert quantizer.stats()["layouts_declared"] == 1

    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.declare_layout("std", {"POSITION": {"kind": "position", "bits": 8}}, version=1)
    assert exc.value.code == "QUANT_LAYOUT_CONFLICT"

    second = quantizer.declare_layout(
        "std", {"POSITION": {"kind": "position", "bits": 12}}, version=2)
    assert second["version"] == 2
    assert second["total_bits_per_vertex"] == 36
    assert quantizer.layout_info("std")["version"] == 2
    assert quantizer.layout_info("std", version=1)["total_bits_per_vertex"] == 64

    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.declare_layout("std", {"POSITION": {"kind": "position", "bits": 4}}, version=1)
    assert exc.value.code == "QUANT_LAYOUT_CONFLICT"


def test_error_bound_scales_bits_and_kind():
    quantizer = vq.VertexAttributeQuantizer()
    quantizer.declare_layout("std", POS_UV)
    quantizer.declare_layout("nrm", {"NORMAL": {"kind": "normal", "bits": 8}})

    assert quantizer.error_bound("std", "POSITION") == pytest.approx(1.0 / 65535)
    assert quantizer.error_bound("std", "POSITION", extent=4.0) == pytest.approx(4.0 / 65535)
    assert quantizer.error_bound("std", "TEXCOORD_0") == pytest.approx(1.0 / 255)
    assert quantizer.error_bound("nrm", "NORMAL") == pytest.approx(2.0 / 255)
    assert quantizer.error_bound("nrm", "NORMAL", extent=9.0) == pytest.approx(2.0 / 255)

    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.error_bound("std", "COLOR")
    assert exc.value.code == "QUANT_INVALID_LAYOUT"


def test_quantize_mesh_emits_bounded_integer_payload():
    quantizer = make()
    encoded = quantizer.quantize_mesh(MESH, "std")
    assert encoded["layout_id"] == "std"
    assert encoded["layout_version"] == 1
    assert encoded["vertex_count"] == 4
    assert encoded["bits"] == {"POSITION": 16, "TEXCOORD_0": 8}
    assert encoded["lo"]["POSITION"] == [0.0, 0.0, 0.0]
    assert encoded["hi"]["POSITION"] == [0.7, 0.5, 0.1]

    position = encoded["data"]["POSITION"]
    assert len(position) == 4
    assert all(len(row) == 3 for row in position)
    assert all(0 <= value <= 65535 for row in position for value in row)
    assert position[0] == [0, 0, 0]
    assert position[2] == [65535, 65535, 65535]
    assert position[3] == [0, 65535, 0]
    assert encoded["data"]["TEXCOORD_0"][1] == [255, 0]
    assert encoded["indices"] == [0, 1, 2, 0, 2, 3]
    assert quantizer.payload_bytes(encoded) == 32
    assert isinstance(encoded["fingerprint"], str)
    assert len(encoded["fingerprint"]) == 64


def test_dequantize_positions_within_error_bound():
    quantizer = make()
    encoded = quantizer.quantize_mesh(MESH, "std")
    decoded = quantizer.dequantize_mesh(encoded)
    assert decoded["vertex_count"] == 4
    assert decoded["layout_id"] == "std"
    assert decoded["layout_version"] == 1

    bound = quantizer.error_bound("std", "POSITION")
    pairs = zip(decoded["attributes"]["POSITION"], MESH["attributes"]["POSITION"])
    for got, want in pairs:
        assert len(got) == 3
        for actual, expected in zip(got, want):
            assert abs(actual - expected) <= bound

    assert decoded["attributes"]["TEXCOORD_0"][1] == pytest.approx([1.0, 0.0])
    assert decoded["indices"] == [0, 1, 2, 0, 2, 3]
    assert quantizer.stats()["meshes_dequantized"] == 1


def test_quantize_rejects_unknown_layout_and_bad_mesh():
    quantizer = vq.VertexAttributeQuantizer()
    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.quantize_mesh(MESH, "missing")
    assert exc.value.code == "QUANT_UNKNOWN_LAYOUT"

    quantizer.declare_layout("std", POS_UV)
    bad_meshes = [
        {"attributes": {"POSITION": [[0.0, 0.0, 0.0]]}},
        {"attributes": {"POSITION": [[0.0, 0.0, 0.0], [0.0, 0.0]],
                        "TEXCOORD_0": [[0.0, 0.0], [0.0, 0.0]]}},
        {"attributes": {"POSITION": [[0.0, 0.0], [0.0, 0.0]],
                        "TEXCOORD_0": [[0.0, 0.0], [0.0, 0.0]]}},
        {"attributes": {"POSITION": [[0.0, 0.0, 0.0]], "TEXCOORD_0": [[0.0, 0.0]],
                        "TANGENT": [[0.0, 0.0, 0.0]]}},
        {"attributes": {"POSITION": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                        "TEXCOORD_0": [[0.0, 0.0], [1.0, 1.0]]}, "indices": [0, 1, 2]},
        {"attributes": {"POSITION": [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]],
                        "TEXCOORD_0": [[0.0, 0.0], [1.0, 1.0]]}, "indices": [-1, 0, 1]},
        {"attributes": {"POSITION": [[0.0, 0.0, "x"]], "TEXCOORD_0": [[0.0, 0.0]]}},
        {"attributes": {"POSITION": [], "TEXCOORD_0": []}},
    ]
    for bad in bad_meshes:
        with pytest.raises(vq.QuantizerError) as exc:
            quantizer.quantize_mesh(bad, "std")
        assert exc.value.code == "QUANT_INVALID_MESH"

    assert quantizer.stats()["meshes_quantized"] == 0
    assert quantizer.stats()["rejected"] == 9


def test_quantize_rejects_vertex_count_over_cap():
    quantizer = vq.VertexAttributeQuantizer(max_vertices=3)
    quantizer.declare_layout("std", POS_UV)
    with pytest.raises(vq.QuantizerError) as exc:
        quantizer.quantize_mesh(MESH, "std")
    assert exc.value.code == "QUANT_MESH_TOO_LARGE"
    assert exc.value.detail["vertex_count"] == 4
    assert exc.value.detail["max_vertices"] == 3

    small = {
        "attributes": {
            "POSITION": [[0.0, 0.0, 0.0]] * 3,
            "TEXCOORD_0": [[0.0, 0.0]] * 3,
        },
    }
    assert quantizer.quantize_mesh(small, "std")["vertex_count"] == 3


def test_quantize_is_deterministic_across_attribute_order():
    first = make().quantize_mesh(MESH, "std")
    reordered = {
        "indices": list(MESH["indices"]),
        "attributes": {
            "TEXCOORD_0": copy.deepcopy(MESH["attributes"]["TEXCOORD_0"]),
            "POSITION": copy.deepcopy(MESH["attributes"]["POSITION"]),
        },
    }
    second = make().quantize_mesh(reordered, "std")
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    assert first["fingerprint"] == second["fingerprint"]

    quantizer = make()
    assert quantizer.quantize_mesh(MESH, "std")["fingerprint"] == first["fingerprint"]
    assert quantizer.quantize_mesh(MESH, "std")["fingerprint"] == first["fingerprint"]


def test_layout_version_selects_quantization_and_decoding():
    quantizer = vq.VertexAttributeQuantizer()
    quantizer.declare_layout("std", {"POSITION": {"kind": "position", "bits": 8}})
    mesh = {"attributes": {"POSITION": [[0.0, 0.0, 0.0], [0.3, 0.3, 0.3], [0.502, 0.502, 0.502]]}}
    coarse = quantizer.quantize_mesh(mesh, "std")
    assert coarse["layout_version"] == 1
    assert coarse["data"]["POSITION"][2] == [255, 255, 255]

    quantizer.declare_layout("std", {"POSITION": {"kind": "position", "bits": 16}}, version=2)
    fine = quantizer.quantize_mesh(mesh, "std")
    assert fine["layout_version"] == 2
    assert fine["fingerprint"] != coarse["fingerprint"]

    decoded_coarse = quantizer.dequantize_mesh(coarse)
    assert decoded_coarse["layout_version"] == 1
    middle = decoded_coarse["attributes"]["POSITION"][1]
    assert all(abs(value - 0.3) <= 1.0 / 255 for value in middle)
    assert quantizer.payload_bytes(coarse) == 9
    assert quantizer.payload_bytes(fine) == 18


def test_cached_result_is_detached_from_caller_mutation():
    quantizer = make()
    first = quantizer.quantize_mesh(MESH, "std")
    first["data"]["POSITION"][0] = [9, 9, 9]
    first["bits"]["POSITION"] = 4

    second = quantizer.quantize_mesh(MESH, "std")
    assert second["data"]["POSITION"][0] == [0, 0, 0]
    assert second["bits"]["POSITION"] == 16
    third = quantizer.quantize_mesh(MESH, "std")
    assert third["fingerprint"] == second["fingerprint"]
    assert quantizer.stats()["meshes_quantized"] == 3
    assert quantizer.stats()["cache_hits"] == 2


def test_constant_attribute_roundtrips_exactly():
    quantizer = vq.VertexAttributeQuantizer()
    quantizer.declare_layout("flat", {"POSITION": {"kind": "position", "bits": 8}})
    mesh = {"attributes": {"POSITION": [[2.5, 2.5, 2.5]] * 3}}
    encoded = quantizer.quantize_mesh(mesh, "flat")
    assert encoded["lo"]["POSITION"] == [2.5, 2.5, 2.5]
    assert encoded["hi"]["POSITION"] == [2.5, 2.5, 2.5]
    assert encoded["data"]["POSITION"] == [[0, 0, 0], [0, 0, 0], [0, 0, 0]]
    decoded = quantizer.dequantize_mesh(encoded)
    assert decoded["attributes"]["POSITION"] == [[2.5, 2.5, 2.5]] * 3


def test_snapshot_restore_and_corrupt_state():
    quantizer = make()
    encoded = quantizer.quantize_mesh(MESH, "std")
    state = quantizer.snapshot()
    pristine = copy.deepcopy(state)
    assert pristine["version"] == 1
    assert set(pristine["layouts"]) == {"std"}
    assert pristine["stats"]["meshes_quantized"] == 1

    state["layouts"]["std"]["1"]["POSITION"]["bits"] = 4
    assert quantizer.layout_info("std")["total_bits_per_vertex"] == 64

    restored = vq.VertexAttributeQuantizer()
    restored.restore(pristine)
    assert restored.stats() == quantizer.stats()
    assert restored.layout_info("std")["version"] == 1
    assert restored.dequantize_mesh(encoded)["vertex_count"] == 4

    bad_states = [
        None,
        {},
        {"version": 2, "layouts": {}, "current": {}, "stats": {}},
        {"version": 1, "layouts": [], "current": {}, "stats": {}},
        {"version": 1, "layouts": {}, "current": {}, "stats": {"meshes_quantized": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(vq.QuantizerError) as exc:
            restored.restore(bad)
        assert exc.value.code == "QUANT_SNAPSHOT_INVALID"

    assert restored.layout_info("std")["version"] == 1
    restored.drop_layout("std")
    with pytest.raises(vq.QuantizerError) as exc:
        restored.layout_info("std")
    assert exc.value.code == "QUANT_UNKNOWN_LAYOUT"


def test_concurrent_quantize_counts_and_fingerprints():
    quantizer = make()
    small = {
        "attributes": {
            "POSITION": [[0.0, 0.0, 0.0], [0.2, 0.4, 0.6]],
            "TEXCOORD_0": [[0.0, 0.0], [0.5, 0.5]],
        },
    }
    meshes = [MESH, small]
    collected = {0: [], 1: []}
    errors = []

    def worker(index):
        try:
            for round_no in range(3):
                slot = (index + round_no) % 2
                encoded = quantizer.quantize_mesh(meshes[slot], "std")
                collected[slot].append(encoded["fingerprint"])
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(set(collected[0])) == 1
    assert len(set(collected[1])) == 1
    assert len(collected[0]) + len(collected[1]) == 24
    stats = quantizer.stats()
    assert stats["meshes_quantized"] == 24
    assert stats["cache_hits"] == 22
