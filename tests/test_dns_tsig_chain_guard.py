import threading

import pytest

import dns_tsig_chain_guard as tg

T0 = 1_700_000_000
KEY = "dns-slave.example."


def make(**kwargs):
    guard = tg.DnsTsigChainGuard(**kwargs)
    guard.register_key(KEY, b"old-secret-1", now=T0)
    return guard


def flip(mac):
    return ("0" if mac[0] != "0" else "1") + mac[1:]


def test_sign_and_verify_round_trip():
    guard = make()
    tsig = guard.sign_message(b"\x00\x01query", key_name=KEY, now=T0)
    assert tsig["key_name"] == KEY
    assert tsig["algorithm"] == "hmac-sha256"
    assert tsig["generation"] == 1
    assert tsig["timestamp"] == T0
    assert tsig["fudge"] == 300
    assert len(tsig["mac"]) == 64

    out = guard.verify_tsig(b"\x00\x01query", tsig, now=T0)
    assert out["status"] == "ok"
    assert out["key_name"] == KEY
    assert out["generation"] == 1
    assert out["signed"] is True
    assert guard.stats() == {
        "signed": 1, "verified": 1, "rejected": 0, "replayed": 0,
    }


def test_tampered_mac_and_payload_are_badsig():
    guard = make()
    tsig = guard.sign_message(b"payload-a", key_name=KEY, now=T0)
    bad = dict(tsig)
    bad["mac"] = flip(tsig["mac"])
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"payload-a", bad, now=T0)
    assert exc.value.code == "BADSIG"

    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"payload-b", tsig, now=T0)
    assert exc.value.code == "BADSIG"
    assert guard.stats()["rejected"] == 2
    assert guard.stats()["verified"] == 0


def test_unknown_key_name_is_badkey():
    guard = make()
    tsig = guard.sign_message(b"payload", key_name=KEY, now=T0)
    stray = dict(tsig)
    stray["key_name"] = "other-master.example."
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"payload", stray, now=T0)
    assert exc.value.code == "BADKEY"


def test_clock_offset_boundary():
    guard = make(clock_skew=120)
    edge = guard.sign_message(b"m-edge", key_name=KEY, now=T0)
    assert guard.verify_tsig(b"m-edge", edge, now=T0 + 120)["status"] == "ok"

    late = guard.sign_message(b"m-late", key_name=KEY, now=T0)
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"m-late", late, now=T0 + 121)
    assert exc.value.code == "BADTIME"

    future = guard.sign_message(b"m-forward", key_name=KEY, now=T0)
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"m-forward", future, now=T0 - 121)
    assert exc.value.code == "BADTIME"


def test_rotation_keeps_both_generations_usable():
    guard = make()
    before = guard.sign_message(b"old-before", key_name=KEY, now=T0)
    rotated = guard.begin_rotation(KEY, b"new-secret-2", now=T0)
    assert rotated["generation"] == 2
    assert rotated["overlap_until"] == T0 + 600
    assert guard.verify_tsig(b"old-before", before, now=T0 + 10)["status"] == "ok"

    newer = guard.sign_message(b"new-after", key_name=KEY, now=T0 + 10)
    assert newer["generation"] == 2
    assert guard.verify_tsig(b"new-after", newer, now=T0 + 10)["status"] == "ok"

    older = guard.sign_message(b"old-late", key_name=KEY, generation=1,
                               now=T0 + 20)
    assert older["generation"] == 1
    assert guard.verify_tsig(b"old-late", older, now=T0 + 20)["status"] == "ok"


def test_overlap_window_end_retires_old_generation():
    guard = make(clock_skew=1000)
    guard.begin_rotation(KEY, b"new-secret-2", now=T0, overlap_seconds=300)

    edge = guard.sign_message(b"edge", key_name=KEY, generation=1, now=T0)
    assert guard.verify_tsig(b"edge", edge, now=T0 + 300)["status"] == "ok"

    stale = guard.sign_message(b"stale", key_name=KEY, generation=1, now=T0)
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"stale", stale, now=T0 + 301)
    assert exc.value.code == "BADKEY"


def test_identical_tuple_replay_is_rejected():
    guard = make()
    tsig = guard.sign_message(b"replay-me", key_name=KEY, now=T0)
    assert guard.verify_tsig(b"replay-me", tsig, now=T0)["status"] == "ok"
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"replay-me", tsig, now=T0 + 1)
    assert exc.value.code == "REPLAY"
    assert guard.stats() == {
        "signed": 1, "verified": 1, "rejected": 1, "replayed": 1,
    }


def test_malformed_tsig_record_is_badform():
    guard = make()
    tsig = guard.sign_message(b"m", key_name=KEY, now=T0)
    missing = {key: value for key, value in tsig.items() if key != "mac"}
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"m", missing, now=T0)
    assert exc.value.code == "BADFORM"

    with pytest.raises(tg.DnsTsigError) as exc:
        guard.verify_tsig(b"m", None, now=T0)
    assert exc.value.code == "BADFORM"


def test_stats_are_exact_and_resettable():
    guard = make()
    good = guard.sign_message(b"ok-1", key_name=KEY, now=T0)
    guard.verify_tsig(b"ok-1", good, now=T0)

    bad = guard.sign_message(b"ok-2", key_name=KEY, now=T0)
    bad["mac"] = flip(bad["mac"])
    with pytest.raises(tg.DnsTsigError):
        guard.verify_tsig(b"ok-2", bad, now=T0)

    assert guard.stats() == {
        "signed": 2, "verified": 1, "rejected": 1, "replayed": 0,
    }
    guard.reset_stats()
    assert guard.stats() == {
        "signed": 0, "verified": 0, "rejected": 0, "replayed": 0,
    }


def test_snapshot_restore_keeps_keys_and_rotation():
    guard = make()
    guard.begin_rotation(KEY, b"new-secret-2", now=T0)
    state = guard.snapshot()

    revived = tg.DnsTsigChainGuard(clock_skew=300)
    revived.restore(state)

    tsig = revived.sign_message(b"after-restore", key_name=KEY, now=T0 + 5)
    assert tsig["generation"] == 2
    assert revived.verify_tsig(b"after-restore", tsig, now=T0 + 5)["status"] == "ok"

    older = revived.sign_message(b"old-after", key_name=KEY, generation=1,
                                 now=T0 + 5)
    assert older["generation"] == 1
    assert revived.verify_tsig(b"old-after", older, now=T0 + 5)["status"] == "ok"


def test_malformed_snapshot_keeps_live_state():
    guard = make()
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.restore({"keys": "not-a-mapping"})
    assert exc.value.code == "STATE_INVALID"

    tsig = guard.sign_message(b"still-works", key_name=KEY, now=T0)
    assert guard.verify_tsig(b"still-works", tsig, now=T0)["status"] == "ok"


def test_duplicate_registration_is_idempotent_otherwise_conflict():
    guard = make()
    again = guard.register_key(KEY, b"old-secret-1", now=T0)
    assert again["generation"] == 1
    assert len(guard.active_keys(KEY, now=T0)) == 1

    with pytest.raises(tg.DnsTsigError) as exc:
        guard.register_key(KEY, b"different-secret", now=T0)
    assert exc.value.code == "KEYCONFLICT"


def test_key_table_capacity():
    guard = tg.DnsTsigChainGuard(max_keys=2)
    guard.register_key("a.example.", b"sa", now=T0)
    guard.register_key("b.example.", b"sb", now=T0)
    with pytest.raises(tg.DnsTsigError) as exc:
        guard.register_key("c.example.", b"sc", now=T0)
    assert exc.value.code == "KEYLIMIT"


def test_concurrent_verify_allows_single_winner():
    guard = make()
    tsig = guard.sign_message(b"race", key_name=KEY, now=T0)
    statuses = []
    errors = []
    start = threading.Barrier(8)

    def run():
        start.wait()
        try:
            statuses.append(guard.verify_tsig(b"race", tsig, now=T0)["status"])
        except tg.DnsTsigError as err:
            errors.append(err.code)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert statuses == ["ok"]
    assert sorted(errors) == ["REPLAY"] * 7
    assert guard.stats()["verified"] == 1
    assert guard.stats()["replayed"] == 7


def test_active_keys_lists_rotation_window_in_order():
    guard = make()
    guard.begin_rotation(KEY, b"new-secret-2", now=T0, overlap_seconds=300)

    during = guard.active_keys(KEY, now=T0 + 100)
    assert [entry["generation"] for entry in during] == [1, 2]
    assert {entry["algorithm"] for entry in during} == {"hmac-sha256"}

    after = guard.active_keys(KEY, now=T0 + 301)
    assert [entry["generation"] for entry in after] == [2]
