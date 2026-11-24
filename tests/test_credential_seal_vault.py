import copy
import threading

import pytest

import credential_seal_vault as cv


def make(**kwargs):
    vault = cv.CredentialSealVault(**kwargs)
    vault.add_kek(1, b"kek-one-material-0001")
    return vault


def test_add_kek_then_put_get_roundtrip():
    vault = make()
    info = vault.put_secret("t1", "db", b"hunter2")
    assert info["tenant"] == "t1"
    assert info["name"] == "db"
    assert info["version"] == 1
    assert info["kek_version"] == 1
    assert isinstance(info["ciphertext"], bytes)
    assert len(info["ciphertext"]) == len(b"hunter2")
    assert info["ciphertext"] != b"hunter2"
    assert vault.get_secret("t1", "db") == b"hunter2"
    assert vault.active_kek_version() == 1


def test_second_put_creates_new_version_and_latest_is_served():
    vault = make()
    vault.put_secret("t1", "db", b"first-value")
    info = vault.put_secret("t1", "db", b"second-value")
    assert info["version"] == 2
    assert vault.versions("t1", "db") == [1, 2]
    assert vault.get_secret("t1", "db") == b"second-value"
    assert vault.get_secret("t1", "db", version=1) == b"first-value"
    counts = vault.stats()
    assert counts["secrets"] == 1
    assert counts["versions"] == 2


def test_aad_binding_is_checked_before_integrity():
    vault = make()
    vault.put_secret("t1", "api", b"token-abc", aad="svc=api")
    with pytest.raises(cv.CredentialVaultError) as missing:
        vault.get_secret("t1", "api")
    assert missing.value.code == "SEAL_AAD_MISMATCH"
    with pytest.raises(cv.CredentialVaultError) as wrong:
        vault.get_secret("t1", "api", aad="svc=other")
    assert wrong.value.code == "SEAL_AAD_MISMATCH"
    assert vault.get_secret("t1", "api", aad="svc=api") == b"token-abc"


def test_tampered_ciphertext_is_reported():
    vault = make()
    vault.put_secret("t1", "db", b"correct-horse")
    record = vault.export_record("t1", "db")
    assert set(record) >= {
        "tenant", "name", "version", "kek_version", "ciphertext",
        "aad_tag", "wrapped_dek", "nonce", "tag",
    }
    broken = copy.deepcopy(record)
    data = bytearray(broken["ciphertext"])
    data[0] ^= 0xFF
    broken["ciphertext"] = bytes(data)
    vault.import_record(broken, replace=True)
    with pytest.raises(cv.CredentialVaultError) as exc:
        vault.get_secret("t1", "db")
    assert exc.value.code == "SEAL_TAMPERED"


def test_unknown_secret_and_unknown_version():
    vault = make()
    with pytest.raises(cv.CredentialVaultError) as unknown:
        vault.get_secret("t1", "absent")
    assert unknown.value.code == "SEAL_UNKNOWN_SECRET"
    vault.put_secret("t1", "db", b"only-version")
    with pytest.raises(cv.CredentialVaultError) as gone:
        vault.get_secret("t1", "db", version=9)
    assert gone.value.code == "SEAL_VERSION_GONE"
    assert vault.versions("t1", "absent") == []


def test_kek_rotation_rewraps_without_touching_ciphertext():
    vault = make()
    vault.put_secret("t1", "a", b"value-a")
    vault.put_secret("t1", "b", b"value-b")
    before = vault.export_record("t1", "a")
    result = vault.rotate_kek(2, b"kek-two-material-0002")
    assert result["kek_version"] == 2
    assert result["rewrapped"] == 2
    after = vault.export_record("t1", "a")
    assert after["ciphertext"] == before["ciphertext"]
    assert after["kek_version"] == 2
    assert vault.active_kek_version() == 2
    assert vault.put_secret("t1", "c", b"value-c")["kek_version"] == 2


def test_rotate_with_limit_then_rewrap_batch_finishes():
    vault = make()
    for name in ("a", "b", "c"):
        vault.put_secret("t1", name, b"payload-" + name.encode())
    result = vault.rotate_kek(2, b"kek-two-material-0002", rewrap_limit=1)
    assert result["rewrapped"] == 1
    assert vault.rewrap_batch("t1", 2) == 2
    assert vault.rewrap_batch("t1", 2) == 0
    with pytest.raises(cv.CredentialVaultError) as bad:
        vault.rewrap_batch("t1", 0)
    assert bad.value.code == "SEAL_BATCH_INVALID"


def test_kek_registration_limits_and_duplicates():
    fresh = cv.CredentialSealVault()
    with pytest.raises(cv.CredentialVaultError) as missing:
        fresh.put_secret("t1", "db", b"value")
    assert missing.value.code == "SEAL_KEK_MISSING"
    with pytest.raises(cv.CredentialVaultError) as zero:
        fresh.add_kek(0, b"kek-material-000000")
    assert zero.value.code == "SEAL_INPUT_INVALID"
    with pytest.raises(cv.CredentialVaultError) as short:
        fresh.add_kek(1, b"tiny")
    assert short.value.code == "SEAL_INPUT_INVALID"
    fresh.add_kek(1, b"kek-material-000001")
    with pytest.raises(cv.CredentialVaultError) as dup:
        fresh.add_kek(1, b"kek-material-000002")
    assert dup.value.code == "SEAL_KEK_DUPLICATE"


def test_revoked_kek_makes_records_unavailable():
    vault = make()
    vault.put_secret("t1", "db", b"sealed-value")
    vault.add_kek(2, b"kek-two-material-0002")
    assert vault.revoke_kek(1) is True
    with pytest.raises(cv.CredentialVaultError) as exc:
        vault.get_secret("t1", "db")
    assert exc.value.code == "SEAL_KEK_MISSING"
    assert vault.revoke_kek(1) is False
    with pytest.raises(cv.CredentialVaultError) as never:
        vault.revoke_kek(9)
    assert never.value.code == "SEAL_KEK_MISSING"


def test_audit_log_records_operations_and_failures():
    vault = make()
    vault.put_secret("t1", "db", b"audit-value", aad="svc=db")
    vault.get_secret("t1", "db", aad="svc=db")
    with pytest.raises(cv.CredentialVaultError):
        vault.get_secret("t1", "db", aad="svc=nope")
    entries = vault.audit_log()
    assert [item["op"] for item in entries] == ["put", "get", "get"]
    assert [item["code"] for item in entries] == [
        "ok", "ok", "SEAL_AAD_MISMATCH",
    ]
    assert entries[0]["tenant"] == "t1"
    assert entries[0]["name"] == "db"


def test_snapshot_restore_preserves_secrets_and_audit():
    vault = make()
    vault.put_secret("t1", "db", b"snapshot-value")
    assert vault.get_secret("t1", "db") == b"snapshot-value"
    state = vault.snapshot()
    assert state["version"] == 1
    assert set(state["keks"]) == {"1"}
    clone = cv.CredentialSealVault()
    clone.restore(state)
    assert clone.get_secret("t1", "db") == b"snapshot-value"
    assert clone.active_kek_version() == 1
    assert clone.stats()["audit"] == vault.stats()["audit"] == 2
    state["secrets"].clear()
    assert vault.get_secret("t1", "db") == b"snapshot-value"


def test_restore_rejects_malformed_state_without_mutating():
    vault = make()
    vault.put_secret("t1", "db", b"live-value")
    bad_states = [
        None,
        {},
        {"version": 2, "active_kek": 1, "keks": {}, "secrets": {}, "audit": []},
        {"version": 1, "active_kek": 1, "keks": "nope", "secrets": {}, "audit": []},
        {"version": 1, "active_kek": None, "keks": {}, "secrets": [], "audit": []},
    ]
    for bad in bad_states:
        with pytest.raises(cv.CredentialVaultError) as exc:
            vault.restore(bad)
        assert exc.value.code == "SEAL_SNAPSHOT_INVALID"
    assert vault.get_secret("t1", "db") == b"live-value"


def test_import_record_rejects_broken_payload():
    vault = make()
    for bad in ({}, {"tenant": "t1"}, {"tenant": "t1", "name": "db"}):
        with pytest.raises(cv.CredentialVaultError) as exc:
            vault.import_record(bad)
        assert exc.value.code == "SEAL_RECORD_INVALID"
    vault.put_secret("t1", "db", b"duplicate-target")
    record = vault.export_record("t1", "db")
    with pytest.raises(cv.CredentialVaultError) as dup:
        vault.import_record(record)
    assert dup.value.code == "SEAL_RECORD_INVALID"


def test_concurrent_put_and_get_keep_counts_exact():
    vault = make()
    errors = []

    def worker(index):
        try:
            for turn in range(3):
                name = "cred-%d-%d" % (index, turn)
                vault.put_secret("tenant-race", name, b"value-%d-%d" % (index, turn))
                assert vault.get_secret("tenant-race", name) == b"value-%d-%d" % (index, turn)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    counts = vault.stats()
    assert counts["secrets"] == 24
    assert counts["versions"] == 24


def test_input_validation_and_tenant_isolation():
    vault = make()
    for args in (("", "db", b"value"), ("t1", "", b"value")):
        with pytest.raises(cv.CredentialVaultError) as exc:
            vault.put_secret(*args)
        assert exc.value.code == "SEAL_INPUT_INVALID"
    with pytest.raises(cv.CredentialVaultError) as not_bytes:
        vault.put_secret("t1", "db", "text-secret")
    assert not_bytes.value.code == "SEAL_INPUT_INVALID"

    vault.put_secret("t1", "db", b"tenant-one")
    vault.put_secret("t2", "db", b"tenant-two")
    assert vault.get_secret("t1", "db") == b"tenant-one"
    assert vault.get_secret("t2", "db") == b"tenant-two"
    assert vault.versions("t1", "db") == [1]
    assert vault.versions("t2", "db") == [1]
