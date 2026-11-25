"""Device enrollment: one-time codes, hardware binding, certificate issuance."""

from __future__ import annotations

import copy
import hashlib
import re
import secrets
import threading

DEFAULT_CERT_TTL = 86400
DEFAULT_MAX_CODES = 1000
DEFAULT_MAX_CERTS = 1000
SNAPSHOT_VERSION = 1

CODE_PATTERN = re.compile(r"^[0-9A-F]{4}(?:-[0-9A-F]{4}){3}$")
SERIAL_PATTERN = re.compile(r"^DEV-\d{6}$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CODE_STATES = ("pending", "used")
CERT_STATES = ("active", "revoked")
_MISSING = object()


class EnrollmentError(ValueError):
    """Raised for every rejected enrollment operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class DeviceEnrollmentGuard:
    """Redeem enrollment codes and bind certificates to hardware digests."""

    def __init__(self, *, cert_ttl=DEFAULT_CERT_TTL,
                 max_codes=DEFAULT_MAX_CODES,
                 max_certs=DEFAULT_MAX_CERTS):
        self.cert_ttl = cert_ttl
        self.max_codes = max_codes
        self.max_certs = max_certs
        self._codes = []
        self._certs = {}
        self._bindings = {}
        self._next_serial = 1
        self._lock = threading.RLock()

    @staticmethod
    def _is_nonempty_str(value):
        return isinstance(value, str) and len(value) > 0

    @staticmethod
    def _is_number(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @staticmethod
    def _digest_of(fingerprint):
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()

    def _generate_code(self):
        existing = {entry["code"] for entry in self._codes}
        while True:
            token = secrets.token_hex(8).upper()
            candidate = "-".join(token[i:i + 4] for i in range(0, 16, 4))
            if candidate not in existing:
                return candidate

    def _find_code(self, code):
        for entry in self._codes:
            if entry["code"] == code:
                return entry
        return None

    @staticmethod
    def _public_code(entry):
        return {
            "code": entry["code"],
            "tenant": entry["tenant"],
            "device_type": entry["device_type"],
            "expires_at": entry["expires_at"],
            "state": entry["state"],
        }

    @staticmethod
    def _enroll_response(cert, *, replayed):
        return {
            "serial": cert["serial"],
            "tenant": cert["tenant"],
            "device_type": cert["device_type"],
            "fingerprint_digest": cert["fingerprint_digest"],
            "issued_at": cert["issued_at"],
            "not_after": cert["not_after"],
            "state": cert["state"],
            "replayed": replayed,
        }

    def issue_code(self, tenant, device_type, *, code=None, expires_at=None):
        if not self._is_nonempty_str(tenant) or not self._is_nonempty_str(device_type):
            raise EnrollmentError(
                "ENR_INPUT_INVALID",
                "tenant and device_type must be non-empty strings",
            )
        if expires_at is not None and not self._is_number(expires_at):
            raise EnrollmentError(
                "ENR_INPUT_INVALID",
                "expires_at must be a numeric timestamp or None",
            )
        with self._lock:
            if len(self._codes) >= self.max_codes:
                raise EnrollmentError(
                    "ENR_LIMIT_EXCEEDED",
                    "enrollment code capacity reached",
                )
            if code is None:
                code = self._generate_code()
            else:
                if not isinstance(code, str) or not CODE_PATTERN.fullmatch(code):
                    raise EnrollmentError(
                        "ENR_INPUT_INVALID",
                        "enrollment code must be four groups of four uppercase hex digits",
                    )
                if self._find_code(code) is not None:
                    raise EnrollmentError(
                        "ENR_CODE_DUPLICATE",
                        "enrollment code has already been issued",
                    )
            entry = {
                "code": code,
                "tenant": tenant,
                "device_type": device_type,
                "expires_at": expires_at,
                "state": "pending",
                "fingerprint_digest": None,
            }
            self._codes.append(entry)
            return self._public_code(entry)

    def enroll(self, code, fingerprint, *, device_type=None, now=0):
        if not isinstance(fingerprint, str) or len(fingerprint) < 8:
            raise EnrollmentError(
                "ENR_FINGERPRINT_INVALID",
                "fingerprint must be a string of at least 8 characters",
            )
        with self._lock:
            entry = self._find_code(code)
            if entry is None:
                raise EnrollmentError(
                    "ENR_CODE_UNKNOWN",
                    "enrollment code is not registered",
                )
            if device_type is not None and device_type != entry["device_type"]:
                raise EnrollmentError(
                    "ENR_TYPE_MISMATCH",
                    "device_type does not match the enrollment code",
                )
            digest = self._digest_of(fingerprint)

            if entry["state"] == "used":
                if entry.get("fingerprint_digest") == digest:
                    serial = self._bindings[digest]
                    return self._enroll_response(
                        self._certs[serial], replayed=True
                    )
                raise EnrollmentError(
                    "ENR_CODE_USED",
                    "enrollment code has already been redeemed",
                )

            if entry["expires_at"] is not None and now >= entry["expires_at"]:
                raise EnrollmentError(
                    "ENR_CODE_EXPIRED",
                    "enrollment code has expired",
                )

            bound_serial = self._bindings.get(digest)
            if bound_serial is not None:
                bound_cert = self._certs[bound_serial]
                if bound_cert["state"] == "revoked":
                    raise EnrollmentError(
                        "ENR_DEVICE_REVOKED",
                        "device is revoked; a new enrollment code cannot lift it",
                    )
                entry["state"] = "used"
                entry["fingerprint_digest"] = digest
                return self._enroll_response(bound_cert, replayed=True)

            if len(self._certs) >= self.max_certs:
                raise EnrollmentError(
                    "ENR_LIMIT_EXCEEDED",
                    "certificate capacity reached",
                )

            serial = "DEV-%06d" % self._next_serial
            self._next_serial += 1
            cert = {
                "serial": serial,
                "tenant": entry["tenant"],
                "device_type": entry["device_type"],
                "fingerprint_digest": digest,
                "issued_at": now,
                "not_after": now + self.cert_ttl,
                "state": "active",
                "reason": "",
            }
            self._certs[serial] = cert
            self._bindings[digest] = serial
            entry["state"] = "used"
            entry["fingerprint_digest"] = digest
            return self._enroll_response(cert, replayed=False)

    def lookup(self, serial):
        with self._lock:
            cert = self._certs.get(serial)
            if cert is None:
                raise EnrollmentError(
                    "ENR_CERT_UNKNOWN",
                    "no certificate for serial",
                )
            return copy.deepcopy(cert)

    def verify(self, serial, *, now=0):
        with self._lock:
            cert = self._certs.get(serial)
            if cert is None:
                raise EnrollmentError(
                    "ENR_CERT_UNKNOWN",
                    "no certificate for serial",
                )
            if cert["state"] == "revoked":
                return "revoked"
            if now >= cert["not_after"]:
                return "expired"
            return "valid"

    def revoke(self, serial, reason=""):
        with self._lock:
            cert = self._certs.get(serial)
            if cert is None or cert["state"] == "revoked":
                return False
            cert["state"] = "revoked"
            cert["reason"] = reason if isinstance(reason, str) else str(reason)
            return True

    def is_revoked(self, serial):
        with self._lock:
            cert = self._certs.get(serial)
            return cert is not None and cert["state"] == "revoked"

    def certificates(self, *, tenant=None):
        with self._lock:
            certs = [
                copy.deepcopy(cert)
                for cert in self._certs.values()
                if tenant is None or cert["tenant"] == tenant
            ]
            certs.sort(key=lambda cert: cert["serial"])
            return certs

    def codes(self):
        with self._lock:
            return [self._public_code(entry) for entry in self._codes]

    def fingerprint_digest(self, fingerprint):
        if not isinstance(fingerprint, str):
            raise EnrollmentError(
                "ENR_FINGERPRINT_INVALID",
                "fingerprint must be a string",
            )
        return self._digest_of(fingerprint)

    def snapshot(self):
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "codes": copy.deepcopy(self._codes),
                "certs": [
                    copy.deepcopy(self._certs[serial])
                    for serial in sorted(self._certs)
                ],
                "next_serial": self._next_serial,
            }

    def restore(self, state):
        codes, certs, next_serial = self._parse_snapshot(state)
        with self._lock:
            self._codes = codes
            self._certs = certs
            self._bindings = {
                cert["fingerprint_digest"]: cert["serial"]
                for cert in certs.values()
            }
            self._next_serial = next_serial

    def stats(self):
        with self._lock:
            return {
                "codes": len(self._codes),
                "certs": len(self._certs),
                "revoked": sum(
                    1 for cert in self._certs.values()
                    if cert["state"] == "revoked"
                ),
                "next_serial": self._next_serial,
            }

    def _parse_snapshot(self, state):
        invalid = EnrollmentError(
            "ENR_SNAPSHOT_INVALID",
            "snapshot structure is invalid",
        )
        if not isinstance(state, dict) or state.get("version") != SNAPSHOT_VERSION:
            raise invalid
        raw_codes = state.get("codes", _MISSING)
        raw_certs = state.get("certs", _MISSING)
        next_serial = state.get("next_serial", _MISSING)
        if not isinstance(raw_codes, list) or not isinstance(raw_certs, list):
            raise invalid
        if (
            not isinstance(next_serial, int)
            or isinstance(next_serial, bool)
            or next_serial < 1
        ):
            raise invalid

        codes = []
        seen_codes = set()
        for raw in raw_codes:
            if not isinstance(raw, dict):
                raise invalid
            code = raw.get("code")
            tenant = raw.get("tenant")
            device_type = raw.get("device_type")
            expires_at = raw.get("expires_at")
            code_state = raw.get("state")
            digest = raw.get("fingerprint_digest")
            if (
                not isinstance(code, str)
                or not CODE_PATTERN.fullmatch(code)
                or code in seen_codes
                or not self._is_nonempty_str(tenant)
                or not self._is_nonempty_str(device_type)
                or (expires_at is not None and not self._is_number(expires_at))
                or code_state not in CODE_STATES
                or (digest is not None and not (
                    isinstance(digest, str) and DIGEST_PATTERN.fullmatch(digest)
                ))
                or (code_state == "used" and not isinstance(digest, str))
            ):
                raise invalid
            seen_codes.add(code)
            codes.append({
                "code": code,
                "tenant": tenant,
                "device_type": device_type,
                "expires_at": expires_at,
                "state": code_state,
                "fingerprint_digest": digest,
            })

        certs = {}
        seen_digests = set()
        for raw in raw_certs:
            if not isinstance(raw, dict):
                raise invalid
            serial = raw.get("serial")
            tenant = raw.get("tenant")
            device_type = raw.get("device_type")
            digest = raw.get("fingerprint_digest")
            issued_at = raw.get("issued_at")
            not_after = raw.get("not_after")
            cert_state = raw.get("state")
            reason = raw.get("reason", "")
            if (
                not isinstance(serial, str)
                or not SERIAL_PATTERN.fullmatch(serial)
                or serial in certs
                or not self._is_nonempty_str(tenant)
                or not self._is_nonempty_str(device_type)
                or not isinstance(digest, str)
                or not DIGEST_PATTERN.fullmatch(digest)
                or digest in seen_digests
                or not self._is_number(issued_at)
                or not self._is_number(not_after)
                or cert_state not in CERT_STATES
                or not isinstance(reason, str)
            ):
                raise invalid
            seen_digests.add(digest)
            certs[serial] = {
                "serial": serial,
                "tenant": tenant,
                "device_type": device_type,
                "fingerprint_digest": digest,
                "issued_at": issued_at,
                "not_after": not_after,
                "state": cert_state,
                "reason": reason,
            }

        return codes, certs, next_serial
