import threading

import pytest

import message_fragment_guard as fg


def make(**kwargs):
    base = {
        "max_inflight_per_stream": 4,
        "max_bytes_per_stream": 65536,
        "max_fragments_per_stream": 64,
        "reassembly_timeout": 5.0,
    }
    base.update(kwargs)
    return fg.MessageFragmentGuard(**base)


def test_single_fragment_message_is_delivered():
    guard = make()
    out = guard.on_fragment("s1", 1, 1, 0, 1, 3, b"abc", now=0.0)
    assert out == b"abc"
    assert guard.stream_state("s1") == {
        "inflight": 0, "received_bytes": 0, "reserved_bytes": 0, "fragments": 0,
    }
    assert guard.pending_messages("s1") == []
    assert guard.stats()["delivered"] == 1


def test_multi_fragment_in_order_is_delivered_on_last():
    guard = make()
    assert guard.on_fragment("s1", 2, 1, 0, 3, 9, b"aaa", now=0.0) is None
    assert guard.on_fragment("s1", 2, 1, 1, 3, 9, b"bbb", now=0.0) is None
    assert guard.on_fragment("s1", 2, 1, 2, 3, 9, b"ccc", now=0.0) == b"aaabbbccc"
    assert guard.stats()["delivered"] == 1
    assert guard.stream_state("s1")["inflight"] == 0


def test_shuffled_arrival_is_assembled_in_index_order():
    guard = make()
    assert guard.on_fragment("s1", 3, 1, 2, 4, 16, b"CCCC", now=0.0) is None
    assert guard.on_fragment("s1", 3, 1, 0, 4, 16, b"AAAA", now=0.0) is None
    assert guard.on_fragment("s1", 3, 1, 3, 4, 16, b"DDDD", now=0.0) is None
    assert guard.on_fragment("s1", 3, 1, 1, 4, 16, b"BBBB", now=0.0) == b"AAAABBBBCCCCDDDD"
    assert guard.stats()["delivered"] == 1


def test_duplicate_fragment_is_ignored_without_byte_growth():
    guard = make()
    assert guard.on_fragment("s1", 4, 1, 0, 2, 6, b"abc", now=0.0) is None
    assert guard.on_fragment("s1", 4, 1, 0, 2, 6, b"abc", now=0.0) is None
    assert guard.stats()["duplicate_ignored"] == 1
    assert guard.stream_state("s1")["received_bytes"] == 3
    assert guard.on_fragment("s1", 4, 1, 1, 2, 6, b"def", now=0.0) == b"abcdef"


def test_index_out_of_range_rejects_whole_message():
    guard = make()
    assert guard.on_fragment("s1", 5, 1, 0, 2, 6, b"abc", now=0.0) is None
    with pytest.raises(fg.FragmentError) as exc:
        guard.on_fragment("s1", 5, 1, 2, 2, 6, b"def", now=0.0)
    assert exc.value.code == "FRAG_INDEX_OUT_OF_RANGE"
    assert guard.stream_state("s1") == {
        "inflight": 0, "received_bytes": 0, "reserved_bytes": 0, "fragments": 0,
    }
    assert guard.stats()["rejected"] == 1


def test_count_or_total_length_mismatch_rejects_whole_message():
    guard = make()
    assert guard.on_fragment("s1", 6, 1, 0, 2, 6, b"abc", now=0.0) is None
    with pytest.raises(fg.FragmentError) as exc:
        guard.on_fragment("s1", 6, 1, 1, 3, 6, b"def", now=0.0)
    assert exc.value.code == "FRAG_INCONSISTENT"
    assert guard.stream_state("s1")["inflight"] == 0
    assert guard.stats()["rejected"] == 1


def test_invalid_fragment_arguments_are_rejected():
    guard = make()
    bad_calls = [
        {"stream": "", "msg_id": 1, "epoch": 1, "index": 0, "count": 1,
         "total_len": 3, "data": b"abc"},
        {"stream": "s1", "msg_id": -1, "epoch": 1, "index": 0, "count": 1,
         "total_len": 3, "data": b"abc"},
        {"stream": "s1", "msg_id": 1, "epoch": -1, "index": 0, "count": 1,
         "total_len": 3, "data": b"abc"},
        {"stream": "s1", "msg_id": 1, "epoch": 1, "index": 0, "count": 0,
         "total_len": 3, "data": b"abc"},
        {"stream": "s1", "msg_id": 1, "epoch": 1, "index": -1, "count": 2,
         "total_len": 3, "data": b"abc"},
        {"stream": "s1", "msg_id": 1, "epoch": 1, "index": 0, "count": 1,
         "total_len": -1, "data": b"abc"},
        {"stream": "s1", "msg_id": 1, "epoch": 1, "index": 0, "count": 1,
         "total_len": 3, "data": "abc"},
    ]
    for call in bad_calls:
        with pytest.raises(fg.FragmentError) as exc:
            guard.on_fragment(now=0.0, **call)
        assert exc.value.code == "FRAG_INVALID_FRAGMENT"
    assert guard.stream_state("s1")["inflight"] == 0


def test_assembled_length_must_match_declared_total():
    guard = make()
    assert guard.on_fragment("s1", 7, 1, 0, 2, 10, b"aaa", now=0.0) is None
    with pytest.raises(fg.FragmentError) as exc:
        guard.on_fragment("s1", 7, 1, 1, 2, 10, b"bbb", now=0.0)
    assert exc.value.code == "FRAG_LENGTH_MISMATCH"
    assert guard.stream_state("s1")["inflight"] == 0
    assert guard.stats()["rejected"] == 1
    assert guard.stats()["delivered"] == 0


def test_partial_message_is_reported_with_missing_indices():
    guard = make()
    assert guard.on_fragment("s1", 8, 1, 0, 3, 9, b"aaa", now=0.0) is None
    assert guard.pending_messages("s1") == [
        {"msg_id": 8, "epoch": 1, "count": 3, "total_len": 9,
         "have": [0], "received_bytes": 3},
    ]
    assert guard.stream_state("s1") == {
        "inflight": 1, "received_bytes": 3, "reserved_bytes": 9, "fragments": 1,
    }


def test_inflight_message_ceiling_per_stream():
    guard = make(max_inflight_per_stream=2)
    assert guard.on_fragment("s1", 10, 1, 0, 2, 4, b"aa", now=0.0) is None
    assert guard.on_fragment("s1", 11, 1, 0, 2, 4, b"bb", now=0.0) is None
    with pytest.raises(fg.FragmentError) as exc:
        guard.on_fragment("s1", 12, 1, 0, 2, 4, b"cc", now=0.0)
    assert exc.value.code == "FRAG_STREAM_LIMIT"
    assert guard.stream_state("s1")["inflight"] == 2


def test_fragment_budget_per_stream_is_enforced():
    guard = make(max_fragments_per_stream=4)
    assert guard.on_fragment("s1", 13, 1, 0, 3, 6, b"aa", now=0.0) is None
    with pytest.raises(fg.FragmentError) as exc:
        guard.on_fragment("s1", 14, 1, 0, 2, 4, b"bb", now=0.0)
    assert exc.value.code == "FRAG_STREAM_LIMIT"
    assert guard.stream_state("s1")["fragments"] == 1


def test_byte_budget_per_stream_is_released_after_delivery():
    guard = make(max_bytes_per_stream=100)
    assert guard.on_fragment("s1", 15, 1, 0, 2, 60, b"a" * 30, now=0.0) is None
    with pytest.raises(fg.FragmentError) as exc:
        guard.on_fragment("s1", 16, 1, 0, 2, 60, b"b" * 30, now=0.0)
    assert exc.value.code == "FRAG_STREAM_LIMIT"
    assert guard.on_fragment("s1", 15, 1, 1, 2, 60, b"c" * 30, now=0.0) == b"a" * 30 + b"c" * 30
    assert guard.on_fragment("s1", 16, 1, 0, 2, 60, b"b" * 30, now=0.0) is None
    assert guard.stream_state("s1")["inflight"] == 1


def test_expire_reports_missing_and_close_stream_drops_the_rest():
    guard = make(max_inflight_per_stream=4, reassembly_timeout=5.0)
    assert guard.on_fragment("s1", 20, 1, 0, 3, 9, b"aaa", now=0.0) is None
    assert guard.on_fragment("s1", 21, 1, 0, 2, 6, b"bbb", now=4.0) is None
    assert guard.on_fragment("s1", 22, 1, 0, 3, 9, b"ccc", now=4.5) is None
    assert guard.expire(now=5.0) == [
        {"stream": "s1", "msg_id": 20, "epoch": 1, "missing": [1, 2]},
    ]
    assert guard.stats()["timeout_dropped"] == 1
    assert guard.stream_state("s1")["inflight"] == 2
    assert guard.expire(now=9.1) == [
        {"stream": "s1", "msg_id": 21, "epoch": 1, "missing": [1]},
    ]
    assert guard.drop_message("s1", 22, 1) is True
    assert guard.drop_message("s1", 22, 1) is False
    assert guard.stream_state("s1")["inflight"] == 0
    assert guard.stream_state("s1")["reserved_bytes"] == 0
    assert guard.on_fragment("s1", 23, 1, 0, 2, 4, b"dd", now=10.0) is None
    assert guard.close_stream("s1") == 1
    assert guard.stats()["closed_dropped"] == 1
    assert guard.stream_state("s1")["inflight"] == 0


def test_snapshot_restore_continues_pending_reassembly():
    guard = make()
    assert guard.on_fragment("s1", 30, 1, 0, 2, 6, b"abc", now=0.5) is None
    state = guard.snapshot()
    assert state["version"] == 1
    restored = make()
    restored.restore(state)
    assert restored.stats() == guard.stats()
    assert restored.stream_state("s1") == guard.stream_state("s1")
    assert restored.pending_messages("s1") == guard.pending_messages("s1")
    assert restored.on_fragment("s1", 30, 1, 1, 2, 6, b"def", now=0.6) == b"abcdef"
    assert restored.stats()["delivered"] == 1
    with pytest.raises(fg.FragmentError) as exc:
        restored.restore({"version": 4})
    assert exc.value.code == "FRAG_SNAPSHOT_INVALID"
    assert restored.stats()["delivered"] == 1


def test_concurrent_duplicate_fragments_deliver_exactly_once():
    guard = make(max_inflight_per_stream=2, max_bytes_per_stream=4096,
                 max_fragments_per_stream=64)
    delivered = []
    guard_lock = threading.Lock()
    failures = []

    def worker(index):
        try:
            out = guard.on_fragment("s1", 40, 1, index, 10, 20,
                                    bytes([index]) * 2, now=1.0)
        except Exception as exc:  # pragma: no cover - surfaced through failures
            failures.append(repr(exc))
            return
        if out is not None:
            with guard_lock:
                delivered.append(out)

    threads = [threading.Thread(target=worker, args=(index,))
               for index in range(10) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    expected = b"".join(bytes([index]) * 2 for index in range(10))
    assert delivered == [expected]
    assert guard.stats()["delivered"] == 1
    assert guard.stats()["duplicate_ignored"] == 30
    assert guard.stream_state("s1")["inflight"] == 0
