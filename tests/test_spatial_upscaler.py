import copy
import threading

import pytest

import spatial_upscaler as sp


def make_upscaler():
    upscaler = sp.SpatialUpscaler()
    upscaler.set_edge_threshold(0.1)
    return upscaler


def test_upscale_reports_output_geometry():
    upscaler = make_upscaler()
    out = upscaler.upscale(1920, 1080, 1152, 648)
    assert out["src_width"] == 1920
    assert out["src_height"] == 1080
    assert out["dst_width"] == 1152
    assert out["dst_height"] == 648
    assert out["scale_x"] == pytest.approx(0.6)
    assert out["scale_y"] == pytest.approx(0.6)
    assert out["taps"] == 16


def test_upscale_rejects_invalid_sizes():
    upscaler = make_upscaler()
    with pytest.raises(sp.UpscaleError) as exc:
        upscaler.upscale(0, 1080, 640, 360)
    assert exc.value.code == "UPSCALE_SIZE_INVALID"
    with pytest.raises(sp.UpscaleError) as exc:
        upscaler.upscale(1920, 1080, 0, 360)
    assert exc.value.code == "UPSCALE_SIZE_INVALID"
    with pytest.raises(sp.UpscaleError) as exc:
        upscaler.upscale(1920, 1080, 16385, 360)
    assert exc.value.code == "UPSCALE_SIZE_TOO_LARGE"
    assert upscaler.stats()["upscales"] == 0


def test_pixel_center_mapping_uses_half_pixel_offset():
    upscaler = make_upscaler()
    origin = upscaler.sample_point(1920, 1080, 1152, 648, 0, 0)
    assert origin["src_x"] == pytest.approx(1.0 / 3.0, abs=1e-6)
    assert origin["src_y"] == pytest.approx(1.0 / 3.0, abs=1e-6)

    inner = upscaler.sample_point(1920, 1080, 1152, 648, 100, 54)
    assert inner["src_x"] == pytest.approx(167.0, abs=1e-4)
    assert inner["src_y"] == pytest.approx(90.333333, abs=1e-4)


def test_weights_are_normalized_sixteen_taps():
    upscaler = make_upscaler()
    upscaler.set_gradient(167, 90, 0.8, 0.2)
    weights = upscaler.weights_at(1920, 1080, 1152, 648, 100, 54)
    assert len(weights) == 16
    assert all(weight >= 0.0 for weight in weights)
    assert sum(weights) == pytest.approx(1.0, abs=1e-5)


def test_weights_are_bitwise_reproducible():
    upscaler = make_upscaler()
    upscaler.set_gradient(167, 90, 0.0, 1.0)
    first = upscaler.weights_at(1920, 1080, 1152, 648, 100, 54)
    second = upscaler.weights_at(1920, 1080, 1152, 648, 100, 54)
    assert first == second


def test_gradient_changes_weight_distribution():
    horizontal = make_upscaler()
    horizontal.set_gradient(167, 90, 1.0, 0.0)
    vertical = make_upscaler()
    vertical.set_gradient(167, 90, 0.0, 1.0)
    assert horizontal.weights_at(1920, 1080, 1152, 648, 100, 54) != \
        vertical.weights_at(1920, 1080, 1152, 648, 100, 54)


def test_set_sharpen_updates_state():
    upscaler = make_upscaler()
    upscaler.set_sharpen(0.25)
    assert upscaler.sharpen_state() == {"amount": 0.25, "clamped": 0}
    upscaler.set_sharpen(1.0)
    assert upscaler.sharpen_state()["amount"] == 1.0
    assert upscaler.sharpen_state()["clamped"] == 0


def test_clamp_out_bounds():
    upscaler = make_upscaler()
    assert upscaler.clamp_out(-0.2) == 0.0
    assert upscaler.clamp_out(1.7) == 1.0
    assert upscaler.clamp_out(0.4) == pytest.approx(0.4)


def test_edge_threshold_validation():
    upscaler = sp.SpatialUpscaler()
    upscaler.set_edge_threshold(0.25)
    assert upscaler.edge_threshold() == pytest.approx(0.25)
    for bad in (0.0, -0.1, 1.5):
        with pytest.raises(sp.UpscaleError) as exc:
            upscaler.set_edge_threshold(bad)
        assert exc.value.code == "UPSCALE_THRESHOLD_INVALID"
    assert upscaler.edge_threshold() == pytest.approx(0.25)


def test_stats_count_upscales_and_queries():
    upscaler = make_upscaler()
    upscaler.upscale(1920, 1080, 1152, 648)
    upscaler.weights_at(1920, 1080, 1152, 648, 100, 54)
    upscaler.weights_at(1920, 1080, 1152, 648, 200, 54)
    stats = upscaler.stats()
    assert stats["upscales"] == 1
    assert stats["weight_queries"] == 2
    assert stats["clamped"] == 0


def test_reset_counters_zeroes_them():
    upscaler = make_upscaler()
    upscaler.upscale(1920, 1080, 1152, 648)
    upscaler.weights_at(1920, 1080, 1152, 648, 100, 54)
    upscaler.reset_counters()
    assert upscaler.stats() == {"upscales": 0, "weight_queries": 0, "clamped": 0}


def test_snapshot_restore_keeps_config_and_counters():
    upscaler = make_upscaler()
    upscaler.set_sharpen(0.25)
    upscaler.set_gradient(10, 5, 1.0, 0.0)
    upscaler.upscale(1920, 1080, 1152, 648)
    weights_before = upscaler.weights_at(1920, 1080, 1152, 648, 6, 3)

    state = upscaler.snapshot()
    assert state["version"] == 1
    pristine = copy.deepcopy(state)
    state["gradients"].clear()
    assert upscaler.weights_at(1920, 1080, 1152, 648, 6, 3) == weights_before

    restored = sp.SpatialUpscaler()
    restored.restore(pristine)
    assert restored.sharpen_state() == upscaler.sharpen_state()
    assert restored.edge_threshold() == pytest.approx(upscaler.edge_threshold())
    assert restored.weights_at(1920, 1080, 1152, 648, 6, 3) == weights_before
    assert restored.stats() == upscaler.stats()


def test_restore_rejects_corrupt_state():
    upscaler = make_upscaler()
    upscaler.set_sharpen(0.25)
    with pytest.raises(sp.UpscaleError) as exc:
        upscaler.restore({"version": 3})
    assert exc.value.code == "UPSCALE_SNAPSHOT_INVALID"
    assert upscaler.sharpen_state()["amount"] == 0.25


def test_concurrent_weight_queries_stay_normalized():
    upscaler = make_upscaler()
    errors = []

    def writer(gx):
        try:
            for step in range(60):
                if step % 2:
                    upscaler.clear_gradients()
                else:
                    upscaler.set_gradient(10 + gx, 10, 1.0, float(gx))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader(salt):
        try:
            for step in range(60):
                weights = upscaler.weights_at(1920, 1080, 1152, 648,
                                              10 + salt % 5, 10 + step % 5)
                assert len(weights) == 16
                assert all(weight >= 0.0 for weight in weights)
                assert sum(weights) == pytest.approx(1.0, abs=1e-4)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(1,)),
               threading.Thread(target=writer, args=(2,))]
    threads += [threading.Thread(target=reader, args=(index,)) for index in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []


def test_upscale_is_deterministic_and_scales():
    upscaler = make_upscaler()
    first = upscaler.upscale(1920, 1080, 1152, 648)
    second = upscaler.upscale(1920, 1080, 1152, 648)
    assert first == second
    doubled = upscaler.upscale(100, 100, 200, 200)
    assert doubled["scale_x"] == pytest.approx(2.0)
    assert doubled["scale_y"] == pytest.approx(2.0)
    assert upscaler.stats()["upscales"] == 3
