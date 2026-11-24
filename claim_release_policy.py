"""OIDC claim release policy: per-client trimming, group expansion, PII minimization."""

from __future__ import annotations

import threading
from dataclasses import dataclass


class ClaimPolicyError(ValueError):
    """Raised when a claim policy operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class ClaimDefinition:
    name: str
    pii_tier: int
    multivalued: bool = False


@dataclass(frozen=True)
class ClientPolicy:
    client_id: str
    version: int
    required: tuple[str, ...]
    optional: tuple[str, ...]
    forbidden: tuple[str, ...]
    max_pii_tier: int


@dataclass(frozen=True)
class ReleaseResult:
    client_id: str
    policy_version: int
    claims: dict
    dropped: tuple[str, ...]
    truncated: bool


class ClaimReleasePolicy:
    """Decide which subject claims a client may receive."""

    def __init__(self, *, max_claims_per_client: int = 32,
                 max_group_depth: int = 2, max_expanded_claims: int = 16) -> None:
        self.max_claims_per_client = max_claims_per_client
        self.max_group_depth = max_group_depth
        self.max_expanded_claims = max_expanded_claims
        self._lock = threading.RLock()

    def define_claim(self, name: str, *, pii_tier: int,
                     multivalued: bool = False) -> None:
        raise NotImplementedError

    def define_group(self, name: str, *, claims=(), parent=None) -> None:
        raise NotImplementedError

    def set_client_policy(self, client_id: str, *, required=(), optional=(),
                          forbidden=(), max_pii_tier: int = 1) -> ClientPolicy:
        raise NotImplementedError

    def policy_for(self, client_id: str) -> ClientPolicy | None:
        raise NotImplementedError

    def policy_versions(self, client_id: str) -> list[int]:
        raise NotImplementedError

    def release_claims(self, *, client_id: str, subject: dict,
                       groups=()) -> ReleaseResult:
        raise NotImplementedError

    def explain(self, *, client_id: str, claim: str) -> dict:
        raise NotImplementedError

    def rollback_policy(self, client_id: str, version: int) -> ClientPolicy:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError
