"""Reconnect input replay and baseline alignment guard."""

from __future__ import annotations

import copy
import threading
import time


_STAT_KEYS = (
    "sessions",
    "baselines",
    "batches",
    "accepted",
    "duplicates",
    "stale",
    "rejected",
    "events",
    "discarded",
)


class ResumeError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _empty_stats() -> dict:
    return {key: 0 for key in _STAT_KEYS}


class ResumeAlignmentGuard:
    """Align buffered inputs with an authoritative reconnect baseline."""

    def __init__(self, *, replay_window: int = 32, max_reconnects: int = 3,
                 reconnect_window: int = 10, clock=None) -> None:
        self.replay_window = int(replay_window)
        self.max_reconnects = int(max_reconnects)
        self.reconnect_window = int(reconnect_window)
        self._clock = clock if clock is not None else time.monotonic
        self._lock = threading.RLock()
        self._accounts: dict = {}
        self._stats = _empty_stats()

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _now(self) -> float:
        return float(self._clock())

    def _new_account(self, epoch: int, rate_limits: list | None = None) -> dict:
        return {
            "epoch": epoch,
            "active": True,
            "baseline_frame": None,
            "baseline_state": None,
            "authority_frame": 0,
            "applied": [],
            "applied_frames": set(),
            "events": [],
            "held": [],
            "rate_limits": list(rate_limits or []),
        }

    def _prune_rate_limits(self, timestamps: list, now: float) -> list:
        return [
            timestamp
            for timestamp in timestamps
            if now - float(timestamp) < self.reconnect_window
        ]

    def _active_session(self, account: str, epoch: object) -> dict:
        state = self._accounts.get(account)
        if state is None or not state["active"] or state["epoch"] != epoch:
            raise ResumeError(
                "RESUME_STALE_EPOCH",
                {
                    "account": account,
                    "epoch": epoch,
                    "current_epoch": state["epoch"] if state is not None else 0,
                },
            )
        return state

    def begin_session(self, account: str) -> dict:
        with self._lock:
            now = self._now()
            previous = self._accounts.get(account)
            timestamps = self._prune_rate_limits(
                list(previous["rate_limits"]) if previous is not None else [],
                now,
            )

            if len(timestamps) >= self.max_reconnects:
                retry_after = self.reconnect_window
                if timestamps:
                    oldest = min(timestamps)
                    retry_after = max(
                        0.0, self.reconnect_window - (now - oldest)
                    )
                raise ResumeError(
                    "RESUME_RATE_LIMITED",
                    {"retry_after": retry_after},
                )

            timestamps.append(now)
            epoch = previous["epoch"] + 1 if previous is not None else 1
            self._accounts[account] = self._new_account(epoch, timestamps)
            self._stats["sessions"] += 1
            return {"account": account, "epoch": epoch}

    def close_session(self, account: str) -> dict:
        with self._lock:
            state = self._accounts.get(account)
            epoch = state["epoch"] if state is not None else 0
            if state is not None:
                state["active"] = False
            return {"account": account, "epoch": epoch}

    def install_baseline(self, account: str, *, epoch: int, frame: int,
                         state: dict) -> dict:
        with self._lock:
            account_state = self._active_session(account, epoch)
            if not self._is_int(frame):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID",
                    {"field": "frame", "frame": frame},
                )

            account_state["baseline_frame"] = frame
            account_state["baseline_state"] = copy.deepcopy(state)
            account_state["authority_frame"] = frame
            self._stats["baselines"] += 1

            held_batches = account_state["held"]
            account_state["held"] = []
            for frames in held_batches:
                try:
                    self._apply_frames(account_state, frames)
                except ResumeError as exc:
                    if exc.code != "RESUME_WINDOW_EXCEEDED":
                        raise

            return {"account": account, "frame": frame, "epoch": epoch}

    def accept_batch(self, account: str, batch: dict) -> dict:
        with self._lock:
            if not isinstance(batch, dict):
                self._stats["discarded"] += 1
                raise ResumeError("RESUME_STALE_EPOCH", {"account": account})

            epoch = batch.get("epoch")
            account_state = self._accounts.get(account)
            if (account_state is None or not account_state["active"]
                    or account_state["epoch"] != epoch):
                self._stats["discarded"] += 1
                raise ResumeError(
                    "RESUME_STALE_EPOCH",
                    {
                        "account": account,
                        "epoch": epoch,
                        "current_epoch": (
                            account_state["epoch"]
                            if account_state is not None else 0
                        ),
                    },
                )

            self._stats["batches"] += 1
            raw_frames = batch.get("frames", [])
            if not isinstance(raw_frames, list):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID",
                    {"field": "frames"},
                )
            frames = [copy.deepcopy(frame) for frame in raw_frames]

            if account_state["baseline_frame"] is None:
                account_state["held"].append(frames)
                return {"status": "held", "accepted": 0}

            return self._apply_frames(account_state, frames)

    def _apply_frames(self, account_state: dict, frames: list) -> dict:
        ordered = sorted(frames, key=lambda item: item["frame"])
        window_limit = account_state["authority_frame"] + self.replay_window

        for frame in ordered:
            frame_number = frame["frame"]
            if frame_number > window_limit:
                self._stats["rejected"] += 1
                raise ResumeError(
                    "RESUME_WINDOW_EXCEEDED",
                    {"require_baseline": True},
                )

        accepted = 0
        for frame in ordered:
            frame_number = frame["frame"]

            if frame_number in account_state["applied_frames"]:
                self._stats["duplicates"] += 1
                continue

            if frame_number <= account_state["authority_frame"]:
                self._stats["stale"] += 1
                continue

            record = copy.deepcopy(frame)
            account_state["applied"].append(record)
            account_state["applied_frames"].add(frame_number)
            account_state["authority_frame"] = frame_number
            accepted += 1
            self._stats["accepted"] += 1

            for event in frame.get("events") or []:
                account_state["events"].append(
                    {"frame": frame_number, "event": copy.deepcopy(event)}
                )
                self._stats["events"] += 1

        return {
            "status": "applied" if accepted else "stale",
            "accepted": accepted,
        }

    def applied_inputs(self, account: str) -> list:
        with self._lock:
            state = self._accounts.get(account)
            if state is None:
                return []
            return copy.deepcopy(state["applied"])

    def applied_frame(self, account: str) -> int:
        with self._lock:
            state = self._accounts.get(account)
            return state["authority_frame"] if state is not None else 0

    def baseline_of(self, account: str) -> dict | None:
        with self._lock:
            state = self._accounts.get(account)
            if state is None or state["baseline_state"] is None:
                return None
            return copy.deepcopy(state["baseline_state"])

    def session_epoch(self, account: str) -> int:
        with self._lock:
            state = self._accounts.get(account)
            return state["epoch"] if state is not None else 0

    def fired_events(self, account: str) -> list:
        with self._lock:
            state = self._accounts.get(account)
            if state is None:
                return []
            return copy.deepcopy(state["events"])

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in _STAT_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            accounts = {}
            for account, state in self._accounts.items():
                accounts[account] = {
                    "epoch": state["epoch"],
                    "active": state["active"],
                    "baseline_frame": state["baseline_frame"],
                    "baseline_state": copy.deepcopy(state["baseline_state"]),
                    "authority_frame": state["authority_frame"],
                    "applied_inputs": copy.deepcopy(state["applied"]),
                    "applied_frames": sorted(state["applied_frames"]),
                    "fired_events": copy.deepcopy(state["events"]),
                    "held_batches": copy.deepcopy(state["held"]),
                    "rate_limits": list(state["rate_limits"]),
                }

            return {
                "version": 1,
                "accounts": accounts,
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                restored_accounts, restored_stats = self._parse_snapshot(state)
            except ResumeError:
                raise
            except Exception as exc:
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID",
                    {"reason": "unparseable snapshot"},
                ) from exc
            self._accounts = restored_accounts
            self._stats = restored_stats

    def _parse_snapshot(self, state: object) -> tuple[dict, dict]:
        if not isinstance(state, dict):
            raise ResumeError(
                "RESUME_SNAPSHOT_INVALID", "snapshot must be a mapping"
            )

        version = state.get("version")
        if not self._is_int(version) or version != 1:
            raise ResumeError("RESUME_SNAPSHOT_INVALID", {"version": version})
        if not isinstance(state.get("accounts"), dict):
            raise ResumeError(
                "RESUME_SNAPSHOT_INVALID", "accounts must be a mapping"
            )
        if not isinstance(state.get("stats"), dict):
            raise ResumeError(
                "RESUME_SNAPSHOT_INVALID", "stats must be a mapping"
            )

        stats = self._parse_stats(state["stats"])
        accounts = {}
        for account, raw_account in state["accounts"].items():
            if not isinstance(account, str):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID", "account keys must be strings"
                )
            accounts[account] = self._parse_account_snapshot(raw_account)

        return accounts, stats

    def _parse_stats(self, raw_stats: dict) -> dict:
        if set(raw_stats.keys()) != set(_STAT_KEYS):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad stats keys")

        stats = _empty_stats()
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not self._is_int(value) or value < 0:
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID",
                    {"stat": key, "value": value},
                )
            stats[key] = value
        return stats

    def _parse_account_snapshot(self, raw: object) -> dict:
        required = {
            "epoch",
            "active",
            "baseline_frame",
            "baseline_state",
            "authority_frame",
            "applied_inputs",
            "applied_frames",
            "fired_events",
            "held_batches",
            "rate_limits",
        }
        if not isinstance(raw, dict) or not required.issubset(raw.keys()):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad account state")

        epoch = raw["epoch"]
        active = raw["active"]
        baseline_frame = raw["baseline_frame"]
        baseline_state = raw["baseline_state"]
        authority_frame = raw["authority_frame"]
        applied_inputs = raw["applied_inputs"]
        applied_frames = raw["applied_frames"]
        fired_events = raw["fired_events"]
        held_batches = raw["held_batches"]
        rate_limits = raw["rate_limits"]

        if not self._is_int(epoch) or epoch < 0:
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad epoch")
        if not isinstance(active, bool):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad active flag")
        if baseline_frame is not None and not self._is_int(baseline_frame):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad baseline frame")
        if not self._is_int(authority_frame):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad authority frame")
        if not isinstance(applied_inputs, list):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad applied inputs")
        if not isinstance(applied_frames, list):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad applied frames")
        if not isinstance(fired_events, list):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad events")
        if not isinstance(held_batches, list):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad held batches")
        if not isinstance(rate_limits, list):
            raise ResumeError("RESUME_SNAPSHOT_INVALID", "bad rate limits")

        applied = copy.deepcopy(applied_inputs)
        frame_set = set()
        for record in applied:
            if not isinstance(record, dict) or not self._is_int(
                record.get("frame")
            ):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID", "bad applied input frame"
                )
            frame_set.add(record["frame"])

        restored_applied_frames = set()
        for frame_number in applied_frames:
            if not self._is_int(frame_number):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID", "bad applied frame"
                )
            restored_applied_frames.add(frame_number)
        if restored_applied_frames != frame_set:
            raise ResumeError(
                "RESUME_SNAPSHOT_INVALID", "applied frame mismatch"
            )

        events = copy.deepcopy(fired_events)
        for event_record in events:
            if (not isinstance(event_record, dict)
                    or not self._is_int(event_record.get("frame"))
                    or "event" not in event_record):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID", "bad fired event record"
                )

        held = copy.deepcopy(held_batches)
        for batch_frames in held:
            if not isinstance(batch_frames, list):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID", "bad held batch"
                )
            for frame in batch_frames:
                if not isinstance(frame, dict) or not self._is_int(
                    frame.get("frame")
                ):
                    raise ResumeError(
                        "RESUME_SNAPSHOT_INVALID", "bad held frame"
                    )

        rates = []
        for timestamp in rate_limits:
            if isinstance(timestamp, bool) or not isinstance(
                timestamp, (int, float)
            ):
                raise ResumeError(
                    "RESUME_SNAPSHOT_INVALID", "bad rate limit timestamp"
                )
            rates.append(float(timestamp))

        return {
            "epoch": epoch,
            "active": active,
            "baseline_frame": baseline_frame,
            "baseline_state": copy.deepcopy(baseline_state),
            "authority_frame": authority_frame,
            "applied": applied,
            "applied_frames": frame_set,
            "events": events,
            "held": held,
            "rate_limits": rates,
        }
