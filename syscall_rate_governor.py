"""Per-process-group syscall admission governed by token buckets."""

from __future__ import annotations

import copy
import math
import re
import threading
from dataclasses import dataclass


_CALL_NAME = re.compile(r"[a-z_][a-z0-9_.-]*\Z")
_TOKEN_EPSILON = 1e-12


class RateGovernorError(ValueError):
    """Refusal raised by the syscall rate governor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _GroupState:
    rate: float
    burst: float
    calls: tuple[str, ...] | None
    tokens: float
    last_refill: float
    last_admit: float | None
    admitted: int = 0
    denied: int = 0
    denied_limit: int = 0
    denied_unknown: int = 0


def _is_finite_number(value: object) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_group_name(group: object) -> bool:
    return isinstance(group, str) and bool(group.strip())


def _valid_call_name(call: object) -> bool:
    return isinstance(call, str) and _CALL_NAME.fullmatch(call) is not None


class SyscallRateGovernor:
    """Keep one refillable token bucket per process group."""

    def __init__(self, *, max_groups: int = 64, max_calls: int = 128,
                 clock=None) -> None:
        if (not isinstance(max_groups, int) or isinstance(max_groups, bool)
                or max_groups < 1):
            raise RateGovernorError(
                "RATE_CONFIG_INVALID", {"max_groups": max_groups}
            )
        if (not isinstance(max_calls, int) or isinstance(max_calls, bool)
                or max_calls < 1):
            raise RateGovernorError(
                "RATE_CONFIG_INVALID", {"max_calls": max_calls}
            )
        if clock is not None and not callable(clock):
            raise RateGovernorError(
                "RATE_CONFIG_INVALID", {"clock": type(clock).__name__}
            )
        self.max_groups = max_groups
        self.max_calls = max_calls
        self._clock = clock
        self._lock = threading.RLock()
        self._groups: dict[str, _GroupState] = {}

    # ------------------------------------------------------------------
    # Validation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _require_group_name(group: object) -> str:
        if not _valid_group_name(group):
            raise RateGovernorError("RATE_GROUP_INVALID", {"group": group})
        return group  # type: ignore[return-value]

    @staticmethod
    def _require_call_name(call: object) -> str:
        if not _valid_call_name(call):
            raise RateGovernorError("RATE_CALL_INVALID", {"call": call})
        return call  # type: ignore[return-value]

    @staticmethod
    def _require_time(now: object) -> float:
        if not _is_finite_number(now):
            raise RateGovernorError("RATE_CONFIG_INVALID", {"now": now})
        return float(now)

    def _resolve_now(self, now: object, state: _GroupState) -> float:
        if now is None:
            if self._clock is None:
                return state.last_refill
            try:
                now = self._clock()
            except Exception as exc:  # noqa: BLE001 - surfaced as invalid input
                raise RateGovernorError(
                    "RATE_CONFIG_INVALID", {"clock": str(exc)}
                ) from exc
        return self._require_time(now)

    @staticmethod
    def _clean_tokens(value: float) -> float:
        if -_TOKEN_EPSILON < value < 0.0:
            return 0.0
        nearest = round(value)
        if nearest >= 1 and abs(value - nearest) <= _TOKEN_EPSILON:
            return float(nearest)
        return float(value)

    def _refill_locked(self, state: _GroupState, now: float) -> tuple[float, float]:
        if now < state.last_refill:
            raise RateGovernorError(
                "RATE_CLOCK_BACKWARDS",
                {
                    "now": now,
                    "last_refill": state.last_refill,
                },
            )
        previous = state.tokens
        if now > state.last_refill:
            state.tokens = min(
                state.burst,
                state.tokens + state.rate * (now - state.last_refill),
            )
            state.last_refill = now
        state.tokens = self._clean_tokens(state.tokens)
        added = self._clean_tokens(state.tokens - previous)
        return state.tokens, added

    def _normalise_calls(self, calls: object) -> tuple[str, ...] | None:
        if calls is None:
            return None
        if isinstance(calls, (str, bytes, bytearray, dict)):
            raise RateGovernorError(
                "RATE_CONFIG_INVALID", {"calls": "must be a sequence of names"}
            )
        try:
            values = list(calls)  # type: ignore[arg-type]
        except TypeError as exc:
            raise RateGovernorError(
                "RATE_CONFIG_INVALID", {"calls": "not iterable"}
            ) from exc
        if not values:
            raise RateGovernorError("RATE_CONFIG_INVALID", {"calls": "empty"})
        if len(values) > self.max_calls:
            raise RateGovernorError(
                "RATE_CONFIG_INVALID",
                {"max_calls": self.max_calls, "count": len(values)},
            )
        for call in values:
            if not _valid_call_name(call):
                raise RateGovernorError(
                    "RATE_CONFIG_INVALID",
                    {"call": call, "reason": "invalid call name"},
                )
        return tuple(dict.fromkeys(values))

    # ------------------------------------------------------------------
    # Configuration and bucket operations
    # ------------------------------------------------------------------

    def configure(self, group: str, rate: float, burst: float,
                  calls=None, now: float = 0) -> dict:
        with self._lock:
            name = self._require_group_name(group)
            if not _is_finite_number(rate) or float(rate) <= 0.0:
                raise RateGovernorError(
                    "RATE_CONFIG_INVALID", {"rate": rate}
                )
            if not _is_finite_number(burst) or float(burst) < 1.0:
                raise RateGovernorError(
                    "RATE_CONFIG_INVALID", {"burst": burst}
                )
            allowlist = self._normalise_calls(calls)
            when = self._require_time(now)

            if name not in self._groups and len(self._groups) >= self.max_groups:
                raise RateGovernorError(
                    "RATE_GROUP_LIMIT", {"max_groups": self.max_groups}
                )

            state = _GroupState(
                rate=float(rate),
                burst=float(burst),
                calls=allowlist,
                tokens=float(burst),
                last_refill=when,
                last_admit=None,
            )
            self._groups[name] = state
            return {
                "group": name,
                "rate": state.rate,
                "burst": state.burst,
                "calls": None if state.calls is None else list(state.calls),
                "tokens": state.tokens,
            }

    def admit(self, group: str, call: str, now: float,
              pid: object = None) -> dict:
        # pid is deliberately not part of the bucket key: processes in the
        # same group must share exactly one bucket.
        del pid
        with self._lock:
            name = self._require_group_name(group)
            state = self._groups.get(name)
            if state is None:
                raise RateGovernorError(
                    "RATE_UNKNOWN_GROUP", {"group": name}
                )
            call_name = self._require_call_name(call)
            when = self._require_time(now)
            self._refill_locked(state, when)

            if state.calls is not None and call_name not in state.calls:
                state.denied += 1
                state.denied_unknown += 1
                return {
                    "allowed": False,
                    "group": name,
                    "call": call_name,
                    "reason": "unknown_call",
                    "remaining": state.tokens,
                }

            if state.tokens + _TOKEN_EPSILON >= 1.0:
                state.tokens = self._clean_tokens(state.tokens - 1.0)
                state.admitted += 1
                state.last_admit = when
                return {
                    "allowed": True,
                    "group": name,
                    "call": call_name,
                    "reason": "ok",
                    "remaining": state.tokens,
                }

            state.denied += 1
            state.denied_limit += 1
            return {
                "allowed": False,
                "group": name,
                "call": call_name,
                "reason": "limit",
                "remaining": state.tokens,
            }

    def refill(self, group: str, now: float) -> dict:
        with self._lock:
            name = self._require_group_name(group)
            state = self._groups.get(name)
            if state is None:
                raise RateGovernorError(
                    "RATE_UNKNOWN_GROUP", {"group": name}
                )
            when = self._require_time(now)
            tokens, added = self._refill_locked(state, when)
            return {
                "group": name,
                "tokens": tokens,
                "added": added,
                "last_refill": state.last_refill,
            }

    def tokens(self, group: str, now: float | None = None) -> float:
        with self._lock:
            name = self._require_group_name(group)
            state = self._groups.get(name)
            if state is None:
                raise RateGovernorError(
                    "RATE_UNKNOWN_GROUP", {"group": name}
                )
            when = self._resolve_now(now, state)
            tokens, _ = self._refill_locked(state, when)
            return tokens

    def stats(self, group: str) -> dict:
        with self._lock:
            name = self._require_group_name(group)
            state = self._groups.get(name)
            if state is None:
                raise RateGovernorError(
                    "RATE_UNKNOWN_GROUP", {"group": name}
                )
            return {
                "group": name,
                "admitted": state.admitted,
                "denied": state.denied,
                "denied_limit": state.denied_limit,
                "denied_unknown": state.denied_unknown,
                "tokens": state.tokens,
                "rate": state.rate,
                "burst": state.burst,
                "last_refill": state.last_refill,
                "last_admit": state.last_admit,
            }

    def reset(self, group: str, now: float = 0) -> dict:
        with self._lock:
            name = self._require_group_name(group)
            state = self._groups.get(name)
            if state is None:
                raise RateGovernorError(
                    "RATE_UNKNOWN_GROUP", {"group": name}
                )
            when = self._require_time(now)
            state.tokens = state.burst
            state.last_refill = when
            state.last_admit = None
            state.admitted = 0
            state.denied = 0
            state.denied_limit = 0
            state.denied_unknown = 0
            return {"group": name, "tokens": state.tokens}

    # ------------------------------------------------------------------
    # Fair rotation and group maintenance
    # ------------------------------------------------------------------

    def pick(self, groups, now: float) -> str:
        with self._lock:
            try:
                candidates = list(groups)
            except TypeError as exc:
                raise RateGovernorError(
                    "RATE_PICK_EMPTY", {"groups": groups}
                ) from exc
            if not candidates:
                raise RateGovernorError("RATE_PICK_EMPTY", {"groups": []})
            when = self._require_time(now)

            seen: set[str] = set()
            best_name: str | None = None
            best_key: tuple[int, float, str] | None = None
            for candidate in candidates:
                name = self._require_group_name(candidate)
                if name in seen:
                    continue
                seen.add(name)
                state = self._groups.get(name)
                if state is None:
                    raise RateGovernorError(
                        "RATE_UNKNOWN_GROUP", {"group": name}
                    )
                if when < state.last_refill:
                    raise RateGovernorError(
                        "RATE_CLOCK_BACKWARDS",
                        {"now": when, "last_refill": state.last_refill},
                    )
                if state.last_admit is None:
                    key = (0, 0.0, name)
                else:
                    key = (1, state.last_admit, name)
                if best_key is None or key < best_key:
                    best_key = key
                    best_name = name
            return best_name  # type: ignore[return-value]

    def groups(self) -> list:
        with self._lock:
            return list(self._groups)

    def remove(self, group: str) -> dict:
        with self._lock:
            name = self._require_group_name(group)
            if name not in self._groups:
                raise RateGovernorError(
                    "RATE_UNKNOWN_GROUP", {"group": name}
                )
            del self._groups[name]
            return {"group": name}

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self) -> dict:
        with self._lock:
            groups: dict[str, dict] = {}
            for name, state in self._groups.items():
                groups[name] = {
                    "rate": state.rate,
                    "burst": state.burst,
                    "calls": None if state.calls is None else list(state.calls),
                    "tokens": state.tokens,
                    "last_refill": state.last_refill,
                    "last_admit": state.last_admit,
                    "admitted": state.admitted,
                    "denied": state.denied,
                    "denied_limit": state.denied_limit,
                    "denied_unknown": state.denied_unknown,
                }
            return {"version": 1, "groups": copy.deepcopy(groups)}

    @staticmethod
    def _snapshot_error(reason: str, detail: object = None) -> RateGovernorError:
        payload = {"reason": reason}
        if detail is not None:
            payload["detail"] = detail
        return RateGovernorError("RATE_SNAPSHOT_INVALID", payload)

    def _parse_snapshot(self, state: object) -> dict[str, _GroupState]:
        if not isinstance(state, dict):
            raise self._snapshot_error("state must be a dict")
        version = state.get("version")
        if (not isinstance(version, int) or isinstance(version, bool)
                or version != 1):
            raise self._snapshot_error("unsupported version", version)
        groups = state.get("groups")
        if not isinstance(groups, dict):
            raise self._snapshot_error("groups must be a dict")
        if len(groups) > self.max_groups:
            raise self._snapshot_error(
                "too many groups", {"max_groups": self.max_groups}
            )

        parsed: dict[str, _GroupState] = {}
        required = ("rate", "burst", "tokens", "last_refill",
                    "admitted", "denied")
        for name, raw in groups.items():
            if not _valid_group_name(name):
                raise self._snapshot_error("invalid group name", name)
            if not isinstance(raw, dict):
                raise self._snapshot_error("group state must be a dict", name)
            if any(field not in raw for field in required):
                raise self._snapshot_error("missing group fields", name)

            rate = raw["rate"]
            burst = raw["burst"]
            if (not _is_finite_number(rate) or float(rate) <= 0.0
                    or not _is_finite_number(burst) or float(burst) < 1.0):
                raise self._snapshot_error("invalid rate or burst", name)

            calls = raw.get("calls")
            if calls is not None:
                if isinstance(calls, (str, bytes, bytearray, dict)):
                    raise self._snapshot_error("invalid calls", name)
                if not isinstance(calls, (list, tuple)):
                    raise self._snapshot_error("invalid calls", name)
                if not calls or len(calls) > self.max_calls:
                    raise self._snapshot_error("invalid calls", name)
                for call in calls:
                    if not _valid_call_name(call):
                        raise self._snapshot_error("invalid call name", call)
                calls = tuple(dict.fromkeys(calls))

            tokens = raw["tokens"]
            if (not _is_finite_number(tokens) or float(tokens) < 0.0
                    or float(tokens) > float(burst) + _TOKEN_EPSILON):
                raise self._snapshot_error("invalid tokens", name)
            tokens_value = min(float(tokens), float(burst))

            last_refill = raw["last_refill"]
            if not _is_finite_number(last_refill):
                raise self._snapshot_error("invalid last_refill", name)

            last_admit = raw.get("last_admit")
            if last_admit is not None and not _is_finite_number(last_admit):
                raise self._snapshot_error("invalid last_admit", name)

            counters: dict[str, int] = {}
            for field in ("admitted", "denied", "denied_limit",
                          "denied_unknown"):
                if field in ("denied_limit", "denied_unknown") and field not in raw:
                    value = 0
                else:
                    value = raw.get(field, 0)
                if (not isinstance(value, int) or isinstance(value, bool)
                        or value < 0):
                    raise self._snapshot_error("invalid counter", field)
                counters[field] = value

            parsed[name] = _GroupState(
                rate=float(rate),
                burst=float(burst),
                calls=calls,
                tokens=tokens_value,
                last_refill=float(last_refill),
                last_admit=(
                    None if last_admit is None else float(last_admit)
                ),
                admitted=counters["admitted"],
                denied=counters["denied"],
                denied_limit=counters["denied_limit"],
                denied_unknown=counters["denied_unknown"],
            )
        return parsed

    def restore(self, state: dict) -> None:
        with self._lock:
            parsed = self._parse_snapshot(state)
            self._groups = parsed
