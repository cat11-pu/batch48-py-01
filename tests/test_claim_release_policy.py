import copy
import threading

import pytest

import claim_release_policy as cp

NOW = "2026-11-24T09:00:00+00:00"
SUBJECT = {"sub": "u1", "email": "u1@example.com", "phone": "+1000"}


def make_policy(**kwargs):
    policy = cp.ClaimReleasePolicy(**kwargs)
    policy.define_claim("sub", pii_tier=0)
    policy.define_claim("email", pii_tier=1)
    policy.define_claim("phone", pii_tier=2)
    policy.define_claim("groups", pii_tier=0, multivalued=True)
    return policy


def test_release_required_and_optional_with_dropped():
    policy = make_policy()
    stored = policy.set_client_policy("c1", required=("sub",),
                                      optional=("email",), forbidden=("phone",))
    assert stored.version == 1
    assert stored.required == ("sub",)
    assert stored.optional == ("email",)
    assert stored.forbidden == ("phone",)

    result = policy.release_claims(client_id="c1",
                                   subject={"sub": "u1", "email": "u1@example.com",
                                            "phone": "+1000", "extra": "x"})
    assert result.client_id == "c1"
    assert result.policy_version == 1
    assert result.claims == {"sub": "u1", "email": "u1@example.com"}
    assert result.dropped == ("extra", "phone")
    assert result.truncated is False
    assert policy.stats() == {"claims": 4, "groups": 0, "policies": 1,
                              "released": 1, "denied": 0, "truncated": 0}


def test_unknown_client_is_rejected():
    policy = make_policy()
    assert policy.policy_for("nobody") is None
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.release_claims(client_id="nobody", subject=SUBJECT)
    assert exc.value.code == "CLAIM_UNKNOWN_CLIENT"
    assert policy.stats()["released"] == 0


def test_forbidden_claim_is_never_released():
    policy = make_policy()
    policy.set_client_policy("c1", required=("sub",), optional=("phone",),
                             forbidden=("email",))
    result = policy.release_claims(client_id="c1", subject=SUBJECT)
    assert result.claims == {"sub": "u1", "phone": "+1000"}
    assert "email" in result.dropped
    assert policy.stats()["released"] == 1
    assert policy.stats()["denied"] == 0


def test_policy_rejects_undefined_claims_and_blank_client():
    policy = make_policy()
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.set_client_policy("c1", required=("nickname",))
    assert exc.value.code == "CLAIM_UNDECLARED"
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.set_client_policy("  ", required=("sub",))
    assert exc.value.code == "CLAIM_INVALID"
    assert policy.stats()["policies"] == 0


def test_policy_versions_increment_and_latest_wins():
    policy = make_policy()
    first = policy.set_client_policy("c1", required=("sub",))
    second = policy.set_client_policy("c1", required=("sub",), optional=("email",))
    assert (first.version, second.version) == (1, 2)
    assert policy.policy_versions("c1") == [1, 2]
    assert policy.policy_for("c1").version == 2
    assert policy.policy_for("c1").optional == ("email",)
    result = policy.release_claims(client_id="c1", subject=SUBJECT)
    assert result.policy_version == 2
    assert result.claims == {"sub": "u1", "email": "u1@example.com"}


def test_missing_required_claim_denies_release():
    policy = make_policy()
    policy.set_client_policy("c1", required=("sub", "email"))
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.release_claims(client_id="c1", subject={"sub": "u1"})
    assert exc.value.code == "CLAIM_REQUIRED_MISSING"
    assert exc.value.detail == "email"
    assert policy.stats()["denied"] == 1
    assert policy.stats()["released"] == 0


def test_pii_tier_above_limit_is_minimized():
    policy = make_policy()
    policy.set_client_policy("c1", required=("sub",), optional=("email", "phone"),
                             max_pii_tier=1)
    result = policy.release_claims(client_id="c1", subject=SUBJECT)
    assert result.claims == {"sub": "u1", "email": "u1@example.com"}
    assert "phone" in result.dropped
    assert "email" not in result.dropped


def test_group_expansion_walks_parents_up_to_depth():
    policy = make_policy(max_group_depth=1)
    policy.define_group("g1", claims=("email",))
    policy.define_group("g2", claims=("phone",), parent="g1")
    policy.define_group("g3", claims=("sub",), parent="g2")
    policy.set_client_policy("c1", optional=("sub", "email", "phone"))
    result = policy.release_claims(client_id="c1", subject={}, groups=("g3",))
    assert result.claims == {"phone": True, "sub": True}
    assert result.truncated is False


def test_expansion_cap_truncates_optional_claims():
    policy = make_policy(max_expanded_claims=2)
    policy.define_group("g1", claims=("sub", "email", "phone"))
    policy.set_client_policy("c1", optional=("sub", "email", "phone"))
    result = policy.release_claims(client_id="c1", subject={}, groups=("g1",))
    assert result.claims == {"email": True, "phone": True}
    assert result.truncated is True
    assert "sub" in result.dropped
    assert policy.stats()["truncated"] == 1


def test_explain_reports_policy_decisions():
    policy = make_policy()
    policy.set_client_policy("c1", required=("sub",), optional=("email",),
                             forbidden=("phone",))
    assert policy.explain(client_id="c1", claim="sub")["decision"] == "required"
    assert policy.explain(client_id="c1", claim="email")["decision"] == "optional"
    assert policy.explain(client_id="c1", claim="phone")["decision"] == "forbidden"
    assert policy.explain(client_id="c1", claim="nickname")["decision"] == "undeclared"
    assert policy.explain(client_id="c1", claim="groups")["decision"] == "undeclared"
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.explain(client_id="nobody", claim="sub")
    assert exc.value.code == "CLAIM_UNKNOWN_CLIENT"


def test_per_client_claim_count_is_capped():
    policy = make_policy(max_claims_per_client=3)
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.set_client_policy("c1", required=("sub",),
                                 optional=("email", "phone", "groups"))
    assert exc.value.code == "CLAIM_CAPACITY_EXCEEDED"
    assert policy.stats()["policies"] == 0
    assert policy.policy_for("c1") is None


def test_concurrent_readers_see_one_complete_policy():
    policy = make_policy()
    policy.set_client_policy("c1", required=("sub",))
    stop = threading.Event()
    anomalies = []
    subject = {"sub": "u1", "email": "u1@example.com"}

    def writer():
        index = 0
        while not stop.is_set():
            index += 1
            if index % 2:
                policy.set_client_policy("c1", required=("sub",))
            else:
                policy.set_client_policy("c1", required=("sub",), optional=("email",))

    def reader():
        for _ in range(200):
            result = policy.release_claims(client_id="c1", subject=subject)
            expected = ({"sub": "u1"} if result.policy_version % 2
                        else {"sub": "u1", "email": "u1@example.com"})
            if result.claims != expected:
                anomalies.append((result.policy_version, dict(result.claims)))

    threads = [threading.Thread(target=writer)]
    threads += [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads[1:]:
        thread.join()
    stop.set()
    threads[0].join()

    assert anomalies == []
    assert policy.stats()["released"] == 800


def test_snapshot_restore_roundtrips_policies_and_groups():
    policy = make_policy(max_group_depth=1)
    policy.define_group("g1", claims=("email",))
    policy.set_client_policy("c1", required=("sub",), optional=("email",))
    state = policy.snapshot()
    pristine = copy.deepcopy(state)

    state["stats"]["policies"] = 999
    assert policy.stats()["policies"] == 1

    restored = make_policy(max_group_depth=1)
    restored.restore(pristine)
    assert restored.stats()["claims"] == 4
    assert restored.policy_for("c1").version == 1
    assert restored.release_claims(client_id="c1",
                                   subject=SUBJECT).claims == {
        "sub": "u1", "email": "u1@example.com"}
    expanded = restored.release_claims(client_id="c1", subject={}, groups=("g1",))
    assert expanded.claims == {"email": True}


def test_restore_rejects_malformed_state_without_touching_live_data():
    policy = make_policy()
    policy.set_client_policy("c1", required=("sub",))
    for bad in (None, [], {}, {"version": 99}, "nope", {"version": 1}):
        with pytest.raises(cp.ClaimPolicyError) as exc:
            policy.restore(bad)
        assert exc.value.code == "CLAIM_SNAPSHOT_INVALID"
    assert policy.policy_for("c1").version == 1
    assert policy.release_claims(client_id="c1", subject=SUBJECT).claims == {
        "sub": "u1"}


def test_define_group_validates_claims_and_parent():
    policy = make_policy()
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.define_group("g1", claims=("ghost",))
    assert exc.value.code == "CLAIM_UNDECLARED"
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.define_group("g2", claims=("sub",), parent="ghost")
    assert exc.value.code == "CLAIM_UNKNOWN_GROUP"

    policy.define_group("g1", claims=("sub",))
    policy.define_group("g1", claims=("sub",))
    assert policy.stats()["groups"] == 1
    with pytest.raises(cp.ClaimPolicyError) as exc:
        policy.define_group("g1", claims=("email",))
    assert exc.value.code == "CLAIM_GROUP_CONFLICT"
    assert policy.stats()["groups"] == 1
