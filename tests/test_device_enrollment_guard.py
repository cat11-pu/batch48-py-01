import copy
import re
import threading

import pytest

import device_enrollment_guard as de

CODE_PATTERN = r"[0-9A-F]{4}(?:-[0-9A-F]{4}){3}"


def test_issue_code_format_and_registry():
    guard = de.DeviceEnrollmentGuard()
    info = guard.issue_code("t1", "pos")
    assert re.fullmatch(CODE_PATTERN, info["code"])
    assert info["tenant"] == "t1"
    assert info["device_type"] == "pos"
    assert info["state"] == "pending"
    assert info["expires_at"] is None

    entries = guard.codes()
    assert len(entries) == 1
    assert entries[0]["code"] == info["code"]
    assert entries[0]["state"] == "pending"

    explicit = guard.issue_code("t1", "pos", code="ABCD-EF01-2345-6789")
    assert explicit["code"] == "ABCD-EF01-2345-6789"
    with pytest.raises(de.EnrollmentError) as dup:
        guard.issue_code("t1", "pos", code="ABCD-EF01-2345-6789")
    assert dup.value.code == "ENR_CODE_DUPLICATE"
    with pytest.raises(de.EnrollmentError) as bad:
        guard.issue_code("t1", "pos", code="lower-case")
    assert bad.value.code == "ENR_INPUT_INVALID"


def test_enroll_issues_serial_and_certificate():
    guard = de.DeviceEnrollmentGuard(cert_ttl=3600)
    code = guard.issue_code("t1", "pos")["code"]
    result = guard.enroll(code, "fingerprint-aaaa", now=100)
    assert result["serial"] == "DEV-000001"
    assert result["tenant"] == "t1"
    assert result["device_type"] == "pos"
    assert result["issued_at"] == 100
    assert result["not_after"] == 3700
    assert result["state"] == "active"
    assert result["replayed"] is False
    assert result["fingerprint_digest"] == guard.fingerprint_digest("fingerprint-aaaa")
    assert len(result["fingerprint_digest"]) == 64
    assert result["fingerprint_digest"] != "fingerprint-aaaa"


def test_unknown_code_rejected():
    guard = de.DeviceEnrollmentGuard()
    with pytest.raises(de.EnrollmentError) as exc:
        guard.enroll("NOPE-NOPE-NOPE-NOPE", "fingerprint-aaaa")
    assert exc.value.code == "ENR_CODE_UNKNOWN"
    assert guard.codes() == []


def test_expired_code_rejected_without_consuming_it():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos", expires_at=100)["code"]
    with pytest.raises(de.EnrollmentError) as exc:
        guard.enroll(code, "fingerprint-aaaa", now=100)
    assert exc.value.code == "ENR_CODE_EXPIRED"
    assert guard.codes()[0]["state"] == "pending"
    assert guard.stats()["certs"] == 0


def test_used_code_with_other_fingerprint_rejected():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos")["code"]
    guard.enroll(code, "fingerprint-aaaa")
    with pytest.raises(de.EnrollmentError) as exc:
        guard.enroll(code, "fingerprint-bbbb")
    assert exc.value.code == "ENR_CODE_USED"
    assert len(guard.certificates()) == 1


def test_replayed_enroll_same_fingerprint_is_idempotent():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos")["code"]
    first = guard.enroll(code, "fingerprint-aaaa", now=5)
    second = guard.enroll(code, "fingerprint-aaaa", now=6)
    assert second["serial"] == first["serial"] == "DEV-000001"
    assert second["replayed"] is True
    assert second["issued_at"] == 5
    assert len(guard.certificates()) == 1
    assert guard.stats()["certs"] == 1


def test_device_type_mismatch_rejected():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos")["code"]
    with pytest.raises(de.EnrollmentError) as exc:
        guard.enroll(code, "fingerprint-aaaa", device_type="camera")
    assert exc.value.code == "ENR_TYPE_MISMATCH"
    assert guard.enroll(code, "fingerprint-aaaa", device_type="pos")["serial"] == "DEV-000001"


def test_serials_increment_across_codes():
    guard = de.DeviceEnrollmentGuard()
    first = guard.issue_code("t1", "pos")["code"]
    second = guard.issue_code("t1", "pos")["code"]
    assert guard.enroll(first, "fingerprint-aaaa")["serial"] == "DEV-000001"
    assert guard.enroll(second, "fingerprint-bbbb")["serial"] == "DEV-000002"


def test_verify_reports_valid_expired_and_unknown():
    guard = de.DeviceEnrollmentGuard(cert_ttl=100)
    code = guard.issue_code("t1", "pos")["code"]
    serial = guard.enroll(code, "fingerprint-aaaa", now=0)["serial"]
    assert guard.verify(serial, now=99) == "valid"
    assert guard.verify(serial, now=100) == "expired"
    with pytest.raises(de.EnrollmentError) as exc:
        guard.verify("DEV-999999")
    assert exc.value.code == "ENR_CERT_UNKNOWN"


def test_revoke_is_sticky_and_idempotent():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos")["code"]
    serial = guard.enroll(code, "fingerprint-aaaa")["serial"]
    assert guard.revoke(serial, reason="board replaced") is True
    assert guard.revoke(serial, reason="again") is False
    assert guard.verify(serial) == "revoked"
    assert guard.is_revoked(serial) is True
    assert guard.is_revoked("DEV-999999") is False
    info = guard.lookup(serial)
    assert info["state"] == "revoked"
    assert info["reason"] == "board replaced"


def test_certificate_listing_filters_and_copies():
    guard = de.DeviceEnrollmentGuard()
    first = guard.issue_code("t1", "pos")["code"]
    second = guard.issue_code("t2", "kiosk")["code"]
    guard.enroll(first, "fingerprint-aaaa")
    guard.enroll(second, "fingerprint-bbbb")
    tenant_one = guard.certificates(tenant="t1")
    assert [item["serial"] for item in tenant_one] == ["DEV-000001"]
    tenant_one[0]["state"] = "tampered"
    assert guard.lookup("DEV-000001")["state"] == "active"
    assert len(guard.certificates()) == 2


def test_certificate_capacity_limit():
    guard = de.DeviceEnrollmentGuard(max_certs=1)
    first = guard.issue_code("t1", "pos")["code"]
    second = guard.issue_code("t1", "pos")["code"]
    guard.enroll(first, "fingerprint-aaaa")
    with pytest.raises(de.EnrollmentError) as exc:
        guard.enroll(second, "fingerprint-bbbb")
    assert exc.value.code == "ENR_LIMIT_EXCEEDED"
    assert guard.stats()["certs"] == 1
    assert guard.codes()[1]["state"] == "pending"


def test_snapshot_restore_preserves_codes_and_certificates():
    guard = de.DeviceEnrollmentGuard(cert_ttl=500)
    first = guard.issue_code("t1", "pos")["code"]
    second = guard.issue_code("t1", "pos")["code"]
    serial_one = guard.enroll(first, "fingerprint-aaaa", now=0)["serial"]
    serial_two = guard.enroll(second, "fingerprint-bbbb", now=0)["serial"]
    state = guard.snapshot()
    assert state["version"] == 1
    assert set(state) >= {"codes", "certs"}

    clone = de.DeviceEnrollmentGuard(cert_ttl=500)
    clone.restore(copy.deepcopy(state))
    assert clone.verify(serial_one, now=10) == "valid"
    assert clone.verify(serial_two, now=10) == "valid"
    assert [item["state"] for item in clone.codes()] == ["used", "used"]
    assert clone.stats() == guard.stats()
    state["certs"].clear()
    assert guard.lookup(serial_one)["state"] == "active"


def test_restore_rejects_malformed_state():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos")["code"]
    serial = guard.enroll(code, "fingerprint-aaaa")["serial"]
    bad_states = [
        None,
        {},
        {"version": 2, "codes": {}, "certs": {}, "next_serial": 1},
        {"version": 1, "codes": [], "certs": {}, "next_serial": 1},
        {"version": 1, "codes": {}, "certs": {}, "next_serial": "one"},
    ]
    for bad in bad_states:
        with pytest.raises(de.EnrollmentError) as exc:
            guard.restore(bad)
        assert exc.value.code == "ENR_SNAPSHOT_INVALID"
    assert guard.lookup(serial)["state"] == "active"


def test_concurrent_redeem_of_one_code_has_single_winner():
    guard = de.DeviceEnrollmentGuard()
    code = guard.issue_code("t1", "pos")["code"]
    winners = []
    rejected = []
    errors = []

    def worker(index):
        try:
            result = guard.enroll(code, "fingerprint-%04d" % index)
            winners.append(result["serial"])
        except de.EnrollmentError as exc:
            rejected.append(exc.code)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(winners) == 1
    assert rejected == ["ENR_CODE_USED"] * 7
    assert guard.stats()["certs"] == 1
