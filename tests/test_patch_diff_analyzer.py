import threading

import pytest

import patch_diff_analyzer as pa

APPLY_PATCH = "\n".join([
    "--- a/f.txt",
    "+++ b/f.txt",
    "@@ -1,3 +1,4 @@",
    " one",
    "-two",
    "+TWO",
    "+extra",
    " three",
])

OLD_FILE = "one\ntwo\nthree\n"
NEW_FILE = "one\nTWO\nextra\nthree\n"

TWO_HUNK_CONFLICT = "\n".join([
    "--- a/f.txt",
    "+++ b/f.txt",
    "@@ -1,3 +1,4 @@",
    " one",
    "-two",
    "+TWO",
    "+extra",
    " three",
    "@@ -20,3 +21,3 @@",
    " ghost",
    "-missing",
    "+GONE",
    " void",
])


def analyzer(**kwargs):
    return pa.PatchDiffAnalyzer(**kwargs)


def test_parse_single_section_shape():
    sections = analyzer().parse("\n".join([
        "--- a/app.py",
        "+++ b/app.py",
        "@@ -1,3 +1,4 @@",
        " line one",
        "-old value",
        "+new value",
        "+extra line",
        " line three",
    ]))
    assert len(sections) == 1
    section = sections[0]
    assert section["old_path"] == "a/app.py"
    assert section["new_path"] == "b/app.py"
    assert len(section["hunks"]) == 1
    hunk = section["hunks"][0]
    assert hunk["index"] == 0
    assert (hunk["old_start"], hunk["old_count"]) == (1, 3)
    assert (hunk["new_start"], hunk["new_count"]) == (1, 4)
    assert hunk["header"] == "@@ -1,3 +1,4 @@"
    assert [(line["kind"], line["text"]) for line in hunk["lines"]] == [
        (" ", "line one"),
        ("-", "old value"),
        ("+", "new value"),
        ("+", "extra line"),
        (" ", "line three"),
    ]


def test_parse_handles_multiple_sections():
    text = "\n".join([
        "--- a/one.txt",
        "+++ b/one.txt",
        "@@ -1,1 +1,1 @@",
        "-a",
        "+A",
        "--- a/two.txt",
        "+++ b/two.txt",
        "@@ -7,2 +7,2 @@",
        " keep",
        "-b",
        "+B",
    ])
    sections = analyzer().parse(text)
    assert [(item["old_path"], item["new_path"]) for item in sections] == [
        ("a/one.txt", "b/one.txt"),
        ("a/two.txt", "b/two.txt"),
    ]
    assert len(sections[0]["hunks"]) == 1
    assert sections[1]["hunks"][0]["old_start"] == 7
    assert sections[1]["hunks"][0]["new_count"] == 2


def test_out_of_order_and_overlapping_hunks_are_refused():
    out_of_order = "\n".join([
        "--- a/f.py",
        "+++ b/f.py",
        "@@ -10,1 +10,1 @@",
        "-alpha",
        "+ALPHA",
        "@@ -5,1 +5,1 @@",
        "-beta",
        "+BETA",
    ])
    with pytest.raises(pa.PatchError) as exc:
        analyzer().parse(out_of_order)
    assert exc.value.code == "diff.hunk_order"
    assert exc.value.detail["line"] == 6

    overlapping = "\n".join([
        "--- a/f.py",
        "+++ b/f.py",
        "@@ -1,2 +1,2 @@",
        "-a",
        "+A",
        "@@ -2,2 +2,2 @@",
        "-b",
        "+B",
    ])
    with pytest.raises(pa.PatchError) as exc:
        analyzer().parse(overlapping)
    assert exc.value.code == "diff.hunk_overlap"
    assert exc.value.detail["line"] == 6


def test_malformed_header_and_line_counts_are_refused():
    bad_header = "--- a/f.py\n+++ b/f.py\n@@ -x,2 +1,2 @@\n-a\n+b\n"
    with pytest.raises(pa.PatchError) as exc:
        analyzer().parse(bad_header)
    assert exc.value.code == "diff.syntax"
    assert exc.value.detail["line"] == 3
    assert isinstance(exc.value.detail["reason"], str)

    bad_counts = "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-a\n+b\n"
    with pytest.raises(pa.PatchError) as exc:
        analyzer().parse(bad_counts)
    assert exc.value.code == "diff.count_mismatch"
    assert exc.value.detail["line"] == 3


def test_patch_size_limits_are_enforced():
    two_sections = "\n".join([
        "--- a/one.txt",
        "+++ b/one.txt",
        "@@ -1,1 +1,1 @@",
        "-a",
        "+A",
        "--- a/two.txt",
        "+++ b/two.txt",
        "@@ -1,1 +1,1 @@",
        "-b",
        "+B",
    ])
    with pytest.raises(pa.PatchError) as exc:
        analyzer(max_sections=1).parse(two_sections)
    assert exc.value.code == "diff.section_limit"

    wide_hunk = "\n".join([
        "--- a/f.txt",
        "+++ b/f.txt",
        "@@ -1,2 +1,2 @@",
        "-a",
        "+A",
        " ctx",
    ])
    with pytest.raises(pa.PatchError) as exc:
        analyzer(max_hunk_lines=2).parse(wide_hunk)
    assert exc.value.code == "diff.hunk_limit"
    assert exc.value.detail["line"] == 3

    narrow = analyzer(max_hunk_lines=2)
    assert len(narrow.parse("--- a/f.txt\n+++ b/f.txt\n@@ -1,1 +1,1 @@\n-a\n+b\n")) == 1


def test_locate_returns_zero_based_positions():
    parser = analyzer()
    hunks = parser.parse("\n".join([
        "--- a/t.txt",
        "+++ b/t.txt",
        "@@ -2,2 +2,3 @@",
        " beta",
        "-gamma",
        "+GAMMA",
        "+EXTRA",
    ]))[0]["hunks"]
    file_text = "alpha\nbeta\ngamma\ndelta\n"
    assert parser.locate(file_text, hunks) == [{"index": 0, "position": 1}]

    with pytest.raises(pa.PatchError) as exc:
        parser.locate("x\ny\nz\n", parser.parse(APPLY_PATCH)[0]["hunks"])
    assert exc.value.code == "diff.locate_conflict"
    assert exc.value.detail["hunk"] == 0


def test_locate_advances_between_repeated_contexts():
    parser = analyzer()
    hunks = parser.parse("\n".join([
        "--- a/r.txt",
        "+++ b/r.txt",
        "@@ -1,2 +1,2 @@",
        " a",
        " b",
        "@@ -3,2 +3,2 @@",
        " a",
        " b",
    ]))[0]["hunks"]
    positions = parser.locate("a\nb\na\nb\na\nb\n", hunks)
    assert positions == [{"index": 0, "position": 0}, {"index": 1, "position": 2}]


def test_classify_reports_applicable_patch():
    result = analyzer().classify(OLD_FILE, APPLY_PATCH)
    assert result["verdict"] == "applicable"
    assert [(item["old_path"], item["new_path"], item["verdict"])
            for item in result["sections"]] == [("a/f.txt", "b/f.txt", "applicable")]
    assert len(result["file_digest"]) == 64
    assert len(result["patch_digest"]) == 64
    int(result["file_digest"], 16)


def test_classify_reports_already_applied_patch():
    result = analyzer().classify(NEW_FILE, APPLY_PATCH)
    assert result["verdict"] == "already_applied"
    assert result["sections"][0]["verdict"] == "already_applied"


def test_classify_reports_conflict_when_context_is_gone():
    result = analyzer().classify("x\ny\nz\n", APPLY_PATCH)
    assert result["verdict"] == "conflict"
    assert result["sections"][0]["verdict"] == "conflict"


def test_one_conflicting_hunk_makes_whole_patch_conflict():
    result = analyzer().classify(OLD_FILE, TWO_HUNK_CONFLICT)
    assert result["verdict"] == "conflict"
    section = result["sections"][0]
    assert section["verdict"] == "conflict"
    assert [(hunk["index"], hunk["verdict"], hunk["code"]) for hunk in section["hunks"]] == [
        (0, "applicable", "patch.removed_present"),
        (1, "conflict", "patch.context_missing"),
    ]


def test_explain_lists_per_hunk_basis():
    parser = analyzer()
    applied = parser.classify(OLD_FILE, APPLY_PATCH)
    assert parser.explain(applied) == [
        {"section": 0, "hunk": 0, "verdict": "applicable",
         "code": "patch.removed_present"},
    ]

    conflicted = parser.classify(OLD_FILE, TWO_HUNK_CONFLICT)
    assert parser.explain(conflicted) == [
        {"section": 0, "hunk": 0, "verdict": "applicable",
         "code": "patch.removed_present"},
        {"section": 0, "hunk": 1, "verdict": "conflict",
         "code": "patch.context_missing"},
    ]


def test_cache_is_keyed_by_both_digests():
    parser = analyzer()
    first = parser.classify(OLD_FILE, APPLY_PATCH)
    assert parser.cache_info() == {"entries": 1, "hits": 0, "misses": 1}
    second = parser.classify(OLD_FILE, APPLY_PATCH)
    assert first == second
    assert parser.cache_info() == {"entries": 1, "hits": 1, "misses": 1}

    parser.classify(NEW_FILE, APPLY_PATCH)
    assert parser.cache_info() == {"entries": 2, "hits": 1, "misses": 2}
    assert parser.stats()["classify_calls"] == 3

    parser.clear_cache()
    assert parser.cache_info() == {"entries": 0, "hits": 0, "misses": 0}
    parser.reset_stats()
    assert parser.stats() == {
        "classify_calls": 0, "parse_calls": 0, "locate_calls": 0,
    }


def test_invert_swaps_paths_and_line_kinds():
    parser = analyzer()
    inverted = parser.parse(parser.invert(APPLY_PATCH))
    assert len(inverted) == 1
    section = inverted[0]
    assert (section["old_path"], section["new_path"]) == ("b/f.txt", "a/f.txt")
    hunk = section["hunks"][0]
    assert (hunk["old_start"], hunk["old_count"]) == (1, 4)
    assert (hunk["new_start"], hunk["new_count"]) == (1, 3)
    assert [(line["kind"], line["text"]) for line in hunk["lines"]] == [
        (" ", "one"),
        ("+", "two"),
        ("-", "TWO"),
        ("-", "extra"),
        (" ", "three"),
    ]


def test_concurrent_classify_is_consistent_and_cached():
    parser = analyzer()
    pairs = [(OLD_FILE, APPLY_PATCH), (NEW_FILE, APPLY_PATCH)]
    errors = []
    verdicts = []
    guard = threading.Lock()

    def worker(index):
        try:
            for round_no in range(3):
                file_text, patch_text = pairs[(index + round_no) % 2]
                result = parser.classify(file_text, patch_text)
                with guard:
                    verdicts.append(result["verdict"])
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(verdicts) == 24
    assert set(verdicts) == {"applicable", "already_applied"}
    info = parser.cache_info()
    assert info["entries"] == 2
    assert info["hits"] + info["misses"] == 24
