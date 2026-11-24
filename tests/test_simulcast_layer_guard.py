import threading

import pytest

import simulcast_layer_guard as slg


def make(**kwargs):
    return slg.SimulcastLayerGuard(**kwargs)


def publish_all(guard):
    for layer in ("low", "mid", "high"):
        guard.publish_layer(layer)


def test_publishing_initial_layer_sets_active_layer():
    guard = make()
    assert guard.active_layer() is None
    out = guard.publish_layer("low")
    assert out == {"layer": "low", "published": True, "active": "low"}
    assert guard.active_layer() == "low"


def test_publishing_same_layer_is_idempotent():
    guard = make()
    guard.publish_layer("low")
    first = guard.publish_layer("low")
    second = guard.publish_layer("low")
    assert first == second == {"layer": "low", "published": True, "active": "low"}
    assert guard.snapshot()["published"] == ["low"]


def test_unknown_layer_on_publish_is_rejected():
    guard = make()
    with pytest.raises(slg.SimulcastError) as exc:
        guard.publish_layer("ultra")
    assert exc.value.code == "SIMULCAST_UNKNOWN_LAYER"


def test_unknown_layer_on_frame_is_rejected():
    guard = make()
    guard.publish_layer("low")
    with pytest.raises(slg.SimulcastError) as exc:
        guard.on_frame("ultra", keyframe=True)
    assert exc.value.code == "SIMULCAST_UNKNOWN_LAYER"


def test_frame_for_unpublished_layer_is_rejected():
    guard = make()
    guard.publish_layer("low")
    with pytest.raises(slg.SimulcastError) as exc:
        guard.on_frame("mid", keyframe=True)
    assert exc.value.code == "SIMULCAST_NOT_PUBLISHED"


def test_switch_to_active_layer_is_idempotent():
    guard = make()
    publish_all(guard)
    out = guard.switch("low")
    assert out == {"status": "active", "target": "low", "active": "low"}
    assert guard.pending() is None


def test_switch_to_published_layer_becomes_pending():
    guard = make()
    publish_all(guard)
    out = guard.switch("high", now_ms=1000)
    assert out == {"status": "pending", "target": "high", "active": "low"}
    assert guard.active_layer() == "low"
    assert guard.pending(now_ms=1000) == {
        "target": "high",
        "from": "low",
        "requested_ms": 1000,
    }


def test_keyframe_on_pending_target_completes_switch():
    guard = make()
    publish_all(guard)
    guard.switch("high", now_ms=1000)
    out = guard.on_frame("high", keyframe=True, now_ms=1050)
    assert out == {"layer": "high", "forward": True, "switched": True,
                   "reason": "keyframe"}
    assert guard.active_layer() == "high"
    assert guard.pending(now_ms=1050) is None


def test_non_keyframe_on_pending_target_is_not_forwarded():
    guard = make()
    publish_all(guard)
    guard.switch("high", now_ms=1000)
    out = guard.on_frame("high", keyframe=False, now_ms=1050)
    assert out == {"layer": "high", "forward": False, "switched": False,
                   "reason": "waiting_keyframe"}
    assert guard.active_layer() == "low"


def test_frame_from_previous_layer_after_switch_is_dropped():
    guard = make()
    publish_all(guard)
    guard.switch("high", now_ms=1000)
    guard.on_frame("high", keyframe=True, now_ms=1050)
    out = guard.on_frame("low", keyframe=True, now_ms=1100)
    assert out == {"layer": "low", "forward": False, "switched": False,
                   "reason": "inactive"}


def test_pause_stops_forwarding_active_layer():
    guard = make()
    guard.publish_layer("low")
    assert guard.pause() == {"paused": True, "layer": "low"}
    out = guard.on_frame("low", keyframe=True)
    assert out == {"layer": "low", "forward": False, "switched": False,
                   "reason": "paused"}


def test_resume_waits_for_keyframe_before_forwarding_again():
    guard = make()
    guard.publish_layer("low")
    guard.pause()
    assert guard.resume() == {"paused": False, "requires_keyframe": True,
                              "layer": "low"}
    blocked = guard.on_frame("low", keyframe=False)
    assert blocked["forward"] is False
    assert blocked["reason"] == "waiting_keyframe"
    resumed = guard.on_frame("low", keyframe=True)
    assert resumed == {"layer": "low", "forward": True, "switched": False,
                       "reason": "resumed"}
    steady = guard.on_frame("low", keyframe=False)
    assert steady == {"layer": "low", "forward": True, "switched": False,
                      "reason": None}


def test_quality_report_within_active_band_holds_layer():
    guard = make()
    publish_all(guard)
    out = guard.report_quality(20)
    assert out == {"desired": "low", "requested": False, "confirmations": 0,
                   "direction": "hold"}
    assert guard.active_layer() == "low"


def test_snapshot_restore_keeps_pending_switch():
    guard = make()
    publish_all(guard)
    guard.switch("mid", now_ms=500)
    snap = guard.snapshot()
    other = make()
    other.restore(snap)
    assert other.active_layer() == "low"
    assert other.pending(now_ms=500) == {"target": "mid", "from": "low",
                                        "requested_ms": 500}
    snap["pending"]["target"] = "high"
    assert guard.pending(now_ms=500)["target"] == "mid"


def test_concurrent_frames_forwarded_exactly_once_each():
    guard = make()
    guard.publish_layer("low")
    forward = [0]
    lock = threading.Lock()

    def worker():
        for _ in range(20):
            out = guard.on_frame("low", keyframe=True)
            if out["forward"]:
                with lock:
                    forward[0] += 1

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert forward[0] == 160
    assert guard.stats()["frames_forwarded"] == 160
