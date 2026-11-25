"""Container restart supervisor with exponential backoff and restart budget."""

from __future__ import annotations

import copy
import math
import numbers
import threading
import time
from dataclasses import dataclass, field


MAX_CONTAINERS = 10_000
MAX_RESTART_RECORDS = 10_000
MAX_EXIT_HISTORY = 1_024

_POLICY_FIELDS = (
    "base_delay",
    "factor",
    "max_delay",
    "budget",
    "window",
    "stable_period",
    "oom_multiplier",
)
_STATES = frozenset({"idle", "backoff", "stable", "terminated"})


class RestartSupervisorError(ValueError):
    """Refusal raised by the restart supervisor."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class _Policy:
    base_delay: float
    factor: float
    max_delay: float
    budget: int
    window: float
    stable_period: float
    oom_multiplier: float

    def as_dict(self) -> dict:
        return {name: getattr(self, name) for name in _POLICY_FIELDS}


@dataclass
class _Container:
    policy: _Policy
    state: str = "idle"
    round: int = 0
    delay: float = 0.0
    next_at: float | None = None
    ready_at: float | None = None
    restart_times: list[float] = field(default_factory=list)
    gate: bool = False
    armed: bool = False
    terminated: bool = False
    last_exit_at: float | None = None
    last_exit_code: int | None = None
    last_time: float | None = None
    seen_exits: dict[tuple[int, float], dict] = field(default_factory=dict)


def _is_number(value: object) -> bool:
    return isinstance(value, numbers.Real) and not isinstance(value, bool)


def _finite_number(value: object, code: str, field_name: str) -> float:
    if not _is_number(value):
        raise RestartSupervisorError(code, f"{field_name} must be a number")
    try:
        number = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise RestartSupervisorError(
            code, f"{field_name} must be finite"
        ) from exc
    if not math.isfinite(number):
        raise RestartSupervisorError(code, f"{field_name} must be finite")
    return number


def _container_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RestartSupervisorError(
            "RESTART_CONTAINER_INVALID", "container_id must be a non-empty string"
        )
    return value


def _policy_values(
    values: dict, code: str = "RESTART_CONTAINER_INVALID"
) -> _Policy:
    missing = [name for name in _POLICY_FIELDS if name not in values]
    if missing:
        raise RestartSupervisorError(code, f"missing policy fields: {missing}")

    base_delay = _finite_number(values["base_delay"], code, "base_delay")
    factor = _finite_number(values["factor"], code, "factor")
    max_delay = _finite_number(values["max_delay"], code, "max_delay")
    window = _finite_number(values["window"], code, "window")
    stable_period = _finite_number(
        values["stable_period"], code, "stable_period"
    )
    oom_multiplier = _finite_number(
        values["oom_multiplier"], code, "oom_multiplier"
    )

    budget = values["budget"]
    if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
        raise RestartSupervisorError(
            code, "budget must be a non-negative integer"
        )
    if base_delay < 0:
        raise RestartSupervisorError(code, "base_delay must not be negative")
    if factor < 1:
        raise RestartSupervisorError(code, "factor must be at least 1")
    if max_delay < 0:
        raise RestartSupervisorError(code, "max_delay must not be negative")
    if window <= 0:
        raise RestartSupervisorError(code, "window must be positive")
    if stable_period < 0:
        raise RestartSupervisorError(
            code, "stable_period must not be negative"
        )
    if oom_multiplier < 0:
        raise RestartSupervisorError(
            code, "oom_multiplier must not be negative"
        )

    return _Policy(
        base_delay=base_delay,
        factor=factor,
        max_delay=max_delay,
        budget=budget,
        window=window,
        stable_period=stable_period,
        oom_multiplier=oom_multiplier,
    )


class RestartSupervisor:
    """Decide when a crashed container may be started again."""

    def __init__(self, *, base_delay: float = 1.0, factor: float = 2.0,
                 max_delay: float = 60.0, budget: int = 5,
                 window: float = 600.0, stable_period: float = 120.0,
                 oom_multiplier: float = 2.0, clock=None) -> None:
        self._defaults = _policy_values({
            "base_delay": base_delay,
            "factor": factor,
            "max_delay": max_delay,
            "budget": budget,
            "window": window,
            "stable_period": stable_period,
            "oom_multiplier": oom_multiplier,
        })
        self._clock = clock
        self._lock = threading.RLock()
        self._containers: dict[str, _Container] = {}
        self._stats = {
            "exits": 0,
            "restarts": 0,
            "stable_marks": 0,
            "resets": 0,
            "budget_exhausted": 0,
        }

    def _require(self, container_id: str) -> _Container:
        container = self._containers.get(container_id)
        if container is None:
            raise RestartSupervisorError(
                "RESTART_UNKNOWN_CONTAINER", container_id
            )
        return container

    def _register(self, container_id: str, policy: _Policy) -> _Container:
        if len(self._containers) >= MAX_CONTAINERS:
            raise RestartSupervisorError(
                "RESTART_CONTAINER_INVALID", "container limit reached"
            )
        container = _Container(policy=policy)
        self._containers[container_id] = container
        return container

    @staticmethod
    def _delay_for(policy: _Policy, round_number: int, code: int) -> float:
        try:
            raw = policy.base_delay * math.pow(policy.factor, round_number - 1)
        except OverflowError:
            raw = policy.max_delay
        if not math.isfinite(raw):
            raw = policy.max_delay
        if code == 137:
            try:
                raw *= policy.oom_multiplier
            except OverflowError:
                raw = policy.max_delay
            if not math.isfinite(raw):
                raw = policy.max_delay
        return max(0.0, min(float(policy.max_delay), float(raw)))

    def _now(self, now: object) -> float:
        if now is None:
            if self._clock is None:
                value = time.time()
            else:
                value = self._clock()
            return _finite_number(
                value, "RESTART_CLOCK_BACKWARDS", "clock value"
            )
        return _finite_number(now, "RESTART_CLOCK_BACKWARDS", "now")

    @staticmethod
    def _active_restarts(container: _Container, now: float) -> list[float]:
        window = container.policy.window
        return [
            timestamp
            for timestamp in container.restart_times
            if now - timestamp < window
        ]

    @staticmethod
    def _prune_restarts(container: _Container, now: float) -> None:
        container.restart_times = RestartSupervisor._active_restarts(
            container, now
        )

    def _export(self, container_id: str, container: _Container) -> dict:
        policy = container.policy.as_dict()
        restart_times = list(container.restart_times)
        return {
            "container_id": container_id,
            **policy,
            "policy": policy,
            "state": container.state,
            "round": container.round,
            "delay": container.delay,
            "next_at": container.next_at,
            "ready_at": container.ready_at,
            "restart_times": restart_times,
            "restarts": restart_times,
            "gate": container.gate,
            "in_progress": container.gate,
            "armed": container.armed,
            "terminated": container.terminated,
            "last_exit_at": container.last_exit_at,
            "last_exit_code": container.last_exit_code,
            "last_time": container.last_time,
        }

    def configure(self, container_id: str, base_delay: float | None = None,
                  factor: float | None = None, max_delay: float | None = None,
                  budget: int | None = None, window: float | None = None,
                  stable_period: float | None = None,
                  oom_multiplier: float | None = None) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            overrides = {
                "base_delay": base_delay,
                "factor": factor,
                "max_delay": max_delay,
                "budget": budget,
                "window": window,
                "stable_period": stable_period,
                "oom_multiplier": oom_multiplier,
            }
            existing = self._containers.get(container_id)
            base = existing.policy.as_dict() if existing else self._defaults.as_dict()
            values = {
                name: base[name] if value is None else value
                for name, value in overrides.items()
            }
            policy = _policy_values(values)
            if existing is None:
                self._register(container_id, policy)
            else:
                existing.policy = policy
            result = {"container_id": container_id}
            result.update(policy.as_dict())
            return result

    def report_exit(self, container_id: str, code: int, at: float) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            if (
                not isinstance(code, int)
                or isinstance(code, bool)
                or code < 0
                or code > 255
            ):
                raise RestartSupervisorError(
                    "RESTART_EXIT_INVALID", "exit code must be in 0..255"
                )
            at = _finite_number(at, "RESTART_EXIT_INVALID", "at")

            container = self._containers.get(container_id)
            if container is None:
                container = self._register(container_id, self._defaults)

            event_key = (code, at)
            duplicate = container.seen_exits.get(event_key)
            if duplicate is not None:
                result = copy.deepcopy(duplicate)
                result["duplicate"] = True
                return result

            if container.terminated or container.state == "terminated":
                raise RestartSupervisorError(
                    "RESTART_TERMINATED", container_id
                )
            if container.last_time is not None and at < container.last_time:
                raise RestartSupervisorError(
                    "RESTART_CLOCK_BACKWARDS",
                    f"exit at {at} predates {container.last_time}",
                )

            self._stats["exits"] += 1
            container.last_exit_at = at
            container.last_exit_code = code
            container.last_time = at
            container.gate = False
            container.armed = True

            if code == 78:
                container.state = "terminated"
                container.terminated = True
                container.round = 0
                container.delay = 0.0
                container.next_at = None
                container.ready_at = None
                container.armed = False
            else:
                previous_round = container.round
                previous_delay = container.delay
                container.round += 1
                container.delay = self._delay_for(
                    container.policy, container.round, code
                )
                container.next_at = at + container.delay
                if container.round == 1 or code == 137:
                    container.ready_at = container.next_at
                else:
                    container.ready_at = at + previous_delay
                    if previous_round < 1:
                        container.ready_at = container.next_at
                container.state = "backoff"

            result = {
                "container_id": container_id,
                "code": code,
                "at": at,
                "round": container.round,
                "delay": container.delay,
                "next_at": container.next_at,
                "state": container.state,
                "duplicate": False,
            }
            container.seen_exits[event_key] = copy.deepcopy(result)
            if len(container.seen_exits) > MAX_EXIT_HISTORY:
                oldest = next(iter(container.seen_exits))
                del container.seen_exits[oldest]
            return result

    def next_delay(self, container_id: str, now: float) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            container = self._require(container_id)
            now = self._now(now)

            delay = 0.0
            if (
                container.state == "backoff"
                and container.next_at is not None
                and not (
                    container.last_time is not None
                    and now < container.last_time
                )
            ):
                delay = max(0.0, container.next_at - now)
            return {
                "container_id": container_id,
                "delay": float(delay),
                "round": container.round,
                "next_at": container.next_at,
                "state": container.state,
            }

    def allow_restart(self, container_id: str, now: float) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            container = self._require(container_id)
            now = self._now(now)

            if container.terminated or container.state == "terminated":
                raise RestartSupervisorError(
                    "RESTART_TERMINATED", container_id
                )
            if container.gate:
                raise RestartSupervisorError(
                    "RESTART_IN_PROGRESS", container_id
                )
            if not container.armed:
                raise RestartSupervisorError(
                    "RESTART_NOT_ARMED", container_id
                )
            if container.last_time is not None and now < container.last_time:
                raise RestartSupervisorError(
                    "RESTART_CLOCK_BACKWARDS",
                    f"now {now} predates {container.last_time}",
                )

            active = self._active_restarts(container, now)
            if len(active) >= container.policy.budget:
                self._stats["budget_exhausted"] += 1
                raise RestartSupervisorError(
                    "RESTART_BUDGET_EXHAUSTED", container_id
                )
            if len(active) >= MAX_RESTART_RECORDS:
                self._stats["budget_exhausted"] += 1
                raise RestartSupervisorError(
                    "RESTART_BUDGET_EXHAUSTED",
                    "restart record limit reached",
                )
            if (
                container.ready_at is not None
                and now < container.ready_at
            ):
                raise RestartSupervisorError(
                    "RESTART_TOO_EARLY",
                    {
                        "next_at": container.next_at,
                        "ready_at": container.ready_at,
                        "now": now,
                    },
                )

            container.restart_times = active
            container.restart_times.append(now)
            container.gate = True
            container.armed = False
            container.last_time = now
            self._stats["restarts"] += 1

            used = len(container.restart_times)
            return {
                "container_id": container_id,
                "allowed": True,
                "restarts_used": used,
                "remaining": max(0, container.policy.budget - used),
                "window_start": container.restart_times[0],
                "at": now,
            }

    def mark_stable(self, container_id: str, uptime: float) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            container = self._require(container_id)
            uptime = _finite_number(uptime, "RESTART_UNSTABLE", "uptime")

            if container.terminated or container.state == "terminated":
                raise RestartSupervisorError(
                    "RESTART_TERMINATED", container_id
                )
            if uptime < container.policy.stable_period:
                raise RestartSupervisorError(
                    "RESTART_UNSTABLE",
                    {
                        "uptime": uptime,
                        "stable_period": container.policy.stable_period,
                    },
                )

            container.round = 0
            container.delay = 0.0
            container.next_at = None
            container.ready_at = None
            container.state = "stable"
            container.gate = False
            container.armed = False
            self._stats["stable_marks"] += 1
            return {
                "container_id": container_id,
                "round": container.round,
                "state": container.state,
                "uptime": uptime,
            }

    def budget(self, container_id: str, now: float | None = None) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            container = self._require(container_id)
            now = self._now(now)

            active = self._active_restarts(container, now)
            used = len(active)
            return {
                "container_id": container_id,
                "budget": container.policy.budget,
                "used": used,
                "remaining": max(0, container.policy.budget - used),
                "window": container.policy.window,
                "window_start": active[0] if active else None,
            }

    def reset(self, container_id: str) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            container = self._require(container_id)

            container.state = "idle"
            container.round = 0
            container.delay = 0.0
            container.next_at = None
            container.ready_at = None
            container.restart_times = []
            container.gate = False
            container.armed = False
            container.terminated = False
            container.last_exit_at = None
            container.last_exit_code = None
            container.last_time = None
            container.seen_exits = {}
            self._stats["resets"] += 1
            return {
                "state": container.state,
                "round": container.round,
                "remaining": container.policy.budget,
            }

    def get(self, container_id: str) -> dict:
        with self._lock:
            container_id = _container_id(container_id)
            container = self._require(container_id)
            return copy.deepcopy(self._export(container_id, container))

    def snapshot(self) -> dict:
        with self._lock:
            containers = {
                container_id: copy.deepcopy(
                    self._export(container_id, container)
                )
                for container_id, container in self._containers.items()
            }
            return {"version": 1, "containers": containers}

    def _snapshot_invalid(self, detail: object) -> RestartSupervisorError:
        return RestartSupervisorError("RESTART_SNAPSHOT_INVALID", detail)

    def _parse_snapshot(self, state: object) -> dict[str, _Container]:
        if not isinstance(state, dict):
            raise self._snapshot_invalid("snapshot must be an object")
        version = state.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != 1
        ):
            raise self._snapshot_invalid("unsupported snapshot version")
        containers = state.get("containers")
        if not isinstance(containers, dict):
            raise self._snapshot_invalid("containers must be an object")
        if len(containers) > MAX_CONTAINERS:
            raise self._snapshot_invalid("too many containers")

        parsed: dict[str, _Container] = {}
        for raw_id, raw_record in containers.items():
            try:
                container_id = _container_id(raw_id)
            except RestartSupervisorError as exc:
                raise self._snapshot_invalid(str(exc.detail)) from exc
            try:
                parsed[container_id] = self._parse_container_record(
                    container_id, raw_record
                )
            except RestartSupervisorError as exc:
                if exc.code == "RESTART_SNAPSHOT_INVALID":
                    raise
                raise self._snapshot_invalid(exc.detail) from exc
            except Exception as exc:
                raise self._snapshot_invalid(str(exc)) from exc
        return parsed

    def _parse_container_record(
        self, container_id: str, record: object
    ) -> _Container:
        if not isinstance(record, dict):
            raise self._snapshot_invalid(
                f"record for {container_id} must be an object"
            )

        policy_values = {
            name: record[name] for name in _POLICY_FIELDS if name in record
        }
        nested_policy = record.get("policy")
        if nested_policy is not None:
            if not isinstance(nested_policy, dict):
                raise self._snapshot_invalid(
                    f"policy for {container_id} must be an object"
                )
            for name in _POLICY_FIELDS:
                if name in nested_policy:
                    policy_values[name] = nested_policy[name]
        try:
            policy = _policy_values(policy_values, "RESTART_SNAPSHOT_INVALID")
        except RestartSupervisorError as exc:
            raise self._snapshot_invalid(exc.detail) from exc

        state = record.get("state")
        terminated_raw = record.get("terminated")
        if state is None:
            if terminated_raw is True:
                state = "terminated"
            elif record.get("next_at") is not None:
                state = "backoff"
            elif record.get("gate", record.get("in_progress", False)):
                state = "backoff"
            else:
                state = "idle"
        if not isinstance(state, str) or state not in _STATES:
            raise self._snapshot_invalid(
                f"invalid state for {container_id}"
            )
        terminated = state == "terminated"
        if terminated_raw is not None:
            if not isinstance(terminated_raw, bool):
                raise self._snapshot_invalid(
                    f"terminated for {container_id} must be boolean"
                )
            if terminated_raw != terminated:
                raise self._snapshot_invalid(
                    f"terminated flag disagrees with state for {container_id}"
                )

        round_number = record.get("round")
        if (
            not isinstance(round_number, int)
            or isinstance(round_number, bool)
            or round_number < 0
        ):
            raise self._snapshot_invalid(
                f"round for {container_id} must be a non-negative integer"
            )

        next_at = record.get("next_at")
        if next_at is not None:
            next_at = _finite_number(
                next_at, "RESTART_SNAPSHOT_INVALID", "next_at"
            )
        ready_at = record.get("ready_at")
        if ready_at is not None:
            ready_at = _finite_number(
                ready_at, "RESTART_SNAPSHOT_INVALID", "ready_at"
            )
        elif state == "backoff":
            ready_at = next_at

        times_value = None
        for key in ("restart_times", "restarts", "window_restarts"):
            if key in record:
                times_value = record[key]
                break
        if times_value is None:
            times_value = []
        if not isinstance(times_value, (list, tuple)):
            raise self._snapshot_invalid(
                f"restart_times for {container_id} must be a list"
            )
        if len(times_value) > MAX_RESTART_RECORDS:
            raise self._snapshot_invalid(
                f"too many restart records for {container_id}"
            )
        restart_times = [
            _finite_number(
                timestamp, "RESTART_SNAPSHOT_INVALID", "restart timestamp"
            )
            for timestamp in times_value
        ]
        restart_times.sort()

        gate = record.get(
            "gate", record.get("in_progress", record.get("granted", False))
        )
        if not isinstance(gate, bool):
            raise self._snapshot_invalid(
                f"gate for {container_id} must be boolean"
            )
        armed = record.get("armed")
        if armed is None:
            armed = state == "backoff" and not gate
        if not isinstance(armed, bool):
            raise self._snapshot_invalid(
                f"armed for {container_id} must be boolean"
            )

        last_exit_at = record.get("last_exit_at")
        if last_exit_at is not None:
            last_exit_at = _finite_number(
                last_exit_at, "RESTART_SNAPSHOT_INVALID", "last_exit_at"
            )
        last_exit_code = record.get("last_exit_code")
        if last_exit_code is not None and (
            not isinstance(last_exit_code, int)
            or isinstance(last_exit_code, bool)
            or last_exit_code < 0
            or last_exit_code > 255
        ):
            raise self._snapshot_invalid(
                f"last_exit_code for {container_id} must be in 0..255"
            )

        last_time = record.get("last_time")
        if last_time is None:
            known_times = list(restart_times)
            if last_exit_at is not None:
                known_times.append(last_exit_at)
            last_time = max(known_times) if known_times else None
        else:
            last_time = _finite_number(
                last_time, "RESTART_SNAPSHOT_INVALID", "last_time"
            )

        delay = record.get("delay")
        if delay is None:
            delay = (
                self._delay_for(policy, round_number, last_exit_code or 0)
                if state == "backoff" and round_number > 0
                else 0.0
            )
        else:
            delay = _finite_number(
                delay, "RESTART_SNAPSHOT_INVALID", "delay"
            )
            if delay < 0:
                raise self._snapshot_invalid(
                    f"delay for {container_id} must not be negative"
                )

        return _Container(
            policy=policy,
            state=state,
            round=round_number,
            delay=delay,
            next_at=next_at,
            ready_at=ready_at,
            restart_times=restart_times,
            gate=gate,
            armed=armed,
            terminated=terminated,
            last_exit_at=last_exit_at,
            last_exit_code=last_exit_code,
            last_time=last_time,
            seen_exits={},
        )

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = self._parse_snapshot(state)
            except RestartSupervisorError as exc:
                if exc.code == "RESTART_SNAPSHOT_INVALID":
                    raise
                raise self._snapshot_invalid(exc.detail) from exc
            except Exception as exc:
                raise self._snapshot_invalid(str(exc)) from exc
            self._containers = parsed

    def stats(self) -> dict:
        with self._lock:
            result = {"containers": len(self._containers)}
            result.update(self._stats)
            return result

    def reset_stats(self) -> None:
        with self._lock:
            for name in self._stats:
                self._stats[name] = 0
