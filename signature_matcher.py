"""Streaming multi-pattern signature matcher with masked bytes."""

from __future__ import annotations

import copy
import threading
from collections.abc import Mapping
from typing import Any

MIN_PATTERN_BYTES = 4
MAX_PATTERN_BYTES = 4096
MAX_PATTERNS = 20000
MAX_CHUNK_BYTES = 8 * 1024 * 1024
STATE_VERSION = 1
_STATE_KEYS = frozenset(
    {"version", "patterns", "counts", "offset", "pending"}
)
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


class SignatureError(ValueError):
    """Refusal raised by the matcher; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class _PatternRejected(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_hex_digit(value: str) -> bool:
    return value in _HEX_DIGITS


def _parse_pattern(text: Any) -> tuple[tuple[int, int], ...]:
    if not isinstance(text, str):
        raise _PatternRejected(
            "sig.pattern_syntax", "pattern text must be a string"
        )

    tokens = text.split()
    length = len(tokens)
    if length < MIN_PATTERN_BYTES:
        raise _PatternRejected(
            "sig.pattern_too_short",
            f"pattern has {length} bytes; minimum is {MIN_PATTERN_BYTES}",
        )
    if length > MAX_PATTERN_BYTES:
        raise _PatternRejected(
            "sig.pattern_too_long",
            f"pattern has {length} bytes; maximum is {MAX_PATTERN_BYTES}",
        )

    steps: list[tuple[int, int]] = []
    for token in tokens:
        if len(token) != 2:
            raise _PatternRejected(
                "sig.pattern_syntax", f"invalid byte token {token!r}"
            )

        left, right = token[0], token[1]
        if left == "?":
            if right == "?":
                mask, value = 0x00, 0x00
            elif _is_hex_digit(right):
                mask, value = 0x0F, int(right, 16)
            else:
                raise _PatternRejected(
                    "sig.pattern_syntax", f"invalid masked byte {token!r}"
                )
        elif right == "?":
            if _is_hex_digit(left):
                mask, value = 0xF0, int(left, 16) << 4
            else:
                raise _PatternRejected(
                    "sig.pattern_syntax", f"invalid masked byte {token!r}"
                )
        elif _is_hex_digit(left) and _is_hex_digit(right):
            mask, value = 0xFF, int(token, 16)
        else:
            raise _PatternRejected(
                "sig.pattern_syntax", f"invalid byte token {token!r}"
            )
        steps.append((mask, value))

    return tuple(steps)


def _decode_pending(value: Any) -> bytes:
    if isinstance(value, str):
        if len(value) % 2:
            raise SignatureError(
                "sig.state_invalid", "pending hex has odd length"
            )
        if any(ch not in _HEX_DIGITS for ch in value):
            raise SignatureError(
                "sig.state_invalid", "pending is not valid hex"
            )
        return bytes.fromhex(value)

    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)

    if isinstance(value, (list, tuple)):
        result = bytearray()
        for item in value:
            if type(item) is not int or not 0 <= item <= 255:
                raise SignatureError(
                    "sig.state_invalid", "pending must contain byte integers"
                )
            result.append(item)
        return bytes(result)

    raise SignatureError(
        "sig.state_invalid", "pending has an invalid representation"
    )


class _MaskedAutomaton:
    __slots__ = (
        "exact",
        "high",
        "low",
        "wild",
        "depth",
        "outputs",
        "dfa_sets",
        "dfa_by_set",
        "dfa_trans",
        "dfa_outputs",
    )

    def __init__(
        self,
        patterns: list[tuple[str, tuple[tuple[int, int], ...]]],
    ) -> None:
        self.exact: list[dict[int, int]] = [{}]
        self.high: list[list[int | None]] = [[None] * 16]
        self.low: list[list[int | None]] = [[None] * 16]
        self.wild: list[int | None] = [None]
        self.depth: list[int] = [0]
        self.outputs: list[list[str]] = [[]]

        for pattern_id, steps in patterns:
            node = 0
            for mask, value in steps:
                node = self._edge(node, mask, value)
            self.outputs[node].append(pattern_id)

        for output in self.outputs:
            output.sort()

        empty = frozenset[int]()
        self.dfa_sets: list[frozenset[int]] = [empty]
        self.dfa_by_set: dict[frozenset[int], int] = {empty: 0}
        self.dfa_trans: list[dict[int, int]] = [{}]
        self.dfa_outputs: list[tuple[tuple[str, int], ...]] = [()]

    def _new_node(self, node_depth: int) -> int:
        node_id = len(self.depth)
        self.exact.append({})
        self.high.append([None] * 16)
        self.low.append([None] * 16)
        self.wild.append(None)
        self.depth.append(node_depth)
        self.outputs.append([])
        return node_id

    def _edge(self, node: int, mask: int, value: int) -> int:
        if mask == 0xFF:
            table = self.exact[node]
            child = table.get(value)
            if child is None:
                child = self._new_node(self.depth[node] + 1)
                table[value] = child
            return child

        if mask == 0xF0:
            nibble = value >> 4
            child = self.high[node][nibble]
            if child is None:
                child = self._new_node(self.depth[node] + 1)
                self.high[node][nibble] = child
            return child

        if mask == 0x0F:
            nibble = value & 0x0F
            child = self.low[node][nibble]
            if child is None:
                child = self._new_node(self.depth[node] + 1)
                self.low[node][nibble] = child
            return child

        child = self.wild[node]
        if child is None:
            child = self._new_node(self.depth[node] + 1)
            self.wild[node] = child
        return child

    def _root_children(self, byte: int, children: set[int]) -> None:
        exact_child = self.exact[0].get(byte)
        if exact_child is not None:
            children.add(exact_child)

        high_child = self.high[0][byte >> 4]
        if high_child is not None:
            children.add(high_child)

        low_child = self.low[0][byte & 0x0F]
        if low_child is not None:
            children.add(low_child)

        wild_child = self.wild[0]
        if wild_child is not None:
            children.add(wild_child)

    def _node_children(
        self, node: int, byte: int, children: set[int]
    ) -> None:
        exact_child = self.exact[node].get(byte)
        if exact_child is not None:
            children.add(exact_child)

        high_child = self.high[node][byte >> 4]
        if high_child is not None:
            children.add(high_child)

        low_child = self.low[node][byte & 0x0F]
        if low_child is not None:
            children.add(low_child)

        wild_child = self.wild[node]
        if wild_child is not None:
            children.add(wild_child)

    def advance(self, state_id: int, byte: int) -> int:
        transition = self.dfa_trans[state_id]
        cached = transition.get(byte)
        if cached is not None:
            return cached

        children: set[int] = set()
        self._root_children(byte, children)
        for node in self.dfa_sets[state_id]:
            self._node_children(node, byte, children)

        key = frozenset(children)
        existing = self.dfa_by_set.get(key)
        if existing is not None:
            transition[byte] = existing
            return existing

        next_state = len(self.dfa_sets)
        self.dfa_by_set[key] = next_state
        self.dfa_sets.append(key)
        self.dfa_trans.append({})

        outputs: list[tuple[str, int]] = []
        for node in key:
            node_outputs = self.outputs[node]
            if node_outputs:
                length = self.depth[node]
                outputs.extend(
                    (pattern_id, length) for pattern_id in node_outputs
                )
        outputs.sort()
        self.dfa_outputs.append(tuple(outputs))

        transition[byte] = next_state
        return next_state

    def outputs_for(self, state_id: int) -> tuple[tuple[str, int], ...]:
        return self.dfa_outputs[state_id]


class SignatureMatcher:
    """Compile byte signatures and match them over chunked streams."""

    def __init__(
        self,
        *,
        max_patterns: int = MAX_PATTERNS,
        max_chunk: int = MAX_CHUNK_BYTES,
    ) -> None:
        self._max_patterns = max_patterns
        self._max_chunk = max_chunk
        self._lock = threading.RLock()
        self._patterns_text: dict[str, str] = {}
        self._pattern_ids: list[str] = []
        self._automaton: _MaskedAutomaton | None = None
        self._max_length = 0
        self._ready = False
        self._counts: dict[str, int] = {}
        self._offset = 0
        self._pending = b""
        self._build_errors: list[dict[str, object]] = []

    def build(self, patterns: dict) -> dict:
        if not isinstance(patterns, Mapping):
            raise SignatureError(
                "sig.patterns_invalid", "patterns must be a mapping"
            )

        with self._lock:
            errors: list[dict[str, object]] = []
            compiled: list[
                tuple[str, tuple[tuple[int, int], ...]]
            ] = []

            for pattern_id, text in patterns.items():
                if len(compiled) >= self._max_patterns:
                    errors.append(
                        {
                            "id": pattern_id,
                            "code": "sig.pattern_limit",
                            "detail": (
                                f"maximum pattern count is {self._max_patterns}"
                            ),
                        }
                    )
                    continue

                try:
                    steps = _parse_pattern(text)
                except _PatternRejected as exc:
                    errors.append(
                        {
                            "id": pattern_id,
                            "code": exc.code,
                            "detail": exc.detail,
                        }
                    )
                    continue

                compiled.append((pattern_id, steps))

            if compiled:
                compiled.sort(key=lambda item: item[0])
                automaton = _MaskedAutomaton(compiled)
                patterns_text = {
                    pattern_id: patterns[pattern_id]
                    for pattern_id, _steps in compiled
                }
                pattern_ids = [
                    pattern_id for pattern_id, _steps in compiled
                ]
                max_length = max(len(steps) for _id, steps in compiled)

                self._patterns_text = patterns_text
                self._pattern_ids = pattern_ids
                self._automaton = automaton
                self._max_length = max_length
                self._ready = True
                self._counts = {
                    pattern_id: int(self._counts.get(pattern_id, 0))
                    for pattern_id in pattern_ids
                }
                tail_limit = max_length - 1
                if len(self._pending) > tail_limit:
                    self._pending = self._pending[-tail_limit:]
            elif not errors:
                self._patterns_text = {}
                self._pattern_ids = []
                self._automaton = None
                self._max_length = 0
                self._ready = False
                self._counts = {}
                self._pending = b""

            self._build_errors = errors
            return {
                "compiled": len(compiled),
                "rejected": len(errors),
            }

    def build_errors(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(self._build_errors)

    def pattern_ids(self) -> list[str]:
        with self._lock:
            return list(self._pattern_ids)

    def pattern_count(self) -> int:
        with self._lock:
            return len(self._pattern_ids)

    def feed(self, chunk: bytes, eof: bool = False) -> list[dict]:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise SignatureError(
                "sig.chunk_invalid", "chunk must be bytes-like"
            )
        if len(chunk) > self._max_chunk:
            raise SignatureError(
                "sig.chunk_too_large",
                f"chunk size {len(chunk)} exceeds {self._max_chunk} bytes",
            )

        raw_chunk = bytes(chunk)
        with self._lock:
            if not self._ready or self._automaton is None:
                raise SignatureError(
                    "sig.not_built",
                    "no non-empty pattern set is built",
                )
            if not raw_chunk:
                return []

            automaton = self._automaton
            pending = self._pending
            prefix_len = len(pending)
            window = pending + raw_chunk
            window_base = self._offset - prefix_len
            state_id = 0
            hits: list[dict[str, object]] = []

            for window_index, byte in enumerate(window):
                state_id = automaton.advance(state_id, byte)
                if window_index < prefix_len:
                    continue

                for pattern_id, length in automaton.outputs_for(state_id):
                    offset = window_base + window_index + 1 - length
                    hits.append(
                        {
                            "pattern_id": pattern_id,
                            "offset": offset,
                            "length": length,
                        }
                    )
                    self._counts[pattern_id] = (
                        self._counts.get(pattern_id, 0) + 1
                    )

            hits.sort(key=lambda hit: (hit["offset"], hit["pattern_id"]))

            self._offset += len(raw_chunk)
            tail_limit = self._max_length - 1
            if tail_limit > 0 and len(window) > tail_limit:
                self._pending = window[-tail_limit:]
            else:
                self._pending = window
            return hits

    def reset(self) -> None:
        with self._lock:
            self._offset = 0
            self._pending = b""

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                pattern_id: int(self._counts.get(pattern_id, 0))
                for pattern_id in self._pattern_ids
            }

    def hits_for(self, pattern_id: str) -> int:
        with self._lock:
            try:
                known = pattern_id in self._patterns_text
            except TypeError:
                known = False
            if not known:
                raise SignatureError(
                    "sig.unknown_pattern",
                    f"unknown pattern id {pattern_id!r}",
                )
            return int(self._counts.get(pattern_id, 0))

    def state(self) -> dict:
        with self._lock:
            counts = {
                pattern_id: int(self._counts.get(pattern_id, 0))
                for pattern_id in self._pattern_ids
            }
            snapshot = {
                "version": STATE_VERSION,
                "patterns": copy.deepcopy(self._patterns_text),
                "counts": counts,
                "offset": self._offset,
                "pending": self._pending.hex(),
            }
            return copy.deepcopy(snapshot)

    def restore(self, state: dict) -> None:
        if not isinstance(state, Mapping):
            raise SignatureError(
                "sig.state_invalid", "state must be an object"
            )
        if set(state.keys()) != _STATE_KEYS:
            raise SignatureError(
                "sig.state_invalid",
                "state keys do not match the supported schema",
            )

        version = state["version"]
        if type(version) is not int or version != STATE_VERSION:
            raise SignatureError(
                "sig.state_invalid", "unsupported state version"
            )

        patterns = state["patterns"]
        if not isinstance(patterns, Mapping):
            raise SignatureError(
                "sig.state_invalid", "patterns must be an object"
            )
        if len(patterns) > self._max_patterns:
            raise SignatureError(
                "sig.state_invalid", "too many patterns in state"
            )

        compiled: list[
            tuple[str, tuple[tuple[int, int], ...]]
        ] = []
        patterns_text: dict[str, str] = {}
        for pattern_id, text in patterns.items():
            if not isinstance(pattern_id, str) or not isinstance(text, str):
                raise SignatureError(
                    "sig.state_invalid",
                    "pattern ids and texts must be strings",
                )
            try:
                steps = _parse_pattern(text)
            except _PatternRejected as exc:
                raise SignatureError(
                    "sig.state_invalid", exc.detail
                ) from exc
            compiled.append((pattern_id, steps))
            patterns_text[pattern_id] = text

        counts = state["counts"]
        if not isinstance(counts, Mapping):
            raise SignatureError(
                "sig.state_invalid", "counts must be an object"
            )
        if set(counts.keys()) != set(patterns.keys()):
            raise SignatureError(
                "sig.state_invalid",
                "counts do not match pattern identifiers",
            )

        restored_counts: dict[str, int] = {}
        for pattern_id, value in counts.items():
            if type(value) is not int or value < 0:
                raise SignatureError(
                    "sig.state_invalid",
                    "counts must be non-negative integers",
                )
            restored_counts[pattern_id] = value

        offset = state["offset"]
        if type(offset) is not int or offset < 0:
            raise SignatureError(
                "sig.state_invalid",
                "offset must be a non-negative integer",
            )

        pending = _decode_pending(state["pending"])
        max_length = max(
            (len(steps) for _id, steps in compiled), default=0
        )
        if max_length:
            if len(pending) > max_length - 1:
                raise SignatureError(
                    "sig.state_invalid",
                    "pending is longer than the longest pattern tail",
                )
            if offset < len(pending):
                raise SignatureError(
                    "sig.state_invalid",
                    "offset is inconsistent with pending length",
                )
        elif pending:
            raise SignatureError(
                "sig.state_invalid",
                "empty pattern set cannot contain pending bytes",
            )

        compiled.sort(key=lambda item: item[0])
        automaton = _MaskedAutomaton(compiled) if compiled else None
        pattern_ids = [pattern_id for pattern_id, _steps in compiled]

        with self._lock:
            self._patterns_text = patterns_text
            self._pattern_ids = pattern_ids
            self._automaton = automaton
            self._max_length = max_length
            self._ready = bool(compiled)
            self._counts = restored_counts
            self._offset = offset
            self._pending = pending
            self._build_errors = []

    def pending_bytes(self) -> int:
        with self._lock:
            return len(self._pending)
