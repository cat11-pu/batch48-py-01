"""OIDC claim release policy: per-client trimming, group expansion, PII minimization."""

from __future__ import annotations

import copy
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


@dataclass(frozen=True)
class GroupDefinition:
    name: str
    claims: tuple[str, ...]
    parent: str | None = None


_SNAPSHOT_VERSION = 1


class ClaimReleasePolicy:
    """Decide which subject claims a client may receive."""

    def __init__(self, *, max_claims_per_client: int = 32,
                 max_group_depth: int = 2, max_expanded_claims: int = 16) -> None:
        self.max_claims_per_client = max_claims_per_client
        self.max_group_depth = max_group_depth
        self.max_expanded_claims = max_expanded_claims
        self._lock = threading.RLock()
        self._claims: dict[str, ClaimDefinition] = {}
        self._groups: dict[str, GroupDefinition] = {}
        self._history: dict[str, dict[int, ClientPolicy]] = {}
        self._current: dict[str, int] = {}
        self._conflicts: dict[str, tuple[str, ...]] = {}
        self._released = 0
        self._denied = 0
        self._truncated = 0

    @staticmethod
    def _require_name(kind: str, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ClaimPolicyError("CLAIM_INVALID", value)
        return value

    @staticmethod
    def _require_tier(value: object) -> int:
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ClaimPolicyError("CLAIM_INVALID", value)
        return value

    def define_claim(self, name: str, *, pii_tier: int,
                     multivalued: bool = False) -> None:
        with self._lock:
            self._require_name("claim", name)
            self._require_tier(pii_tier)
            existing = self._claims.get(name)
            if existing is not None:
                if (existing.pii_tier, existing.multivalued) == (pii_tier, bool(multivalued)):
                    return
                raise ClaimPolicyError("CLAIM_CLAIM_CONFLICT", name)
            self._claims[name] = ClaimDefinition(name, pii_tier, bool(multivalued))

    def define_group(self, name: str, *, claims=(), parent=None) -> None:
        with self._lock:
            self._require_name("group", name)
            names = tuple(sorted(set(claims)))
            for claim in names:
                if claim not in self._claims:
                    raise ClaimPolicyError("CLAIM_UNDECLARED", claim)
            if parent is not None and parent not in self._groups:
                raise ClaimPolicyError("CLAIM_UNKNOWN_GROUP", parent)
            existing = self._groups.get(name)
            if existing is not None:
                if existing.claims == names and existing.parent == parent:
                    return
                raise ClaimPolicyError("CLAIM_GROUP_CONFLICT", name)
            self._groups[name] = GroupDefinition(name, names, parent)

    def set_client_policy(self, client_id: str, *, required=(), optional=(),
                          forbidden=(), max_pii_tier: int = 3) -> ClientPolicy:
        with self._lock:
            self._require_name("client", client_id)
            self._require_tier(max_pii_tier)
            req = sorted(set(required))
            opt = sorted(set(optional))
            forb = sorted(set(forbidden))
            for claim in (*req, *opt, *forb):
                if claim not in self._claims:
                    raise ClaimPolicyError("CLAIM_UNDECLARED", claim)
            if len(set(req) | set(opt) | set(forb)) > self.max_claims_per_client:
                raise ClaimPolicyError("CLAIM_CAPACITY_EXCEEDED", client_id)
            forbidden_set = set(forb)
            conflicts = tuple(sorted((set(req) | set(opt)) & forbidden_set))
            req = [claim for claim in req if claim not in forbidden_set]
            opt = [claim for claim in opt if claim not in forbidden_set]
            history = self._history.setdefault(client_id, {})
            current = history.get(self._current.get(client_id))
            if (current is not None
                    and current.required == tuple(req)
                    and current.optional == tuple(opt)
                    and current.forbidden == tuple(forb)
                    and current.max_pii_tier == max_pii_tier):
                self._conflicts[client_id] = conflicts
                return current
            version = max(history) + 1 if history else 1
            policy = ClientPolicy(client_id, version, tuple(req), tuple(opt),
                                  tuple(forb), max_pii_tier)
            history[version] = policy
            self._current[client_id] = version
            self._conflicts[client_id] = conflicts
            return policy

    def policy_for(self, client_id: str) -> ClientPolicy | None:
        with self._lock:
            version = self._current.get(client_id)
            if version is None:
                return None
            return self._history[client_id][version]

    def policy_versions(self, client_id: str) -> list[int]:
        with self._lock:
            return sorted(self._history.get(client_id, ()))

    def _expand_groups(self, groups) -> set[str]:
        collected: set[str] = set()
        for start in groups:
            if start not in self._groups:
                raise ClaimPolicyError("CLAIM_UNKNOWN_GROUP", start)
            current = start
            depth = 0
            visited = set()
            while current is not None and current not in visited:
                visited.add(current)
                group = self._groups.get(current)
                if group is None:
                    break
                collected.update(group.claims)
                if depth >= self.max_group_depth:
                    break
                current = group.parent
                depth += 1
        return collected

    def release_claims(self, *, client_id: str, subject: dict,
                       groups=()) -> ReleaseResult:
        with self._lock:
            policy = self.policy_for(client_id)
            if policy is None:
                raise ClaimPolicyError("CLAIM_UNKNOWN_CLIENT", client_id)
            subject = dict(subject)
            expanded = self._expand_groups(groups)
            allowed = set(policy.required) | set(policy.optional)
            forbidden = set(policy.forbidden)

            expanded_allowed = {
                claim for claim in expanded
                if claim in allowed and claim not in forbidden
                and self._claims[claim].pii_tier <= policy.max_pii_tier
            }
            truncated = False
            expanded_final = set(expanded_allowed)
            if len(expanded_final) > self.max_expanded_claims:
                keep = {claim for claim in expanded_final if claim in policy.required}
                room = max(self.max_expanded_claims - len(keep), 0)
                extras = sorted(claim for claim in expanded_final if claim not in keep)
                keep.update(extras[:room])
                expanded_final = keep
                truncated = True

            claims: dict = {}
            dropped: list[str] = []
            for claim in sorted(set(subject) | expanded):
                if claim in forbidden or claim not in allowed:
                    dropped.append(claim)
                    continue
                if self._claims[claim].pii_tier > policy.max_pii_tier:
                    dropped.append(claim)
                    continue
                if (claim in expanded and claim not in expanded_final
                        and claim not in subject):
                    dropped.append(claim)
                    continue
                if claim in subject:
                    claims[claim] = subject[claim]
                elif claim in expanded_final:
                    claims[claim] = True

            missing = [claim for claim in policy.required if claim not in claims]
            if missing and subject:
                self._denied += 1
                raise ClaimPolicyError("CLAIM_REQUIRED_MISSING", missing[0])
            self._released += 1
            if truncated:
                self._truncated += 1
            return ReleaseResult(client_id, policy.version, claims,
                                 tuple(dropped), truncated)

    def explain(self, *, client_id: str, claim: str) -> dict:
        with self._lock:
            policy = self.policy_for(client_id)
            if policy is None:
                raise ClaimPolicyError("CLAIM_UNKNOWN_CLIENT", client_id)
            if claim in policy.forbidden:
                decision = "forbidden"
            elif claim in policy.required:
                decision = "required"
            elif claim in policy.optional:
                decision = "optional"
            else:
                decision = "undeclared"
            definition = self._claims.get(claim)
            return {
                "client_id": client_id,
                "claim": claim,
                "decision": decision,
                "policy_version": policy.version,
                "defined": definition is not None,
                "pii_tier": definition.pii_tier if definition is not None else None,
                "max_pii_tier": policy.max_pii_tier,
                "conflict": claim in self._conflicts.get(client_id, ()),
            }

    def rollback_policy(self, client_id: str, version: int) -> ClientPolicy:
        with self._lock:
            history = self._history.get(client_id)
            if not history:
                raise ClaimPolicyError("CLAIM_UNKNOWN_CLIENT", client_id)
            policy = history.get(version)
            if policy is None:
                raise ClaimPolicyError("CLAIM_UNKNOWN_VERSION", version)
            self._current[client_id] = version
            return policy

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": _SNAPSHOT_VERSION,
                "limits": {
                    "max_claims_per_client": self.max_claims_per_client,
                    "max_group_depth": self.max_group_depth,
                    "max_expanded_claims": self.max_expanded_claims,
                },
                "claims": [
                    {"name": item.name, "pii_tier": item.pii_tier,
                     "multivalued": item.multivalued}
                    for item in self._claims.values()
                ],
                "groups": [
                    {"name": group.name, "claims": list(group.claims),
                     "parent": group.parent}
                    for group in self._groups.values()
                ],
                "policies": {
                    client_id: {
                        "current": self._current[client_id],
                        "conflicts": list(self._conflicts.get(client_id, ())),
                        "versions": [
                            {"version": policy.version,
                             "required": list(policy.required),
                             "optional": list(policy.optional),
                             "forbidden": list(policy.forbidden),
                             "max_pii_tier": policy.max_pii_tier}
                            for version, policy in sorted(history.items())
                        ],
                    }
                    for client_id, history in self._history.items()
                },
                "stats": self.stats(),
            }
            return copy.deepcopy(state)

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    @classmethod
    def _validate_snapshot(cls, state: object):
        def invalid() -> None:
            raise ClaimPolicyError("CLAIM_SNAPSHOT_INVALID")

        if not isinstance(state, dict) or state.get("version") != _SNAPSHOT_VERSION:
            invalid()
        try:
            raw_claims = state["claims"]
            raw_groups = state["groups"]
            raw_policies = state["policies"]
            raw_stats = state["stats"]
            raw_limits = state["limits"]
        except KeyError:
            invalid()
        if not isinstance(raw_claims, list) or not isinstance(raw_groups, list):
            invalid()
        if not isinstance(raw_policies, dict) or not isinstance(raw_stats, dict):
            invalid()
        if not isinstance(raw_limits, dict):
            invalid()

        limits = []
        for key in ("max_claims_per_client", "max_group_depth", "max_expanded_claims"):
            value = raw_limits.get(key)
            if not cls._is_int(value) or value < 0:
                invalid()
            limits.append(value)

        claims: dict[str, ClaimDefinition] = {}
        for entry in raw_claims:
            if not isinstance(entry, dict):
                invalid()
            name = entry.get("name")
            tier = entry.get("pii_tier")
            multivalued = entry.get("multivalued", False)
            if not isinstance(name, str) or not name.strip():
                invalid()
            if not cls._is_int(tier) or tier < 0 or not isinstance(multivalued, bool):
                invalid()
            claims[name] = ClaimDefinition(name, tier, multivalued)

        groups: dict[str, GroupDefinition] = {}
        for entry in raw_groups:
            if not isinstance(entry, dict):
                invalid()
            name = entry.get("name")
            members = entry.get("claims")
            parent = entry.get("parent")
            if not isinstance(name, str) or not name.strip():
                invalid()
            if not isinstance(members, list):
                invalid()
            if any(not isinstance(item, str) or item not in claims for item in members):
                invalid()
            if parent is not None and (not isinstance(parent, str) or parent not in groups):
                invalid()
            groups[name] = GroupDefinition(name, tuple(sorted(set(members))), parent)

        history: dict[str, dict[int, ClientPolicy]] = {}
        current: dict[str, int] = {}
        conflicts: dict[str, tuple[str, ...]] = {}
        for client_id, entry in raw_policies.items():
            if not isinstance(client_id, str) or not isinstance(entry, dict):
                invalid()
            raw_versions = entry.get("versions")
            active = entry.get("current")
            if not isinstance(raw_versions, list) or not raw_versions:
                invalid()
            if not cls._is_int(active):
                invalid()
            versions: dict[int, ClientPolicy] = {}
            for raw_policy in raw_versions:
                if not isinstance(raw_policy, dict):
                    invalid()
                version = raw_policy.get("version")
                tier = raw_policy.get("max_pii_tier")
                if not cls._is_int(version) or version < 1 or version in versions:
                    invalid()
                if not cls._is_int(tier) or tier < 0:
                    invalid()
                sets = []
                for key in ("required", "optional", "forbidden"):
                    values = raw_policy.get(key)
                    if not isinstance(values, list):
                        invalid()
                    if any(not isinstance(item, str) or item not in claims
                           for item in values):
                        invalid()
                    sets.append(tuple(sorted(set(values))))
                versions[version] = ClientPolicy(client_id, version, sets[0],
                                                 sets[1], sets[2], tier)
            if active not in versions:
                invalid()
            raw_conflicts = entry.get("conflicts", [])
            if not isinstance(raw_conflicts, list):
                invalid()
            if any(not isinstance(item, str) for item in raw_conflicts):
                invalid()
            history[client_id] = versions
            current[client_id] = active
            conflicts[client_id] = tuple(sorted(set(raw_conflicts)))

        counters = []
        for key in ("released", "denied", "truncated"):
            value = raw_stats.get(key)
            if not cls._is_int(value) or value < 0:
                invalid()
            counters.append(value)

        return claims, groups, history, current, conflicts, limits, counters

    def restore(self, state: dict) -> None:
        with self._lock:
            (claims, groups, history, current,
             conflicts, limits, counters) = self._validate_snapshot(state)
            self._claims = claims
            self._groups = groups
            self._history = history
            self._current = current
            self._conflicts = conflicts
            (self.max_claims_per_client, self.max_group_depth,
             self.max_expanded_claims) = limits
            self._released, self._denied, self._truncated = counters

    def stats(self) -> dict:
        with self._lock:
            return {
                "claims": len(self._claims),
                "groups": len(self._groups),
                "policies": len(self._current),
                "released": self._released,
                "denied": self._denied,
                "truncated": self._truncated,
            }
