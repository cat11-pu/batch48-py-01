"""ESP extended sequence number anti-replay guard."""

from __future__ import annotations

import collections
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Optional


MAX_SEQ = (1 << 64) - 1
MAX_SPI = (1 << 32) - 1

_STAT_KEYS = (
    "accepted",
    "replayed",
    "too_old",
    "expired",
    "byte_limited",
    "exhausted",
)

_REPORT_REASONS = frozenset(("replay", "too_old"))

_DEFAULT_WINDOW = object()
_DEFAULT_SOFT_LIFETIME = object()


class EspError(ValueError):
    """Rejection raised by the ESP replay guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or isinstance(
        value, float
    )


def _finite_number(value: object) -> bool:
    return _is_number(value) and math.isfinite(float(value))


class _SAView(dict):
    """Snapshot mapping keyed by SA id, with ordered integer lookup."""

    def __getitem__(self, key):
        if isinstance(key, int) and not isinstance(key, bool) and key not in self:
            return tuple(self.values())[key]
        return super().__getitem__(key)

    def __eq__(self, other):
        if isinstance(other, list):
            return list(self.values()) == other
        return super().__eq__(other)

    def append(self, item):
        key = item.get("sa_id") if isinstance(item, dict) else None
        if not isinstance(key, str) or not key:
            key = len(self)
        self[key] = item


@dataclass
class _SAState:
    sa_id: str
    spi: int
    window_size: int
    capacity: int
    soft_lifetime: float
    hard_lifetime: float
    max_bytes: int
    created_at: float
    highest_seq: Optional[int] = None
    # Bit N represents sequence (highest_seq - N).  The retained capacity is
    # the largest window this SA has ever used so a shrink followed by a grow
    # cannot forget that a sequence was already seen.
    bitmap: int = 0
    accepted: int = 0
    replayed: int = 0
    too_old: int = 0
    bytes_used: int = 0
    report: collections.deque = field(default_factory=collections.deque)

    def note(self, seq: int, reason: str) -> None:
        self.report.append({"seq": seq, "reason": reason})


class EspReplayGuard:
    """Track per-SA 64 bit extended sequence numbers with a sliding bitmap."""

    def __init__(self, *, default_window: int = 64, max_report_records: int = 256,
                 clock=None) -> None:
        if not _is_int(default_window) or default_window < 1:
            raise EspError("ESP_INVALID_ARGUMENT")
        if not _is_int(max_report_records) or max_report_records < 0:
            raise EspError("ESP_INVALID_ARGUMENT")
        if clock is not None and not callable(clock):
            raise EspError("ESP_INVALID_ARGUMENT")

        self.default_window = default_window
        self.max_report_records = max_report_records
        self._clock = clock
        self._lock = threading.RLock()
        self._sas: dict[str, _SAState] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self, now):
        if now is not None:
            value = now
        elif self._clock is not None:
            value = self._clock()
        else:
            value = time.monotonic()
        if not _finite_number(value):
            raise EspError("ESP_INVALID_ARGUMENT")
        return float(value)

    def _get_sa(self, sa_id: str) -> _SAState:
        try:
            return self._sas[sa_id]
        except (KeyError, TypeError):
            raise EspError("ESP_UNKNOWN_SA") from None

    @staticmethod
    def _validate_window_size(window_size: int) -> None:
        if not _is_int(window_size) or window_size < 1:
            raise EspError("ESP_INVALID_ARGUMENT")

    @staticmethod
    def _mask(bits: int) -> int:
        return (1 << bits) - 1

    def _remove_locked(self, sa_id: str) -> bool:
        try:
            return self._sas.pop(sa_id, None) is not None
        except TypeError:
            return False

    @staticmethod
    def _snapshot_stats(stats: dict) -> dict:
        return {key: int(stats[key]) for key in _STAT_KEYS}

    def _snapshot_sa(self, sa: _SAState) -> dict:
        return {
            "sa_id": sa.sa_id,
            "spi": sa.spi,
            "window_size": sa.window_size,
            "capacity": sa.capacity,
            "soft_lifetime": sa.soft_lifetime,
            "hard_lifetime": sa.hard_lifetime,
            "max_bytes": sa.max_bytes,
            "created_at": sa.created_at,
            "highest_seq": sa.highest_seq,
            "bitmap": sa.bitmap,
            "accepted": sa.accepted,
            "replayed": sa.replayed,
            "too_old": sa.too_old,
            "bytes_used": sa.bytes_used,
            "report": [dict(record) for record in sa.report],
        }

    def _parse_snapshot_sa(self, item: object, report_limit: int) -> _SAState:
        if not isinstance(item, dict):
            raise EspError("ESP_SNAPSHOT_INVALID")

        try:
            sa_id = item["sa_id"]
            spi = item["spi"]
            window_size = item["window_size"]
            capacity = item["capacity"]
            soft_lifetime = item["soft_lifetime"]
            hard_lifetime = item["hard_lifetime"]
            max_bytes = item["max_bytes"]
            created_at = item["created_at"]
            highest_seq = item["highest_seq"]
            bitmap = item["bitmap"]
            accepted = item["accepted"]
            replayed = item["replayed"]
            too_old = item["too_old"]
            bytes_used = item["bytes_used"]
            report = item["report"]
        except (KeyError, TypeError):
            raise EspError("ESP_SNAPSHOT_INVALID") from None

        if not isinstance(sa_id, str) or not sa_id:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _is_int(spi) or not 0 <= spi <= MAX_SPI:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _is_int(window_size) or window_size < 1:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _is_int(capacity) or capacity < window_size:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _finite_number(soft_lifetime) or soft_lifetime <= 0:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _finite_number(hard_lifetime) or hard_lifetime <= 0:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if float(soft_lifetime) > float(hard_lifetime):
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _is_int(max_bytes) or max_bytes <= 0:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if not _finite_number(created_at):
            raise EspError("ESP_SNAPSHOT_INVALID")
        if highest_seq is not None:
            if not _is_int(highest_seq) or not 0 <= highest_seq <= MAX_SEQ:
                raise EspError("ESP_SNAPSHOT_INVALID")
        if not _is_int(bitmap) or bitmap < 0:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if bitmap and highest_seq is None:
            raise EspError("ESP_SNAPSHOT_INVALID")
        if bitmap:
            if bitmap.bit_length() > capacity:
                raise EspError("ESP_SNAPSHOT_INVALID")
            if bitmap.bit_length() - 1 > highest_seq:
                raise EspError("ESP_SNAPSHOT_INVALID")

        for value in (accepted, replayed, too_old, bytes_used):
            if not _is_int(value) or value < 0:
                raise EspError("ESP_SNAPSHOT_INVALID")
        if bytes_used > max_bytes:
            raise EspError("ESP_SNAPSHOT_INVALID")

        if not isinstance(report, list):
            raise EspError("ESP_SNAPSHOT_INVALID")
        if len(report) > report_limit:
            raise EspError("ESP_SNAPSHOT_INVALID")
        parsed_report = collections.deque(maxlen=report_limit)
        for record in report:
            if not isinstance(record, dict):
                raise EspError("ESP_SNAPSHOT_INVALID")
            seq = record.get("seq")
            reason = record.get("reason")
            if not _is_int(seq) or not 0 <= seq <= MAX_SEQ:
                raise EspError("ESP_SNAPSHOT_INVALID")
            if reason not in _REPORT_REASONS:
                raise EspError("ESP_SNAPSHOT_INVALID")
            parsed_report.append({"seq": seq, "reason": reason})

        return _SAState(
            sa_id=sa_id,
            spi=spi,
            window_size=window_size,
            capacity=capacity,
            soft_lifetime=float(soft_lifetime),
            hard_lifetime=float(hard_lifetime),
            max_bytes=max_bytes,
            created_at=float(created_at),
            highest_seq=highest_seq,
            bitmap=bitmap,
            accepted=accepted,
            replayed=replayed,
            too_old=too_old,
            bytes_used=bytes_used,
            report=parsed_report,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_sa(self, sa_id: str, *, spi: int, window_size: int = _DEFAULT_WINDOW,
               soft_lifetime: float = _DEFAULT_SOFT_LIFETIME,
               hard_lifetime: float = 7200.0,
               max_bytes: int = 1 << 30, now=None) -> dict:
        with self._lock:
            if not isinstance(sa_id, str) or not sa_id:
                raise EspError("ESP_INVALID_ARGUMENT")
            if sa_id in self._sas:
                raise EspError("ESP_DUPLICATE_SA")

            if window_size is _DEFAULT_WINDOW or window_size is None:
                window_size = self.default_window
            self._validate_window_size(window_size)
            soft_was_default = soft_lifetime is _DEFAULT_SOFT_LIFETIME
            if soft_was_default:
                soft_lifetime = 3600.0
            if not _is_int(spi) or not 0 <= spi <= MAX_SPI:
                raise EspError("ESP_INVALID_ARGUMENT")
            if not _finite_number(soft_lifetime) or soft_lifetime <= 0:
                raise EspError("ESP_INVALID_ARGUMENT")
            if not _finite_number(hard_lifetime) or hard_lifetime <= 0:
                raise EspError("ESP_INVALID_ARGUMENT")
            if float(soft_lifetime) > float(hard_lifetime):
                if not soft_was_default:
                    raise EspError("ESP_INVALID_ARGUMENT")
                soft_lifetime = hard_lifetime
            if not _is_int(max_bytes) or max_bytes <= 0:
                raise EspError("ESP_INVALID_ARGUMENT")

            created_at = self._now(now)
            sa = _SAState(
                sa_id=sa_id,
                spi=spi,
                window_size=window_size,
                capacity=window_size,
                soft_lifetime=float(soft_lifetime),
                hard_lifetime=float(hard_lifetime),
                max_bytes=max_bytes,
                created_at=created_at,
                report=collections.deque(maxlen=self.max_report_records),
            )
            self._sas[sa_id] = sa
            return {
                "sa_id": sa.sa_id,
                "spi": sa.spi,
                "window_size": sa.window_size,
                "soft_lifetime": sa.soft_lifetime,
                "hard_lifetime": sa.hard_lifetime,
                "max_bytes": sa.max_bytes,
                "created_at": sa.created_at,
            }

    def remove_sa(self, sa_id: str) -> bool:
        with self._lock:
            return self._remove_locked(sa_id)

    def accept(self, sa_id: str, seq: int, *, length: int = 0, now=None) -> bool:
        with self._lock:
            sa = self._get_sa(sa_id)

            if not _is_int(seq):
                raise EspError("ESP_INVALID_SEQ")
            if seq < 0 or seq > MAX_SEQ:
                raise EspError("ESP_INVALID_SEQ")
            if not _is_int(length) or length < 0:
                raise EspError("ESP_INVALID_ARGUMENT")

            current = self._now(now)
            if current >= sa.created_at + sa.hard_lifetime:
                self._stats["expired"] += 1
                self._remove_locked(sa_id)
                raise EspError("ESP_SA_EXPIRED")
            if current >= sa.created_at + sa.soft_lifetime:
                self._stats["expired"] += 1
                raise EspError("ESP_SA_EXPIRED")

            if sa.highest_seq == MAX_SEQ:
                self._stats["exhausted"] += 1
                raise EspError("ESP_SEQ_EXHAUSTED")

            highest = sa.highest_seq
            if highest is not None:
                offset = highest - seq
                if offset >= 0:
                    if offset >= sa.window_size:
                        sa.too_old += 1
                        self._stats["too_old"] += 1
                        sa.note(seq, "too_old")
                        return False
                    if (sa.bitmap >> offset) & 1:
                        sa.replayed += 1
                        self._stats["replayed"] += 1
                        sa.note(seq, "replay")
                        return False
                else:
                    diff = -offset
                    if diff >= sa.capacity:
                        sa.bitmap = 1
                    else:
                        sa.bitmap = (
                            (sa.bitmap << diff) | 1
                        ) & self._mask(sa.capacity)
                    sa.highest_seq = seq
            else:
                sa.highest_seq = seq
                sa.bitmap = 1

            # A replay or an out-of-window packet is rejected above and must
            # not consume the byte budget.  Only a packet that would otherwise
            # be accepted can push the SA over its byte limit.
            if sa.bytes_used + length > sa.max_bytes:
                self._stats["byte_limited"] += 1
                self._remove_locked(sa_id)
                raise EspError("ESP_SA_BYTE_LIMIT")

            if highest is not None and seq < highest:
                sa.bitmap |= 1 << (highest - seq)
            sa.bytes_used += length
            sa.accepted += 1
            self._stats["accepted"] += 1
            return True

    def set_window_size(self, sa_id: str, window_size: int) -> None:
        with self._lock:
            sa = self._get_sa(sa_id)
            self._validate_window_size(window_size)
            if window_size > sa.capacity:
                sa.capacity = window_size
            sa.window_size = window_size

    def sa_state(self, sa_id: str) -> dict:
        with self._lock:
            sa = self._get_sa(sa_id)
            return {
                "spi": sa.spi,
                "window_size": sa.window_size,
                "highest_seq": 0 if sa.highest_seq is None else sa.highest_seq,
                "accepted": sa.accepted,
                "replayed": sa.replayed,
                "too_old": sa.too_old,
                "bytes_used": sa.bytes_used,
            }

    def list_sas(self) -> list:
        with self._lock:
            return list(self._sas)

    def replay_report(self, sa_id: str) -> list:
        with self._lock:
            sa = self._get_sa(sa_id)
            return [dict(record) for record in sa.report]

    def snapshot(self) -> dict:
        with self._lock:
            sas = _SAView()
            for sa in self._sas.values():
                sas[sa.sa_id] = self._snapshot_sa(sa)
            return {
                "version": 1,
                "default_window": self.default_window,
                "max_report_records": self.max_report_records,
                "stats": self._snapshot_stats(self._stats),
                "sas": sas,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                if not isinstance(state, dict) or state.get("version") != 1:
                    raise EspError("ESP_SNAPSHOT_INVALID")

                stats = state["stats"]
                items = state["sas"]
                if not isinstance(stats, dict) or not isinstance(items, (dict, list)):
                    raise EspError("ESP_SNAPSHOT_INVALID")

                parsed_stats = {}
                for key in _STAT_KEYS:
                    value = stats[key]
                    if not _is_int(value) or value < 0:
                        raise EspError("ESP_SNAPSHOT_INVALID")
                    parsed_stats[key] = value

                default_window = state.get("default_window", self.default_window)
                max_report_records = state.get(
                    "max_report_records", self.max_report_records
                )
                if not _is_int(default_window) or default_window < 1:
                    raise EspError("ESP_SNAPSHOT_INVALID")
                if not _is_int(max_report_records) or max_report_records < 0:
                    raise EspError("ESP_SNAPSHOT_INVALID")

                parsed_sas: dict[str, _SAState] = {}
                if isinstance(items, dict):
                    pairs = items.items()
                else:
                    pairs = ((None, item) for item in items)
                for key, item in pairs:
                    sa = self._parse_snapshot_sa(item, max_report_records)
                    if key is not None and key != sa.sa_id:
                        raise EspError("ESP_SNAPSHOT_INVALID")
                    if sa.sa_id in parsed_sas:
                        raise EspError("ESP_SNAPSHOT_INVALID")
                    parsed_sas[sa.sa_id] = sa
            except EspError as exc:
                if exc.code == "ESP_SNAPSHOT_INVALID":
                    raise
                raise EspError("ESP_SNAPSHOT_INVALID") from None
            except Exception:
                raise EspError("ESP_SNAPSHOT_INVALID") from None

            # Commit only after the entire snapshot has been validated.
            self.default_window = default_window
            self.max_report_records = max_report_records
            self._stats = parsed_stats
            self._sas = parsed_sas

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._stats[key]) for key in _STAT_KEYS}
