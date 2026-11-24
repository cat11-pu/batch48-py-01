import threading

import pytest

import clipboard_isolation_guard as cb

NOW = 1_800_000_000


def make(**kwargs):
    return cb.ClipboardIsolationGuard(**kwargs)


def single(**kwargs):
    guard = make(**kwargs)
    guard.open_session("console-a", user="alice")
    guard.open_session("console-b", user="alice")
    return guard


def test_write_then_read_returns_payload_and_metadata():
    guard = single()
    entry = guard.write("console-a", "hello", now=NOW)
    assert entry["entry_id"] == "e1"
    assert entry["session"] == "console-a"
    assert entry["user"] == "alice"
    assert entry["mime"] == "text/plain"
    assert entry["size"] == 5
    assert entry["sensitive"] is False
    assert entry["pinned"] is False
    assert entry["expires_at"] == NOW + 300

    data = guard.read("console-a", "e1", now=NOW)
    assert data["payload"] == "hello"
    assert data["mime"] == "text/plain"
    assert guard.stats()["written"] == 1
    assert guard.stats()["read"] == 1


def test_write_requires_an_open_session():
    guard = make()
    with pytest.raises(cb.ClipboardError) as exc:
        guard.write("console-x", "hello", now=NOW)
    assert exc.value.code == "CLIPBOARD_SESSION_INVALID"


def test_payload_must_be_non_empty_text_or_bytes():
    guard = single()
    for bad in ("", b"", 7, None):
        with pytest.raises(cb.ClipboardError) as exc:
            guard.write("console-a", bad, now=NOW)
        assert exc.value.code == "CLIPBOARD_PAYLOAD_INVALID"
    assert guard.list_entries("console-a") == []


def test_read_unknown_entry_reports_not_found():
    guard = single()
    with pytest.raises(cb.ClipboardError) as exc:
        guard.read("console-a", "e404", now=NOW)
    assert exc.value.code == "CLIPBOARD_ENTRY_NOT_FOUND"


def test_read_of_foreign_session_entry_is_denied():
    guard = single()
    guard.write("console-a", "private", now=NOW)
    with pytest.raises(cb.ClipboardError) as exc:
        guard.read("console-b", "e1", now=NOW)
    assert exc.value.code == "CLIPBOARD_CROSS_SESSION_DENIED"


def test_paste_between_sessions_of_the_same_user_copies_payload():
    guard = single()
    guard.write("console-a", b"\x01\x02\x03", mime="application/octet-stream",
                now=NOW)
    out = guard.paste("console-a", "console-b", "e1", now=NOW)
    assert out["session"] == "console-b"
    assert out["pasted_from"] == "e1"
    assert out["size"] == 3
    assert out["mime"] == "application/octet-stream"
    data = guard.read("console-b", out["entry_id"], now=NOW)
    assert data["payload"] == b"\x01\x02\x03"
    assert guard.stats()["pasted"] == 1


def test_paste_between_different_users_is_denied():
    guard = make()
    guard.open_session("console-a", user="alice")
    guard.open_session("console-b", user="bob")
    guard.write("console-a", "secret", now=NOW)
    with pytest.raises(cb.ClipboardError) as exc:
        guard.paste("console-a", "console-b", "e1", now=NOW)
    assert exc.value.code == "CLIPBOARD_CROSS_SESSION_DENIED"
    assert guard.list_entries("console-b") == []


def test_entries_expire_at_ttl_and_expire_reports_removed_count():
    guard = single()
    entry = guard.write("console-a", "code", ttl=60, now=NOW)
    assert entry["expires_at"] == NOW + 60
    assert guard.read("console-a", entry["entry_id"], now=NOW + 59)["payload"] == "code"
    with pytest.raises(cb.ClipboardError) as exc:
        guard.read("console-a", entry["entry_id"], now=NOW + 60)
    assert exc.value.code == "CLIPBOARD_ENTRY_EXPIRED"
    assert guard.expire(now=NOW + 60) == 1
    assert guard.list_entries("console-a", now=NOW + 60) == []
    assert guard.stats()["expired"] == 1


def test_entry_quota_evicts_oldest_unpinned_entry():
    guard = make(max_entries=2)
    guard.open_session("console-a", user="alice")
    guard.write("console-a", "one", now=NOW)
    guard.write("console-a", "two", now=NOW)
    guard.write("console-a", "three", now=NOW)
    ids = [item["entry_id"] for item in guard.list_entries("console-a", now=NOW)]
    assert ids == ["e2", "e3"]
    with pytest.raises(cb.ClipboardError) as exc:
        guard.read("console-a", "e1", now=NOW)
    assert exc.value.code == "CLIPBOARD_ENTRY_NOT_FOUND"
    assert guard.stats()["evicted"] == 1


def test_pinned_entries_block_count_quota():
    guard = make(max_entries=2)
    guard.open_session("console-a", user="alice")
    guard.write("console-a", "one", pinned=True, now=NOW)
    guard.write("console-a", "two", pinned=True, now=NOW)
    with pytest.raises(cb.ClipboardError) as exc:
        guard.write("console-a", "three", now=NOW)
    assert exc.value.code == "CLIPBOARD_QUOTA_EXCEEDED"
    assert len(guard.list_entries("console-a", now=NOW)) == 2


def test_pinned_entry_blocking_byte_quota_reports_exceeded():
    guard = make(max_bytes=10)
    guard.open_session("console-a", user="alice")
    guard.write("console-a", "aaaaaaaa", pinned=True, now=NOW)
    with pytest.raises(cb.ClipboardError) as exc:
        guard.write("console-a", "bbbbb", now=NOW)
    assert exc.value.code == "CLIPBOARD_QUOTA_EXCEEDED"
    assert len(guard.list_entries("console-a", now=NOW)) == 1


def test_lock_blocks_access_until_unlocked_and_logout_closes_session():
    guard = single()
    guard.write("console-a", "one", now=NOW)
    guard.lock_session("console-a")
    with pytest.raises(cb.ClipboardError) as exc:
        guard.write("console-a", "two", now=NOW)
    assert exc.value.code == "CLIPBOARD_SESSION_LOCKED"

    guard.unlock_session("console-a")
    entry = guard.write("console-a", "three", now=NOW)
    guard.logout("console-a")
    with pytest.raises(cb.ClipboardError) as exc:
        guard.read("console-a", entry["entry_id"], now=NOW)
    assert exc.value.code == "CLIPBOARD_SESSION_CLOSED"


def test_snapshot_restores_session_metadata():
    guard = single()
    guard.write("console-a", "hello", now=NOW)
    snap = guard.snapshot()

    other = make()
    other.restore(snap)
    meta = other.list_entries("console-a", now=NOW)
    assert [item["entry_id"] for item in meta] == ["e1"]
    assert meta[0]["mime"] == "text/plain"
    assert meta[0]["size"] == 5
    fresh = other.write("console-a", "next", now=NOW)
    assert fresh["entry_id"] == "e2"


def test_restore_rejects_malformed_state_without_dropping_entries():
    guard = single()
    guard.write("console-a", "hello", now=NOW)
    with pytest.raises(cb.ClipboardError) as exc:
        guard.restore({"sessions": {}})
    assert exc.value.code == "CLIPBOARD_SNAPSHOT_INVALID"
    assert [item["entry_id"] for item in guard.list_entries("console-a", now=NOW)] == [
        "e1"
    ]


def test_concurrent_writes_keep_ids_unique_and_counters_exact():
    guard = make(max_entries=32, max_bytes=4096)
    for index in range(8):
        guard.open_session("s%d" % index, user="u%d" % index)
    errors = []

    def worker(index):
        try:
            for item in range(10):
                guard.write("s%d" % index, "payload-%d-%d" % (index, item), now=NOW)
        except Exception as exc:  # pragma: no cover - only on broken locking
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["written"] == 80
    ids = set()
    for index in range(8):
        entries = guard.list_entries("s%d" % index, now=NOW)
        assert len(entries) == 10
        ids.update(item["entry_id"] for item in entries)
    assert len(ids) == 80
