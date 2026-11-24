import threading

import pytest

import caption_reading_fit_guard as crf


def guard(**kwargs):
    return crf.CaptionReadingFitGuard(**kwargs)


def nonspace(text):
    return len(text.replace(" ", ""))


def test_new_guard_reports_defaults_and_empty_table():
    g = guard()
    assert g.max_cps == 17.0
    assert g.max_columns == 42
    assert g.max_lines == 2
    assert g.min_duration_ms == 800
    assert g.cues() == []
    assert g.timeline_valid() is True
    assert g.stats()["cues_added"] == 0


def test_add_cue_returns_record_and_keeps_start_order():
    g = guard()
    out = g.add_cue(1000, 3000, "hello")
    assert out == {
        "cue_id": "cue-1",
        "start_ms": 1000,
        "end_ms": 3000,
        "text": "hello",
    }
    g.add_cue(200, 900, "earlier")
    starts = [c["start_ms"] for c in g.cues()]
    assert starts == [200, 1000]
    assert g.stats()["cues_added"] == 2


def test_add_cue_rejects_invalid_timing():
    g = guard()
    for start, end in ((1000, 1000), (2000, 1500), (-1, 500)):
        with pytest.raises(crf.CaptionError) as exc:
            g.add_cue(start, end, "bad timing")
        assert exc.value.code == "CAPTION_INVALID_TIMING"
    assert g.stats()["rejected"] == 3
    assert g.cues() == []


def test_add_cue_rejects_blank_text_and_duplicate_id():
    g = guard()
    with pytest.raises(crf.CaptionError) as exc:
        g.add_cue(0, 1000, "   ")
    assert exc.value.code == "CAPTION_INVALID_TEXT"
    g.add_cue(0, 1000, "hello", cue_id="main")
    with pytest.raises(crf.CaptionError) as exc:
        g.add_cue(1000, 2000, "again", cue_id="main")
    assert exc.value.code == "CAPTION_DUPLICATE_CUE"
    assert [c["cue_id"] for c in g.cues()] == ["main"]


def test_cps_counts_non_space_characters_over_duration():
    g = guard()
    assert g.cps_of("ab cd", 0, 2000) == 2.0
    assert g.cps_of("abc", 0, 1000) == 3.0
    assert g.cps_of("", 500, 1500) == 0.0
    with pytest.raises(crf.CaptionError) as exc:
        g.cps_of("abc", 1000, 1000)
    assert exc.value.code == "CAPTION_INVALID_TIMING"
    assert g.stats()["cps_queries"] == 3


def test_display_width_counts_ascii_columns():
    g = guard()
    assert g.display_width("abc") == 3
    assert g.display_width("a b") == 3
    assert g.display_width("") == 0
    assert g.display_width("ab cd ef") == 8


def test_wrap_text_breaks_between_words_within_limit():
    g = guard()
    assert g.wrap_text("the quick brown fox", 10) == ["the quick", "brown fox"]
    assert g.wrap_text("a b", 1) == ["a", "b"]
    assert g.wrap_text("solo", 42) == ["solo"]


def test_wrap_text_keeps_overlong_word_whole():
    g = guard()
    assert g.wrap_text("abcde fg", 3) == ["abcde", "fg"]
    with pytest.raises(crf.CaptionError) as exc:
        g.wrap_text("abc", 0)
    assert exc.value.code == "CAPTION_INVALID_WIDTH"


def test_split_cue_breaks_over_budget_cue_into_pieces():
    g = guard()
    text = "the quick brown fox jumps over"
    assert nonspace(text) == 25
    g.add_cue(1000, 2000, text)
    pieces = g.split_cue("cue-1")
    assert len(pieces) == 2
    assert pieces[0]["start_ms"] == 1000
    assert " ".join(p["text"] for p in pieces).split() == text.split()
    for piece in pieces:
        span = piece["end_ms"] - piece["start_ms"]
        assert span >= 800
        assert g.cps_of(piece["text"], piece["start_ms"], piece["end_ms"]) <= 17.0
    assert g.stats()["cues_split"] == 1


def test_split_pieces_are_contiguous_and_timeline_stays_monotonic():
    g = guard()
    g.add_cue(1000, 2000, "the quick brown fox jumps over")
    pieces = g.split_cue("cue-1")
    assert pieces[1]["start_ms"] == pieces[0]["end_ms"]
    assert pieces[1]["end_ms"] > pieces[1]["start_ms"]
    assert g.timeline_valid() is True
    assert [c["cue_id"] for c in g.cues()] == [p["cue_id"] for p in pieces]


def test_split_leaves_compliant_cue_untouched_and_rejects_unknown_ids():
    g = guard()
    g.add_cue(0, 2000, "short enough")
    before = g.cues()
    out = g.split_cue("cue-1")
    assert out == before
    assert g.stats()["cues_split"] == 0
    with pytest.raises(crf.CaptionError) as exc:
        g.split_cue("cue-9")
    assert exc.value.code == "CAPTION_CUE_NOT_FOUND"
    with pytest.raises(crf.CaptionError) as exc:
        g.remove_cue("cue-9")
    assert exc.value.code == "CAPTION_CUE_NOT_FOUND"


def test_extend_durations_lifts_short_tail_cue_to_minimum():
    g = guard()
    g.add_cue(0, 2000, "already long")
    g.add_cue(5000, 5200, "hi")
    assert g.extend_durations() == 1
    by_id = {c["cue_id"]: c for c in g.cues()}
    assert by_id["cue-2"]["end_ms"] == 5800
    assert by_id["cue-1"]["end_ms"] == 2000
    assert g.extend_durations() == 0
    assert g.stats()["extended"] == 1


def test_snapshot_restore_round_trip_and_invalid_restore():
    g = guard()
    g.add_cue(0, 1500, "first")
    g.add_cue(2000, 3500, "second")
    snap = g.snapshot()
    g.add_cue(4000, 5000, "third")
    g.restore(snap)
    assert [c["text"] for c in g.cues()] == ["first", "second"]
    with pytest.raises(crf.CaptionError) as exc:
        g.restore({"version": 1, "cues": "nope"})
    assert exc.value.code == "CAPTION_SNAPSHOT_INVALID"
    assert [c["text"] for c in g.cues()] == ["first", "second"]


def test_fit_all_reports_split_and_extended_counts():
    g = guard()
    g.add_cue(0, 1000, "the quick brown fox jumps over")
    g.add_cue(6000, 6100, "ok")
    out = g.fit_all()
    assert out == {"split": 1, "extended": 1}
    assert len(g.cues()) == 3
    assert g.timeline_valid() is True
    assert g.cues()[-1]["end_ms"] == 6800


def test_concurrent_add_and_query_are_serialized():
    g = guard()

    def worker(prefix):
        try:
            for i in range(5):
                g.add_cue(i * 1000, i * 1000 + 900, "line %d" % i,
                          cue_id="%s-%d" % (prefix, i))
                g.cues()
        except Exception as exc:  # pragma: no cover - surfaced by assert below
            errors.append(exc)

    errors = []
    threads = [threading.Thread(target=worker, args=("t%d" % i,)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(g.cues()) == 15
    assert g.stats()["cues_added"] == 15
    assert g.timeline_valid() is True
