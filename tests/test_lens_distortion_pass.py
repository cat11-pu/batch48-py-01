import threading

import pytest

import lens_distortion_pass as ldp


def make(**kwargs):
    return ldp.LensDistortionPass(**kwargs)


def test_set_profile_returns_copy_and_profile_is_deep_copy():
    pass_ = make()
    out = pass_.set_profile(k1=0.2, k2=0.03, k3=0.004, ca_r=0.02, ca_b=-0.01)
    assert out == {"k1": 0.2, "k2": 0.03, "k3": 0.004,
                   "ca_r": 0.02, "ca_b": -0.01}
    out["k1"] = 9.0
    assert pass_.profile() == {"k1": 0.2, "k2": 0.03, "k3": 0.004,
                               "ca_r": 0.02, "ca_b": -0.01}
    copy = pass_.profile()
    copy["k2"] = 9.0
    assert pass_.profile()["k2"] == 0.03


def test_set_profile_rejects_non_finite_or_out_of_range_and_keeps_state():
    pass_ = make()
    pass_.set_profile(k1=0.1)
    for bad in ({"k1": 11.0}, {"ca_r": 0.6}, {"k2": float("nan")},
                {"k3": float("inf")}):
        with pytest.raises(ldp.LensError) as err:
            pass_.set_profile(**bad)
        assert err.value.code == "LENS_PROFILE_INVALID"
    assert pass_.profile()["k1"] == 0.1
    assert pass_.profile()["ca_r"] == 0.0


def test_distort_identity_profile_returns_input_coordinates():
    pass_ = make()
    out = pass_.distort((0.25, 0.8))
    assert out["g"] == pytest.approx((0.25, 0.8))
    assert out["r"] == pytest.approx((0.25, 0.8))
    assert out["b"] == pytest.approx((0.25, 0.8))
    assert out["valid"] is True


def test_distort_applies_k1_radial_factor():
    pass_ = make()
    pass_.set_profile(k1=0.2)
    out = pass_.distort((0.75, 0.5))
    assert out["g"] == pytest.approx((0.7625, 0.5))
    assert out["valid"] is True


def test_distort_applies_k2_and_k3_terms():
    pass_ = make()
    pass_.set_profile(k2=0.1, k3=0.2)
    out = pass_.distort((0.75, 0.5))
    assert out["g"] == pytest.approx((0.75234375, 0.5))


def test_distort_shifts_only_red_and_blue_channels():
    pass_ = make()
    pass_.set_profile(ca_r=0.05, ca_b=-0.05)
    out = pass_.distort((0.6, 0.5))
    assert out["g"] == pytest.approx((0.6, 0.5))
    assert out["r"] == pytest.approx((0.71, 0.5))
    assert out["b"] == pytest.approx((0.595, 0.5))
    assert out["r"] != out["b"]


def test_border_clamp_keeps_sample_inside_and_valid():
    pass_ = make()
    pass_.set_profile(k1=1.0)
    out = pass_.distort((1.0, 0.05))
    assert out["g"] == pytest.approx((1.0, 0.0))
    assert out["valid"] is True


def test_border_black_marks_out_of_range_sample_invalid():
    pass_ = make()
    pass_.border_mode("black")
    out = pass_.border_uv((1.2, 0.5))
    assert out["uv"] == pytest.approx((1.2, 0.5))
    assert out["valid"] is False
    inside = pass_.border_uv((0.9, 0.1))
    assert inside["valid"] is True


def test_border_mirror_folds_once_across_the_edge():
    pass_ = make()
    pass_.border_mode("mirror")
    assert pass_.border_uv((1.25, 0.5))["uv"] == pytest.approx((0.75, 0.5))
    assert pass_.border_uv((0.5, -0.4))["uv"] == pytest.approx((0.5, 0.4))
    assert pass_.border_uv((1.25, 0.5))["valid"] is True
    assert pass_.border() == "mirror"


def test_border_mode_rejects_unknown_mode_and_keeps_current():
    pass_ = make()
    assert pass_.border() == "clamp"
    with pytest.raises(ldp.LensError) as err:
        pass_.border_mode("wrap")
    assert err.value.code == "LENS_BORDER_MODE_INVALID"
    assert pass_.border() == "clamp"


def test_undistort_inverts_distort_and_checks_iteration_limit():
    pass_ = make()
    pass_.set_profile(k1=0.2)
    source = (0.8, 0.5)
    distorted = pass_.distort(source)["g"]
    out = pass_.undistort(distorted)
    assert out["converged"] is True
    assert 1 <= out["iterations"] <= 16
    assert out["uv"] == pytest.approx(source, abs=1e-6)

    with pytest.raises(ldp.LensError) as err:
        pass_.undistort(distorted, max_iter=0)
    assert err.value.code == "LENS_ITER_INVALID"
    with pytest.raises(ldp.LensError) as err:
        pass_.undistort(distorted, max_iter=65)
    assert err.value.code == "LENS_ITER_INVALID"


def test_sample_grid_geometry_and_step_validation():
    pass_ = make()
    grid = pass_.sample_grid(5, 4, 2)
    assert grid["width"] == 5
    assert grid["height"] == 4
    assert grid["step"] == 2
    assert grid["cols"] == 3
    assert grid["rows"] == 2
    assert grid["count"] == 6
    assert len(grid["cells"]) == 6
    assert grid["cells"][0]["u"] == 0
    assert grid["cells"][0]["v"] == 0
    assert grid["cells"][0]["valid"] is True
    assert grid["cells"][-1]["u"] == 4
    assert grid["cells"][-1]["v"] == 2

    with pytest.raises(ldp.LensError) as err:
        pass_.sample_grid(5, 5, 0)
    assert err.value.code == "LENS_STEP_INVALID"


def test_sample_grid_rejects_oversized_table_without_changing_profile():
    pass_ = make()
    pass_.set_profile(k1=0.1)
    pass_.border_mode("black")
    with pytest.raises(ldp.LensError) as err:
        pass_.sample_grid(16385, 8, 1)
    assert err.value.code == "LENS_GRID_TOO_LARGE"
    with pytest.raises(ldp.LensError) as err:
        pass_.sample_grid(0, 8, 1)
    assert err.value.code == "LENS_GRID_TOO_LARGE"
    assert pass_.profile()["k1"] == 0.1
    assert pass_.border() == "black"
    assert pass_.last_grid() is None


def test_stats_snapshot_and_restore_roundtrip():
    pass_ = make()
    pass_.set_profile(k1=0.15, ca_r=0.02)
    pass_.border_mode("black")
    pass_.distort((0.75, 0.5))
    pass_.distort((0.4, 0.4))
    pass_.distort((1.03, 0.5))
    grid = pass_.sample_grid(4, 4, 2)
    assert pass_.stats() == {"distorts": 3, "undistorts": 0,
                             "grids": 1, "rejected": 1}

    state = pass_.snapshot()
    expected = pass_.distort((0.7, 0.5))
    pass_.set_profile(k1=-0.4)
    pass_.border_mode("mirror")
    pass_.sample_grid(2, 2, 1)
    pass_.restore(state)

    assert pass_.profile()["k1"] == pytest.approx(0.15)
    assert pass_.border() == "black"
    assert pass_.distort((0.7, 0.5)) == expected
    assert pass_.last_grid() == grid
    assert pass_.stats() == {"distorts": 4, "undistorts": 0,
                             "grids": 1, "rejected": 1}

    with pytest.raises(ldp.LensError) as err:
        pass_.restore({"profile": "bad"})
    assert err.value.code == "LENS_SNAPSHOT_INVALID"
    assert pass_.profile()["k1"] == pytest.approx(0.15)


def test_concurrent_distort_sees_complete_profile_swaps():
    pass_ = make()
    pass_.set_profile(k1=0.2)
    ref_a = pass_.distort((0.7, 0.5))["g"]
    pass_.set_profile(k1=-0.2)
    ref_b = pass_.distort((0.7, 0.5))["g"]

    stop = threading.Event()
    errors = []

    def swapper():
        try:
            toggle = True
            while not stop.is_set():
                pass_.set_profile(k1=0.2 if toggle else -0.2)
                toggle = not toggle
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    def reader():
        try:
            for _ in range(150):
                out = pass_.distort((0.7, 0.5))["g"]
                assert out == pytest.approx(ref_a) or out == pytest.approx(ref_b)
                assert out[1] == pytest.approx(0.5)
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    writer = threading.Thread(target=swapper)
    readers = [threading.Thread(target=reader) for _ in range(4)]
    writer.start()
    for thread in readers:
        thread.start()
    for thread in readers:
        thread.join()
    stop.set()
    writer.join()

    assert errors == []
