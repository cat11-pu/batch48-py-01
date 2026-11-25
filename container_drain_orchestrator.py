"""Container drain orchestration: readiness removal, inflight drain, process-group escalation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

MAIN_PID = 0

_CREATED = "CREATED"
_RUNNING = "RUNNING"
_DRAINING = "DRAINING"
_STOPPING = "STOPPING"
_EXITED = "EXITED"

_STATES = {_CREATED, _RUNNING, _DRAINING, _STOPPING, _EXITED}
_SIGNAL_NAMES = {"SIGTERM", "SIGKILL"}


class DrainError(ValueError):
    """Raised when a drain operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DrainPolicy:
    grace_seconds: int = 30
    sigterm_delay: int = 5
    max_children: int = 16


@dataclass
class _Container:
    policy: DrainPolicy
    state: str = _CREATED
    ready: bool = True
    drain_started: Optional[float] = None
    deadline: Optional[float] = None
    stopping_at: Optional[float] = None
    exited_at: Optional[float] = None
    pids: Dict[int, bool] = field(default_factory=lambda: {MAIN_PID: True})
    signals: List[dict] = field(default_factory=list)
    next_seq: int = 1
    backlog: int = 0
    inflight: int = 0


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_policy(policy: object) -> DrainPolicy:
    if not isinstance(policy, DrainPolicy):
        raise DrainError("DRAIN_INVALID_POLICY", policy)
    if not _is_number(policy.grace_seconds) or policy.grace_seconds <= 0:
        raise DrainError("DRAIN_INVALID_POLICY", policy)
    if not _is_number(policy.sigterm_delay) or policy.sigterm_delay < 0:
        raise DrainError("DRAIN_INVALID_POLICY", policy)
    if (
        isinstance(policy.max_children, bool)
        or not isinstance(policy.max_children, int)
        or policy.max_children < 1
    ):
        raise DrainError("DRAIN_INVALID_POLICY", policy)
    return policy


class DrainOrchestrator:
    """Drive one container through CREATED/RUNNING/DRAINING/STOPPING/EXITED."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._containers: Dict[str, _Container] = {}

    @staticmethod
    def _require_now(now: object) -> None:
        if not _is_number(now):
            raise DrainError("DRAIN_INVALID_TIME", now)

    def _get(self, container_id: str) -> _Container:
        if isinstance(container_id, str):
            container = self._containers.get(container_id)
            if container is not None:
                return container
        raise DrainError("DRAIN_UNKNOWN_CONTAINER", container_id)

    @staticmethod
    def _info(container: _Container) -> dict:
        return {
            "state": container.state,
            "ready": container.ready,
            "alive": sum(1 for alive in container.pids.values() if alive),
            "drain_started": container.drain_started,
            "deadline": container.deadline,
            "stopping_at": container.stopping_at,
            "exited_at": container.exited_at,
            "signal_count": len(container.signals),
        }

    @staticmethod
    def _emit(container: _Container, signal: str, now: float) -> dict:
        record = {
            "signal": signal,
            "scope": "group",
            "seq": container.next_seq,
            "at": now,
        }
        container.next_seq += 1
        container.signals.append(record)
        return dict(record)

    @staticmethod
    def _has_signal(container: _Container, signal: str) -> bool:
        return any(record["signal"] == signal for record in container.signals)

    @staticmethod
    def _finish(container: _Container, now: float) -> None:
        container.state = _EXITED
        container.ready = False
        container.exited_at = now
        for pid in container.pids:
            container.pids[pid] = False
        container.inflight = 0

    def register(self, container_id: str, policy: DrainPolicy | None = None) -> dict:
        with self._lock:
            if not isinstance(container_id, str) or not container_id.strip():
                raise DrainError("DRAIN_INVALID_CONTAINER", container_id)
            if container_id in self._containers:
                raise DrainError("DRAIN_DUPLICATE_CONTAINER", container_id)
            selected = DrainPolicy() if policy is None else policy
            _validate_policy(selected)
            container = _Container(policy=selected)
            self._containers[container_id] = container
            return self._info(container)

    def start(self, container_id: str) -> str:
        with self._lock:
            container = self._get(container_id)
            if container.state != _CREATED:
                raise DrainError("DRAIN_INVALID_STATE", container.state)
            container.state = _RUNNING
            return container.state

    def begin_drain(self, container_id: str, now: int) -> str:
        self._require_now(now)
        with self._lock:
            container = self._get(container_id)
            if container.state in (_DRAINING, _STOPPING):
                raise DrainError("DRAIN_ALREADY_DRAINING", container.state)
            if container.state == _EXITED:
                raise DrainError("DRAIN_ALREADY_EXITED", container.state)
            if container.state not in (_CREATED, _RUNNING):
                raise DrainError("DRAIN_INVALID_STATE", container.state)

            container.ready = False
            container.state = _DRAINING
            container.drain_started = now
            container.deadline = now + container.policy.grace_seconds
            container.stopping_at = None
            container.exited_at = None
            return container.state

    def admit_request(self, container_id: str) -> bool:
        with self._lock:
            container = self._get(container_id)
            if container.state == _RUNNING and container.ready:
                container.inflight += 1
                return True
            if container.state == _DRAINING:
                container.backlog += 1
            return False

    def mark_idle(self, container_id: str, now: int) -> dict:
        self._require_now(now)
        with self._lock:
            container = self._get(container_id)
            if container.state != _DRAINING:
                raise DrainError("DRAIN_INVALID_STATE", container.state)
            signal = self._emit(container, "SIGTERM", now)
            container.state = _STOPPING
            container.stopping_at = now
            container.inflight = 0
            return signal

    def add_child(self, container_id: str, pid: int) -> int:
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise DrainError("DRAIN_INVALID_PID", pid)
        with self._lock:
            container = self._get(container_id)
            if container.state == _EXITED:
                raise DrainError("DRAIN_INVALID_STATE", container.state)
            if pid in container.pids:
                raise DrainError("DRAIN_DUPLICATE_PID", pid)
            live_children = sum(
                1
                for child_pid, alive in container.pids.items()
                if child_pid != MAIN_PID and alive
            )
            if live_children >= container.policy.max_children:
                raise DrainError("DRAIN_CHILD_LIMIT", pid)
            container.pids[pid] = True
            return sum(1 for alive in container.pids.values() if alive)

    def note_exit(self, container_id: str, pid: int) -> int:
        if isinstance(pid, bool) or not isinstance(pid, int):
            raise DrainError("DRAIN_INVALID_PID", pid)
        with self._lock:
            container = self._get(container_id)
            if pid not in container.pids or not container.pids[pid]:
                raise DrainError("DRAIN_UNKNOWN_PID", pid)
            container.pids[pid] = False
            return sum(1 for alive in container.pids.values() if alive)

    def _advance_one(self, container: _Container, now: float) -> Optional[str]:
        if container.state not in (_DRAINING, _STOPPING):
            return None

        if not any(container.pids.values()):
            self._finish(container, now)
            return "exited"

        emitted_term = False
        if container.state == _DRAINING:
            if now < container.deadline:
                return None
            self._emit(container, "SIGTERM", now)
            container.state = _STOPPING
            container.stopping_at = now
            emitted_term = True

        force_at = container.deadline + container.policy.sigterm_delay
        if now >= force_at:
            if not self._has_signal(container, "SIGKILL"):
                self._emit(container, "SIGKILL", now)
            self._finish(container, now)
            return "exited"

        kill_at = (
            container.stopping_at + container.policy.sigterm_delay
            if container.stopping_at is not None
            else force_at
        )
        if now >= kill_at and not self._has_signal(container, "SIGKILL"):
            self._emit(container, "SIGKILL", now)
            return "sigkill"

        return "sigterm" if emitted_term else None

    def advance(self, now: int) -> dict:
        self._require_now(now)
        with self._lock:
            changes = {}
            for container_id, container in self._containers.items():
                event = self._advance_one(container, now)
                if event is not None:
                    changes[container_id] = event
            return changes

    def signals(self, container_id: str) -> list:
        with self._lock:
            container = self._get(container_id)
            return [dict(record) for record in container.signals]

    def inspect(self, container_id: str) -> dict:
        with self._lock:
            return self._info(self._get(container_id))

    def backlog_count(self, container_id: str) -> int:
        with self._lock:
            return self._get(container_id).backlog

    @staticmethod
    def _snapshot_container(container: _Container) -> dict:
        return {
            "state": container.state,
            "ready": container.ready,
            "policy": {
                "grace_seconds": container.policy.grace_seconds,
                "sigterm_delay": container.policy.sigterm_delay,
                "max_children": container.policy.max_children,
            },
            "drain_started": container.drain_started,
            "deadline": container.deadline,
            "stopping_at": container.stopping_at,
            "exited_at": container.exited_at,
            "pids": {pid: alive for pid, alive in container.pids.items()},
            "signals": [dict(record) for record in container.signals],
            "next_seq": container.next_seq,
            "backlog": container.backlog,
            "inflight": container.inflight,
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "containers": {
                    container_id: self._snapshot_container(container)
                    for container_id, container in self._containers.items()
                }
            }

    @staticmethod
    def _snapshot_invalid(detail: object) -> DrainError:
        return DrainError("DRAIN_SNAPSHOT_INVALID", detail)

    def _parse_policy(self, raw: object) -> DrainPolicy:
        if isinstance(raw, DrainPolicy):
            policy = raw
        elif isinstance(raw, dict):
            try:
                policy = DrainPolicy(
                    grace_seconds=raw["grace_seconds"],
                    sigterm_delay=raw["sigterm_delay"],
                    max_children=raw["max_children"],
                )
            except (KeyError, TypeError):
                raise self._snapshot_invalid(raw) from None
        else:
            raise self._snapshot_invalid(raw)
        try:
            return _validate_policy(policy)
        except DrainError as exc:
            raise self._snapshot_invalid(exc.detail) from None

    def _parse_time(self, raw: object, *, allow_none: bool = True) -> Optional[float]:
        if raw is None and allow_none:
            return None
        if not _is_number(raw):
            raise self._snapshot_invalid(raw)
        return raw

    def _parse_pids(self, raw: object) -> Dict[int, bool]:
        pids: Dict[int, bool] = {}

        def add_pid(pid: object, alive: object) -> None:
            if isinstance(pid, str):
                try:
                    pid = int(pid)
                except ValueError:
                    raise self._snapshot_invalid(pid) from None
            if isinstance(pid, bool) or not isinstance(pid, int):
                raise self._snapshot_invalid(pid)
            if not isinstance(alive, bool):
                raise self._snapshot_invalid(alive)
            if pid in pids:
                raise self._snapshot_invalid(pid)
            pids[pid] = alive

        if isinstance(raw, dict):
            for pid, alive in raw.items():
                add_pid(pid, alive)
        elif isinstance(raw, list):
            for item in raw:
                if isinstance(item, int) and not isinstance(item, bool):
                    add_pid(item, True)
                elif isinstance(item, (list, tuple)) and len(item) == 2:
                    add_pid(item[0], item[1])
                elif isinstance(item, dict) and "pid" in item and "alive" in item:
                    add_pid(item["pid"], item["alive"])
                else:
                    raise self._snapshot_invalid(item)
        else:
            raise self._snapshot_invalid(raw)

        if MAIN_PID not in pids:
            raise self._snapshot_invalid("main pid is missing")
        return pids

    def _parse_signals(self, raw: object) -> List[dict]:
        if not isinstance(raw, list):
            raise self._snapshot_invalid(raw)
        signals: List[dict] = []
        seen = set()
        for index, item in enumerate(raw, start=1):
            if not isinstance(item, dict):
                raise self._snapshot_invalid(item)
            try:
                signal = item["signal"]
                scope = item["scope"]
                seq = item["seq"]
                at = item["at"]
            except KeyError:
                raise self._snapshot_invalid(item) from None
            if signal not in _SIGNAL_NAMES or signal in seen:
                raise self._snapshot_invalid(item)
            if scope != "group":
                raise self._snapshot_invalid(item)
            if isinstance(seq, bool) or not isinstance(seq, int) or seq != index:
                raise self._snapshot_invalid(item)
            if not _is_number(at):
                raise self._snapshot_invalid(item)
            seen.add(signal)
            signals.append(
                {"signal": signal, "scope": "group", "seq": seq, "at": at}
            )
        return signals

    def _parse_container(self, container_id: str, raw: object) -> _Container:
        if not isinstance(raw, dict):
            raise self._snapshot_invalid(raw)

        required = {
            "state",
            "ready",
            "policy",
            "drain_started",
            "deadline",
            "pids",
            "signals",
            "next_seq",
        }
        if not required.issubset(raw):
            raise self._snapshot_invalid(container_id)

        state = raw["state"]
        if state not in _STATES:
            raise self._snapshot_invalid(state)
        if not isinstance(raw["ready"], bool):
            raise self._snapshot_invalid(raw["ready"])

        policy = self._parse_policy(raw["policy"])
        drain_started = self._parse_time(raw["drain_started"])
        deadline = self._parse_time(raw["deadline"])
        stopping_at = self._parse_time(raw.get("stopping_at"))
        exited_at = self._parse_time(raw.get("exited_at"))
        pids = self._parse_pids(raw["pids"])
        signals = self._parse_signals(raw["signals"])

        next_seq = raw["next_seq"]
        if isinstance(next_seq, bool) or not isinstance(next_seq, int):
            raise self._snapshot_invalid(next_seq)
        if next_seq != len(signals) + 1:
            raise self._snapshot_invalid(next_seq)

        backlog = raw.get("backlog", 0)
        inflight = raw.get("inflight", 0)
        for value in (backlog, inflight):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise self._snapshot_invalid(value)

        if state in (_DRAINING, _STOPPING, _EXITED):
            if drain_started is None or deadline is None:
                raise self._snapshot_invalid(container_id)
        if state == _STOPPING and stopping_at is None:
            raise self._snapshot_invalid(container_id)
        if state == _EXITED and exited_at is None:
            raise self._snapshot_invalid(container_id)
        if state == _EXITED and any(pids.values()):
            raise self._snapshot_invalid(container_id)

        live_children = sum(
            1 for pid, alive in pids.items() if pid != MAIN_PID and alive
        )
        if live_children > policy.max_children:
            raise self._snapshot_invalid(container_id)

        return _Container(
            policy=policy,
            state=state,
            ready=raw["ready"],
            drain_started=drain_started,
            deadline=deadline,
            stopping_at=stopping_at,
            exited_at=exited_at,
            pids=pids,
            signals=signals,
            next_seq=next_seq,
            backlog=backlog,
            inflight=inflight,
        )

    def _parse_snapshot(self, state: object) -> Dict[str, _Container]:
        if not isinstance(state, dict):
            raise self._snapshot_invalid(state)
        raw_containers = state.get("containers")
        if not isinstance(raw_containers, dict):
            raise self._snapshot_invalid(raw_containers)

        containers: Dict[str, _Container] = {}
        for container_id, raw in raw_containers.items():
            if not isinstance(container_id, str) or not container_id.strip():
                raise self._snapshot_invalid(container_id)
            containers[container_id] = self._parse_container(container_id, raw)
        return containers

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                containers = self._parse_snapshot(state)
            except DrainError as exc:
                if exc.code == "DRAIN_SNAPSHOT_INVALID":
                    raise
                raise self._snapshot_invalid(exc.detail) from None
            except Exception as exc:
                raise self._snapshot_invalid(str(exc)) from None
            self._containers = containers
