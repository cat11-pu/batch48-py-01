"""Three color traffic policer with single rate and dual rate token buckets."""

from __future__ import annotations

import copy
import threading

MODES = ("single", "dual")
COLORS = ("green", "yellow", "red")

STATS_KEYS = (
    "policies_declared",
    "policies_upgraded",
    "flows_attached",
    "flows_detached",
    "packets_classified",
    "green",
    "yellow",
    "red",
    "rejected",
)

_UNSET = object()
_MAX_INT = (1 << 63) - 1


class PolicerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_positive_int(value: object) -> bool:
    return _is_int(value) and 0 < value <= _MAX_INT


def _is_policy_name(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64


def _is_flow_name(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


def _policy_problem(mode: str, committed_rate: int, peak_rate: int,
                    burst: int, peak_burst: int, version: int) -> str | None:
    if mode not in MODES:
        return "mode"
    for field, value in (
        ("committed_rate", committed_rate),
        ("burst", burst),
        ("version", version),
    ):
        if not _is_positive_int(value):
            return field
    for field, value in (("peak_rate", peak_rate), ("peak_burst", peak_burst)):
        if value is not None and not _is_positive_int(value):
            return field
    if mode == "dual" and (peak_rate is None or peak_burst is None):
        return "peak"
    return None


class TrafficPolicerGuard:
    """Meter each flow with committed and peak token buckets."""

    def __init__(self, *, max_flows: int = 16, clock=None) -> None:
        if not _is_int(max_flows) or max_flows < 0:
            raise PolicerError("TP_INVALID_POLICY", {"max_flows": max_flows})
        self.max_flows = max_flows
        self._clock = clock
        self._lock = threading.RLock()
        self._policies: dict[str, dict] = {}
        self._flows: dict[str, dict] = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    def _fail(self, code: str, detail: object = None):
        self._stats["rejected"] += 1
        raise PolicerError(code, detail)

    def _clock_ms(self):
        if self._clock is None:
            return 0
        if callable(self._clock):
            value = self._clock()
        else:
            value = self._clock.now_ms()
        if not _is_int(value):
            self._fail("TP_TIME_REGRESSION", {"now_ms": value})
        return value

    def _resolve_ms(self, now_ms):
        if now_ms is _UNSET or now_ms is None:
            return self._clock_ms()
        if not _is_int(now_ms):
            self._fail("TP_TIME_REGRESSION", {"now_ms": now_ms})
        return now_ms

    def _require_policy_values(self, mode, committed_rate, peak_rate,
                               burst, peak_burst, version) -> None:
        problem = _policy_problem(
            mode, committed_rate, peak_rate, burst, peak_burst, version
        )
        if problem is not None:
            self._fail("TP_INVALID_POLICY", {"field": problem})

    @staticmethod
    def _policy_copy(policy: dict) -> dict:
        return {
            "name": policy["name"],
            "mode": policy["mode"],
            "committed_rate": policy["committed_rate"],
            "peak_rate": policy["peak_rate"],
            "burst": policy["burst"],
            "peak_burst": policy["peak_burst"],
            "version": policy["version"],
        }

    @staticmethod
    def _same_policy(left: dict, right: dict) -> bool:
        keys = (
            "name",
            "mode",
            "committed_rate",
            "peak_rate",
            "burst",
            "peak_burst",
            "version",
        )
        return all(left.get(key) == right.get(key) for key in keys)

    @staticmethod
    def _bucket(tokens: int, capacity: int) -> dict:
        return {
            "tokens": tokens,
            "capacity": capacity,
            "remainder": 0,
        }

    def _new_flow(self, policy: dict, now_ms: int) -> dict:
        flow = {
            "policy": policy["name"],
            "last_ms": now_ms,
            "committed": self._bucket(policy["burst"], policy["burst"]),
            "peak": None,
        }
        if policy["mode"] == "dual":
            flow["peak"] = self._bucket(
                policy["peak_burst"], policy["peak_burst"]
            )
        return flow

    @staticmethod
    def _reset_bucket_capacity(bucket: dict, capacity: int) -> None:
        bucket["capacity"] = capacity
        if bucket["tokens"] > capacity:
            bucket["tokens"] = capacity
        if bucket["tokens"] >= capacity:
            bucket["remainder"] = 0

    def _reconfigure_flow(self, flow: dict, old_policy: dict,
                          new_policy: dict) -> None:
        committed = flow["committed"]
        old_peak = flow["peak"]

        if old_policy["mode"] == "single" and new_policy["mode"] == "dual":
            self._reset_bucket_capacity(committed, new_policy["burst"])
            peak_tokens = min(committed["tokens"], new_policy["peak_burst"])
            flow["peak"] = self._bucket(
                peak_tokens, new_policy["peak_burst"]
            )
            return

        if old_policy["mode"] == "dual" and new_policy["mode"] == "single":
            committed_tokens = committed["tokens"]
            if old_peak is not None:
                committed_tokens = min(committed_tokens, old_peak["tokens"])
            committed["tokens"] = committed_tokens
            self._reset_bucket_capacity(committed, new_policy["burst"])
            flow["peak"] = None
            return

        self._reset_bucket_capacity(committed, new_policy["burst"])
        if new_policy["mode"] == "dual":
            if old_peak is None:
                flow["peak"] = self._bucket(
                    min(committed["tokens"], new_policy["peak_burst"]),
                    new_policy["peak_burst"],
                )
            else:
                self._reset_bucket_capacity(
                    old_peak, new_policy["peak_burst"]
                )
                flow["peak"] = old_peak
        else:
            flow["peak"] = None

    def _reconfigure_attached(self, name: str, old_policy: dict,
                              new_policy: dict) -> None:
        for flow in self._flows.values():
            if flow["policy"] == name:
                self._reconfigure_flow(flow, old_policy, new_policy)

    @staticmethod
    def _refill_bucket(bucket: dict, rate: int, elapsed_ms: int) -> None:
        if elapsed_ms <= 0:
            return
        capacity = bucket["capacity"]
        if bucket["tokens"] >= capacity:
            bucket["tokens"] = capacity
            bucket["remainder"] = 0
            return
        numerator = elapsed_ms * rate + bucket["remainder"]
        added, remainder = divmod(numerator, 1000)
        if bucket["tokens"] + added >= capacity:
            bucket["tokens"] = capacity
            bucket["remainder"] = 0
        else:
            bucket["tokens"] += added
            bucket["remainder"] = remainder

    def _refill_flow(self, flow: dict, policy: dict, now_ms: int) -> None:
        elapsed = now_ms - flow["last_ms"]
        if elapsed < 0:
            self._fail(
                "TP_TIME_REGRESSION",
                {"now_ms": now_ms, "last_ms": flow["last_ms"]},
            )
        self._refill_bucket(
            flow["committed"], policy["committed_rate"], elapsed
        )
        if policy["mode"] == "dual":
            if flow["peak"] is None:
                flow["peak"] = self._bucket(
                    min(flow["committed"]["tokens"],
                        policy["peak_burst"]),
                    policy["peak_burst"],
                )
            self._refill_bucket(flow["peak"], policy["peak_rate"], elapsed)
        flow["last_ms"] = now_ms

    def _tokens_view(self, flow: dict, mode: str) -> dict:
        tokens = {"committed": flow["committed"]["tokens"]}
        if mode == "dual":
            tokens["peak"] = flow["peak"]["tokens"]
        return tokens

    def _flow_view(self, name: str, flow: dict) -> dict:
        policy = self._policies[flow["policy"]]
        return {
            "flow": name,
            "policy": flow["policy"],
            "mode": policy["mode"],
            "version": policy["version"],
            "tokens": self._tokens_view(flow, policy["mode"]),
            "last_ms": flow["last_ms"],
        }

    def _result(self, name: str, size: int, now_ms: int, color: str,
                action: str, allowed: bool, flow: dict,
                policy: dict) -> dict:
        return {
            "flow": name,
            "policy": flow["policy"],
            "mode": policy["mode"],
            "version": policy["version"],
            "size": size,
            "now_ms": now_ms,
            "color": color,
            "action": action,
            "allowed": allowed,
            "tokens": self._tokens_view(flow, policy["mode"]),
        }

    def declare_policy(self, name: str, *, mode: str = "single",
                       committed_rate: int = 1000, peak_rate: int = None,
                       burst: int = 1500, peak_burst: int = None,
                       version: int = 1) -> dict:
        with self._lock:
            if not _is_policy_name(name):
                self._fail("TP_INVALID_POLICY", {"field": "name"})
            self._require_policy_values(
                mode, committed_rate, peak_rate, burst, peak_burst, version
            )
            policy = {
                "name": name,
                "mode": mode,
                "committed_rate": committed_rate,
                "peak_rate": peak_rate,
                "burst": burst,
                "peak_burst": peak_burst,
                "version": version,
            }
            existing = self._policies.get(name)
            if existing is not None:
                if self._same_policy(existing, policy):
                    return self._policy_copy(existing)
                self._fail("TP_DUPLICATE_POLICY", {"name": name})
            self._policies[name] = policy
            self._stats["policies_declared"] += 1
            return self._policy_copy(policy)

    def policy_info(self, name: str) -> dict:
        with self._lock:
            if not _is_policy_name(name):
                self._fail("TP_INVALID_POLICY", {"field": "name"})
            policy = self._policies.get(name)
            if policy is None:
                self._fail("TP_UNKNOWN_POLICY", {"name": name})
            return self._policy_copy(policy)

    def upgrade_policy(self, name: str, *, mode: str = None,
                       committed_rate: int = None, peak_rate: int = None,
                       burst: int = None, peak_burst: int = None) -> dict:
        with self._lock:
            if not _is_policy_name(name):
                self._fail("TP_INVALID_POLICY", {"field": "name"})
            current = self._policies.get(name)
            if current is None:
                self._fail("TP_UNKNOWN_POLICY", {"name": name})
            if mode is not None and mode not in MODES:
                self._fail("TP_INVALID_MODE", {"mode": mode})

            updated = {
                "name": name,
                "mode": current["mode"] if mode is None else mode,
                "committed_rate": (
                    current["committed_rate"]
                    if committed_rate is None else committed_rate
                ),
                "peak_rate": (
                    current["peak_rate"]
                    if peak_rate is None else peak_rate
                ),
                "burst": current["burst"] if burst is None else burst,
                "peak_burst": (
                    current["peak_burst"]
                    if peak_burst is None else peak_burst
                ),
                "version": current["version"] + 1,
            }
            self._require_policy_values(
                updated["mode"], updated["committed_rate"],
                updated["peak_rate"], updated["burst"],
                updated["peak_burst"], updated["version"],
            )
            comparable = dict(updated)
            comparable["version"] = current["version"]
            if self._same_policy(current, comparable):
                self._fail("TP_INVALID_POLICY", {"field": "no_change"})

            old_policy = self._policy_copy(current)
            self._reconfigure_attached(name, old_policy, updated)
            self._policies[name] = updated
            self._stats["policies_upgraded"] += 1
            return self._policy_copy(updated)

    def set_mode(self, name: str, mode: str) -> dict:
        with self._lock:
            if not _is_policy_name(name):
                self._fail("TP_INVALID_POLICY", {"field": "name"})
            current = self._policies.get(name)
            if current is None:
                self._fail("TP_UNKNOWN_POLICY", {"name": name})
            if mode not in MODES:
                self._fail("TP_INVALID_MODE", {"mode": mode})
            if current["mode"] == mode:
                return self._policy_copy(current)

            updated = self._policy_copy(current)
            updated["mode"] = mode
            updated["version"] = current["version"] + 1
            self._require_policy_values(
                updated["mode"], updated["committed_rate"],
                updated["peak_rate"], updated["burst"],
                updated["peak_burst"], updated["version"],
            )
            old_policy = self._policy_copy(current)
            self._reconfigure_attached(name, old_policy, updated)
            self._policies[name] = updated
            self._stats["policies_upgraded"] += 1
            return self._policy_copy(updated)

    def attach(self, flow: str, policy: str) -> dict:
        with self._lock:
            if not _is_flow_name(flow):
                self._fail("TP_INVALID_FLOW", {"flow": flow})
            if not isinstance(policy, str):
                self._fail("TP_UNKNOWN_POLICY", {"policy": policy})
            selected = self._policies.get(policy)
            if selected is None:
                self._fail("TP_UNKNOWN_POLICY", {"policy": policy})
            existing = self._flows.get(flow)
            if existing is not None:
                if existing["policy"] == policy:
                    return self._flow_view(flow, existing)
                self._fail(
                    "TP_FLOW_EXISTS",
                    {"flow": flow, "policy": existing["policy"]},
                )
            if len(self._flows) >= self.max_flows:
                self._fail("TP_FLOW_LIMIT", {"max_flows": self.max_flows})

            attached = self._new_flow(selected, self._clock_ms())
            self._flows[flow] = attached
            self._stats["flows_attached"] += 1
            return self._flow_view(flow, attached)

    def detach(self, flow: str) -> dict:
        with self._lock:
            if not _is_flow_name(flow):
                self._fail("TP_INVALID_FLOW", {"flow": flow})
            existing = self._flows.get(flow)
            if existing is None:
                self._fail("TP_UNKNOWN_FLOW", {"flow": flow})
            del self._flows[flow]
            self._stats["flows_detached"] += 1
            return {
                "flow": flow,
                "policy": existing["policy"],
                "detached": True,
            }

    def flow_state(self, flow: str, *, now_ms=_UNSET) -> dict:
        with self._lock:
            if not _is_flow_name(flow):
                self._fail("TP_INVALID_FLOW", {"flow": flow})
            existing = self._flows.get(flow)
            if existing is None:
                self._fail("TP_UNKNOWN_FLOW", {"flow": flow})
            now_ms = self._resolve_ms(now_ms)
            policy = self._policies[existing["policy"]]
            self._refill_flow(existing, policy, now_ms)
            return self._flow_view(flow, existing)

    def classify(self, flow: str, size: int, *, now_ms=_UNSET,
                 color: str = None) -> dict:
        with self._lock:
            if not _is_flow_name(flow):
                self._fail("TP_INVALID_FLOW", {"flow": flow})
            existing = self._flows.get(flow)
            if existing is None:
                self._fail("TP_UNKNOWN_FLOW", {"flow": flow})
            if not _is_positive_int(size):
                self._fail("TP_INVALID_BYTES", {"size": size})
            if color is not None and color not in COLORS:
                self._fail("TP_INVALID_COLOR", {"color": color})

            now_ms = self._resolve_ms(now_ms)
            policy = self._policies[existing["policy"]]
            self._refill_flow(existing, policy, now_ms)
            committed = existing["committed"]

            if color == "red":
                result_color, action, allowed = "red", "drop", False
            elif color == "yellow":
                result_color, action, allowed = "yellow", "mark", True
            elif policy["mode"] == "single":
                if size > committed["capacity"]:
                    result_color, action, allowed = "red", "drop", False
                elif committed["tokens"] >= size:
                    committed["tokens"] -= size
                    result_color, action, allowed = (
                        "green", "forward", True
                    )
                else:
                    result_color, action, allowed = "yellow", "mark", True
            else:
                peak = existing["peak"]
                if size > peak["capacity"] or peak["tokens"] < size:
                    result_color, action, allowed = "red", "drop", False
                elif committed["tokens"] >= size:
                    committed["tokens"] -= size
                    peak["tokens"] -= size
                    result_color, action, allowed = (
                        "green", "forward", True
                    )
                else:
                    peak["tokens"] -= size
                    result_color, action, allowed = (
                        "yellow", "mark", True
                    )

            self._stats["packets_classified"] += 1
            self._stats[result_color] += 1
            return self._result(
                flow, size, now_ms, result_color, action, allowed,
                existing, policy,
            )

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            policies = {
                name: self._policy_copy(self._policies[name])
                for name in sorted(self._policies)
            }
            flows = []
            for name in sorted(self._flows):
                flow = self._flows[name]
                policy = self._policies[flow["policy"]]
                peak = None
                if flow["peak"] is not None:
                    peak = copy.deepcopy(flow["peak"])
                flows.append({
                    "flow": name,
                    "policy": flow["policy"],
                    "mode": policy["mode"],
                    "version": policy["version"],
                    "last_ms": flow["last_ms"],
                    "committed": copy.deepcopy(flow["committed"]),
                    "peak": peak,
                })
            return copy.deepcopy({
                "version": 1,
                "policies": policies,
                "flows": flows,
                "stats": dict(self._stats),
            })

    @staticmethod
    def _snapshot_bucket(raw, expected_capacity):
        if not isinstance(raw, dict):
            raise ValueError("bucket")
        tokens = raw.get("tokens")
        capacity = raw.get("capacity")
        remainder = raw.get("remainder")
        if not _is_int(tokens) or not _is_int(capacity) or not _is_int(
                remainder):
            raise ValueError("bucket")
        if capacity != expected_capacity:
            raise ValueError("capacity")
        if tokens < 0 or tokens > capacity:
            raise ValueError("tokens")
        if remainder < 0 or remainder >= 1000:
            raise ValueError("remainder")
        return {
            "tokens": tokens,
            "capacity": capacity,
            "remainder": remainder,
        }

    def _parse_snapshot(self, state):
        if not isinstance(state, dict):
            raise ValueError("state")
        if "version" in state and state.get("version") != 1:
            raise ValueError("version")
        raw_policies = state.get("policies")
        raw_flows = state.get("flows")
        raw_stats = state.get("stats")
        if not isinstance(raw_policies, dict):
            raise ValueError("policies")
        if not isinstance(raw_flows, list):
            raise ValueError("flows")
        if not isinstance(raw_stats, dict):
            raise ValueError("stats")

        policies = {}
        for name, raw in raw_policies.items():
            if not _is_policy_name(name) or not isinstance(raw, dict):
                raise ValueError("policy")
            mode = raw.get("mode")
            committed_rate = raw.get("committed_rate")
            peak_rate = raw.get("peak_rate")
            burst = raw.get("burst")
            peak_burst = raw.get("peak_burst")
            version = raw.get("version")
            if _policy_problem(
                    mode, committed_rate, peak_rate, burst, peak_burst,
                    version) is not None:
                raise ValueError("policy")
            policies[name] = {
                "name": name,
                "mode": mode,
                "committed_rate": committed_rate,
                "peak_rate": peak_rate,
                "burst": burst,
                "peak_burst": peak_burst,
                "version": version,
            }

        flows = {}
        for raw in raw_flows:
            if not isinstance(raw, dict):
                raise ValueError("flow")
            name = raw.get("flow")
            policy_name = raw.get("policy")
            if not _is_flow_name(name) or name in flows:
                raise ValueError("flow")
            policy = policies.get(policy_name)
            if policy is None:
                raise ValueError("flow policy")
            if raw.get("mode") != policy["mode"]:
                raise ValueError("flow mode")
            if raw.get("version") != policy["version"]:
                raise ValueError("flow version")
            last_ms = raw.get("last_ms")
            if not _is_int(last_ms) or last_ms < 0:
                raise ValueError("last_ms")
            committed = self._snapshot_bucket(
                raw.get("committed"), policy["burst"]
            )
            peak = None
            if policy["mode"] == "dual":
                peak = self._snapshot_bucket(
                    raw.get("peak"), policy["peak_burst"]
                )
            elif raw.get("peak") is not None:
                raise ValueError("flow peak")
            flows[name] = {
                "policy": policy_name,
                "last_ms": last_ms,
                "committed": committed,
                "peak": peak,
            }
        if len(flows) > self.max_flows:
            raise ValueError("flow limit")

        stats = {}
        for key in STATS_KEYS:
            value = raw_stats.get(key)
            if not _is_int(value) or value < 0:
                raise ValueError("stats")
            stats[key] = value
        return policies, flows, stats

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                policies, flows, stats = self._parse_snapshot(state)
            except (TypeError, ValueError, KeyError):
                self._fail("TP_SNAPSHOT_INVALID", {"reason": "structure"})
            self._policies = policies
            self._flows = flows
            self._stats = stats
