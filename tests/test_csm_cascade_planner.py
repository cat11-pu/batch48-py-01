import copy
import math
import threading

import pytest

import csm_cascade_planner as csm


CAMERA = {"near": 1.0, "far": 101.0, "fov": 60.0, "aspect": 1.0}


def approx(value):
    return pytest.approx(value, rel=1e-9, abs=1e-9)


def test_uniform_split_matches_lambda_zero():
    planner = csm.CsmCascadePlanner()
    planned = planner.plan(CAMERA, split_lambda=0.0, count=4)
    assert planned["count"] == 4
    assert planned["lambda"] == 0.0
    cascades = planned["cascades"]
    assert [c["index"] for c in cascades] == [0, 1, 2, 3]
    assert [c["near"] for c in cascades] == [approx(1.0), approx(26.0),
                                             approx(51.0), approx(76.0)]
    assert [c["far"] for c in cascades] == [approx(26.0), approx(51.0),
                                            approx(76.0), approx(101.0)]


def test_splits_are_monotonic_and_cover_camera_range():
    planner = csm.CsmCascadePlanner()
    planned = planner.plan(CAMERA, split_lambda=0.5, count=4)
    cascades = planned["cascades"]
    assert cascades[0]["near"] == approx(CAMERA["near"])
    assert cascades[-1]["far"] == approx(CAMERA["far"])
    for previous, current in zip(cascades, cascades[1:]):
        assert current["near"] == approx(previous["far"])
        assert current["far"] > current["near"]
    fars = [c["far"] for c in cascades]
    assert fars == sorted(fars)
    assert len(set(fars)) == 4


def test_logarithmic_mix_moves_splits_closer():
    planner = csm.CsmCascadePlanner()
    uniform = planner.plan(CAMERA, split_lambda=0.0, count=4)
    logarithmic = planner.plan(CAMERA, split_lambda=1.0, count=4)
    assert logarithmic["cascades"][0]["far"] == approx(101.0 ** 0.25)
    assert logarithmic["cascades"][1]["far"] == approx(math.sqrt(101.0))
    for uniform_cascade, log_cascade in zip(uniform["cascades"],
                                            logarithmic["cascades"]):
        assert log_cascade["far"] < uniform_cascade["far"] or (
            log_cascade["index"] == 3
        )
    assert logarithmic["cascades"][-1]["far"] == approx(101.0)


def test_invalid_lambda_keeps_previous_plan():
    planner = csm.CsmCascadePlanner()
    planned = planner.plan(CAMERA, split_lambda=0.5, count=3)
    for bad in (-0.1, 1.5, "half", None):
        with pytest.raises(csm.CsmError) as exc:
            planner.plan(CAMERA, split_lambda=bad, count=3)
        assert exc.value.code == "CSM_INVALID_LAMBDA"
    assert planner.current_plan() == planned


def test_invalid_count_and_cascade_ceiling():
    planner = csm.CsmCascadePlanner()
    planned = planner.plan(CAMERA, split_lambda=0.5, count=2)
    for bad in (0, -2, "three"):
        with pytest.raises(csm.CsmError) as exc:
            planner.plan(CAMERA, split_lambda=0.5, count=bad)
        assert exc.value.code == "CSM_INVALID_COUNT"
    with pytest.raises(csm.CsmError) as exc:
        planner.plan(CAMERA, split_lambda=0.5, count=5)
    assert exc.value.code == "CSM_COUNT_LIMIT"
    assert planner.current_plan() == planned


def test_resolution_bounds_and_plan_uses_it():
    planner = csm.CsmCascadePlanner()
    assert planner.set_resolution(1024) == 1024
    with pytest.raises(csm.CsmError) as exc:
        planner.set_resolution(0)
    assert exc.value.code == "CSM_INVALID_RESOLUTION"
    with pytest.raises(csm.CsmError) as exc:
        planner.set_resolution(4097)
    assert exc.value.code == "CSM_INVALID_RESOLUTION"
    assert planner.set_resolution(1024) == 1024
    planned = planner.plan(CAMERA, 0.0, 2)
    assert planned["resolution"] == 1024
    for cascade in planned["cascades"]:
        assert cascade["texel_size"] == approx(
            cascade["ortho_width"] / 1024
        )


def test_invalid_camera_is_rejected_without_plan_change():
    planner = csm.CsmCascadePlanner()
    planned = planner.plan(CAMERA, 0.5, 3)
    bad_cameras = [
        {"near": 0.0, "far": 101.0, "fov": 60.0, "aspect": 1.0},
        {"near": 10.0, "far": 10.0, "fov": 60.0, "aspect": 1.0},
        {"near": 20.0, "far": 10.0, "fov": 60.0, "aspect": 1.0},
        {"near": 1.0, "far": 101.0, "fov": 0.0, "aspect": 1.0},
        {"near": 1.0, "far": 101.0, "fov": 200.0, "aspect": 1.0},
        {"near": 1.0, "far": 101.0, "fov": 60.0, "aspect": 0.0},
    ]
    for camera in bad_cameras:
        with pytest.raises(csm.CsmError) as exc:
            planner.plan(camera, 0.5, 3)
        assert exc.value.code == "CSM_INVALID_CAMERA"
    assert planner.current_plan() == planned


def test_ortho_extent_and_texel_size_follow_resolution():
    planner = csm.CsmCascadePlanner(resolution=2048)
    camera = {"near": 1.0, "far": 101.0, "fov": 60.0, "aspect": 2.0}
    planned = planner.plan(camera, 0.0, 2)
    expected_width = 2.0 * 51.0 * math.tan(math.radians(30.0))
    first = planned["cascades"][0]
    assert first["far"] == approx(51.0)
    assert first["ortho_width"] == approx(expected_width)
    assert first["ortho_height"] == approx(expected_width / 2.0)
    assert first["texel_size"] == approx(expected_width / 2048.0)


def test_snap_to_texel_lands_on_lattice():
    planner = csm.CsmCascadePlanner()
    cascade = planner.plan(CAMERA, 0.0, 2)["cascades"][0]
    texel = cascade["texel_size"]
    snapped = planner.snap_to_texel(cascade, (1.1, -2.3, 0.4), texel)
    for component in snapped:
        assert component / texel == pytest.approx(round(component / texel), abs=1e-9)

    coarse = planner.snap_to_texel(cascade, (1.1, -2.3, 0.4), 0.5)
    assert coarse == (pytest.approx(1.0), pytest.approx(-2.5),
                      pytest.approx(0.5))
    other = planner.snap_to_texel(cascade, (9.9, 7.2, -0.2), 0.5)
    assert (other[0] - coarse[0]) % 0.5 == pytest.approx(0.0, abs=1e-9)


def test_snap_rejects_invalid_inputs():
    planner = csm.CsmCascadePlanner()
    cascade = planner.plan(CAMERA, 0.0, 2)["cascades"][0]
    for bad_size in (0.0, -0.25):
        with pytest.raises(csm.CsmError) as exc:
            planner.snap_to_texel(cascade, (0.0, 0.0, 0.0), bad_size)
        assert exc.value.code == "CSM_INVALID_TEXEL"
    with pytest.raises(csm.CsmError) as exc:
        planner.snap_to_texel(cascade, (0.0, 0.0), 0.5)
    assert exc.value.code == "CSM_INVALID_POSITION"
    with pytest.raises(csm.CsmError) as exc:
        planner.snap_to_texel(None, (0.0, 0.0, 0.0), 0.5)
    assert exc.value.code == "CSM_INVALID_CASCADE"


def test_blend_band_covers_tail_of_each_cascade():
    planner = csm.CsmCascadePlanner()
    planned = planner.plan(CAMERA, 0.0, 4)
    for cascade in planned["cascades"]:
        band = planner.blend_band(cascade)
        span = cascade["far"] - cascade["near"]
        assert band["width"] == approx(span * 0.1)
        assert band["start"] == approx(cascade["far"] - span * 0.1)
        assert band["end"] == approx(cascade["far"])
        assert band["start"] == approx(band["end"] - band["width"])
        assert band["start"] >= cascade["near"]


def test_stable_split_count_converges_by_distance():
    planner = csm.CsmCascadePlanner()
    expected = [
        (0.5, 1), (2500.0, 1), (2501.0, 2), (5001.0, 3),
        (7500.0, 3), (7501.0, 4), (100000.0, 4),
    ]
    for distance, count in expected:
        assert planner.stable_split_count(distance) == count
    for bad in (0.0, -3.0):
        with pytest.raises(csm.CsmError) as exc:
            planner.stable_split_count(bad)
        assert exc.value.code == "CSM_INVALID_DISTANCE"


def test_same_camera_plans_identically():
    planner = csm.CsmCascadePlanner()
    first = planner.plan(CAMERA, 0.5, 4)
    second = planner.plan(dict(CAMERA), 0.5, 4)
    assert first == second
    twin = csm.CsmCascadePlanner()
    assert twin.plan(CAMERA, 0.5, 4) == first
    assert planner.current_plan() == first


def test_snapshot_restore_keeps_resolution_and_plan():
    planner = csm.CsmCascadePlanner()
    planner.set_resolution(1024)
    planned = planner.plan(CAMERA, 0.5, 4)
    state = planner.snapshot()
    pristine = copy.deepcopy(state)
    assert set(state) == {"version", "resolution", "plan", "stats"}
    state["plan"]["cascades"][0]["far"] = 0.0
    state["resolution"] = 1
    assert planner.current_plan() == planned
    assert planner.plan(CAMERA, 0.5, 4)["resolution"] == 1024

    restored = csm.CsmCascadePlanner()
    restored.restore(pristine)
    assert restored.current_plan() == planned
    assert restored.plan(CAMERA, 0.5, 4) == planned


def test_concurrent_plan_and_resolution_changes_stay_consistent():
    planner = csm.CsmCascadePlanner()
    errors = []
    results = []

    def worker(worker_id):
        try:
            for step in range(40):
                resolution = 1024 if (worker_id + step) % 2 == 0 else 2048
                planner.set_resolution(resolution)
                results.append(planner.plan(CAMERA, 0.5, 4))
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            errors.append((worker_id, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert results
    for planned in results:
        assert planned["resolution"] in (1024, 2048)
        for cascade in planned["cascades"]:
            assert cascade["texel_size"] == approx(
                cascade["ortho_width"] / planned["resolution"]
            )
    assert planner.current_plan()["resolution"] in (1024, 2048)
