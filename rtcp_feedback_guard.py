"""Receiver-side RTCP feedback accounting and report block generation."""

from __future__ import annotations

import threading
import time
from typing import Any


class RtcpFeedbackError(ValueError):
    """Rejection raised by the feedback guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


DEFAULT_MAX_REPORT_BLOCKS = 31
DEFAULT_MAX_STREAMS = 32
_SEQ_MODULUS = 1 << 16
_SEQ_HALF = 1 << 15
_UINT32_MODULUS = 1 << 32
_SIGNED_24_MODULUS = 1 << 24
_SIGNED_24_LIMIT = 1 << 23
_SNAPSHOT_VERSION = 1


class _StreamState:
    """Mutable per-SSRC receive state.

    ``received_ext`` also retains packets older than the stream's initial
    extended sequence number.  They are useful for duplicate detection, but
    only entries within the current loss window affect cumulative loss.
    """

    __slots__ = (
        "base_ext",
        "highest_ext",
        "received_ext",
        "received_in_window",
        "last_arrival_ms",
        "last_packet_arrival_ms",
        "previous_interval_ms",
        "jitter",
        "last_report_seq",
        "interval_received",
        "sender_report",
        "last_activity_ms",
        "activity_serial",
    )

    def __init__(self) -> None:
        self.base_ext: int | None = None
        self.highest_ext: int | None = None
        self.received_ext: set[int] = set()
        self.received_in_window = 0
        self.last_arrival_ms: int | None = None
        self.last_packet_arrival_ms: int | None = None
        self.previous_interval_ms: int | None = None
        self.jitter = 0
        self.last_report_seq: int | None = None
        self.interval_received: set[int] = set()
        self.sender_report: dict[str, int] | None = None
        self.last_activity_ms: int | None = None
        self.activity_serial = 0


class RtcpFeedbackGuard:
    """Track per-SSRC loss, jitter and sender report timing."""

    def __init__(self, *, max_report_blocks: int = DEFAULT_MAX_REPORT_BLOCKS,
                 max_streams: int = DEFAULT_MAX_STREAMS,
                 jitter_scale: int = 16, clock=None) -> None:
        if (not self._is_int(max_report_blocks)
                or max_report_blocks < 0):
            self._error("RTCP_CONFIG_INVALID",
                        {"max_report_blocks": max_report_blocks})
        if not self._is_int(max_streams) or max_streams < 0:
            self._error("RTCP_CONFIG_INVALID",
                        {"max_streams": max_streams})
        if not self._is_int(jitter_scale) or jitter_scale <= 0:
            self._error("RTCP_CONFIG_INVALID",
                        {"jitter_scale": jitter_scale})
        if clock is not None and not callable(clock):
            self._error("RTCP_CONFIG_INVALID", {"clock": clock})

        self.max_report_blocks = max_report_blocks
        self.max_streams = max_streams
        self.jitter_scale = jitter_scale
        self._clock = clock
        self._lock = threading.RLock()
        self._streams: dict[int, _StreamState] = {}
        self._activity_serial = 0
        self._stats = self._empty_stats()

    @staticmethod
    def _empty_stats() -> dict[str, int]:
        return {
            "packets": 0,
            "duplicates": 0,
            "bytes": 0,
            "sender_reports": 0,
            "report_blocks": 0,
            "report_builds": 0,
        }

    @staticmethod
    def _error(code: str, detail: object) -> None:
        raise RtcpFeedbackError(code, detail)

    @staticmethod
    def _is_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def _validate_ssrc(self, ssrc: object) -> int:
        if (not self._is_int(ssrc) or ssrc < 0
                or ssrc >= _UINT32_MODULUS):
            self._error("RTCP_SSRC_INVALID", ssrc)
        return int(ssrc)

    def _validate_seq(self, seq: object) -> int:
        if (not self._is_int(seq) or seq < 0
                or seq >= _SEQ_MODULUS):
            self._error("RTCP_SEQ_INVALID", seq)
        return int(seq)

    @staticmethod
    def _validate_ms(value: object) -> int:
        if (not isinstance(value, (int, float))
                or isinstance(value, bool)):
            raise RtcpFeedbackError("RTCP_ARRIVAL_INVALID", value)
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                raise RtcpFeedbackError("RTCP_ARRIVAL_INVALID", value)
        return int(value)

    def _now_ms(self) -> int:
        if self._clock is not None:
            return self._validate_ms(self._clock())
        return int(time.monotonic() * 1000)

    def _touch(self, state: _StreamState, at_ms: int) -> None:
        self._activity_serial += 1
        state.last_activity_ms = at_ms
        state.activity_serial = self._activity_serial

    def _known_stream(self, ssrc: object) -> _StreamState:
        validated = self._validate_ssrc(ssrc)
        state = self._streams.get(validated)
        if state is None:
            self._error("RTCP_UNKNOWN_SSRC", validated)
        return state

    @staticmethod
    def _extend_seq(seq: int, highest_ext: int | None) -> int:
        if highest_ext is None:
            return seq
        highest_seq = highest_ext & 0xFFFF
        delta = (seq - highest_seq) & 0xFFFF
        if delta < _SEQ_HALF:
            return highest_ext + delta
        return highest_ext - (_SEQ_MODULUS - delta)

    @staticmethod
    def _signed24(value: int) -> int:
        value &= _SIGNED_24_MODULUS - 1
        if value >= _SIGNED_24_LIMIT:
            value -= _SIGNED_24_MODULUS
        return value

    @staticmethod
    def _raw_cumulative_lost(state: _StreamState) -> int:
        if state.base_ext is None or state.highest_ext is None:
            return 0
        expected = state.highest_ext - state.base_ext + 1
        return max(0, expected - state.received_in_window)

    @staticmethod
    def _raw_fraction_lost(state: _StreamState) -> int:
        if state.last_report_seq is None or state.highest_ext is None:
            return 0
        expected = state.highest_ext - state.last_report_seq
        if expected <= 0:
            return 0
        received = len(state.interval_received)
        lost = max(0, expected - received)
        return min(255, (lost * 256) // expected)

    @staticmethod
    def _lsr(ntp_seconds: int, ntp_fraction: int) -> int:
        return (((ntp_seconds & 0xFFFF) << 16)
                | ((ntp_fraction >> 16) & 0xFFFF))

    @staticmethod
    def _report_state_locked(ssrc: int, state: _StreamState,
                             now_ms: int) -> dict[str, Any]:
        fraction = RtcpFeedbackGuard._raw_fraction_lost(state)
        cumulative = RtcpFeedbackGuard._signed24(
            RtcpFeedbackGuard._raw_cumulative_lost(state))
        highest = 0 if state.highest_ext is None else state.highest_ext

        sender_report = state.sender_report
        if sender_report is None:
            lsr = 0
            dlsr = 0
        else:
            lsr = sender_report["lsr"]
            delay_ms = max(0, now_ms - sender_report["arrival_ms"])
            dlsr = (delay_ms * 0x10000) // 1000

        block = {
            "ssrc": ssrc,
            "fraction_lost": fraction,
            "cumulative_lost": cumulative,
            "highest_seq": highest,
            "jitter": int(state.jitter),
            "lsr": int(lsr),
            "dlsr": int(dlsr),
        }

        if state.highest_ext is not None:
            state.last_report_seq = state.highest_ext
        state.interval_received.clear()
        return block

    def on_packet(self, ssrc: int, seq: int, *, arrival_ms: int | None = None,
                  payload_size: int = 1000) -> dict:
        ssrc = self._validate_ssrc(ssrc)
        seq = self._validate_seq(seq)
        if not self._is_int(payload_size) or payload_size < 0:
            self._error("RTCP_PAYLOAD_INVALID", payload_size)
        arrival = (self._now_ms() if arrival_ms is None
                   else self._validate_ms(arrival_ms))

        with self._lock:
            state = self._streams.get(ssrc)
            if state is None:
                if len(self._streams) >= self.max_streams:
                    self._error(
                        "RTCP_TOO_MANY_STREAMS",
                        {"ssrc": ssrc, "max_streams": self.max_streams},
                    )
                state = _StreamState()
                self._streams[ssrc] = state

            if (state.last_arrival_ms is not None
                    and arrival < state.last_arrival_ms):
                self._error(
                    "RTCP_ARRIVAL_INVALID",
                    {"ssrc": ssrc, "arrival_ms": arrival,
                     "last_arrival_ms": state.last_arrival_ms},
                )

            extended = self._extend_seq(seq, state.highest_ext)
            duplicate = extended in state.received_ext

            state.last_arrival_ms = arrival
            if state.last_packet_arrival_ms is not None:
                interval = arrival - state.last_packet_arrival_ms
                if state.previous_interval_ms is not None:
                    delta = abs(interval - state.previous_interval_ms)
                    state.jitter += (
                        (delta - state.jitter) // self.jitter_scale
                    )
                state.previous_interval_ms = interval
            state.last_packet_arrival_ms = arrival

            if not duplicate:
                state.received_ext.add(extended)
                if state.base_ext is None:
                    state.base_ext = extended
                    state.highest_ext = extended
                    state.last_report_seq = extended - 1
                    state.interval_received.add(extended)
                    state.received_in_window = 1
                else:
                    if extended > state.highest_ext:
                        state.highest_ext = extended
                    if state.base_ext <= extended <= state.highest_ext:
                        state.received_in_window += 1
                    if (state.last_report_seq is not None
                            and extended > state.last_report_seq):
                        state.interval_received.add(extended)

            self._touch(state, arrival)
            self._stats["packets"] += 1
            self._stats["bytes"] += payload_size
            if duplicate:
                self._stats["duplicates"] += 1

            return {
                "ssrc": ssrc,
                "extended_seq": extended,
                "duplicate": duplicate,
                "received": state.received_in_window,
                "cumulative_lost": self._signed24(
                    self._raw_cumulative_lost(state)),
            }

    def on_sender_report(self, ssrc: int, *, ntp_seconds: int,
                         ntp_fraction: int,
                         arrival_ms: int | None = None) -> dict:
        ssrc = self._validate_ssrc(ssrc)
        if (not self._is_int(ntp_seconds) or ntp_seconds < 0
                or ntp_seconds >= _UINT32_MODULUS):
            self._error("RTCP_NTP_INVALID", {"ntp_seconds": ntp_seconds})
        if (not self._is_int(ntp_fraction) or ntp_fraction < 0
                or ntp_fraction >= _UINT32_MODULUS):
            self._error("RTCP_NTP_INVALID", {"ntp_fraction": ntp_fraction})
        arrival = (self._now_ms() if arrival_ms is None
                   else self._validate_ms(arrival_ms))
        lsr = self._lsr(ntp_seconds, ntp_fraction)

        with self._lock:
            state = self._streams.get(ssrc)
            if state is None:
                if len(self._streams) >= self.max_streams:
                    self._error(
                        "RTCP_TOO_MANY_STREAMS",
                        {"ssrc": ssrc, "max_streams": self.max_streams},
                    )
                state = _StreamState()
                self._streams[ssrc] = state

            if (state.last_arrival_ms is not None
                    and arrival < state.last_arrival_ms):
                self._error(
                    "RTCP_ARRIVAL_INVALID",
                    {"ssrc": ssrc, "arrival_ms": arrival,
                     "last_arrival_ms": state.last_arrival_ms},
                )

            state.last_arrival_ms = arrival
            state.sender_report = {
                "lsr": lsr,
                "arrival_ms": arrival,
            }
            self._touch(state, arrival)
            self._stats["sender_reports"] += 1
            return {
                "ssrc": ssrc,
                "lsr": lsr,
                "dlsr": 0,
            }

    def highest_seq(self, ssrc: int) -> int:
        with self._lock:
            state = self._known_stream(ssrc)
            return 0 if state.highest_ext is None else state.highest_ext

    def cumulative_lost(self, ssrc: int) -> int:
        with self._lock:
            state = self._known_stream(ssrc)
            return self._signed24(self._raw_cumulative_lost(state))

    def fraction_lost(self, ssrc: int) -> int:
        with self._lock:
            state = self._known_stream(ssrc)
            return self._raw_fraction_lost(state)

    def interarrival_jitter(self, ssrc: int) -> int:
        with self._lock:
            state = self._known_stream(ssrc)
            return int(state.jitter)

    def report_block(self, ssrc: int, *, now_ms: int | None = None) -> dict:
        with self._lock:
            state = self._known_stream(ssrc)
            now = (self._now_ms() if now_ms is None
                   else self._validate_ms(now_ms))
            block = self._report_state_locked(ssrc, state, now)
            self._stats["report_blocks"] += 1
            return block

    def build_report(self, *, reporter_ssrc: int = 0) -> dict:
        reporter_ssrc = self._validate_ssrc(reporter_ssrc)
        with self._lock:
            now = self._now_ms()
            ranked = sorted(
                self._streams.items(),
                key=lambda item: (
                    -1 if item[1].last_activity_ms is None
                    else item[1].last_activity_ms,
                    item[1].activity_serial,
                ),
                reverse=True,
            )
            selected = ranked[:self.max_report_blocks]
            selected.sort(key=lambda item: item[0])
            blocks = [
                self._report_state_locked(ssrc, state, now)
                for ssrc, state in selected
            ]
            self._stats["report_blocks"] += len(blocks)
            self._stats["report_builds"] += 1
            return {
                "reporter_ssrc": reporter_ssrc,
                "blocks": blocks,
                "omitted": len(self._streams) - len(blocks),
            }

    def restart(self) -> None:
        with self._lock:
            for state in self._streams.values():
                if state.highest_ext is not None:
                    state.last_report_seq = state.highest_ext
                else:
                    state.last_report_seq = None
                state.interval_received.clear()
                state.sender_report = None

    def ssrcs(self) -> list[int]:
        with self._lock:
            return sorted(self._streams)

    def snapshot(self) -> dict:
        with self._lock:
            streams: dict[int, dict[str, Any]] = {}
            for ssrc, state in sorted(self._streams.items()):
                streams[ssrc] = {
                    "base_ext": state.base_ext,
                    "highest_ext": state.highest_ext,
                    "received_ext": sorted(state.received_ext),
                    "received_in_window": state.received_in_window,
                    "last_arrival_ms": state.last_arrival_ms,
                    "last_packet_arrival_ms": state.last_packet_arrival_ms,
                    "previous_interval_ms": state.previous_interval_ms,
                    "jitter": state.jitter,
                    "last_report_seq": state.last_report_seq,
                    "interval_received": sorted(state.interval_received),
                    "sender_report": (None if state.sender_report is None
                                      else dict(state.sender_report)),
                    "last_activity_ms": state.last_activity_ms,
                    "activity_serial": state.activity_serial,
                }
            return {
                "version": _SNAPSHOT_VERSION,
                "streams": streams,
                "stats": dict(self._stats),
                "activity_serial": self._activity_serial,
            }

    @staticmethod
    def _snapshot_invalid(detail: object) -> None:
        raise RtcpFeedbackError("RTCP_SNAPSHOT_INVALID", detail)

    @classmethod
    def _snapshot_int(cls, value: object, field: str,
                      *, minimum: int | None = None) -> int:
        if not cls._is_int(value):
            cls._snapshot_invalid(field)
        if minimum is not None and value < minimum:
            cls._snapshot_invalid(field)
        return int(value)

    @classmethod
    def _snapshot_optional_int(cls, value: object, field: str,
                               *, minimum: int | None = None) -> int | None:
        if value is None:
            return None
        return cls._snapshot_int(value, field, minimum=minimum)

    @classmethod
    def _snapshot_int_list(cls, value: object,
                           field: str) -> set[int]:
        if not isinstance(value, list):
            cls._snapshot_invalid(field)
        result: set[int] = set()
        for item in value:
            result.add(cls._snapshot_int(item, field))
        return result

    def _validate_snapshot_ssrc(self, ssrc: object) -> int:
        if (not self._is_int(ssrc) or ssrc < 0
                or ssrc >= _UINT32_MODULUS):
            self._snapshot_invalid("streams.ssrc")
        return int(ssrc)

    def _restore_streams(self, raw_streams: object) -> dict[int, _StreamState]:
        if not isinstance(raw_streams, dict):
            self._snapshot_invalid("streams")
        if len(raw_streams) > self.max_streams:
            self._snapshot_invalid("max_streams")

        streams: dict[int, _StreamState] = {}
        required = {
            "base_ext",
            "highest_ext",
            "received_ext",
            "received_in_window",
            "last_arrival_ms",
            "last_packet_arrival_ms",
            "previous_interval_ms",
            "jitter",
            "last_report_seq",
            "interval_received",
            "sender_report",
            "last_activity_ms",
            "activity_serial",
        }
        for raw_ssrc, raw_state in raw_streams.items():
            ssrc = self._validate_snapshot_ssrc(raw_ssrc)
            if not isinstance(raw_state, dict):
                self._snapshot_invalid(f"streams.{ssrc}")
            if not required.issubset(raw_state):
                self._snapshot_invalid(f"streams.{ssrc}.fields")

            base_ext = self._snapshot_optional_int(
                raw_state["base_ext"], f"streams.{ssrc}.base_ext")
            highest_ext = self._snapshot_optional_int(
                raw_state["highest_ext"], f"streams.{ssrc}.highest_ext")
            if (base_ext is None) != (highest_ext is None):
                self._snapshot_invalid(f"streams.{ssrc}.window")
            if (base_ext is not None
                    and highest_ext is not None
                    and base_ext > highest_ext):
                self._snapshot_invalid(f"streams.{ssrc}.window")

            received_ext = self._snapshot_int_list(
                raw_state["received_ext"], f"streams.{ssrc}.received_ext")
            interval_received = self._snapshot_int_list(
                raw_state["interval_received"],
                f"streams.{ssrc}.interval_received",
            )
            received_in_window = self._snapshot_int(
                raw_state["received_in_window"],
                f"streams.{ssrc}.received_in_window", minimum=0)

            last_report_seq = self._snapshot_optional_int(
                raw_state["last_report_seq"],
                f"streams.{ssrc}.last_report_seq",
            )
            last_activity_ms = self._snapshot_optional_int(
                raw_state["last_activity_ms"],
                f"streams.{ssrc}.last_activity_ms",
            )
            last_arrival_ms = self._snapshot_optional_int(
                raw_state["last_arrival_ms"],
                f"streams.{ssrc}.last_arrival_ms",
            )
            last_packet_arrival_ms = self._snapshot_optional_int(
                raw_state["last_packet_arrival_ms"],
                f"streams.{ssrc}.last_packet_arrival_ms",
            )
            previous_interval_ms = self._snapshot_optional_int(
                raw_state["previous_interval_ms"],
                f"streams.{ssrc}.previous_interval_ms",
            )
            jitter = self._snapshot_int(
                raw_state["jitter"], f"streams.{ssrc}.jitter", minimum=0)
            activity_serial = self._snapshot_int(
                raw_state["activity_serial"],
                f"streams.{ssrc}.activity_serial", minimum=0)

            raw_sender = raw_state["sender_report"]
            if raw_sender is None:
                sender_report = None
            elif isinstance(raw_sender, dict):
                if not {"lsr", "arrival_ms"}.issubset(raw_sender):
                    self._snapshot_invalid(
                        f"streams.{ssrc}.sender_report")
                lsr = self._snapshot_int(
                    raw_sender["lsr"],
                    f"streams.{ssrc}.sender_report.lsr", minimum=0)
                if lsr >= _UINT32_MODULUS:
                    self._snapshot_invalid(
                        f"streams.{ssrc}.sender_report.lsr")
                sender_arrival = self._snapshot_int(
                    raw_sender["arrival_ms"],
                    f"streams.{ssrc}.sender_report.arrival_ms",
                )
                sender_report = {"lsr": lsr, "arrival_ms": sender_arrival}
            else:
                self._snapshot_invalid(f"streams.{ssrc}.sender_report")

            if base_ext is None:
                expected_received = 0
                if received_ext or interval_received:
                    self._snapshot_invalid(f"streams.{ssrc}.window")
            else:
                expected_received = sum(
                    1 for value in received_ext
                    if base_ext <= value <= highest_ext
                )
            if received_in_window != expected_received:
                self._snapshot_invalid(
                    f"streams.{ssrc}.received_in_window")
            if last_report_seq is not None:
                if (highest_ext is not None
                        and last_report_seq > highest_ext):
                    self._snapshot_invalid(
                        f"streams.{ssrc}.last_report_seq")
                if any(value <= last_report_seq
                       for value in interval_received):
                    self._snapshot_invalid(
                        f"streams.{ssrc}.interval_received")
            elif interval_received:
                self._snapshot_invalid(
                    f"streams.{ssrc}.interval_received")

            state = _StreamState()
            state.base_ext = base_ext
            state.highest_ext = highest_ext
            state.received_ext = received_ext
            state.received_in_window = received_in_window
            state.last_arrival_ms = last_arrival_ms
            state.last_packet_arrival_ms = last_packet_arrival_ms
            state.previous_interval_ms = previous_interval_ms
            state.jitter = jitter
            state.last_report_seq = last_report_seq
            state.interval_received = interval_received
            state.sender_report = sender_report
            state.last_activity_ms = last_activity_ms
            state.activity_serial = activity_serial
            streams[ssrc] = state
        return streams

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            self._snapshot_invalid("snapshot")
        if state.get("version") != _SNAPSHOT_VERSION:
            self._snapshot_invalid("version")
        if "streams" not in state or "stats" not in state:
            self._snapshot_invalid("fields")

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict):
            self._snapshot_invalid("stats")
        expected_stat_names = set(self._empty_stats())
        if set(raw_stats) != expected_stat_names:
            self._snapshot_invalid("stats.fields")
        stats = {
            name: self._snapshot_int(
                raw_stats[name], f"stats.{name}", minimum=0)
            for name in expected_stat_names
        }

        activity_serial = self._snapshot_int(
            state.get("activity_serial"), "activity_serial", minimum=0)
        streams = self._restore_streams(state["streams"])
        if any(item.activity_serial > activity_serial
               for item in streams.values()):
            self._snapshot_invalid("activity_serial")

        with self._lock:
            self._streams = streams
            self._stats = stats
            self._activity_serial = activity_serial

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = self._empty_stats()
