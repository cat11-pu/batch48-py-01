"""Fragmented tunnel message reassembly guard."""

from __future__ import annotations

import threading
import time


FRAG_INVALID_FRAGMENT = "FRAG_INVALID_FRAGMENT"
FRAG_INDEX_OUT_OF_RANGE = "FRAG_INDEX_OUT_OF_RANGE"
FRAG_INCONSISTENT = "FRAG_INCONSISTENT"
FRAG_LENGTH_MISMATCH = "FRAG_LENGTH_MISMATCH"
FRAG_STREAM_LIMIT = "FRAG_STREAM_LIMIT"
FRAG_SNAPSHOT_INVALID = "FRAG_SNAPSHOT_INVALID"

_STAT_KEYS = (
    "delivered",
    "timeout_dropped",
    "closed_dropped",
    "duplicate_ignored",
    "rejected",
)


class FragmentError(ValueError):
    """Rejection raised by the fragment guard."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _PendingMessage:
    """Reassembly state of a single message identified by (msg_id, epoch)."""

    __slots__ = ("msg_id", "epoch", "count", "total_len", "first_seen",
                 "chunks", "received_bytes", "completed")

    def __init__(self, msg_id: int, epoch: int, count: int,
                 total_len: int, first_seen: float) -> None:
        self.msg_id = msg_id
        self.epoch = epoch
        self.count = count
        self.total_len = total_len
        self.first_seen = first_seen
        self.chunks: dict[int, bytes] = {}
        self.received_bytes = 0
        self.completed = False

    @property
    def key(self) -> tuple[int, int]:
        return self.msg_id, self.epoch

    def missing(self) -> list[int]:
        return [index for index in range(self.count)
                if index not in self.chunks]

    def is_complete(self) -> bool:
        return len(self.chunks) == self.count

    def assemble(self) -> bytes:
        return b"".join(self.chunks[index]
                        for index in range(self.count))


class _StreamState:
    """Per-stream aggregate budgets and pending messages."""

    __slots__ = ("inflight", "received_bytes", "reserved_bytes",
                 "fragments", "reserved_fragments", "messages", "order")

    def __init__(self) -> None:
        self.inflight = 0
        self.received_bytes = 0
        self.reserved_bytes = 0
        self.fragments = 0
        self.reserved_fragments = 0
        self.messages: dict[tuple[int, int], _PendingMessage] = {}
        self.order: list[tuple[int, int]] = []


class _SnapshotInvalid(Exception):
    """Internal signal used while validating a snapshot document."""


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class MessageFragmentGuard:
    """Reassemble tunnel messages from fragments with bounded per-stream state."""

    def __init__(self, *, max_inflight_per_stream: int = 4,
                 max_bytes_per_stream: int = 65536,
                 max_fragments_per_stream: int = 64,
                 reassembly_timeout: float = 5.0, clock=None) -> None:
        self.max_inflight_per_stream = max_inflight_per_stream
        self.max_bytes_per_stream = max_bytes_per_stream
        self.max_fragments_per_stream = max_fragments_per_stream
        self.reassembly_timeout = reassembly_timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._streams: dict[str, _StreamState] = {}
        self._stats: dict[str, int] = {key: 0 for key in _STAT_KEYS}

    def _now(self, now) -> float:
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    @staticmethod
    def _validate_arguments(stream, msg_id, epoch, index, count,
                            total_len, data) -> None:
        if (not isinstance(stream, str) or stream == ""
                or not _is_int(msg_id) or msg_id < 0
                or not _is_int(epoch) or epoch < 0
                or not _is_int(count) or count < 1
                or not _is_int(index) or index < 0
                or not _is_int(total_len) or total_len < 0
                or not isinstance(data, bytes)):
            raise FragmentError(FRAG_INVALID_FRAGMENT)

    def _get_stream(self, stream: str) -> _StreamState:
        state = self._streams.get(stream)
        if state is None:
            state = _StreamState()
            self._streams[stream] = state
        return state

    def _release(self, state: _StreamState,
                 message: _PendingMessage) -> None:
        state.inflight -= 1
        state.reserved_bytes -= message.total_len
        state.reserved_fragments -= message.count
        state.fragments -= len(message.chunks)
        state.received_bytes -= message.received_bytes
        del state.messages[message.key]
        state.order.remove(message.key)

    def _prune_stream(self, stream: str, state: _StreamState) -> None:
        if not state.order:
            state.messages.clear()
            self._streams.pop(stream, None)

    def _reject(self, state: _StreamState, message: _PendingMessage,
                code: str) -> None:
        self._release(state, message)
        self._stats["rejected"] += 1
        raise FragmentError(code)

    def on_fragment(self, stream: str, msg_id: int, epoch: int, index: int,
                    count: int, total_len: int, data: bytes,
                    now=None) -> bytes | None:
        self._validate_arguments(stream, msg_id, epoch, index, count,
                                 total_len, data)
        arrival = self._now(now)
        with self._lock:
            state = self._get_stream(stream)
            message = state.messages.get((msg_id, epoch))

            if message is not None:
                if message.completed:
                    self._stats["duplicate_ignored"] += 1
                    return None
                if (message.count != count
                        or message.total_len != total_len):
                    self._reject(state, message, FRAG_INCONSISTENT)
                if index >= message.count:
                    self._reject(state, message,
                                 FRAG_INDEX_OUT_OF_RANGE)
                if index in message.chunks:
                    self._stats["duplicate_ignored"] += 1
                    return None
            else:
                if (state.inflight + 1 > self.max_inflight_per_stream
                        or state.reserved_bytes + total_len
                        > self.max_bytes_per_stream
                        or state.reserved_fragments + count
                        > self.max_fragments_per_stream):
                    raise FragmentError(FRAG_STREAM_LIMIT)
                message = _PendingMessage(msg_id, epoch, count,
                                          total_len, arrival)
                state.messages[message.key] = message
                state.order.append(message.key)
                state.inflight += 1
                state.reserved_bytes += total_len
                state.reserved_fragments += count

            message.chunks[index] = bytes(data)
            message.received_bytes += len(data)
            state.received_bytes += len(data)
            state.fragments += 1

            if not message.is_complete():
                return None

            payload = message.assemble()
            if len(payload) != total_len:
                self._reject(state, message, FRAG_LENGTH_MISMATCH)
            state.inflight -= 1
            state.reserved_bytes -= message.total_len
            state.reserved_fragments -= message.count
            state.fragments -= message.count
            state.received_bytes -= message.received_bytes
            state.order.remove(message.key)
            message.completed = True
            self._stats["delivered"] += 1
            return payload

    def pending_messages(self, stream: str) -> list:
        with self._lock:
            state = self._streams.get(stream)
            if state is None:
                return []
            return [
                {
                    "msg_id": message.msg_id,
                    "epoch": message.epoch,
                    "count": message.count,
                    "total_len": message.total_len,
                    "have": sorted(message.chunks),
                    "received_bytes": message.received_bytes,
                }
                for key in state.order
                for message in (state.messages[key],)
            ]

    def stream_state(self, stream: str) -> dict:
        with self._lock:
            state = self._streams.get(stream)
            if state is None:
                return {
                    "inflight": 0,
                    "received_bytes": 0,
                    "reserved_bytes": 0,
                    "fragments": 0,
                }
            return {
                "inflight": state.inflight,
                "received_bytes": state.received_bytes,
                "reserved_bytes": state.reserved_bytes,
                "fragments": state.fragments,
            }

    def expire(self, now) -> list:
        with self._lock:
            expired: list[dict] = []
            for stream in list(self._streams):
                state = self._streams[stream]
                for key in list(state.order):
                    message = state.messages[key]
                    if now - message.first_seen >= self.reassembly_timeout:
                        expired.append({
                            "stream": stream,
                            "msg_id": message.msg_id,
                            "epoch": message.epoch,
                            "missing": message.missing(),
                        })
                        self._release(state, message)
                        self._prune_stream(stream, state)
                        self._stats["timeout_dropped"] += 1
            return expired

    def close_stream(self, stream: str) -> int:
        with self._lock:
            state = self._streams.pop(stream, None)
            if state is None:
                return 0
            dropped = state.inflight
            self._stats["closed_dropped"] += dropped
            return dropped

    def drop_message(self, stream: str, msg_id: int, epoch: int) -> bool:
        with self._lock:
            state = self._streams.get(stream)
            if state is None:
                return False
            message = state.messages.get((msg_id, epoch))
            if message is None or message.completed:
                return False
            self._release(state, message)
            self._prune_stream(stream, state)
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "stats": dict(self._stats),
                "streams": [
                    {
                        "stream": stream,
                        "messages": [
                            {
                                "msg_id": message.msg_id,
                                "epoch": message.epoch,
                                "count": message.count,
                                "total_len": message.total_len,
                                "first_seen": message.first_seen,
                                "chunks": {
                                    str(index): bytes(chunk)
                                    for index, chunk
                                    in message.chunks.items()
                                },
                            }
                            for key in state.order
                            for message in (state.messages[key],)
                        ],
                    }
                    for stream, state in self._streams.items()
                ],
            }

    def restore(self, state: dict) -> None:
        try:
            streams, stats = self._parse_snapshot(state)
        except _SnapshotInvalid:
            raise FragmentError(FRAG_SNAPSHOT_INVALID)
        with self._lock:
            self._streams = streams
            self._stats = stats

    @staticmethod
    def _parse_snapshot(state: dict):
        if not isinstance(state, dict) or state.get("version") != 1:
            raise _SnapshotInvalid
        raw_stats = state.get("stats")
        if (not isinstance(raw_stats, dict)
                or set(raw_stats) != set(_STAT_KEYS)):
            raise _SnapshotInvalid
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if not _is_int(value) or value < 0:
                raise _SnapshotInvalid
            stats[key] = value

        raw_streams = state.get("streams")
        if (not isinstance(raw_streams, list)
                or set(state) != {"version", "stats", "streams"}):
            raise _SnapshotInvalid

        streams: dict[str, _StreamState] = {}
        for raw_stream in raw_streams:
            if not isinstance(raw_stream, dict):
                raise _SnapshotInvalid
            name = raw_stream.get("stream")
            raw_messages = raw_stream.get("messages")
            if (not isinstance(name, str) or name == ""
                    or name in streams
                    or not isinstance(raw_messages, list)):
                raise _SnapshotInvalid
            stream_state = _StreamState()
            seen_keys: set[tuple[int, int]] = set()
            for raw_message in raw_messages:
                message = MessageFragmentGuard._parse_message(raw_message)
                if message.key in seen_keys:
                    raise _SnapshotInvalid
                seen_keys.add(message.key)
                stream_state.messages[message.key] = message
                stream_state.order.append(message.key)
                stream_state.inflight += 1
                stream_state.reserved_bytes += message.total_len
                stream_state.reserved_fragments += message.count
                stream_state.fragments += len(message.chunks)
                stream_state.received_bytes += message.received_bytes
            streams[name] = stream_state
        return streams, stats

    @staticmethod
    def _parse_message(raw_message: dict) -> _PendingMessage:
        if not isinstance(raw_message, dict):
            raise _SnapshotInvalid
        msg_id = raw_message.get("msg_id")
        epoch = raw_message.get("epoch")
        count = raw_message.get("count")
        total_len = raw_message.get("total_len")
        first_seen = raw_message.get("first_seen")
        raw_chunks = raw_message.get("chunks")
        if (not _is_int(msg_id) or msg_id < 0
                or not _is_int(epoch) or epoch < 0
                or not _is_int(count) or count < 1
                or not _is_int(total_len) or total_len < 0
                or not (isinstance(first_seen, (int, float))
                        and not isinstance(first_seen, bool))
                or first_seen < 0
                or not isinstance(raw_chunks, dict)):
            raise _SnapshotInvalid
        message = _PendingMessage(msg_id, epoch, count, total_len,
                                  first_seen)
        for raw_index, chunk in raw_chunks.items():
            if not isinstance(raw_index, str):
                raise _SnapshotInvalid
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                raise _SnapshotInvalid
            if (index < 0 or index >= count
                    or index in message.chunks
                    or not isinstance(chunk, bytes)):
                raise _SnapshotInvalid
            message.chunks[index] = bytes(chunk)
            message.received_bytes += len(chunk)
        return message

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)
