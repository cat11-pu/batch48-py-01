"""External identity provider group to tenant role mapping registry."""

from __future__ import annotations

import threading


class GroupMappingError(ValueError):
    """Raised when a mapping operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class GroupMappingRegistry:
    """Map external groups onto tenant roles and keep derived memberships."""

    def __init__(self, *, max_mappings_per_tenant: int = 200, clock=None) -> None:
        self.max_mappings_per_tenant = max_mappings_per_tenant
        self._clock = clock
        self._lock = threading.RLock()

    def add_mapping(self, mapping: dict) -> dict:
        raise NotImplementedError

    def list_mappings(self, tenant: str) -> list:
        raise NotImplementedError

    def remove_mapping(self, tenant: str, mapping_id: str) -> dict:
        raise NotImplementedError

    def disable_mapping(self, tenant: str, mapping_id: str,
                        *, now: int = 0) -> dict:
        raise NotImplementedError

    def enable_mapping(self, tenant: str, mapping_id: str,
                       *, now: int = 0) -> dict:
        raise NotImplementedError

    def resolve_roles(self, tenant: str, groups) -> dict:
        raise NotImplementedError

    def sync_membership(self, tenant: str, group: str, members,
                        *, now: int = 0) -> dict:
        raise NotImplementedError

    def grant_manual(self, tenant: str, user: str, role: str) -> dict:
        raise NotImplementedError

    def revoke_manual(self, tenant: str, user: str, role: str) -> dict:
        raise NotImplementedError

    def roles_of(self, tenant: str, user: str) -> list:
        raise NotImplementedError

    def drift_report(self, tenant: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def reconcile(self, tenant: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
