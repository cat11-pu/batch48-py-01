import math
import threading

import pytest

import shadow_bias_tuner as sbt

DIAG = math.sqrt(0.5)


def make(**kwargs):
    return sbt.ShadowBiasTuner(**kwargs)


def light(light_id="sun", direction=(0.0, 0.0, 1.0), base=0.0):
    return {"id": light_id, "dir": direction, "normal_bias_base": base}


def receiver(recv_id="floor", normal=(0.0, DIAG, DIAG), texel=0.01,
             grad=0.1):
    return {"id": recv_id, "normal": normal, "texel_world": texel,
            "depth_grad": grad}


def test_slope_bias_forty_five_degrees_matches_tangent():
    tuner = make()
    value = tuner.slope_bias((0.0, 0.0, 1.0), (0.0, DIAG, DIAG), 0.01)
    assert value == pytest.approx(0.01, abs=1e-9)


def test_slope_bias_sixty_degrees_uses_tangent_ratio():
    tuner = make()
    normal = (0.0, math.sqrt(3.0) / 2.0, 0.5)
    value = tuner.slope_bias((0.0, 0.0, 1.0), normal, 0.01)
    assert value == pytest.approx(0.017320508, abs=1e-9)


def test_slope_bias_is_capped_at_max_offset():
    tuner = make()
    normal = (0.0, math.sqrt(1.0 - 0.0004), 0.02)
    value = tuner.slope_bias((0.0, 0.0, 1.0), normal, 0.01)
    assert value == pytest.approx(0.05, abs=1e-9)


def test_slope_bias_rejects_texel_outside_supported_range():
    tuner = make()
    for texel in (0.0, 11.0, -0.01):
        with pytest.raises(sbt.ShadowBiasError) as err:
            tuner.slope_bias((0.0, 0.0, 1.0), (0.0, DIAG, DIAG), texel)
        assert err.value.code == "SHADOW_TEXEL_INVALID"


def test_normal_offset_adds_half_a_texel_to_base():
    tuner = make()
    assert tuner.normal_offset(light(), 0.01, 0.001) == pytest.approx(0.006)
    assert tuner.normal_offset(light(), 0.01) == pytest.approx(0.005)


def test_normal_offset_is_capped_at_max_offset():
    tuner = make()
    value = tuner.normal_offset(light(base=0.04), 0.04)
    assert value == pytest.approx(0.05)


def test_receiver_bias_multiplies_gradient_by_texel_and_rejects_negative():
    tuner = make()
    assert tuner.receiver_bias(2.0, 0.001) == pytest.approx(0.002)
    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.receiver_bias(-0.5, 0.001)
    assert err.value.code == "SHADOW_RECEIVER_INVALID"


def test_resolve_combines_three_terms_without_flags():
    tuner = make()
    out = tuner.resolve(light(), receiver())
    assert out["light"] == "sun"
    assert out["receiver"] == "floor"
    assert out["slope_bias"] == pytest.approx(0.01, abs=1e-9)
    assert out["normal_offset"] == pytest.approx(0.005)
    assert out["receiver_bias"] == pytest.approx(0.001)
    assert out["offset"] == pytest.approx(0.016, abs=1e-9)
    assert out["floating"] is False
    assert out["clamped"] is False


def test_resolve_normalizes_non_unit_light_and_normal_vectors():
    tuner = make()
    unit = tuner.resolve(light(), receiver())
    scaled = tuner.resolve(light(direction=(0.0, 0.0, 2.0)),
                           receiver(normal=(0.0, 2.0 * DIAG, 2.0 * DIAG)))
    assert scaled["slope_bias"] == pytest.approx(unit["slope_bias"])
    assert scaled["normal_offset"] == pytest.approx(unit["normal_offset"])
    assert scaled["offset"] == pytest.approx(unit["offset"])


def test_resolve_rejects_malformed_light_and_receiver():
    tuner = make()
    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.resolve({"id": "sun"}, receiver())
    assert err.value.code == "SHADOW_LIGHT_INVALID"

    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.resolve(light(), {"id": "floor", "normal": (0.0, DIAG, DIAG)})
    assert err.value.code == "SHADOW_RECEIVER_INVALID"

    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.resolve(light(), receiver(normal=(0.0, 0.0, 0.0)))
    assert err.value.code == "SHADOW_RECEIVER_INVALID"


def test_cache_reports_hits_and_misses_per_light_receiver_pair():
    tuner = make()
    tuner.resolve(light(), receiver())
    assert tuner.cache_stats() == {"hits": 0, "misses": 1, "size": 1}

    tuner.resolve(light(), receiver())
    assert tuner.cache_stats() == {"hits": 1, "misses": 1, "size": 1}

    tuner.resolve(light(), receiver(recv_id="wall"))
    assert tuner.cache_stats() == {"hits": 1, "misses": 2, "size": 2}


def test_invalidate_by_light_and_clear_cache_keep_counters():
    tuner = make()
    tuner.resolve(light("sun"), receiver())
    tuner.resolve(light("moon"), receiver())
    assert tuner.invalidate("sun") == 1
    assert tuner.cache_stats()["size"] == 1
    assert tuner.invalidate("unknown") == 0

    tuner.clear_cache()
    assert tuner.cache_stats() == {"hits": 0, "misses": 2, "size": 0}


def test_set_limits_changes_cap_and_rejects_invalid_combinations():
    tuner = make()
    limits = tuner.set_limits(max_offset=0.02)
    assert limits["max_offset"] == 0.02
    normal = (0.0, math.sqrt(1.0 - 0.0004), 0.02)
    assert tuner.slope_bias((0.0, 0.0, 1.0), normal, 0.01) == pytest.approx(0.02)

    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.set_limits(max_offset=2.0)
    assert err.value.code == "SHADOW_LIMIT_INVALID"
    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.set_limits(texel_min=5.0, texel_max=1.0)
    assert err.value.code == "SHADOW_LIMIT_INVALID"
    assert tuner.slope_bias((0.0, 0.0, 1.0), normal, 0.01) == pytest.approx(0.02)

    narrowed = tuner.set_limits(texel_min=0.005)
    assert narrowed["texel_min"] == 0.005
    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.slope_bias((0.0, 0.0, 1.0), normal, 0.001)
    assert err.value.code == "SHADOW_TEXEL_INVALID"


def test_stats_snapshot_and_restore_roundtrip():
    tuner = make()
    expected = tuner.resolve(light(), receiver())["offset"]
    tuner.resolve(light(), receiver())
    tuner.resolve(light(), receiver(recv_id="huge", texel=100.0))

    stats = tuner.stats()
    assert stats["resolves"] == 3
    assert stats["texel_clamped"] == 1
    cache = tuner.cache_stats()
    state = tuner.snapshot()

    tuner.clear_cache()
    tuner.reset_stats()
    tuner.set_limits(max_offset=0.03)
    tuner.restore(state)

    assert tuner.stats() == stats
    assert tuner.cache_stats() == cache
    out = tuner.resolve(light(), receiver())
    assert out["offset"] == pytest.approx(expected)
    assert tuner.cache_stats()["hits"] == cache["hits"] + 1

    cache_after = tuner.cache_stats()
    stats_after = tuner.stats()
    with pytest.raises(sbt.ShadowBiasError) as err:
        tuner.restore({"limits": "bad"})
    assert err.value.code == "SHADOW_SNAPSHOT_INVALID"
    assert tuner.cache_stats() == cache_after
    assert tuner.stats() == stats_after


def test_concurrent_resolve_keeps_cache_counts_consistent():
    tuner = make()
    pairs = [(light(f"l{i}"), receiver(f"r{i}"))
             for i in range(4)]
    expected = [tuner.resolve(l, r)["offset"] for l, r in pairs]

    errors = []

    def worker():
        try:
            for _ in range(20):
                for index, (item_light, item_receiver) in enumerate(pairs):
                    out = tuner.resolve(item_light, item_receiver)
                    assert out["offset"] == pytest.approx(expected[index])
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    stats = tuner.stats()
    cache = tuner.cache_stats()
    assert stats["resolves"] == 644
    assert cache["hits"] + cache["misses"] == 644
    assert cache["size"] == 4
