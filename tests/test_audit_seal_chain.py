import copy
import threading

import pytest

import audit_seal_chain as sc

UTC = "2026-11-24T09:00:00Z"


def make(**kwargs):
    return sc.AuditSealChain(**kwargs)


def ev(n, target=None):
    return {
        "actor": f"user-{n}",
        "action": "read",
        "target": target if target is not None else f"doc-{n}",
        "at": UTC,
    }


def test_append_hands_out_strictly_increasing_sequence_numbers():
    chain = make()
    assert chain.append_event(ev(1)) == 1
    assert chain.append_event(ev(2)) == 2
    assert chain.append_event(ev(3)) == 3
    assert chain.head() == {"last_seq": 3, "last_sealed": 0, "segments": 0,
                            "pending": 3, "last_digest": ""}


def test_event_validation_and_the_record_ceiling():
    chain = make()
    with pytest.raises(sc.SealChainError) as exc:
        chain.append_event({"action": "read", "target": "doc", "at": UTC})
    assert exc.value.code == "SEAL_EVENT_INVALID"
    with pytest.raises(sc.SealChainError) as exc:
        chain.append_event({"actor": "u", "action": "", "target": "doc", "at": UTC})
    assert exc.value.code == "SEAL_EVENT_INVALID"
    with pytest.raises(sc.SealChainError) as exc:
        chain.append_event({"actor": "u", "action": "read", "target": "doc",
                            "at": "2026-11-24T09:00:00+08:00"})
    assert exc.value.code == "SEAL_TIMESTAMP_INVALID"
    with pytest.raises(sc.SealChainError) as exc:
        chain.append_event({"actor": "u", "action": "read", "target": "doc",
                            "at": "not-a-time"})
    assert exc.value.code == "SEAL_TIMESTAMP_INVALID"

    tiny = make(max_records=2)
    tiny.append_event(ev(1))
    tiny.append_event(ev(2))
    with pytest.raises(sc.SealChainError) as exc:
        tiny.append_event(ev(3))
    assert exc.value.code == "SEAL_RECORD_LIMIT"


def test_seal_range_is_bounded_by_segment_size_and_head():
    chain = make(segment_size=2)
    for n in range(1, 4):
        chain.append_event(ev(n))
    with pytest.raises(sc.SealChainError) as exc:
        chain.seal(3)
    assert exc.value.code == "SEAL_SEAL_TOO_LARGE"
    first = chain.seal(2)
    assert first["segment_id"] == 1
    assert first["first_seq"] == 1
    assert first["last_seq"] == 2
    assert first["count"] == 2
    for bad in (2, 0, 9):
        with pytest.raises(sc.SealChainError) as exc:
            chain.seal(bad)
        assert exc.value.code == "SEAL_SEAL_RANGE"
    second = chain.seal(3)
    assert second["segment_id"] == 2
    assert second["first_seq"] == 3
    assert second["last_seq"] == 3


def test_segment_digest_covers_the_previous_digest_and_the_body():
    chain = make(segment_size=1)
    chain.append_event(ev(1))
    first = chain.seal(1)
    chain.append_event(ev(2))
    second = chain.seal(2)
    assert first["prev_digest"] == ""
    assert second["prev_digest"] == first["digest"]
    assert len(first["digest"]) == 16
    assert first["digest"] != second["digest"]

    twin = make(segment_size=1)
    twin.append_event(ev(1))
    twin_first = twin.seal(1)
    assert twin_first["digest"] == first["digest"]


def test_verify_accepts_an_untouched_segment():
    chain = make(segment_size=2)
    for n in range(1, 3):
        chain.append_event(ev(n))
    segment = chain.seal()
    out = chain.verify(segment)
    assert out["ok"] is True
    assert out["segment_id"] == 1
    assert out["count"] == 2
    assert out["digest"] == segment["digest"]
    assert chain.stats()["verified"] == 1


def test_verify_refuses_body_or_digest_tampering():
    chain = make(segment_size=2)
    for n in range(1, 3):
        chain.append_event(ev(n))
    segment = chain.seal()

    body = copy.deepcopy(segment)
    body["events"][0]["target"] = "swapped-in-document"
    with pytest.raises(sc.SealChainError) as exc:
        chain.verify(body)
    assert exc.value.code == "SEAL_DIGEST_MISMATCH"

    digest = copy.deepcopy(segment)
    digest["digest"] = "0" * 16
    with pytest.raises(sc.SealChainError) as exc:
        chain.verify(digest)
    assert exc.value.code == "SEAL_DIGEST_MISMATCH"

    broken = copy.deepcopy(segment)
    del broken["events"]
    with pytest.raises(sc.SealChainError) as exc:
        chain.verify(broken)
    assert exc.value.code == "SEAL_SEGMENT_INVALID"


def test_sealed_ranges_are_closed_for_new_events_and_resealing():
    chain = make(segment_size=2)
    chain.append_event(ev(1))
    segment = chain.seal(1)
    assert chain.append_event(ev(2)) == 2
    assert chain.head()["pending"] == 1
    assert chain.verify(segment)["ok"] is True
    with pytest.raises(sc.SealChainError) as exc:
        chain.seal(1)
    assert exc.value.code == "SEAL_SEAL_RANGE"
    assert chain.seal(2)["first_seq"] == 2


def test_checkpoint_ids_are_monotonic_and_replays_are_refused():
    chain = make(segment_size=3)
    for n in range(1, 5):
        chain.append_event(ev(n))
    chain.seal(3)
    first = chain.checkpoint()
    second = chain.checkpoint()
    assert first["checkpoint_id"] == 1
    assert second["checkpoint_id"] == 2
    assert first["last_seq"] == 3
    assert first["last_digest"] == chain.head()["last_digest"]
    assert second["segments"] == 1
    assert isinstance(first["issued_at"], str)

    assert chain.adopt_checkpoint(second)["adopted"] == 2
    with pytest.raises(sc.SealChainError) as exc:
        chain.adopt_checkpoint(first)
    assert exc.value.code == "SEAL_CHECKPOINT_STALE"
    with pytest.raises(sc.SealChainError) as exc:
        chain.adopt_checkpoint(second)
    assert exc.value.code == "SEAL_CHECKPOINT_STALE"


def test_checkpoint_adoption_refuses_malformed_or_ahead_anchors():
    chain = make(segment_size=2)
    for n in range(1, 4):
        chain.append_event(ev(n))
    chain.seal(2)
    good = chain.checkpoint()
    for bad in (None, {}, {"checkpoint_id": 3, "last_seq": 2},
                {"checkpoint_id": "x", "last_seq": 2, "last_digest": "",
                 "segments": 1, "issued_at": "now"}):
        with pytest.raises(sc.SealChainError) as exc:
            chain.adopt_checkpoint(bad)
        assert exc.value.code == "SEAL_CHECKPOINT_INVALID"

    ahead = dict(good)
    ahead["checkpoint_id"] = 9
    ahead["last_seq"] = 99
    with pytest.raises(sc.SealChainError) as exc:
        chain.adopt_checkpoint(ahead)
    assert exc.value.code == "SEAL_CHECKPOINT_INVALID"

    behind = dict(good)
    behind["checkpoint_id"] = 5
    behind["last_seq"] = 1
    with pytest.raises(sc.SealChainError) as exc:
        chain.adopt_checkpoint(behind)
    assert exc.value.code == "SEAL_CHECKPOINT_STALE"


def test_find_gap_reports_missing_sequence_ranges():
    empty = make()
    assert empty.find_gap(1, 3) == [[1, 3]]

    chain = make(segment_size=2)
    for n in range(1, 5):
        chain.append_event(ev(n))
    chain.seal(3)
    assert chain.find_gap(1, 5) == [[4, 5]]
    assert chain.find_gap(2, 3) == []
    assert chain.find_gap(1, 3) == []
    for bad in ((0, 3), (3, 2)):
        with pytest.raises(sc.SealChainError) as exc:
            chain.find_gap(*bad)
        assert exc.value.code == "SEAL_GAP_RANGE"


def test_export_only_covers_whole_sealed_segments():
    chain = make(segment_size=2)
    for n in range(1, 5):
        chain.append_event(ev(n))
    chain.seal(2)
    chain.seal(4)
    payload = chain.export(1, 4)
    assert payload["version"] == 1
    assert payload["start"] == 1
    assert payload["end"] == 4
    assert [seg["segment_id"] for seg in payload["segments"]] == [1, 2]
    for bad in ((2, 3), (1, 5), (4, 1)):
        with pytest.raises(sc.SealChainError) as exc:
            chain.export(*bad)
        assert exc.value.code == "SEAL_EXPORT_RANGE"


def test_import_round_trip_keeps_the_chain_continuous():
    source = make(segment_size=2)
    for n in range(1, 5):
        source.append_event(ev(n))
    source.seal(2)
    source.seal(4)
    payload = source.export(1, 4)

    target = make(segment_size=2)
    out = target.import_payload(payload)
    assert out["imported"] == 2
    assert out["last_seq"] == 4
    assert out["last_digest"] == source.head()["last_digest"]
    assert target.stats()["imported"] == 2
    assert target.head()["last_seq"] == 4

    assert target.append_event(ev(5)) == 5
    segment = target.seal(5)
    assert segment["prev_digest"] == source.head()["last_digest"]
    assert target.verify(segment)["ok"] is True


def test_import_refuses_tampered_payloads_and_broken_continuation():
    source = make(segment_size=2)
    for n in range(1, 5):
        source.append_event(ev(n))
    source.seal(2)
    source.seal(4)
    payload = source.export(1, 4)

    tampered = copy.deepcopy(payload)
    tampered["segments"][0]["digest"] = "0" * 16
    with pytest.raises(sc.SealChainError) as exc:
        make().import_payload(tampered)
    assert exc.value.code == "SEAL_IMPORT_INVALID"

    gap = copy.deepcopy(payload)
    gap["start"] = 3
    gap["segments"] = [gap["segments"][1]]
    with pytest.raises(sc.SealChainError) as exc:
        make().import_payload(gap)
    assert exc.value.code == "SEAL_IMPORT_GAP"

    for bad in (None, {}, {"version": 1, "segments": []},
                {"version": 2, "start": 1, "end": 2, "segments": []}):
        with pytest.raises(sc.SealChainError) as exc:
            make().import_payload(bad)
        assert exc.value.code == "SEAL_IMPORT_INVALID"


def test_snapshot_restore_keeps_head_digest_and_checkpoint():
    chain = make(segment_size=2)
    chain.append_event(ev(1))
    chain.append_event(ev(2))
    segment = chain.seal(2)
    anchor = chain.checkpoint()
    state = chain.snapshot()
    assert state["version"] == 1
    assert state["last_seq"] == 2
    assert len(state["segments"]) == 1
    state["last_seq"] = 99
    state["segments"].clear()
    assert chain.head()["last_seq"] == 2
    assert chain.head()["last_digest"] == segment["digest"]

    restored = make(segment_size=2)
    restored.restore(chain.snapshot())
    assert restored.head()["last_seq"] == 2
    assert restored.head()["last_digest"] == segment["digest"]
    assert restored.head()["last_sealed"] == 2
    assert restored.checkpoint()["last_seq"] == 2
    assert restored.append_event(ev(3)) == 3
    third = restored.seal(3)
    assert third["prev_digest"] == segment["digest"]
    assert restored.verify(third)["ok"] is True
    assert anchor["last_digest"] == segment["digest"]

    for bad in (None, {}, {"version": 2}, {"version": 1, "last_seq": "x",
                                           "segments": []}):
        with pytest.raises(sc.SealChainError) as exc:
            restored.restore(bad)
        assert exc.value.code == "SEAL_SNAPSHOT_INVALID"
    assert restored.head()["last_seq"] == 3


def test_parallel_appends_never_share_a_sequence_number():
    chain = make(segment_size=3)
    seqs = []
    errors = []
    lock = threading.Lock()

    def worker(worker_id):
        try:
            local = [chain.append_event(ev(worker_id * 10 + n)) for n in range(5)]
        except Exception as exc:  # pragma: no cover - surfaced through errors
            with lock:
                errors.append(repr(exc))
            return
        with lock:
            seqs.extend(local)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sorted(seqs) == list(range(1, 31))
    assert len(set(seqs)) == 30
    for up_to in range(3, 31, 3):
        chain.seal(up_to)
    assert chain.head()["last_sealed"] == 30
    assert chain.stats()["appended"] == 30
    assert chain.stats()["sealed"] == 10
    segments = chain.export(1, 30)["segments"]
    for segment in segments:
        assert chain.verify(segment)["ok"] is True
    for left, right in zip(segments, segments[1:]):
        assert right["prev_digest"] == left["digest"]
