"""Approval delegation chains with separation of duty and expiry reclaim."""

from __future__ import annotations

import threading


class DelegationError(ValueError):
    """Raised when an approval delegation operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ApprovalDelegationChain:
    """Track temporary approver stand-ins and the requests they vote on."""

    def __init__(self, *, max_depth: int = 3, max_duration: int = 86400,
                 clock=None) -> None:
        self.max_depth = max_depth
        self.max_duration = max_duration
        self._clock = clock
        self._lock = threading.RLock()

    def delegate_approval(self, delegator: str, delegate: str, scopes,
                          *, now: int = 0, ttl: int | None = None) -> dict:
        raise NotImplementedError

    def accept_delegation(self, delegation_id: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def list_delegations(self, approver: str) -> list:
        raise NotImplementedError

    def effective_approvers(self, approver: str, *, now: int = 0,
                            scopes=None) -> list:
        raise NotImplementedError

    def create_request(self, requester: str, category: str, required_approvers,
                       *, quorum: int | None = None, now: int = 0) -> dict:
        raise NotImplementedError

    def request_state(self, request_id: str) -> dict:
        raise NotImplementedError

    def decide(self, request_id: str, approver: str, decision: str,
               *, now: int = 0) -> dict:
        raise NotImplementedError

    def revoke_delegation(self, delegation_id: str, *, now: int = 0) -> dict:
        raise NotImplementedError

    def expire_due(self, *, now: int = 0) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
