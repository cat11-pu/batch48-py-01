import copy
import json
import threading

import pytest

import oauth_client_registration as oauth

T0 = 1_700_000_000


def make(**kwargs):
    return oauth.ClientRegistrationAuthority(**kwargs)


def metadata(**overrides):
    base = {
        "client_name": "billing-app",
        "grant_types": ["authorization_code"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "client_secret_basic",
        "redirect_uris": ["https://app.example.com/cb"],
    }
    base.update(overrides)
    return base


def test_register_normalizes_metadata_and_returns_secret_once():
    auth = make()
    record = auth.register_client(
        "t1",
        metadata(redirect_uris=["HTTPS://App.Example.COM:443/cb"]),
        now=T0,
    )

    assert isinstance(record["client_id"], str) and len(record["client_id"]) >= 3
    assert isinstance(record["client_secret"], str)
    assert len(record["client_secret"]) >= 20
    assert record["reused"] is False
    assert record["version"] == 1
    assert record["metadata"]["grant_types"] == ["authorization_code"]
    assert record["metadata"]["response_types"] == ["code"]
    assert record["metadata"]["token_endpoint_auth_method"] == "client_secret_basic"
    assert record["metadata"]["redirect_uris"] == ["https://app.example.com/cb"]

    resolved = auth.resolve_client("t1", record["client_id"])
    assert resolved["client_id"] == record["client_id"]
    assert resolved["client_secret"] is None
    assert resolved["version"] == 1


def test_duplicate_registration_of_equal_metadata_is_idempotent():
    auth = make()
    first = auth.register_client("t1", metadata(), now=T0)
    second = auth.register_client(
        "t1",
        metadata(redirect_uris=["HTTPS://APP.EXAMPLE.COM:443/cb"]),
        now=T0 + 1,
    )

    assert second["client_id"] == first["client_id"]
    assert second["reused"] is True
    assert second["client_secret"] is None


def test_metadata_combinations_are_validated_together():
    auth = make()
    invalid = [
        metadata(redirect_uris=[]),
        metadata(grant_types=["client_credentials"], response_types=["code"]),
        metadata(grant_types=["refresh_token"], response_types=[]),
        metadata(token_endpoint_auth_method="private_key_jwt"),
    ]
    for meta in invalid:
        with pytest.raises(oauth.OAuthRegistrationError) as exc:
            auth.register_client("t1", meta, now=T0)
        assert exc.value.code == "OAUTH_METADATA_INVALID"



def test_redirect_uri_normalization_folds_case_and_default_ports():
    auth = make()

    assert auth.normalize_redirect_uri(
        "HTTPS://App.Example.COM:443/cb?x=1"
    ) == "https://app.example.com/cb?x=1"
    assert auth.normalize_redirect_uri(
        "http://app.example.com:80/cb"
    ) == "http://app.example.com/cb"
    assert auth.normalize_redirect_uri(
        "https://app.example.com:8443/cb"
    ) == "https://app.example.com:8443/cb"
    assert auth.normalize_redirect_uri(
        "https://app.example.com"
    ) == "https://app.example.com/"
    assert auth.normalize_redirect_uri(
        "https://app.example.com/cb/"
    ) == "https://app.example.com/cb/"


def test_redirect_uri_normalization_rejects_unsafe_forms():
    auth = make()
    for bad in ("ftp://app.example.com/cb", "https:///cb",
                "https://user:pass@app.example.com/cb", "", 5):
        with pytest.raises(oauth.OAuthRegistrationError) as exc:
            auth.normalize_redirect_uri(bad)
        assert exc.value.code == "OAUTH_REDIRECT_INVALID"


def test_client_ids_are_namespaced_per_tenant():
    auth = make()
    meta = metadata()
    first = auth.register_client("t1", meta, now=T0)
    second = auth.register_client("t2", meta, now=T0)

    assert first["client_id"] != second["client_id"]
    assert auth.resolve_client("t1", first["client_id"])["client_id"] == first["client_id"]
    assert auth.resolve_client("t2", second["client_id"])["client_id"] == second["client_id"]


def test_resolve_and_redirect_match_never_expose_the_secret():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)

    resolved = auth.resolve_client("t1", record["client_id"])
    assert resolved["client_secret"] is None
    assert resolved["metadata"]["redirect_uris"] == ["https://app.example.com/cb"]
    assert auth.match_redirect_uri(
        "t1", record["client_id"], "https://app.example.com/cb"
    ) == "https://app.example.com/cb"

    with pytest.raises(oauth.OAuthRegistrationError) as exc:
        auth.resolve_client("t1", "missing")
    assert exc.value.code == "OAUTH_UNKNOWN_CLIENT"


def test_secrets_are_stored_as_hashes_only():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)
    secret = record["client_secret"]

    state = auth.snapshot()
    assert secret not in json.dumps(state)

    out = auth.authenticate_secret("t1", record["client_id"], secret, now=T0)
    assert out["status"] == "ok"
    assert out["secret_slot"] == "current"

    with pytest.raises(oauth.OAuthRegistrationError) as exc:
        auth.authenticate_secret("t1", record["client_id"], "wrong-secret", now=T0)
    assert exc.value.code == "OAUTH_SECRET_INVALID"


def test_rotated_secret_overlaps_until_its_deadline():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)
    old_secret = record["client_secret"]

    rotated = auth.rotate_secret("t1", record["client_id"], now=T0 + 100,
                                 overlap=300)
    new_secret = rotated["client_secret"]
    assert new_secret != old_secret
    assert rotated["previous_valid_until"] == T0 + 400

    assert auth.authenticate_secret(
        "t1", record["client_id"], old_secret, now=T0 + 399
    )["secret_slot"] == "previous"
    assert auth.authenticate_secret(
        "t1", record["client_id"], new_secret, now=T0 + 399
    )["secret_slot"] == "current"

    with pytest.raises(oauth.OAuthRegistrationError) as exc:
        auth.authenticate_secret("t1", record["client_id"], old_secret, now=T0 + 400)
    assert exc.value.code == "OAUTH_SECRET_INVALID"
    assert auth.authenticate_secret(
        "t1", record["client_id"], new_secret, now=T0 + 400
    )["status"] == "ok"


def test_update_bumps_version_and_rejects_bad_metadata_atomically():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)

    updated = auth.update_client(
        "t1", record["client_id"],
        metadata(client_name="billing-v2",
                 redirect_uris=["https://app.example.com/cb2"]),
        now=T0 + 10,
    )
    assert updated["version"] == 2
    assert updated["metadata"]["redirect_uris"] == ["https://app.example.com/cb2"]

    with pytest.raises(oauth.OAuthRegistrationError) as exc:
        auth.update_client("t1", record["client_id"],
                           metadata(redirect_uris=[]), now=T0 + 20)
    assert exc.value.code == "OAUTH_METADATA_INVALID"

    resolved = auth.resolve_client("t1", record["client_id"])
    assert resolved["version"] == 2
    assert resolved["metadata"]["redirect_uris"] == ["https://app.example.com/cb2"]


def test_list_clients_paginates_on_the_client_id_sort_key():
    auth = make()
    client_ids = []
    for index in range(5):
        record = auth.register_client(
            "t1",
            metadata(redirect_uris=[f"https://app.example.com/cb-{index}"]),
            now=T0 + index,
        )
        client_ids.append(record["client_id"])

    page1 = auth.list_clients("t1", limit=2)
    page2 = auth.list_clients("t1", limit=2, cursor=page1["next_cursor"])
    page3 = auth.list_clients("t1", limit=2, cursor=page2["next_cursor"])

    assert [c["client_id"] for c in page1["clients"]] == sorted(client_ids)[:2]
    assert [c["client_id"] for c in page2["clients"]] == sorted(client_ids)[2:4]
    assert [c["client_id"] for c in page3["clients"]] == sorted(client_ids)[4:]
    assert page3["next_cursor"] is None

    collected = [c["client_id"] for page in (page1, page2, page3)
                 for c in page["clients"]]
    assert collected == sorted(client_ids)
    assert len(set(collected)) == 5
    assert all(c["client_secret"] is None for c in page1["clients"])


def test_revocation_removes_the_client_from_resolution():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)

    out = auth.revoke_client("t1", record["client_id"])
    assert out["client_id"] == record["client_id"]
    assert out["revoked"] is True

    with pytest.raises(oauth.OAuthRegistrationError) as exc:
        auth.resolve_client("t1", record["client_id"])
    assert exc.value.code == "OAUTH_UNKNOWN_CLIENT"
    with pytest.raises(oauth.OAuthRegistrationError) as exc:
        auth.revoke_client("t1", record["client_id"])
    assert exc.value.code == "OAUTH_UNKNOWN_CLIENT"


def test_snapshot_restore_keeps_hashes_rotation_and_metadata():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)
    old_secret = record["client_secret"]
    rotated = auth.rotate_secret("t1", record["client_id"], now=T0 + 100,
                                 overlap=300)
    new_secret = rotated["client_secret"]

    state = auth.snapshot()
    assert state["stats"]["registered"] == 1
    frozen = copy.deepcopy(state)
    state["clients"].clear()
    state["stats"]["registered"] = 99

    restored = make()
    restored.restore(frozen)

    resolved = restored.resolve_client("t1", record["client_id"])
    assert resolved["metadata"]["redirect_uris"] == ["https://app.example.com/cb"]
    assert restored.authenticate_secret(
        "t1", record["client_id"], old_secret, now=T0 + 200
    )["secret_slot"] == "previous"
    assert restored.authenticate_secret(
        "t1", record["client_id"], new_secret, now=T0 + 200
    )["secret_slot"] == "current"


def test_corrupt_snapshot_is_refused_and_live_state_survives():
    auth = make()
    record = auth.register_client("t1", metadata(), now=T0)

    broken = [
        None,
        {},
        {"version": 2, "clients": {}, "stats": {}},
        {"version": 1, "clients": "nope", "stats": {}},
        {"version": 1, "clients": {}, "stats": "nope"},
        {"version": 1, "clients": {}},
    ]
    for state in broken:
        with pytest.raises(oauth.OAuthRegistrationError) as exc:
            auth.restore(state)
        assert exc.value.code == "OAUTH_SNAPSHOT_INVALID"

    assert auth.resolve_client("t1", record["client_id"])["client_id"] == record["client_id"]


def test_concurrent_duplicate_registration_creates_one_client():
    auth = make()
    meta = metadata()
    barrier = threading.Barrier(8)
    records = []
    errors = []
    guard = threading.Lock()

    def worker():
        barrier.wait()
        try:
            record = auth.register_client("t1", dict(meta), now=T0)
        except Exception as exc:  # pragma: no cover - reported through errors
            with guard:
                errors.append(repr(exc))
        else:
            with guard:
                records.append(record)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len({record["client_id"] for record in records}) == 1
    secrets = [record["client_secret"] for record in records
               if record["client_secret"] is not None]
    assert len(secrets) == 1
