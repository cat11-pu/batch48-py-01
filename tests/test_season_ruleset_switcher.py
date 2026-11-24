import copy
import threading

import pytest

import season_ruleset_switcher as sr


def make(**kwargs):
    return sr.SeasonRulesetSwitcher(**kwargs)


def register(sw, name="rs-a", season="s1", payload=None):
    payload = {"mode": "ranked", "rounds": 3} if payload is None else payload
    return sw.register_ruleset(name, season=season, payload=payload)


def test_register_ruleset_returns_digest_and_is_idempotent():
    sw = make()
    payload = {"mode": "ranked", "rounds": 3}
    digest = sw.canonical_digest(payload)

    out = sw.register_ruleset("rs-a", season="s1", payload=payload)
    assert out == {
        "ruleset": "rs-a", "season": "s1", "version": 1,
        "digest": digest, "changed": True,
    }
    again = sw.register_ruleset("rs-a", season="s1",
                                payload={"rounds": 3, "mode": "ranked"})
    assert again == {
        "ruleset": "rs-a", "season": "s1", "version": 1,
        "digest": digest, "changed": False,
    }
    assert sw.stats()["registered"] == 1
    assert sw.stats()["updated"] == 0
    assert sw.ruleset_info("rs-a", season="s1") == {
        "ruleset": "rs-a", "season": "s1", "version": 1,
        "digest": digest, "payload": payload,
    }


def test_register_ruleset_bumps_version_and_keeps_old_versions():
    sw = make()
    first = {"mode": "ranked", "rounds": 3}
    second = {"mode": "ranked", "rounds": 5}
    register(sw, payload=first)

    out = register(sw, payload=second)
    assert out["version"] == 2
    assert out["changed"] is True
    assert out["digest"] == sw.canonical_digest(second)
    assert sw.stats()["updated"] == 1

    assert sw.ruleset_info("rs-a", season="s1", version=1)["payload"] == first
    current = sw.ruleset_info("rs-a", season="s1")
    assert current["version"] == 2
    assert current["payload"] == second

    with pytest.raises(sr.RulesetError) as exc:
        sw.register_ruleset("rs-a", season="s1", payload={})
    assert exc.value.code == "RULESET_INVALID_PAYLOAD"

    with pytest.raises(sr.RulesetError) as exc:
        sw.register_ruleset("rs-a", season="", payload={"rounds": 3})
    assert exc.value.code == "RULESET_INVALID_SEASON"

    with pytest.raises(sr.RulesetError) as exc:
        sw.register_ruleset("", season="s1", payload={"rounds": 3})
    assert exc.value.code == "RULESET_INVALID_NAME"

    with pytest.raises(sr.RulesetError) as exc:
        sw.ruleset_info("rs-a", season="s1", version=9)
    assert exc.value.code == "RULESET_UNKNOWN_VERSION"


def test_register_ruleset_respects_max_rulesets():
    sw = make(max_rulesets=2)
    register(sw, name="rs-a")
    register(sw, name="rs-b", payload={"mode": "casual", "rounds": 1})

    with pytest.raises(sr.RulesetError) as exc:
        register(sw, name="rs-c", payload={"mode": "duel", "rounds": 2})
    assert exc.value.code == "RULESET_LIMIT"

    out = register(sw, name="rs-a", payload={"mode": "ranked", "rounds": 7})
    assert out["version"] == 2
    assert sw.stats()["registered"] == 2
    assert sw.stats()["updated"] == 1


def test_activate_sets_generation_and_is_idempotent():
    sw = make()
    payload = {"mode": "ranked", "rounds": 3}
    register(sw, payload=payload)
    digest = sw.canonical_digest(payload)

    out = sw.activate("rs-a", season="s1")
    assert out == {
        "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": digest, "generation": 1, "changed": True,
    }
    again = sw.activate("rs-a", season="s1")
    assert again == {
        "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": digest, "generation": 1, "changed": False,
    }
    assert sw.active_for("s1") == {
        "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": digest, "generation": 1,
    }
    assert sw.stats()["activated"] == 1
    assert sw.stats()["switched"] == 0


def test_activate_switches_and_unknown_ruleset_leaves_active():
    sw = make()
    register(sw, name="rs-a", payload={"mode": "ranked", "rounds": 3})
    register(sw, name="rs-b", payload={"mode": "ranked", "rounds": 5})
    sw.activate("rs-a", season="s1")

    out = sw.activate("rs-b", season="s1")
    assert out["ruleset"] == "rs-b"
    assert out["generation"] == 2
    assert out["changed"] is True
    assert out["digest"] == sw.canonical_digest({"mode": "ranked", "rounds": 5})
    assert sw.stats()["switched"] == 1

    with pytest.raises(sr.RulesetError) as exc:
        sw.activate("ghost", season="s1")
    assert exc.value.code == "RULESET_UNKNOWN"
    active = sw.active_for("s1")
    assert active["ruleset"] == "rs-b"
    assert active["generation"] == 2
    assert sw.stats()["rejected"] == 1


def test_pin_for_match_records_digest_and_resolve_returns_detached_payload():
    sw = make()
    payload = {"mode": "ranked", "rounds": 3, "modes": ["ranked", "duel"]}
    register(sw, payload=payload)
    sw.activate("rs-a", season="s1")
    digest = sw.canonical_digest(payload)

    out = sw.pin_for_match("m1", season="s1")
    assert out == {
        "match_id": "m1", "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": digest, "state": "pinned", "result": None, "anomaly": False,
    }
    resolved = sw.resolve_for_match("m1")
    assert resolved == {
        "match_id": "m1", "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": digest, "payload": payload, "state": "pinned",
    }

    resolved["payload"]["rounds"] = 99
    resolved["payload"]["modes"].append("chaos")
    assert sw.ruleset_info("rs-a", season="s1")["payload"] == payload
    assert sw.resolve_for_match("m1")["payload"] == payload
    assert sw.match_state("m1")["state"] == "pinned"


def test_pin_for_match_requires_active_and_unique_match():
    sw = make()
    with pytest.raises(sr.RulesetError) as exc:
        sw.pin_for_match("m1", season="s1")
    assert exc.value.code == "RULESET_NO_ACTIVE"

    with pytest.raises(sr.RulesetError) as exc:
        sw.active_for("s1")
    assert exc.value.code == "RULESET_NO_ACTIVE"

    register(sw)
    sw.activate("rs-a", season="s1")
    sw.pin_for_match("m1", season="s1")

    with pytest.raises(sr.RulesetError) as exc:
        sw.pin_for_match("m1", season="s1")
    assert exc.value.code == "RULESET_MATCH_EXISTS"

    with pytest.raises(sr.RulesetError) as exc:
        sw.resolve_for_match("ghost")
    assert exc.value.code == "RULESET_UNKNOWN_MATCH"

    with pytest.raises(sr.RulesetError) as exc:
        sw.match_state("ghost")
    assert exc.value.code == "RULESET_UNKNOWN_MATCH"
    assert sw.stats()["rejected"] == 5


def test_create_room_then_start_room_pins_active_ruleset():
    sw = make()
    payload = {"mode": "ranked", "rounds": 3}
    register(sw, payload=payload)
    sw.activate("rs-a", season="s1")
    digest = sw.canonical_digest(payload)

    assert sw.create_room("r1", season="s1") == {
        "room_id": "r1", "season": "s1", "state": "created",
    }
    out = sw.start_room("r1")
    assert out == {
        "room_id": "r1", "match_id": "match:r1", "season": "s1",
        "ruleset": "rs-a", "version": 1, "digest": digest,
        "state": "pinned",
    }
    assert sw.match_state("match:r1")["state"] == "pinned"
    assert sw.resolve_for_match("match:r1")["payload"] == payload

    with pytest.raises(sr.RulesetError) as exc:
        sw.create_room("r1", season="s1")
    assert exc.value.code == "RULESET_ROOM_EXISTS"
    assert sw.stats()["pinned"] == 1


def test_start_room_errors_keep_room_created():
    sw = make()
    register(sw)
    sw.create_room("r1", season="s1")

    with pytest.raises(sr.RulesetError) as exc:
        sw.start_room("r1")
    assert exc.value.code == "RULESET_NO_ACTIVE"

    sw.activate("rs-a", season="s1")
    out = sw.start_room("r1")
    assert out["match_id"] == "match:r1"

    with pytest.raises(sr.RulesetError) as exc:
        sw.start_room("r1")
    assert exc.value.code == "RULESET_ROOM_ALREADY_STARTED"

    with pytest.raises(sr.RulesetError) as exc:
        sw.start_room("r9")
    assert exc.value.code == "RULESET_UNKNOWN_ROOM"

    with pytest.raises(sr.RulesetError) as exc:
        sw.start_room("")
    assert exc.value.code == "RULESET_INVALID_ROOM"
    assert sw.stats()["rejected"] == 4


def test_finish_match_keeps_pinned_ruleset_resolvable():
    sw = make()
    payload = {"mode": "ranked", "rounds": 3}
    register(sw, payload=payload)
    sw.activate("rs-a", season="s1")
    sw.pin_for_match("m1", season="s1")

    out = sw.finish_match("m1", result="blue")
    assert out == {"match_id": "m1", "state": "finished", "result": "blue"}

    with pytest.raises(sr.RulesetError) as exc:
        sw.finish_match("m1", result="red")
    assert exc.value.code == "RULESET_MATCH_FINISHED"

    resolved = sw.resolve_for_match("m1")
    assert resolved["state"] == "finished"
    assert resolved["digest"] == sw.canonical_digest(payload)
    assert resolved["payload"] == payload

    sw.pin_for_match("m2", season="s1")
    with pytest.raises(sr.RulesetError) as exc:
        sw.finish_match("m2", result="")
    assert exc.value.code == "RULESET_INVALID_RESULT"
    assert sw.match_state("m2")["state"] == "pinned"
    assert sw.stats()["finished"] == 1


def test_rollback_restores_previous_active_and_bumps_generation():
    sw = make()
    payload_a = {"mode": "ranked", "rounds": 3}
    payload_b = {"mode": "ranked", "rounds": 5}
    register(sw, name="rs-a", payload=payload_a)
    register(sw, name="rs-b", payload=payload_b)
    sw.activate("rs-a", season="s1")
    sw.activate("rs-b", season="s1")

    out = sw.rollback("s1")
    assert out == {
        "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": sw.canonical_digest(payload_a),
        "generation": 3, "changed": True,
    }
    assert sw.active_for("s1") == {
        "season": "s1", "ruleset": "rs-a", "version": 1,
        "digest": sw.canonical_digest(payload_a), "generation": 3,
    }

    with pytest.raises(sr.RulesetError) as exc:
        sw.rollback("s1")
    assert exc.value.code == "RULESET_NOTHING_TO_ROLLBACK"

    with pytest.raises(sr.RulesetError) as exc:
        sw.rollback("s2")
    assert exc.value.code == "RULESET_NOTHING_TO_ROLLBACK"
    assert sw.stats()["rollbacks"] == 1


def test_audit_and_replay_accept_matching_digest():
    sw = make()
    payload = {"mode": "ranked", "rounds": 3}
    register(sw, payload=payload)
    sw.activate("rs-a", season="s1")
    sw.pin_for_match("m1", season="s1")
    digest = sw.canonical_digest(payload)

    assert sw.audit_mismatch("m1", digest) == {
        "match_id": "m1", "status": "ok",
        "pinned_digest": digest, "expected_digest": digest,
    }
    assert sw.match_state("m1")["anomaly"] is False

    replayed = sw.replay("m1", digest)
    assert replayed["replayed"] is True
    assert replayed["match_id"] == "m1"
    assert replayed["digest"] == digest
    assert replayed["payload"] == payload

    with pytest.raises(sr.RulesetError) as exc:
        sw.audit_mismatch("ghost", digest)
    assert exc.value.code == "RULESET_UNKNOWN_MATCH"

    with pytest.raises(sr.RulesetError) as exc:
        sw.audit_mismatch("m1", "")
    assert exc.value.code == "RULESET_INVALID_DIGEST"

    with pytest.raises(sr.RulesetError) as exc:
        sw.replay("ghost", digest)
    assert exc.value.code == "RULESET_UNKNOWN_MATCH"

    with pytest.raises(sr.RulesetError) as exc:
        sw.replay("m1", "")
    assert exc.value.code == "RULESET_INVALID_DIGEST"
    assert sw.stats()["anomalies"] == 0


def test_stats_counters_and_reset():
    sw = make()
    register(sw, name="rs-a", payload={"mode": "ranked", "rounds": 3})
    register(sw, name="rs-a", payload={"mode": "ranked", "rounds": 4})
    register(sw, name="rs-b", payload={"mode": "casual", "rounds": 1})
    sw.activate("rs-a", season="s1")
    sw.activate("rs-b", season="s1")
    sw.pin_for_match("m1", season="s1")
    sw.finish_match("m1", result="blue")
    sw.rollback("s1")
    with pytest.raises(sr.RulesetError):
        sw.activate("ghost", season="s1")

    assert sw.stats() == {
        "registered": 2,
        "updated": 1,
        "activated": 1,
        "switched": 1,
        "pinned": 1,
        "finished": 1,
        "rollbacks": 1,
        "anomalies": 0,
        "rejected": 1,
    }
    sw.reset_stats()
    assert sw.stats() == {key: 0 for key in sr.STATS_KEYS}


def test_snapshot_restore_roundtrip_and_rejects_corrupt_state():
    sw = make()
    payload_a = {"mode": "ranked", "rounds": 3}
    payload_b = {"mode": "ranked", "rounds": 5}
    register(sw, name="rs-a", payload=payload_a)
    register(sw, name="rs-b", payload=payload_b)
    sw.activate("rs-a", season="s1")
    sw.activate("rs-b", season="s1")
    sw.pin_for_match("m1", season="s1")
    sw.finish_match("m1", result="blue")
    sw.create_room("r1", season="s1")
    sw.start_room("r1")

    snap = sw.snapshot()
    assert snap["version"] == 1
    corrupted = copy.deepcopy(snap)
    corrupted["matches"][0]["digest"] = "0" * 64
    assert sw.match_state("m1")["digest"] == sw.canonical_digest(payload_b)

    fresh = make()
    fresh.restore(copy.deepcopy(snap))
    assert fresh.active_for("s1")["ruleset"] == "rs-b"
    assert fresh.match_state("m1")["state"] == "finished"
    assert fresh.match_state("m1")["result"] == "blue"
    assert fresh.resolve_for_match("m1")["payload"] == payload_b
    assert fresh.match_state("match:r1")["state"] == "pinned"
    assert fresh.ruleset_info("rs-a", season="s1", version=1)["payload"] == payload_a
    assert fresh.stats() == sw.stats()

    out = fresh.rollback("s1")
    assert out["ruleset"] == "rs-a"
    assert out["version"] == 1

    bad_states = [
        None,
        {},
        {**copy.deepcopy(snap), "version": 2},
        {**copy.deepcopy(snap), "rulesets": "nope"},
        {**copy.deepcopy(snap), "stats": {"registered": 1}},
        {**copy.deepcopy(snap), "generation": 0},
    ]
    broken_active = copy.deepcopy(snap)
    broken_active["active"][0]["version"] = 99
    bad_states.append(broken_active)
    broken_payload = copy.deepcopy(snap)
    broken_payload["rulesets"][0]["versions"][0]["payload"] = {"tampered": True}
    bad_states.append(broken_payload)
    broken_room = copy.deepcopy(snap)
    for room in broken_room["rooms"]:
        room["match_id"] = "ghost"
    bad_states.append(broken_room)
    broken_history = copy.deepcopy(snap)
    broken_history["history"][0]["entries"][0]["ruleset"] = "ghost"
    bad_states.append(broken_history)

    restored = make()
    restored.restore(copy.deepcopy(snap))
    for state in bad_states:
        with pytest.raises(sr.RulesetError) as exc:
            restored.restore(state)
        assert exc.value.code == "RULESET_SNAPSHOT_INVALID"

    assert restored.active_for("s1")["ruleset"] == "rs-b"
    assert restored.match_state("m1")["state"] == "finished"
    assert restored.match_state("match:r1")["state"] == "pinned"
    assert restored.stats()["registered"] == 2


def test_concurrent_pins_see_one_complete_ruleset_version():
    sw = make()
    payload_a = {"mode": "ranked", "rounds": 3}
    payload_b = {"mode": "ranked", "rounds": 5}
    sw.register_ruleset("rs-a", season="s1", payload=payload_a)
    sw.register_ruleset("rs-b", season="s1", payload=payload_b)
    sw.activate("rs-a", season="s1")
    known = {
        sw.canonical_digest(payload_a),
        sw.canonical_digest(payload_b),
    }
    errors = []

    def pinner(index):
        try:
            out = sw.pin_for_match(f"m{index}", season="s1")
            resolved = sw.resolve_for_match(f"m{index}")
            if resolved["digest"] != out["digest"]:
                errors.append(f"digest drift on m{index}")
            if sw.canonical_digest(resolved["payload"]) != out["digest"]:
                errors.append(f"payload digest mismatch on m{index}")
            if out["digest"] not in known:
                errors.append(f"unknown digest on m{index}")
            info = sw.ruleset_info(resolved["ruleset"], season="s1",
                                   version=resolved["version"])
            if info["digest"] != resolved["digest"]:
                errors.append(f"version lookup mismatch on m{index}")
            if info["payload"] != resolved["payload"]:
                errors.append(f"payload lookup mismatch on m{index}")
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    def switcher():
        try:
            for _ in range(30):
                sw.activate("rs-a", season="s1")
                sw.activate("rs-b", season="s1")
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=pinner, args=(index,)) for index in range(8)]
    threads += [threading.Thread(target=switcher) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert sw.stats()["pinned"] == 8
    assert sw.stats()["rejected"] == 0
