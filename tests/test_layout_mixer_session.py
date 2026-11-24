import threading

import pytest

import layout_mixer_session as lms


def session(**kwargs):
    return lms.LayoutMixerSession(**kwargs)


def test_new_session_reports_empty_canvas():
    g = session()
    assert g.revision() == 0
    layout = g.current_layout()
    assert layout["canvas"] == {"width": 1280, "height": 720}
    assert layout["slots"] == {}
    assert layout["revision"] == 0


def test_add_slot_stores_rect_z_and_revision():
    g = session()
    out = g.add_slot("main", [100, 50, 400, 300], z=5)
    assert out == {
        "slot_id": "main", "rect": [100, 50, 400, 300], "z": 5, "source": None,
    }
    info = g.slot_info("main")
    assert info["rect"] == [100, 50, 400, 300]
    assert info["z"] == 5
    assert info["source"] is None
    assert g.revision() == 1
    assert g.stats()["slots_added"] == 1


def test_add_slot_rejects_malformed_rect_and_z():
    g = session()
    for rect in ([0, 0, 0, 100], [0, 0, 100, -5], "not-a-rect", [0, 0, 100]):
        with pytest.raises(lms.LayoutError) as exc:
            g.add_slot("bad", rect)
        assert exc.value.code == "LAYOUT_INVALID_RECT"
    with pytest.raises(lms.LayoutError) as exc:
        g.add_slot("bad", [0, 0, 100, 100], z=1001)
    assert exc.value.code == "LAYOUT_INVALID_Z"
    assert g.revision() == 0
    assert g.stats()["rejected"] == 5


def test_add_slot_rejects_rect_outside_canvas():
    g = session()
    for rect in ([1200, 600, 200, 200], [-1, 0, 100, 100], [0, -1, 100, 100]):
        with pytest.raises(lms.LayoutError) as exc:
            g.add_slot("bad", rect)
        assert exc.value.code == "LAYOUT_RECT_OUT_OF_BOUNDS"
    assert g.revision() == 0


def test_duplicate_slot_slot_limit_and_source_limit():
    g = session()
    g.add_slot("a", [0, 0, 100, 100])
    with pytest.raises(lms.LayoutError) as exc:
        g.add_slot("a", [0, 0, 100, 100])
    assert exc.value.code == "LAYOUT_DUPLICATE_SLOT"

    limited = session(max_slots=2)
    limited.add_slot("a", [0, 0, 100, 100])
    limited.add_slot("b", [200, 0, 100, 100])
    with pytest.raises(lms.LayoutError) as exc:
        limited.add_slot("c", [400, 0, 100, 100])
    assert exc.value.code == "LAYOUT_SLOT_LIMIT"

    supply = session(max_sources=1)
    supply.add_slot("a", [0, 0, 100, 100])
    supply.add_slot("b", [200, 0, 100, 100])
    supply.bind_source("a", "cam1")
    with pytest.raises(lms.LayoutError) as exc:
        supply.bind_source("b", "cam2")
    assert exc.value.code == "LAYOUT_SOURCE_LIMIT"


def test_remove_slot_and_unknown_slot():
    g = session()
    g.add_slot("a", [0, 0, 100, 100])
    assert g.remove_slot("a") is None
    with pytest.raises(lms.LayoutError) as exc:
        g.slot_info("a")
    assert exc.value.code == "LAYOUT_UNKNOWN_SLOT"
    with pytest.raises(lms.LayoutError) as exc:
        g.remove_slot("a")
    assert exc.value.code == "LAYOUT_UNKNOWN_SLOT"
    assert g.revision() == 2
    assert g.stats()["slots_removed"] == 1


def test_bind_source_roundtrip_and_replace():
    g = session()
    g.add_slot("a", [0, 0, 100, 100])
    assert g.bind_source("a", "cam1") == {"slot_id": "a", "source": "cam1"}
    assert g.source_slot("cam1") == "a"
    assert g.slot_info("a")["source"] == "cam1"
    g.bind_source("a", "cam2")
    assert g.source_slot("cam1") is None
    assert g.source_slot("cam2") == "a"
    assert g.unbind_source("a") is None
    assert g.slot_info("a")["source"] is None
    assert g.stats()["binds"] == 2
    assert g.stats()["unbinds"] == 1


def test_bind_errors_for_unknown_slot_and_empty_source():
    g = session()
    with pytest.raises(lms.LayoutError) as exc:
        g.bind_source("ghost", "cam")
    assert exc.value.code == "LAYOUT_UNKNOWN_SLOT"
    with pytest.raises(lms.LayoutError) as exc:
        g.unbind_source("ghost")
    assert exc.value.code == "LAYOUT_UNKNOWN_SLOT"
    g.add_slot("a", [0, 0, 100, 100])
    with pytest.raises(lms.LayoutError) as exc:
        g.bind_source("a", "")
    assert exc.value.code == "LAYOUT_INVALID_SOURCE"


def test_apply_layout_replaces_all_slots():
    g = session()
    g.add_slot("old", [0, 0, 100, 100])
    out = g.apply_layout({"slots": {
        "a": {"rect": [0, 0, 100, 100], "z": 1, "source": "s1"},
        "b": {"rect": [200, 0, 100, 100], "z": 2},
    }})
    assert out == {"revision": 2, "slots": 2}
    assert set(g.current_layout()["slots"]) == {"a", "b"}
    assert g.source_slot("s1") == "a"
    with pytest.raises(lms.LayoutError) as exc:
        g.slot_info("old")
    assert exc.value.code == "LAYOUT_UNKNOWN_SLOT"
    assert g.revision() == 2
    assert g.stats()["layouts_applied"] == 1


def test_apply_layout_failure_leaves_previous_layout():
    g = session()
    g.add_slot("keep", [0, 0, 100, 100], z=1)
    before = g.current_layout()
    with pytest.raises(lms.LayoutError) as exc:
        g.apply_layout({"slots": {
            "bad": {"rect": [1200, 700, 200, 200], "z": 0},
        }})
    assert exc.value.code == "LAYOUT_RECT_OUT_OF_BOUNDS"
    assert g.current_layout() == before
    assert g.revision() == 1
    with pytest.raises(lms.LayoutError) as exc:
        g.apply_layout({})
    assert exc.value.code == "LAYOUT_LAYOUT_INVALID"
    assert g.current_layout() == before
    assert g.stats()["rejected"] == 2


def test_hit_test_follows_z_order():
    g = session()
    g.add_slot("back", [0, 0, 400, 400], z=1)
    g.add_slot("front", [100, 100, 400, 400], z=2)
    assert g.hit_test(150, 150) == "front"
    assert g.hit_test(50, 50) == "back"
    assert g.hit_test(700, 700) is None
    assert g.hit_test(100, 100) == "front"
    assert g.stats()["hit_tests"] == 4


def test_hit_test_equal_z_prefers_later_slot():
    g = session()
    g.add_slot("first", [0, 0, 300, 300], z=3)
    g.add_slot("second", [100, 100, 300, 300], z=3)
    assert g.hit_test(150, 150) == "second"
    assert g.hit_test(50, 50) == "first"


def test_resize_canvas_scales_slots_and_keeps_sources():
    g = session()
    g.add_slot("cam", [100, 50, 400, 300], z=2)
    g.bind_source("cam", "s1")
    out = g.resize_canvas(2560, 1440)
    assert out == {"width": 2560, "height": 1440, "revision": 2, "slots": 1}
    info = g.slot_info("cam")
    assert info["rect"] == [200, 100, 800, 600]
    assert info["z"] == 2
    assert info["source"] == "s1"
    assert g.hit_test(300, 200) == "cam"
    assert g.stats()["resizes"] == 1
    with pytest.raises(lms.LayoutError) as exc:
        g.resize_canvas(0, 720)
    assert exc.value.code == "LAYOUT_INVALID_CANVAS"


def test_snapshot_restore_and_corrupt_state():
    g = session()
    g.add_slot("a", [0, 0, 100, 100], z=1)
    g.bind_source("a", "cam1")
    layout = g.current_layout()
    layout["slots"]["a"]["rect"][0] = 999
    assert g.slot_info("a")["rect"][0] == 0
    snap = g.snapshot()
    assert {"revision", "canvas", "slots"} <= set(snap)
    g.remove_slot("a")
    g.restore(snap)
    assert g.slot_info("a")["rect"] == [0, 0, 100, 100]
    assert g.source_slot("cam1") == "a"
    with pytest.raises(lms.LayoutError) as exc:
        g.restore({"slots": 5})
    assert exc.value.code == "LAYOUT_SNAPSHOT_INVALID"
    assert g.slot_info("a")["source"] == "cam1"


def test_concurrent_slot_changes_are_serialised():
    g = session()
    barrier = threading.Barrier(4)
    failures = []

    def worker(tid):
        try:
            barrier.wait()
            for col in range(4):
                g.add_slot(
                    "s%d-%d" % (tid, col),
                    [col * 300 + 10, tid * 150 + 10, 200, 100],
                    z=tid * 10 + col,
                )
        except Exception as exc:  # pragma: no cover - surfaced after join
            failures.append(exc)

    threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    assert g.stats()["slots_added"] == 16
    assert len(g.current_layout()["slots"]) == 16
    assert g.revision() == 16
