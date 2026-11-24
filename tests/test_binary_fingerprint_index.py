import threading

import pytest

import binary_fingerprint_index as bfi

ALPHA = b"\x01\x02\x03\x04\x05\x06\x07\x08" * 4
GAMMA = ALPHA[:-1] + b"\x09"
BETA = bytes(range(0x20, 0x40))
BLOB = ALPHA


def make(**kwargs):
    return bfi.BinaryFingerprintIndex(**kwargs)


def seed(index):
    index.add_function("alpha", ALPHA, ["alpha-const"])
    index.add_function("gamma", GAMMA, ["gamma-const"])
    index.add_function("beta", BETA, [])
    return index


def test_add_function_builds_feature_sets():
    index = make()
    info = index.add_function("alpha", b"abcdefg", ["root", "admin", "ab"])
    assert info == {
        "name": "alpha",
        "ngram_count": 10,
        "string_count": 2,
        "length": 7,
    }
    features = index.features("alpha")
    assert features["length"] == 7
    assert features["length_bucket"] == 3
    assert features["ngram_count"] == 10
    assert features["strings"] == ["admin", "root"]
    assert index.functions() == ["alpha"]


def test_add_function_rejects_bad_arguments():
    index = make()
    bad_calls = [
        ("", b"abcd", []),
        ("alpha", "abcd", []),
        ("alpha", b"", []),
        ("alpha", b"abcd", "root"),
        ("alpha", b"abcd", [b"root"]),
        (7, b"abcd", []),
    ]
    for name, code, strings in bad_calls:
        with pytest.raises(bfi.FingerprintError) as exc:
            index.add_function(name, code, strings)
        assert exc.value.code == "FP_INVALID_INPUT"

    index.add_function("alpha", b"abcd", [])
    with pytest.raises(bfi.FingerprintError) as exc:
        index.add_function("alpha", b"abcd", [])
    assert exc.value.code == "FP_DUPLICATE_FUNCTION"
    assert index.functions() == ["alpha"]


def test_add_function_enforces_size_and_count_limits():
    small = make(max_function_bytes=4)
    with pytest.raises(bfi.FingerprintError) as exc:
        small.add_function("big", b"12345", [])
    assert exc.value.code == "FP_FUNCTION_TOO_LARGE"
    assert small.functions() == []

    capped = make(max_functions=1)
    capped.add_function("first", b"abcd", [])
    with pytest.raises(bfi.FingerprintError) as exc:
        capped.add_function("second", b"abcd", [])
    assert exc.value.code == "FP_FUNCTION_LIMIT"
    assert capped.functions() == ["first"]

    narrow = make(max_input_bytes=8)
    with pytest.raises(bfi.FingerprintError) as exc:
        narrow.match(b"0123456789")
    assert exc.value.code == "FP_INPUT_TOO_LARGE"


def test_match_scores_and_orders_candidates():
    index = seed(make())
    result = index.match(BLOB, threshold=0.01)
    assert [item["function"] for item in result][:2] == ["alpha", "gamma"]
    scores = [item["similarity"] for item in result]
    assert scores == sorted(scores, reverse=True)
    assert result[0]["similarity"] > result[1]["similarity"]
    assert result[0]["shared_strings"] == []
    assert "beta" not in [item["function"] for item in result]


def test_match_reports_shape_and_breaks_ties_by_name():
    index = make()
    code = b"\x01\x02\x03\x04" * 8
    index.add_function("zulu", code, [])
    index.add_function("alpha", code, [])
    result = index.match(code, threshold=0.01)
    assert [item["function"] for item in result] == ["alpha", "zulu"]
    assert result[0]["similarity"] == result[1]["similarity"]
    assert set(result[0]) == {"match_id", "function", "similarity", "shared_strings"}
    assert result[0]["shared_strings"] == []
    assert result[0]["match_id"] != result[1]["match_id"]


def test_match_threshold_filters_all_candidates():
    index = seed(make())
    assert index.match(BLOB, threshold=0.99) == []
    assert index.match(BLOB, threshold=0.01) != []


def test_match_rejects_invalid_threshold():
    index = seed(make())
    for value in (0.0, -0.5, 1.5, "high"):
        with pytest.raises(bfi.FingerprintError) as exc:
            index.match(BLOB, threshold=value)
        assert exc.value.code == "FP_INVALID_THRESHOLD"


def test_explain_reports_participating_features():
    index = seed(make())
    result = index.match(BLOB, threshold=0.01)
    top = result[0]
    info = index.explain(top["match_id"])
    assert info["match_id"] == top["match_id"]
    assert info["function"] == "alpha"
    assert info["shared_strings"] == []
    assert info["shared_ngram_count"] > 0
    assert info["length_ratio"] == 1.0

    second = index.explain(result[1]["match_id"])
    assert second["function"] == "gamma"
    assert second["shared_ngram_count"] > 0


def test_explain_rejects_unknown_match_id():
    index = seed(make())
    with pytest.raises(bfi.FingerprintError) as exc:
        index.explain("no-such-match")
    assert exc.value.code == "FP_UNKNOWN_MATCH"


def test_versions_lists_annotated_library_versions():
    index = seed(make())
    assert index.versions("alpha") == []
    entry = index.annotate("alpha", "libssl", "1.0.0")
    assert entry == {"library": "libssl", "version": "1.0.0"}
    index.annotate("alpha", "openssl", "1.1.1")
    listed = index.versions("alpha")
    assert [item["library"] for item in listed] == ["libssl", "openssl"]
    assert [item["version"] for item in listed] == ["1.0.0", "1.1.1"]
    assert index.stats()["annotations"] == 2


def test_annotate_rejects_unknown_function_and_bad_version():
    index = seed(make())
    with pytest.raises(bfi.FingerprintError) as exc:
        index.annotate("missing", "libssl", "1.0.0")
    assert exc.value.code == "FP_UNKNOWN_FUNCTION"

    for version in ("1.0", "", "v1.2.3", "1.2.3.4"):
        with pytest.raises(bfi.FingerprintError) as exc:
            index.annotate("alpha", "libssl", version)
        assert exc.value.code == "FP_INVALID_INPUT"

    with pytest.raises(bfi.FingerprintError) as exc:
        index.annotate("alpha", "", "1.0.0")
    assert exc.value.code == "FP_INVALID_INPUT"
    assert index.stats()["annotations"] == 0


def test_prune_drops_low_value_features_and_rejects_bad_threshold():
    index = make()
    index.add_function("tiny", b"abc", [])
    index.add_function("bigc", bytes(range(256)), [])
    assert index.prune(100.0) == 1
    assert index.functions() == ["bigc"]
    assert index.stats()["pruned"] == 1
    assert index.prune(100.0) == 0

    with pytest.raises(bfi.FingerprintError) as exc:
        index.prune(-1.0)
    assert exc.value.code == "FP_INVALID_THRESHOLD"
    assert index.functions() == ["bigc"]


def test_snapshot_restore_keeps_features_matches_and_versions():
    index = seed(make())
    index.annotate("alpha", "libssl", "1.0.0")
    before = index.match(BLOB, threshold=0.01)

    state = index.snapshot()
    assert state["version"] == 1
    clone = make()
    clone.restore(state)
    after = clone.match(BLOB, threshold=0.01)
    assert [(item["function"], item["similarity"]) for item in after] == \
        [(item["function"], item["similarity"]) for item in before]
    assert clone.versions("alpha") == index.versions("alpha")
    assert clone.features("gamma") == index.features("gamma")

    state["functions"].clear()
    assert clone.functions() != []
    assert clone.stats()["functions"] == 3


def test_corrupt_snapshot_is_rejected_without_state_change():
    index = seed(make())
    before = index.stats()

    for bad in (
        None,
        {},
        {"version": 2, "functions": {}, "annotations": {}, "stats": {}},
        {"version": 1, "functions": [], "annotations": {}, "stats": {}},
        {"version": 1, "functions": {}, "annotations": "nope", "stats": {}},
        {"version": 1, "functions": {}, "annotations": {}, "stats": []},
    ):
        with pytest.raises(bfi.FingerprintError) as exc:
            index.restore(bad)
        assert exc.value.code == "FP_SNAPSHOT_INVALID"
    assert index.stats() == before
    assert index.match(BLOB, threshold=0.01) != []


def test_concurrent_add_and_match_is_consistent():
    index = make()
    errors = []

    def writer(worker):
        try:
            for offset in range(5):
                code = bytes([worker, offset, 1, 2, 3, 4, 5, 6]) * 2
                index.add_function("f-%d-%d" % (worker, offset), code, [])
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def reader():
        try:
            for _ in range(5):
                index.match(BLOB, threshold=0.01)
                index.functions()
                index.stats()
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(6)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert index.stats()["functions"] == 30
    assert len(index.functions()) == 30
