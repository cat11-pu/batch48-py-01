import copy
import threading

import pytest

import consent_grant_registry as cg

NOW = "2026-11-24T09:00:00+00:00"
LATER = "2026-11-24T09:05:00+00:00"
EXPIRED_AT = "2026-11-24T09:10:01+00:00"


def registry(**kwargs):
    return cg.ConsentGrantRegistry(**kwargs)


def grant(reg, scopes, *, tenant="t1", user="u1", client="c1", now=NOW):
    return reg.grant_consent(tenant=tenant, user=user, client=client,
                             scopes=scopes, now=now)


def check(reg, scopes, *, tenant="t1", user="u1", client="c1", now=NOW):
    return reg.check_consent(tenant=tenant, user=user, client=client,
                             scopes=scopes, now=now)


def test_grant_creates_sorted_record_and_counts():
    reg = registry()
    out = grant(reg, ["write", "read", "read"])
    assert out.status == "granted"
    assert out.missing_scopes == ()
    assert out.rejected_scopes == ()
    record = out.record
    assert (record.tenant, record.user, record.client) == ("t1", "u1", "c1")
    assert record.scopes == ("read", "write")
    assert record.version == 1
    assert record.granted_at == NOW
    assert reg.stats() == {"versions": 1, "suppressed": 0, "prompted": 0,
                           "withdrawn": 0, "revoked_tokens": 0, "purged": 0}


def test_check_without_record_prompts_for_all_scopes():
    reg = registry()
    out = check(reg, ["read", "write"])
    assert out.status == "prompt"
    assert out.record is None
    assert out.missing_scopes == ("read", "write")
    assert reg.stats()["prompted"] == 1
    assert reg.stats()["versions"] == 0


def test_subset_check_is_suppressed():
    reg = registry()
    grant(reg, ["read", "write"])
    out = check(reg, ["read"])
    assert out.status == "granted"
    assert out.missing_scopes == ()
    assert out.record.version == 1
    assert out.record.scopes == ("read", "write")
    assert reg.stats()["suppressed"] == 1
    assert reg.stats()["prompted"] == 0
    assert reg.stats()["versions"] == 1


def test_grant_union_bumps_version_and_refreshes_time():
    reg = registry()
    first = grant(reg, ["read"])
    second = grant(reg, ["write", "read"], now=LATER)
    assert first.record.version == 1
    assert second.record.version == 2
    assert second.record.scopes == ("read", "write")
    assert second.record.granted_at == LATER
    assert reg.stats()["versions"] == 2
    full = check(reg, ["read", "write"], now="2026-11-24T09:06:00+00:00")
    assert full.status == "granted"
    assert full.record.version == 2


def test_invalid_grant_input_is_rejected():
    reg = registry()
    bad_calls = [
        dict(tenant="", user="u1", client="c1", scopes=["read"]),
        dict(tenant="t1", user="   ", client="c1", scopes=["read"]),
        dict(tenant="t1", user="u1", client="", scopes=["read"]),
        dict(tenant="t1", user="u1", client="c1", scopes=[]),
        dict(tenant="t1", user="u1", client="c1", scopes=["read", 5]),
    ]
    for kwargs in bad_calls:
        with pytest.raises(cg.ConsentError) as exc:
            reg.grant_consent(now=NOW, **kwargs)
        assert exc.value.code == "CONSENT_INVALID_REQUEST"
    assert reg.stats()["versions"] == 0


def test_withdraw_cascades_to_registered_tokens():
    reg = registry()
    grant(reg, ["read"])
    reg.register_token(tenant="t1", user="u1", client="c1", token_id="tok-1", now=NOW)
    reg.register_token(tenant="t1", user="u1", client="c1", token_id="tok-2", now=NOW)
    grant(reg, ["read"], client="c2")
    reg.register_token(tenant="t1", user="u1", client="c2", token_id="tok-3", now=NOW)

    assert reg.withdraw_consent(tenant="t1", user="u1", client="c1", now=NOW) == 2
    assert reg.token_usable("tok-1") is False
    assert reg.token_usable("tok-2") is False
    assert reg.token_usable("tok-3") is True
    remaining = reg.records_for("t1")
    assert [(row.client, row.scopes) for row in remaining] == [("c2", ("read",))]
    assert reg.stats()["withdrawn"] == 1
    assert reg.stats()["revoked_tokens"] == 2


def test_withdraw_unknown_key_is_idempotent():
    reg = registry()
    assert reg.withdraw_consent(tenant="t1", user="nobody", client="c1", now=NOW) == 0
    assert reg.withdraw_consent(tenant="t1", user="nobody", client="c1", now=LATER) == 0
    assert reg.stats()["withdrawn"] == 0
    assert reg.stats()["revoked_tokens"] == 0
    assert reg.records_for("t1") == []


def test_expired_record_prompts_and_purges():
    reg = registry(default_ttl=600)
    grant(reg, ["read"])
    out = check(reg, ["read"], now=EXPIRED_AT)
    assert out.status == "prompt"
    assert out.record is None
    assert out.missing_scopes == ("read",)
    assert reg.stats()["prompted"] == 1

    assert reg.purge_expired(now=EXPIRED_AT) == 1
    assert reg.purge_expired(now=EXPIRED_AT) == 0
    assert reg.records_for("t1") == []
    assert reg.stats()["purged"] == 1


def test_tenant_capacity_cap_blocks_new_grant():
    reg = registry(max_grants_per_tenant=2)
    grant(reg, ["read"], user="u1")
    grant(reg, ["read"], user="u2")
    with pytest.raises(cg.ConsentError) as exc:
        grant(reg, ["read"], user="u3")
    assert exc.value.code == "CONSENT_CAPACITY_EXCEEDED"
    assert len(reg.records_for("t1")) == 2
    assert reg.stats()["versions"] == 2

    reg.withdraw_consent(tenant="t1", user="u1", client="c1", now=NOW)
    grant(reg, ["read"], user="u3")
    assert [row.user for row in reg.records_for("t1")] == ["u2", "u3"]
    assert reg.stats()["versions"] == 3


def test_records_for_is_sorted_and_tenant_scoped():
    reg = registry()
    grant(reg, ["b"], user="u2", client="c9")
    grant(reg, ["a"], user="u1", client="c2")
    grant(reg, ["a"], tenant="t2", user="u0", client="c0")
    rows = reg.records_for("t1")
    assert [(row.user, row.client) for row in rows] == [("u1", "c2"), ("u2", "c9")]
    assert [row.scopes for row in rows] == [("a",), ("b",)]
    assert [row.user for row in reg.records_for("t2")] == ["u0"]
    assert reg.records_for("t3") == []


def test_concurrent_grants_merge_into_single_record():
    reg = registry()
    scopes = [f"scope-{index:02d}" for index in range(16)]
    errors = []
    barrier = threading.Barrier(len(scopes))

    def worker(scope):
        try:
            barrier.wait(timeout=5)
            grant(reg, [scope])
        except Exception as exc:  # pragma: no cover - reported through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(scope,)) for scope in scopes]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    rows = reg.records_for("t1")
    assert len(rows) == 1
    assert rows[0].scopes == tuple(sorted(scopes))
    assert rows[0].version == len(scopes)
    assert reg.stats()["versions"] == len(scopes)


def test_snapshot_is_detached_and_restore_roundtrips():
    reg = registry()
    grant(reg, ["read"])
    reg.register_token(tenant="t1", user="u1", client="c1", token_id="tok-1", now=NOW)
    state = reg.snapshot()
    pristine = copy.deepcopy(state)
    assert set(pristine) == {"version", "records", "tokens", "deprecated",
                             "counters", "stats"}

    state["records"].clear()
    state["stats"]["versions"] = 999
    assert reg.stats()["versions"] == 1
    assert check(reg, ["read"]).status == "granted"

    clone = registry()
    clone.restore(pristine)
    assert clone.stats()["versions"] == 1
    assert clone.token_usable("tok-1") is True
    assert check(clone, ["read"]).status == "granted"
    assert clone.records_for("t1")[0].version == 1


def test_restore_rejects_malformed_state_without_touching_live_data():
    reg = registry()
    grant(reg, ["read"])
    bad_states = [
        None,
        {},
        {"version": 2, "records": [], "tokens": {}, "deprecated": [],
         "counters": {}, "stats": {}},
        {"version": 1, "records": "nope", "tokens": {}, "deprecated": [],
         "counters": {}, "stats": {}},
        {"version": 1, "records": [], "tokens": [], "deprecated": [],
         "counters": {}, "stats": {}},
        {"version": 1, "records": [], "tokens": {}, "deprecated": [],
         "counters": {}, "stats": {"versions": 1}},
    ]
    for bad in bad_states:
        with pytest.raises(cg.ConsentError) as exc:
            reg.restore(bad)
        assert exc.value.code == "CONSENT_SNAPSHOT_INVALID"

    assert reg.stats()["versions"] == 1
    assert check(reg, ["read"]).status == "granted"


def test_stats_track_each_operation_exactly():
    reg = registry()
    grant(reg, ["a"])
    check(reg, ["a"])
    check(reg, ["a"], user="missing")
    reg.withdraw_consent(tenant="t1", user="u1", client="c1", now=NOW)
    assert reg.stats() == {"versions": 1, "suppressed": 1, "prompted": 1,
                           "withdrawn": 1, "revoked_tokens": 0, "purged": 0}


def test_grant_identical_set_is_idempotent():
    reg = registry()
    first = grant(reg, ["read", "write"])
    second = grant(reg, ["write", "read"], now=LATER)
    assert second.record.version == 1
    assert second.record.granted_at == NOW
    assert second.record.scopes == first.record.scopes == ("read", "write")
    assert reg.stats()["versions"] == 1
    assert reg.stats()["suppressed"] == 1
