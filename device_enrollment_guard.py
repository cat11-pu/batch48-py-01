"""Device enrollment: one-time codes, hardware binding, certificate issuance."""

from __future__ import annotations

import threading

DEFAULT_CERT_TTL = 86400
DEFAULT_MAX_CODES = 1000
DEFAULT_MAX_CERTS = 1000


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
        self._lock = threading.RLock()

    def issue_code(self, tenant, device_type, *, code=None, expires_at=None):
        raise NotImplementedError

    def enroll(self, code, fingerprint, *, device_type=None, now=0):
        raise NotImplementedError

    def lookup(self, serial):
        raise NotImplementedError

    def verify(self, serial, *, now=0):
        raise NotImplementedError

    def revoke(self, serial, reason=""):
        raise NotImplementedError

    def is_revoked(self, serial):
        raise NotImplementedError

    def certificates(self, *, tenant=None):
        raise NotImplementedError

    def codes(self):
        raise NotImplementedError

    def fingerprint_digest(self, fingerprint):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
