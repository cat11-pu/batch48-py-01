import json
import threading

import pytest

import archive_bomb_guard as ab


def guard(**limits):
    return ab.ArchiveBombGuard(limits or None)


def entry(name="a.txt", compressed=100, nesting=0):
    return {"name": name, "compressed_size": compressed, "nesting": nesting}


def test_open_returns_session_and_limits_copy():
    g = ab.ArchiveBombGuard({"max_entries": 2})
    session = g.open("s1")
    assert session["stream_id"] == "s1"
    assert set(session["limits"]) == set(ab.DEFAULT_LIMITS)
    assert session["limits"]["max_entries"] == 2
    session["limits"]["max_entries"] = 99
    assert g.limits()["max_entries"] == 2
    assert g.verdict("s1") == {
        "status": "open", "code": None, "entries": 0, "expanded": 0, "compressed": 0,
    }
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.verdict("nope")
    assert exc.value.code == "bomb.unknown_stream"


def test_entries_are_declared_once():
    g = guard()
    g.open("s1")
    first = g.declare_entry("s1", entry("a.txt", 100, 0))
    assert first == {
        "allowed": True, "name": "a.txt", "entries": 1, "nesting": 0, "compressed": 100,
    }
    second = g.declare_entry("s1", entry("b.txt", 50, 1))
    assert second["entries"] == 2
    assert second["compressed"] == 150

    with pytest.raises(ab.ArchiveBombError) as exc:
        g.declare_entry("s1", entry("a.txt", 10, 0))
    assert exc.value.code == "bomb.duplicate_entry"
    assert g.verdict("s1")["status"] == "rejected"
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.consume("s1", "a.txt", b"x")
    assert exc.value.code == "bomb.session_closed"


def test_entry_cap_aborts_session():
    g = ab.ArchiveBombGuard({"max_entries": 1})
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.declare_entry("s1", entry("b.txt", 10))
    assert exc.value.code == "bomb.entry_limit"
    assert g.verdict("s1")["code"] == "bomb.entry_limit"
    assert g.stats()["rejected"] == 1
    assert g.stats()["entries"] == 1


def test_consume_tracks_totals_and_ratio():
    g = guard()
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 100))
    out = g.consume("s1", "a.txt", b"x" * 300)
    assert out == {
        "entry": "a.txt",
        "entry_expanded": 300,
        "entry_compressed": 100,
        "total_expanded": 300,
        "total_compressed": 100,
        "ratio": 3.0,
    }
    out = g.consume("s1", "a.txt", b"y" * 200)
    assert out["entry_expanded"] == 500
    assert out["total_expanded"] == 500
    assert out["ratio"] == 5.0
    assert g.ratio("s1") == 5.0
    assert g.verdict("s1")["expanded"] == 500
    assert g.stats()["expanded"] == 500


def test_entry_expanded_limit_aborts_without_counting_chunk():
    g = ab.ArchiveBombGuard({"max_entry_expanded": 10})
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    assert g.consume("s1", "a.txt", b"x" * 10)["entry_expanded"] == 10

    with pytest.raises(ab.ArchiveBombError) as exc:
        g.consume("s1", "a.txt", b"x")
    assert exc.value.code == "bomb.entry_expanded"
    assert g.stats()["expanded"] == 10
    assert g.verdict("s1") == {
        "status": "rejected",
        "code": "bomb.entry_expanded",
        "entries": 1,
        "expanded": 10,
        "compressed": 10,
    }
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.consume("s1", "a.txt", b"x")
    assert exc.value.code == "bomb.session_closed"


def test_total_expanded_limit_aborts():
    g = ab.ArchiveBombGuard({"max_total_expanded": 15, "max_entry_expanded": 100})
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    g.declare_entry("s1", entry("b.txt", 10))
    assert g.consume("s1", "a.txt", b"x" * 10)["total_expanded"] == 10

    with pytest.raises(ab.ArchiveBombError) as exc:
        g.consume("s1", "b.txt", b"x" * 6)
    assert exc.value.code == "bomb.total_expanded"
    assert g.stats()["expanded"] == 10
    assert g.verdict("s1")["code"] == "bomb.total_expanded"


def test_ratio_limit_aborts_after_floor():
    g = ab.ArchiveBombGuard({
        "max_ratio": 10,
        "ratio_floor_bytes": 64,
        "max_entry_expanded": 100000,
    })
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 100))
    assert g.consume("s1", "a.txt", b"x" * 1000)["ratio"] == 10.0

    with pytest.raises(ab.ArchiveBombError) as exc:
        g.consume("s1", "a.txt", b"x")
    assert exc.value.code == "bomb.ratio"
    assert g.stats()["expanded"] == 1000
    assert g.verdict("s1")["code"] == "bomb.ratio"


def test_nesting_depth_tracks_and_rejects_deeper_entries():
    g = ab.ArchiveBombGuard({"max_nesting": 2})
    g.open("s1")
    for depth in range(3):
        g.declare_entry("s1", entry(f"e{depth}.zip", 10, depth))
    assert g.nesting_depth("s1") == 2

    with pytest.raises(ab.ArchiveBombError) as exc:
        g.declare_entry("s1", entry("deep.zip", 10, 3))
    assert exc.value.code == "bomb.nesting"
    assert g.verdict("s1")["status"] == "rejected"


def test_path_traversal_entries_are_refused():
    bad_names = ["/etc/passwd", "a/../../etc/shadow", "C:\\Windows\\win.ini",
                 "../x.txt", ""]
    for index, name in enumerate(bad_names):
        g = guard()
        stream_id = f"s{index}"
        g.open(stream_id)
        with pytest.raises(ab.ArchiveBombError) as exc:
            g.declare_entry(stream_id, entry(name, 10))
        assert exc.value.code == "bomb.path"
        assert g.verdict(stream_id)["code"] == "bomb.path"


def test_snapshot_restore_continues_counting():
    g = ab.ArchiveBombGuard({"max_entry_expanded": 150})
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    g.consume("s1", "a.txt", b"x" * 100)

    snap = g.snapshot()
    assert json.loads(json.dumps(snap)) == snap
    assert snap["stats"]["expanded"] == 100
    snap["limits"]["max_ratio"] = 999
    assert g.limits()["max_ratio"] == 100.0
    snap["sessions"]["s1"]["expanded"] = 1
    assert g.verdict("s1")["expanded"] == 100

    restored = ab.ArchiveBombGuard()
    restored.restore(g.snapshot())
    assert restored.verdict("s1")["expanded"] == 100
    assert restored.stats()["expanded"] == 100
    assert restored.consume("s1", "a.txt", b"x" * 40)["entry_expanded"] == 140
    with pytest.raises(ab.ArchiveBombError) as exc:
        restored.consume("s1", "a.txt", b"x" * 20)
    assert exc.value.code == "bomb.entry_expanded"


def test_corrupt_snapshot_is_refused():
    g = guard()
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    bad_states = [
        None,
        {},
        {"version": 2, "limits": {}, "stats": {}, "sessions": {}},
        {"version": 1, "limits": {}, "stats": {}, "sessions": {"s1": "broken"}},
    ]
    for bad in bad_states:
        with pytest.raises(ab.ArchiveBombError) as exc:
            g.restore(bad)
        assert exc.value.code == "bomb.snapshot_invalid"

    assert g.consume("s1", "a.txt", b"x" * 5)["total_expanded"] == 5


def test_concurrent_sessions_are_isolated():
    g = guard()
    errors = []
    results = []
    guard_lock = threading.Lock()

    def worker(index):
        try:
            stream_id = f"t{index}"
            g.open(stream_id)
            g.declare_entry(stream_id, entry("a.txt", 10, 0))
            for _ in range(3):
                g.consume(stream_id, "a.txt", b"x" * 100)
            with guard_lock:
                results.append(g.verdict(stream_id))
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert [item["expanded"] for item in results] == [300] * 6
    assert g.stats() == {
        "streams": 6, "entries": 6, "expanded": 1800, "compressed": 60, "rejected": 0,
    }


def test_stats_shape_after_mixed_outcomes():
    g = guard()
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    g.consume("s1", "a.txt", b"x" * 100)
    g.close("s1")

    g.open("s2")
    g.declare_entry("s2", entry("b.txt", 5))
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.declare_entry("s2", entry("b.txt", 5))
    assert exc.value.code == "bomb.duplicate_entry"

    assert g.stats() == {
        "streams": 2, "entries": 2, "expanded": 100, "compressed": 15, "rejected": 1,
    }


def test_limits_are_validated():
    for bad in ({"max_ratio": 0}, {"max_evil": 1}, {"max_entries": "4"},
                {"max_nesting": -1}):
        with pytest.raises(ab.ArchiveBombError) as exc:
            ab.ArchiveBombGuard(bad)
        assert exc.value.code == "bomb.limits_invalid"

    g = ab.ArchiveBombGuard()
    assert g.limits() == ab.DEFAULT_LIMITS
    detached = g.limits()
    detached["max_entries"] = 1
    assert g.limits()["max_entries"] == 1024


def test_close_is_idempotent_and_releases_input():
    g = guard()
    g.open("s1")
    g.declare_entry("s1", entry("a.txt", 10))
    out = g.close("s1")
    assert out == {
        "status": "ok", "code": None, "entries": 1, "expanded": 0, "compressed": 10,
    }
    assert g.close("s1") == out
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.consume("s1", "a.txt", b"x")
    assert exc.value.code == "bomb.session_closed"
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.open("s1")
    assert exc.value.code == "bomb.duplicate_stream"
    with pytest.raises(ab.ArchiveBombError) as exc:
        g.close("nope")
    assert exc.value.code == "bomb.unknown_stream"
