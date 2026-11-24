"""Tenant credential vault: envelope sealing, KEK rotation and snapshots."""

from __future__ import annotations

import threading

DEFAULT_AUDIT_LIMIT = 64
DEFAULT_KEEP_VERSIONS = 3
DEFAULT_BATCH_LIMIT = 32


class CredentialVaultError(ValueError):
    """Raised for every rejected vault operation."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code = code
        self.detail = detail


class CredentialSealVault:
    """Seal tenant credentials with per-record DEKs wrapped by a KEK."""

    def __init__(self, *, audit_limit=DEFAULT_AUDIT_LIMIT,
                 keep_versions=DEFAULT_KEEP_VERSIONS,
                 batch_limit=DEFAULT_BATCH_LIMIT):
        self.audit_limit = audit_limit
        self.keep_versions = keep_versions
        self.batch_limit = batch_limit
        self._lock = threading.RLock()

    def add_kek(self, version, key):
        raise NotImplementedError

    def active_kek_version(self):
        raise NotImplementedError

    def revoke_kek(self, version):
        raise NotImplementedError

    def rotate_kek(self, version, key, *, rewrap_limit=None):
        raise NotImplementedError

    def put_secret(self, tenant, name, plaintext, *, aad=""):
        raise NotImplementedError

    def get_secret(self, tenant, name, *, aad="", version=None):
        raise NotImplementedError

    def versions(self, tenant, name):
        raise NotImplementedError

    def export_record(self, tenant, name, *, version=None):
        raise NotImplementedError

    def import_record(self, record, *, replace=False):
        raise NotImplementedError

    def rewrap_batch(self, tenant, batch_size):
        raise NotImplementedError

    def audit_log(self):
        raise NotImplementedError

    def snapshot(self):
        raise NotImplementedError

    def restore(self, state):
        raise NotImplementedError

    def stats(self):
        raise NotImplementedError
