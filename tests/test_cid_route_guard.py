import copy
import threading

import pytest

import cid_route_guard as cg

KEY = b"chunk9-reset-key"


def make_guard(**kwargs):
    kwargs.setdefault("reset_key", KEY)
    return cg.CidRouteGuard(**kwargs)


def test_allocate_returns_cid_worker_token_and_time():
    guard = make_guard()
    out = guard.allocate("w1", now_ms=100)
    assert set(out) == {"cid", "worker", "token", "created_ms"}
    assert out["worker"] == "w1"
    assert out["created_ms"] == 100
    assert len(out["cid"]) == 16
    assert out["cid"] == out["cid"].lower()
    int(out["cid"], 16)
    assert out["token"] == guard.reset_token(out["cid"])
    assert len(out["token"]) == 32

    parsed = guard.parse_cid(out["cid"])
    assert parsed["version"] == 1
    assert parsed["valid"] is True
    assert isinstance(parsed["serial"], int)
    assert parsed["serial"] > 0


def test_generated_cids_are_unique_with_increasing_serials():
    guard = make_guard()
    cids = []
    serials = []
    for _ in range(25):
        item = guard.allocate("w1")
        cids.append(item["cid"])
        serials.append(guard.parse_cid(item["cid"])["serial"])

    assert len(set(cids)) == 25
    assert serials == sorted(serials)
    assert all(later > earlier for earlier, later in zip(serials, serials[1:]))


def test_route_active_cid_forwards_to_its_worker():
    guard = make_guard()
    cid = guard.allocate("w7", now_ms=5)["cid"]
    assert guard.route(cid, now_ms=10) == {
        "action": "forward", "cid": cid, "worker": "w7",
    }
    assert guard.stats()["forwarded"] == 1


def test_retire_moves_cid_and_answers_with_reset():
    guard = make_guard(retire_grace_ms=4000)
    cid = guard.allocate("w7", now_ms=1000)["cid"]

    retired = guard.retire(cid, now_ms=1000)
    assert retired == {"cid": cid, "worker": "w7", "retire_until_ms": 5000}
    assert guard.worker_stats("w7") == {
        "worker": "w7", "active": 0, "retired": 1, "capacity": 64,
    }

    answered = guard.route(cid, now_ms=2000)
    assert answered == {"action": "reset", "cid": cid,
                        "token": guard.reset_token(cid)}
    assert guard.stats()["retired"] == 1
    assert guard.stats()["resets"] == 1


def test_parse_cid_rejects_bad_prefix_and_bad_length():
    guard = make_guard()
    cid = guard.allocate("w1")["cid"]

    flipped = "%02x" % (int(cid[:2], 16) ^ 0xFF) + cid[2:]
    with pytest.raises(cg.CidError) as exc:
        guard.parse_cid(flipped)
    assert exc.value.code == "CID_BAD_PREFIX"

    with pytest.raises(cg.CidError) as exc:
        guard.parse_cid(cid[:-2])
    assert exc.value.code == "CID_BAD_PREFIX"

    with pytest.raises(cg.CidError) as exc:
        guard.parse_cid("zz" * 8)
    assert exc.value.code == "CID_BAD_PREFIX"


def test_client_cid_length_bounds():
    guard = make_guard()
    with pytest.raises(cg.CidError) as exc:
        guard.allocate("w1", client_cid="ab" * 7)
    assert exc.value.code == "CID_CLIENT_LEN"

    with pytest.raises(cg.CidError) as exc:
        guard.allocate("w1", client_cid="cd" * 21)
    assert exc.value.code == "CID_CLIENT_LEN"

    assert guard.stats()["allocated"] == 0
    assert guard.stats()["rejected"] == 2


def test_client_cid_within_bounds_is_accepted_and_respects_retirement():
    guard = make_guard(worker_capacity=8)
    small = "0a0b0c0d0e0f1011"
    out = guard.allocate("w1", now_ms=1, client_cid=small)
    assert out["cid"] == small
    assert out["worker"] == "w1"
    assert guard.route(small) == {"action": "forward", "cid": small,
                                  "worker": "w1"}

    wide = "11" * 20
    wide_out = guard.allocate("w2", now_ms=2, client_cid=wide)
    assert wide_out["cid"] == wide

    guard.retire(wide, now_ms=10)
    with pytest.raises(cg.CidError) as exc:
        guard.allocate("w3", now_ms=20, client_cid=wide)
    assert exc.value.code == "CID_RETIRED_CONFLICT"


def test_worker_capacity_is_enforced():
    guard = make_guard(worker_capacity=2)
    guard.allocate("w1")
    second = guard.allocate("w1")
    with pytest.raises(cg.CidError) as exc:
        guard.allocate("w1")
    assert exc.value.code == "CID_WORKER_FULL"

    guard.allocate("w2")
    assert guard.worker_stats("w1") == {
        "worker": "w1", "active": 2, "retired": 0, "capacity": 2,
    }

    guard.retire(second["cid"], now_ms=1)
    assert guard.worker_stats("w1")["active"] == 1
    assert guard.worker_stats("w1")["retired"] == 1
    assert guard.allocate("w1")["worker"] == "w1"


def test_reset_token_is_deterministic_and_verifiable():
    guard = make_guard()
    first = guard.allocate("w1")["cid"]
    second = guard.allocate("w1")["cid"]

    token = guard.reset_token(first)
    assert token == guard.reset_token(first)
    assert guard.verify_token(first, token) is True

    forged = ("0" if token[0] != "0" else "1") + token[1:]
    assert guard.verify_token(first, forged) is False
    assert guard.verify_token(second, token) is False
    assert guard.reset_token(second) != token


def test_reset_token_depends_on_reset_key():
    one = make_guard(reset_key=b"key-one")
    two = make_guard(reset_key=b"key-two")
    same = make_guard(reset_key=b"key-one")
    cid = "0a0b0c0d0e0f1011"

    assert one.reset_token(cid) != two.reset_token(cid)
    assert one.reset_token(cid) == same.reset_token(cid)


def test_retire_unknown_cid_is_rejected():
    guard = make_guard()
    with pytest.raises(cg.CidError) as exc:
        guard.retire("0011223344556677", now_ms=0)
    assert exc.value.code == "CID_UNKNOWN"
    assert guard.stats()["retired"] == 0
    assert guard.stats()["rejected"] == 1


def test_stats_counters_and_reset():
    guard = make_guard()
    first = guard.allocate("w1", now_ms=0)
    second = guard.allocate("w1", now_ms=0)

    guard.route(first["cid"], now_ms=10)
    guard.retire(second["cid"], now_ms=100)
    guard.route(second["cid"], now_ms=200)

    assert guard.stats() == {
        "allocated": 2, "forwarded": 1, "resets": 1,
        "retired": 1, "rejected": 0,
    }

    with pytest.raises(cg.CidError) as exc:
        guard.allocate("", now_ms=0)
    assert exc.value.code == "CID_INVALID_ARGUMENT"
    assert guard.stats()["rejected"] == 1
    assert guard.stats()["allocated"] == 2

    guard.reset_stats()
    assert guard.stats() == {
        "allocated": 0, "forwarded": 0, "resets": 0,
        "retired": 0, "rejected": 0,
    }


def test_snapshot_is_deep_copy_and_rejects_malformed_restore():
    guard = make_guard()
    first = guard.allocate("w1")
    second = guard.allocate("w2")
    guard.retire(second["cid"], now_ms=50)

    snap = guard.snapshot()
    assert set(snap) == {"version", "active", "retired", "serial", "stats"}
    assert snap["version"] == 1

    live_cid = next(iter(snap["active"]))
    snap["active"][live_cid]["worker"] = "tampered"
    snap["stats"]["allocated"] = 999
    assert guard.route(first["cid"]) == {"action": "forward",
                                         "cid": first["cid"],
                                         "worker": "w1"}
    assert guard.stats()["allocated"] == 2

    bad_states = [
        {"version": 2},
        {"version": 1},
        {"version": 1, "active": [], "retired": {}, "serial": 0, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(cg.CidError) as exc:
            guard.restore(bad)
        assert exc.value.code == "CID_SNAPSHOT_INVALID"
    assert guard.stats()["allocated"] == 2


def test_restore_preserves_routes_retirement_and_serial():
    guard = make_guard(retire_grace_ms=3000)
    live = guard.allocate("w1", now_ms=10)
    going = guard.allocate("w2", now_ms=20)
    guard.retire(going["cid"], now_ms=100)
    guard.route(live["cid"], now_ms=200)
    frozen_stats = guard.stats()
    snap = copy.deepcopy(guard.snapshot())

    revived = make_guard(retire_grace_ms=3000)
    revived.restore(snap)
    assert revived.stats() == frozen_stats
    assert revived.route(live["cid"], now_ms=300) == {
        "action": "forward", "cid": live["cid"], "worker": "w1",
    }
    assert revived.route(going["cid"], now_ms=400) == {
        "action": "reset", "cid": going["cid"],
        "token": revived.reset_token(going["cid"]),
    }
    assert revived.worker_stats("w1") == {
        "worker": "w1", "active": 1, "retired": 0, "capacity": 64,
    }

    fresh = revived.allocate("w3", now_ms=500)
    assert revived.parse_cid(fresh["cid"])["serial"] > guard.parse_cid(
        live["cid"])["serial"]


def test_concurrent_allocations_stay_unique():
    guard = make_guard(worker_capacity=64)
    results = [None] * 8
    errors = []
    barrier = threading.Barrier(8)

    def worker(index):
        try:
            barrier.wait(timeout=10)
            results[index] = [
                guard.allocate("w%d" % index, now_ms=round_no)["cid"]
                for round_no in range(12)
            ]
        except Exception as exc:  # noqa: BLE001 - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    flat = [cid for chunk in results for cid in chunk]
    assert len(flat) == 96
    assert len(set(flat)) == 96
    assert guard.stats()["allocated"] == 96
