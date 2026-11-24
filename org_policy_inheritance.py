"""Organization unit tree with inherited policies, hard denies and subtree moves."""

from __future__ import annotations

import threading


class OrgPolicyError(ValueError):
    """Raised when an org policy operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class OrgPolicyInheritance:
    """Keep an org unit tree and resolve the effective policy of each node."""

    def __init__(self, root: str = "org", *, max_depth: int = 8,
                 max_policies_per_node: int = 8, clock=None) -> None:
        self.root = root
        self.max_depth = max_depth
        self.max_policies_per_node = max_policies_per_node
        self._clock = clock
        self._lock = threading.RLock()

    def add_org_unit(self, ou_id: str, parent_id: str) -> dict:
        raise NotImplementedError

    def remove_org_unit(self, ou_id: str) -> dict:
        raise NotImplementedError

    def path_of(self, ou_id: str) -> list:
        raise NotImplementedError

    def move_subtree(self, ou_id: str, new_parent_id: str) -> dict:
        raise NotImplementedError

    def attach_policy(self, ou_id: str, policy: dict) -> dict:
        raise NotImplementedError

    def detach_policy(self, ou_id: str, policy_id: str) -> dict:
        raise NotImplementedError

    def effective_policy(self, ou_id: str) -> dict:
        raise NotImplementedError

    def explain(self, ou_id: str, field: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
