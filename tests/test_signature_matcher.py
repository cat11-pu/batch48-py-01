import json
import threading

import pytest

import signature_matcher as sm

SIG = b"\xde\xad\xbe\xef"


def built(patterns=None, **kwargs):
    matcher = sm.SignatureMatcher(**kwargs)
    matcher.build(patterns if patterns is not None else {"p": "de ad be ef"})
    return matcher


def test_build_reports_compiled_patterns():
    matcher = sm.SignatureMatcher()
    out = matcher.build({"p1": "de ad be ef", "p2": "00 11 22 33 44"})
    assert out == {"compiled": 2, "rejected": 0}
    assert matcher.pattern_ids() == ["p1", "p2"]
    assert matcher.pattern_count() == 2
    assert matcher.build_errors() == []
    assert matcher.stats() == {"p1": 0, "p2": 0}
    assert matcher.pending_bytes() == 0


def test_feed_reports_every_hit_with_absolute_offset():
    matcher = built()
    data = b"\x00\x01\xde\xad\xbe\xef\x02\xde\xad\xbe\xef"
    assert matcher.feed(data) == [
        {"pattern_id": "p", "offset": 2, "length": 4},
        {"pattern_id": "p", "offset": 7, "length": 4},
    ]
    assert matcher.stats() == {"p": 2}
    assert matcher.hits_for("p") == 2


def test_same_offset_hits_are_sorted_by_pattern_id():
    matcher = built({
        "p_b": "aa bb cc dd ee",
        "p_c": "aa bb cc dd ee ff",
        "p_a": "aa bb cc dd",
    })
    hits = matcher.feed(bytes([0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]))
    assert [(hit["pattern_id"], hit["offset"], hit["length"]) for hit in hits] == [
        ("p_a", 0, 4), ("p_b", 0, 5), ("p_c", 0, 6),
    ]


def test_cross_chunk_match_keeps_tail_and_reports_once():
    matcher = built({"p": "de ad be ef 99"})
    assert matcher.feed(b"\x00\xde\xad") == []
    assert matcher.feed(b"\xbe\xef\x99\x01") == [
        {"pattern_id": "p", "offset": 1, "length": 5},
    ]
    assert matcher.feed(b"\xde\xad\xbe\xef\x99") == [
        {"pattern_id": "p", "offset": 7, "length": 5},
    ]
    assert matcher.stats() == {"p": 2}


def test_overlapping_matches_are_all_reported():
    matcher = built({"p": "aa aa aa aa"})
    hits = matcher.feed(b"\xaa" * 6)
    assert [(hit["offset"], hit["length"]) for hit in hits] == [
        (0, 4), (1, 4), (2, 4),
    ]


def test_masked_bytes_match_as_wildcards():
    matcher = built({
        "mask": "de ?? be ?? ef",
        "nib": "de a? be ef",
    })
    hits = matcher.feed(b"\xde\x00\xbe\xff\xef")
    assert len(hits) == 1
    assert hits[0]["pattern_id"] == "mask"

    nibble_hits = matcher.feed(b"\xde\xab\xbe\xef")
    assert len(nibble_hits) == 1
    assert nibble_hits[0]["pattern_id"] == "nib"
    assert matcher.stats() == {"mask": 1, "nib": 1}


def test_build_rejects_invalid_pattern_text():
    matcher = sm.SignatureMatcher()
    out = matcher.build({
        "short": "de ad be",
        "chars": "de zz be ef",
        "odd": "de a be ef",
        "long": " ".join(["00"] * 4097),
    })
    assert out["compiled"] == 0
    assert out["rejected"] == 4
    assert sorted((err["id"], err["code"]) for err in matcher.build_errors()) == [
        ("chars", "sig.pattern_syntax"),
        ("long", "sig.pattern_too_long"),
        ("odd", "sig.pattern_syntax"),
        ("short", "sig.pattern_too_short"),
    ]
    with pytest.raises(sm.SignatureError) as exc:
        matcher.feed(SIG)
    assert exc.value.code == "sig.not_built"

    assert matcher.build({"p": "de ad be ef"})["compiled"] == 1
    assert matcher.feed(SIG) == [{"pattern_id": "p", "offset": 0, "length": 4}]


def test_pattern_cap_rejects_excess_signatures():
    matcher = sm.SignatureMatcher(max_patterns=2)
    out = matcher.build({
        "p1": "00 11 22 33",
        "p2": "00 11 22 34",
        "p3": "00 11 22 35",
    })
    assert out == {"compiled": 2, "rejected": 1}
    assert matcher.pattern_ids() == ["p1", "p2"]
    errors = matcher.build_errors()
    assert len(errors) == 1
    assert errors[0]["code"] == "sig.pattern_limit"
    assert errors[0]["id"] == "p3"


def test_oversized_chunk_is_refused_without_advancing():
    matcher = built(max_chunk=8)
    with pytest.raises(sm.SignatureError) as exc:
        matcher.feed(b"\x00" * 9)
    assert exc.value.code == "sig.chunk_too_large"
    assert matcher.stats() == {"p": 0}
    assert matcher.pending_bytes() == 0

    assert matcher.feed(SIG) == [{"pattern_id": "p", "offset": 0, "length": 4}]


def test_reset_clears_stream_state_but_keeps_counters():
    matcher = built({"p": "aa bb cc dd"})
    assert matcher.feed(b"\x00\x00\xaa\xbb\xcc\xdd") == [
        {"pattern_id": "p", "offset": 2, "length": 4},
    ]
    matcher.reset()
    assert matcher.stats() == {"p": 1}
    assert matcher.pending_bytes() == 0
    assert matcher.feed(b"\xaa\xbb\xcc\xdd") == [
        {"pattern_id": "p", "offset": 0, "length": 4},
    ]
    assert matcher.stats() == {"p": 2}


def test_state_roundtrip_resumes_matching():
    matcher = built({"p": "de ad be ef 99"})
    assert matcher.feed(b"\x00\xde\xad") == []
    state = matcher.state()
    assert state["patterns"] == {"p": "de ad be ef 99"}
    assert state["counts"] == {"p": 0}
    json.dumps(state)
    state["counts"]["p"] = 99
    assert matcher.stats() == {"p": 0}

    resumed = sm.SignatureMatcher()
    resumed.restore(matcher.state())
    assert resumed.pattern_ids() == ["p"]
    assert resumed.feed(b"\xbe\xef\x99\x01") == [
        {"pattern_id": "p", "offset": 1, "length": 5},
    ]
    assert resumed.stats() == {"p": 1}


def test_corrupt_state_is_refused_and_matcher_stays_usable():
    matcher = built()
    bad_states = [
        None,
        {},
        {"version": 1},
        {"version": 1, "patterns": {}, "counts": {}, "offset": 0, "pending": "zz"},
    ]
    for bad in bad_states:
        with pytest.raises(sm.SignatureError) as exc:
            matcher.restore(bad)
        assert exc.value.code == "sig.state_invalid"

    assert matcher.stats() == {"p": 0}
    assert matcher.feed(SIG) == [{"pattern_id": "p", "offset": 0, "length": 4}]


def test_hits_for_and_pending_report_stream_position():
    matcher = built({"p": "aa bb cc dd ee"})
    assert matcher.feed(b"\x00\xaa\xbb") == []
    assert matcher.pending_bytes() == 3
    assert matcher.hits_for("p") == 0
    assert matcher.stats() == {"p": 0}

    with pytest.raises(sm.SignatureError) as exc:
        matcher.hits_for("missing")
    assert exc.value.code == "sig.unknown_pattern"

    assert matcher.feed(b"\xcc\xdd\xee") == [
        {"pattern_id": "p", "offset": 1, "length": 5},
    ]
    assert matcher.pending_bytes() == 4
    assert matcher.stats() == {"p": 1}


def test_feed_before_build_is_refused():
    matcher = sm.SignatureMatcher()
    with pytest.raises(sm.SignatureError) as exc:
        matcher.feed(b"")
    assert exc.value.code == "sig.not_built"

    matcher.build({"p": "de ad be ef"})
    assert matcher.feed(b"") == []
    assert matcher.stats() == {"p": 0}


def test_concurrent_feeds_keep_counts_exact():
    matcher = built({"p": "aa bb cc dd"})
    payload = b"\xaa\xbb\xcc\xdd"
    errors = []
    total = []

    def worker(index):
        try:
            seen = 0
            for _ in range(5):
                seen += len(matcher.feed(payload))
            total.append(seen)
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sum(total) == 40
    assert matcher.stats() == {"p": 40}
