"""TLS 1.3 key phase epochs with retention windows and per epoch sequences."""

from __future__ import annotations

import copy
import threading
from typing import Any

STATS_KEYS = (
    "updates_accepted",
    "updates_rejected",
    "records_encrypted",
    "records_decrypted",
    "discarded",
    "rejected",
)


_MISSING = object()


class KeyPhaseError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_plain_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class KeyPhaseGuard:
    """Track key epochs, retained receive keys and per epoch sequence spaces."""

    def __init__(
        self,
        *,
        seq_limit: int = 8,
        max_updates: int = 4,
        min_update_interval_ms: int = 500,
        retain_epochs: int = 1,
        retain_ms: int = 2000,
        clock=None,
    ) -> None:
        self.seq_limit = seq_limit
        self.max_updates = max_updates
        self.min_update_interval_ms = min_update_interval_ms
        self.retain_epochs = retain_epochs
        self.retain_ms = retain_ms
        self._clock = clock
        self._lock = threading.RLock()
        self._reset_state_locked()

    def _reset_state_locked(self) -> None:
        self._started = False
        self._recv_epoch = 0
        self._send_epoch = 0
        self._pending_send_epoch: int | None = None
        self._retained: dict[int, int | None] = {}
        self._seen: dict[int, set[int]] = {}
        self._send_last: dict[int, int | None] = {}
        self._pending_writes: set[tuple[int, int]] = set()
        self._update_count = 0
        self._last_update_at: int | None = None
        self._stats = {key: 0 for key in STATS_KEYS}

    def _reject(
        self, code: str, detail: object = None, *, update_rejected: bool = False
    ) -> None:
        self._stats["rejected"] += 1
        if update_rejected:
            self._stats["updates_rejected"] += 1
        raise KeyPhaseError(code, detail)

    def _ensure_started_locked(self) -> None:
        if not self._started:
            self._reject("KP_NOT_STARTED")

    def _valid_seq(self, seq: object) -> bool:
        return _is_plain_int(seq) and 0 <= seq < self.seq_limit

    def _retained_phase_locked(self) -> list[dict[str, int | None]]:
        return [
            {"epoch": epoch, "expires_at": self._retained[epoch]}
            for epoch in sorted(self._retained)
        ]

    def _summary_locked(self) -> dict[str, Any]:
        return {
            "recv_epoch": self._recv_epoch,
            "send_epoch": self._send_epoch,
            "retained": sorted(self._retained),
        }

    def _authorize_update_locked(self, now: int) -> None:
        if self._update_count >= self.max_updates:
            self._reject(
                "KP_UPDATE_LIMIT",
                {"max_updates": self.max_updates},
                update_rejected=True,
            )

        if self._last_update_at is not None:
            elapsed_ms = now - self._last_update_at
            if elapsed_ms < self.min_update_interval_ms:
                self._reject(
                    "KP_UPDATE_TOO_SOON",
                    {
                        "elapsed_ms": elapsed_ms,
                        "min_update_interval_ms": self.min_update_interval_ms,
                    },
                    update_rejected=True,
                )

        self._update_count += 1
        self._last_update_at = now
        self._stats["updates_accepted"] += 1

    def _trim_retention_locked(self) -> None:
        current_epoch = self._recv_epoch
        old_epochs = sorted(epoch for epoch in self._retained if epoch < current_epoch)
        excess = len(old_epochs) - self.retain_epochs
        for epoch in old_epochs[: max(0, excess)]:
            self._retained.pop(epoch, None)
            self._seen.pop(epoch, None)

    def _commit_pending_send_locked(self) -> bool:
        if self._pending_send_epoch is None or self._pending_writes:
            return False

        next_epoch = self._pending_send_epoch
        self._send_epoch = next_epoch
        self._pending_send_epoch = None
        self._send_last.setdefault(next_epoch, None)
        return True

    def start(self, *, now: int = 0) -> dict:
        with self._lock:
            if self._started:
                self._reject(
                    "KP_ALREADY_STARTED",
                    {
                        "recv_epoch": self._recv_epoch,
                        "send_epoch": self._send_epoch,
                    },
                )

            stats = copy.deepcopy(self._stats)
            self._reset_state_locked()
            self._stats = stats
            self._started = True
            self._recv_epoch = 1
            self._send_epoch = 1
            self._retained = {1: None}
            self._seen = {1: set()}
            self._send_last = {1: None}
            return self._summary_locked()

    def key_phase(self) -> dict:
        with self._lock:
            self._ensure_started_locked()
            return {
                "recv_epoch": self._recv_epoch,
                "send_epoch": self._send_epoch,
                "retained": self._retained_phase_locked(),
                "pending_send_epoch": self._pending_send_epoch,
            }

    def request_update(self, *, now: int = 0) -> dict:
        with self._lock:
            self._ensure_started_locked()
            self._authorize_update_locked(now)

            previous_epoch = self._recv_epoch
            self._retained[previous_epoch] = now + self.retain_ms
            self._recv_epoch = previous_epoch + 1
            self._retained[self._recv_epoch] = None
            self._seen.setdefault(self._recv_epoch, set())
            self._trim_retention_locked()
            return self._summary_locked()

    def on_key_update(self, *, now: int = 0) -> dict:
        with self._lock:
            self._ensure_started_locked()
            self._authorize_update_locked(now)

            next_epoch = self._pending_send_epoch or self._send_epoch + 1
            if self._pending_writes:
                self._pending_send_epoch = next_epoch
                return {
                    "deferred": True,
                    "send_epoch": self._send_epoch,
                    "pending_send_epoch": next_epoch,
                }

            self._pending_send_epoch = next_epoch
            self._commit_pending_send_locked()
            return {
                "deferred": False,
                "send_epoch": self._send_epoch,
                "pending_send_epoch": None,
            }

    def encrypt(self, seq: int, *, now: int = 0) -> dict:
        with self._lock:
            self._ensure_started_locked()
            if not self._valid_seq(seq):
                self._reject(
                    "KP_SEQ_OUT_OF_RANGE",
                    {"seq": seq, "seq_limit": self.seq_limit},
                )

            epoch = self._send_epoch
            last_seq = self._send_last.get(epoch)
            if last_seq is not None and seq <= last_seq:
                self._reject(
                    "KP_SEQ_REUSED",
                    {"epoch": epoch, "seq": seq},
                )

            self._send_last[epoch] = seq
            self._stats["records_encrypted"] += 1
            return {"epoch": epoch, "seq": seq}

    def decrypt(self, epoch: int, seq: int, *, now: int = 0) -> dict:
        with self._lock:
            self._ensure_started_locked()
            if not self._valid_seq(seq):
                self._reject(
                    "KP_SEQ_OUT_OF_RANGE",
                    {"epoch": epoch, "seq": seq, "seq_limit": self.seq_limit},
                )

            if not _is_plain_int(epoch) or epoch < 1 or epoch > self._recv_epoch:
                self._reject("KP_EPOCH_UNKNOWN", {"epoch": epoch})

            expires_at = self._retained.get(epoch, _MISSING)
            if epoch < self._recv_epoch:
                expired_by_time = expires_at is _MISSING or (
                    expires_at is not None and now >= expires_at
                )
                if expired_by_time:
                    self._stats["discarded"] += 1
                    return {
                        "status": "discarded",
                        "reason": "KP_KEY_EXPIRED",
                        "epoch": epoch,
                        "seq": seq,
                    }

            seen = self._seen.setdefault(epoch, set())
            if seq in seen:
                self._reject(
                    "KP_SEQ_REUSED",
                    {"epoch": epoch, "seq": seq},
                )

            seen.add(seq)
            self._stats["records_decrypted"] += 1
            return {"status": "ok", "epoch": epoch, "seq": seq}

    def track_write(self, seq: int, *, now: int = 0) -> dict:
        with self._lock:
            self._ensure_started_locked()
            if not self._valid_seq(seq):
                self._reject(
                    "KP_SEQ_OUT_OF_RANGE",
                    {"seq": seq, "seq_limit": self.seq_limit},
                )

            epoch = self._send_epoch
            self._pending_writes.add((epoch, seq))
            return {
                "epoch": epoch,
                "seq": seq,
                "pending_writes": len(self._pending_writes),
            }

    def ack_write(self, epoch: int, seq: int) -> dict:
        with self._lock:
            self._ensure_started_locked()
            if not _is_plain_int(epoch) or epoch < 1:
                self._reject("KP_EPOCH_UNKNOWN", {"epoch": epoch})
            if not self._valid_seq(seq):
                self._reject(
                    "KP_SEQ_OUT_OF_RANGE",
                    {"epoch": epoch, "seq": seq, "seq_limit": self.seq_limit},
                )

            key = (epoch, seq)
            acknowledged = key in self._pending_writes
            self._pending_writes.discard(key)
            committed = self._commit_pending_send_locked()
            return {
                "acked": acknowledged,
                "epoch": epoch,
                "seq": seq,
                "committed": committed,
                "send_epoch": self._send_epoch,
                "pending_send_epoch": self._pending_send_epoch,
                "pending_writes": len(self._pending_writes),
            }

    def pending_writes(self) -> int:
        with self._lock:
            return len(self._pending_writes)

    def expire(self, *, now: int = 0) -> dict:
        with self._lock:
            expired = []
            for epoch in sorted(self._retained):
                expires_at = self._retained[epoch]
                if expires_at is not None and now >= expires_at:
                    expired.append(epoch)

            for epoch in expired:
                self._retained.pop(epoch, None)
                self._seen.pop(epoch, None)

            return {"expired": expired}

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            state = {
                "version": 1,
                "started": self._started,
                "recv_epoch": self._recv_epoch,
                "send_epoch": self._send_epoch,
                "pending_send_epoch": self._pending_send_epoch,
                "update_count": self._update_count,
                "last_update_at": self._last_update_at,
                "retained": [
                    {"epoch": epoch, "expires_at": expires_at}
                    for epoch, expires_at in sorted(self._retained.items())
                ],
                "seen": {
                    epoch: sorted(sequences)
                    for epoch, sequences in sorted(self._seen.items())
                },
                "send_last": {
                    epoch: last_seq
                    for epoch, last_seq in sorted(self._send_last.items())
                },
                "pending_writes": [
                    {"epoch": epoch, "seq": seq}
                    for epoch, seq in sorted(self._pending_writes)
                ],
                "stats": copy.deepcopy(self._stats),
            }
            return copy.deepcopy(state)

    @staticmethod
    def _snapshot_invalid(message: str = "invalid snapshot") -> None:
        raise KeyPhaseError("KP_SNAPSHOT_INVALID", {"reason": message})

    @classmethod
    def _require_int_field(cls, value: object, field: str) -> int:
        if not _is_plain_int(value):
            cls._snapshot_invalid(f"{field} must be an integer")
        return value

    @classmethod
    def _parse_epoch_key(cls, key: object) -> int:
        if _is_plain_int(key):
            epoch = key
        elif isinstance(key, str):
            try:
                epoch = int(key)
            except ValueError:
                cls._snapshot_invalid("epoch keys must be integers")
        else:
            cls._snapshot_invalid("epoch keys must be integers or strings")

        if not _is_plain_int(epoch) or epoch < 1:
            cls._snapshot_invalid("epoch must be positive")
        return epoch

    def _parse_snapshot(self, raw_state: object) -> dict[str, Any]:
        if not isinstance(raw_state, dict):
            self._snapshot_invalid("snapshot must be a dict")

        try:
            state = copy.deepcopy(raw_state)
        except Exception as exc:
            raise KeyPhaseError(
                "KP_SNAPSHOT_INVALID", {"reason": "snapshot is not copyable"}
            ) from exc

        if state.get("version", 1) != 1:
            self._snapshot_invalid("unsupported snapshot version")

        started = state.get("started", True)
        if not isinstance(started, bool):
            self._snapshot_invalid("started must be a bool")

        recv_epoch = self._require_int_field(state.get("recv_epoch"), "recv_epoch")
        send_epoch = self._require_int_field(state.get("send_epoch"), "send_epoch")
        if recv_epoch < 1 or send_epoch < 1:
            self._snapshot_invalid("epochs must be positive")

        pending_send_epoch = state.get("pending_send_epoch")
        if pending_send_epoch is not None:
            pending_send_epoch = self._require_int_field(
                pending_send_epoch, "pending_send_epoch"
            )
            if pending_send_epoch < 1:
                self._snapshot_invalid("pending_send_epoch must be positive")

        update_count = state.get("update_count", 0)
        update_count = self._require_int_field(update_count, "update_count")
        if update_count < 0:
            self._snapshot_invalid("update_count must be non-negative")

        last_update_at = state.get("last_update_at")
        if last_update_at is not None:
            last_update_at = self._require_int_field(last_update_at, "last_update_at")

        retained: dict[int, int | None] = {}
        raw_retained = state.get(
            "retained",
            [{"epoch": recv_epoch, "expires_at": None}],
        )
        if isinstance(raw_retained, list):
            for item in raw_retained:
                if _is_plain_int(item):
                    epoch = item
                    expires_at = None
                elif isinstance(item, dict):
                    epoch = self._require_int_field(item.get("epoch"), "retained.epoch")
                    expires_at = item.get("expires_at")
                else:
                    self._snapshot_invalid("retained entries are invalid")

                epoch = self._parse_epoch_key(epoch)
                if expires_at is not None:
                    expires_at = self._require_int_field(
                        expires_at, "retained.expires_at"
                    )
                retained[epoch] = expires_at
        elif isinstance(raw_retained, dict):
            for raw_epoch, raw_expiry in raw_retained.items():
                epoch = self._parse_epoch_key(raw_epoch)
                if isinstance(raw_expiry, dict):
                    expires_at = raw_expiry.get("expires_at")
                else:
                    expires_at = raw_expiry
                if expires_at is not None:
                    expires_at = self._require_int_field(
                        expires_at, "retained.expires_at"
                    )
                retained[epoch] = expires_at
        else:
            self._snapshot_invalid("retained must be a list or dict")

        retained.setdefault(recv_epoch, None)
        retained[recv_epoch] = None

        raw_seen = state.get("seen", {})
        if not isinstance(raw_seen, dict):
            self._snapshot_invalid("seen must be a dict")

        seen: dict[int, set[int]] = {}
        for raw_epoch, raw_sequences in raw_seen.items():
            epoch = self._parse_epoch_key(raw_epoch)
            if not isinstance(raw_sequences, (list, tuple, set, frozenset)):
                self._snapshot_invalid("seen sequences must be a sequence")

            sequences = set()
            for seq in raw_sequences:
                if not self._valid_seq(seq):
                    self._snapshot_invalid("seen sequence is out of range")
                sequences.add(seq)
            seen[epoch] = sequences

        raw_send_last = state.get("send_last", state.get("send_seq", {}))
        send_last: dict[int, int | None] = {}
        if _is_plain_int(raw_send_last):
            send_last[send_epoch] = None if raw_send_last < 0 else raw_send_last
        elif isinstance(raw_send_last, dict):
            for raw_epoch, raw_seq in raw_send_last.items():
                epoch = self._parse_epoch_key(raw_epoch)
                if raw_seq is not None:
                    raw_seq = self._require_int_field(raw_seq, "send_last.seq")
                    if raw_seq < 0:
                        raw_seq = None
                    elif raw_seq >= self.seq_limit:
                        self._snapshot_invalid("send_last sequence is out of range")
                send_last[epoch] = raw_seq
        else:
            self._snapshot_invalid("send_last must be a dict or integer")

        send_last.setdefault(send_epoch, None)

        raw_pending = state.get("pending_writes", [])
        if not isinstance(raw_pending, list):
            self._snapshot_invalid("pending_writes must be a list")

        pending_writes: set[tuple[int, int]] = set()
        for item in raw_pending:
            if isinstance(item, dict):
                raw_epoch = item.get("epoch")
                raw_seq = item.get("seq")
            elif isinstance(item, (list, tuple)) and len(item) == 2:
                raw_epoch, raw_seq = item
            else:
                self._snapshot_invalid("pending_writes entry is invalid")

            epoch = self._parse_epoch_key(raw_epoch)
            if not self._valid_seq(raw_seq):
                self._snapshot_invalid("pending write sequence is invalid")
            pending_writes.add((epoch, raw_seq))

        raw_stats = state.get("stats", {key: 0 for key in STATS_KEYS})
        if not isinstance(raw_stats, dict):
            self._snapshot_invalid("stats must be a dict")

        stats = {key: 0 for key in STATS_KEYS}
        for key in STATS_KEYS:
            value = raw_stats.get(key, 0)
            if not _is_plain_int(value) or value < 0:
                self._snapshot_invalid(f"stats.{key} must be non-negative")
            stats[key] = value

        return {
            "started": started,
            "recv_epoch": recv_epoch,
            "send_epoch": send_epoch,
            "pending_send_epoch": pending_send_epoch,
            "update_count": update_count,
            "last_update_at": last_update_at,
            "retained": retained,
            "seen": seen,
            "send_last": send_last,
            "pending_writes": pending_writes,
            "stats": stats,
        }

    def restore(self, state: dict) -> None:
        with self._lock:
            restored = self._parse_snapshot(state)
            self._started = restored["started"]
            self._recv_epoch = restored["recv_epoch"]
            self._send_epoch = restored["send_epoch"]
            self._pending_send_epoch = restored["pending_send_epoch"]
            self._update_count = restored["update_count"]
            self._last_update_at = restored["last_update_at"]
            self._retained = restored["retained"]
            self._seen = restored["seen"]
            self._send_last = restored["send_last"]
            self._pending_writes = restored["pending_writes"]
            self._stats = restored["stats"]
