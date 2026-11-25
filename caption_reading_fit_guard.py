"""Reading speed budget, display width and line fitting for timed captions."""

from __future__ import annotations

import bisect
import copy
import math
import threading
import unicodedata


STAT_KEYS = (
    "cues_added",
    "cues_removed",
    "cues_split",
    "extended",
    "rejected",
    "cps_queries",
)


class CaptionError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CaptionReadingFitGuard:
    """Keep caption cues inside a reading speed budget and column layout."""

    SNAPSHOT_VERSION = 1

    def __init__(self, *, max_cps: float = 17.0, max_columns: int = 42,
                 max_lines: int = 2, min_duration_ms: int = 800,
                 max_duration_ms: int = 7000, clock=None) -> None:
        self.max_cps = max_cps
        self.max_columns = max_columns
        self.max_lines = max_lines
        self.min_duration_ms = min_duration_ms
        self.max_duration_ms = max_duration_ms
        self._clock = clock
        self._lock = threading.RLock()
        self._cues: list[dict] = []
        self._auto_number = 1
        self._stats = {key: 0 for key in STAT_KEYS}

    def add_cue(self, start_ms: int, end_ms: int, text: str, *,
                cue_id: str | None = None) -> dict:
        with self._lock:
            try:
                self._validate_timing(start_ms, end_ms)
                if not isinstance(text, str) or self._nonspace_len(text) == 0:
                    raise CaptionError("CAPTION_INVALID_TEXT", text)

                if cue_id is None:
                    cue_id = self._next_cue_id()
                if any(cue["cue_id"] == cue_id for cue in self._cues):
                    raise CaptionError("CAPTION_DUPLICATE_CUE", cue_id)

                cue = {
                    "cue_id": cue_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text,
                }
                self._cues.append(cue)
                self._cues.sort(key=lambda item: item["start_ms"])
                self._stats["cues_added"] += 1
                return copy.deepcopy(cue)
            except CaptionError:
                self._stats["rejected"] += 1
                raise

    def remove_cue(self, cue_id: str) -> None:
        with self._lock:
            try:
                index, _ = self._find_cue(cue_id)
                del self._cues[index]
                self._stats["cues_removed"] += 1
            except CaptionError:
                self._stats["rejected"] += 1
                raise

    def cues(self) -> list:
        with self._lock:
            return copy.deepcopy(
                sorted(self._cues, key=lambda cue: cue["start_ms"])
            )

    def cps_of(self, text: str, start_ms: int, end_ms: int) -> float:
        with self._lock:
            try:
                self._validate_timing(start_ms, end_ms)
                if not isinstance(text, str):
                    raise CaptionError("CAPTION_INVALID_TEXT", text)
                cps = self._cps_value(text, start_ms, end_ms)
                self._stats["cps_queries"] += 1
                return cps
            except CaptionError:
                self._stats["rejected"] += 1
                raise

    def display_width(self, text: str) -> int:
        with self._lock:
            if not isinstance(text, str):
                self._stats["rejected"] += 1
                raise CaptionError("CAPTION_INVALID_TEXT", text)
            return sum(self._char_columns(char) for char in text)

    def wrap_text(self, text: str, columns: int) -> list:
        with self._lock:
            try:
                if (isinstance(columns, bool)
                        or not isinstance(columns, int)
                        or columns <= 0):
                    raise CaptionError("CAPTION_INVALID_WIDTH", columns)
                if not isinstance(text, str):
                    raise CaptionError("CAPTION_INVALID_TEXT", text)

                return self._wrap_lines(text, columns)
            except CaptionError:
                self._stats["rejected"] += 1
                raise

    def _wrap_lines(self, text: str, columns: int) -> list[str]:
        lines: list[str] = []
        line_words: list[str] = []
        line_width = 0

        for word in text.split():
            word_width = sum(self._char_columns(char) for char in word)
            if not line_words:
                line_words = [word]
                line_width = word_width
            elif line_width + 1 + word_width <= columns:
                line_words.append(word)
                line_width += 1 + word_width
            else:
                lines.append(" ".join(line_words))
                line_words = [word]
                line_width = word_width

        if line_words:
            lines.append(" ".join(line_words))
        return lines

    def split_cue(self, cue_id: str) -> list:
        with self._lock:
            try:
                index, cue = self._find_cue(cue_id)
                pieces = self._build_split_pieces(cue)
                if len(pieces) == 1:
                    return copy.deepcopy([cue])

                unique_pieces = self._assign_split_ids(cue, pieces)
                self._cues[index:index + 1] = unique_pieces
                self._stats["cues_split"] += 1
                return copy.deepcopy(unique_pieces)
            except CaptionError:
                self._stats["rejected"] += 1
                raise

    def extend_durations(self) -> int:
        with self._lock:
            changed = 0
            ordered = sorted(self._cues, key=lambda cue: cue["start_ms"])

            for position, cue in enumerate(ordered):
                duration = cue["end_ms"] - cue["start_ms"]
                if duration >= self.min_duration_ms:
                    continue

                desired_end = cue["start_ms"] + self.min_duration_ms
                desired_end = min(
                    desired_end,
                    cue["start_ms"] + self.max_duration_ms,
                )
                if position + 1 < len(ordered):
                    desired_end = min(
                        desired_end, ordered[position + 1]["start_ms"]
                    )

                if desired_end > cue["end_ms"]:
                    cue["end_ms"] = desired_end
                    changed += 1

            self._stats["extended"] += changed
            return changed

    def fit_all(self) -> dict:
        with self._lock:
            split_count = 0
            for cue_id in [cue["cue_id"] for cue in self._cues]:
                _, cue = self._find_cue(cue_id)
                if len(self._build_split_pieces(cue)) > 1:
                    self.split_cue(cue_id)
                    split_count += 1

            extended_count = self.extend_durations()
            return {"split": split_count, "extended": extended_count}

    def timeline_valid(self) -> bool:
        with self._lock:
            ordered = sorted(self._cues, key=lambda cue: cue["start_ms"])
            max_end_by_start: dict[int, int] = {}
            for cue in ordered:
                start = cue["start_ms"]
                max_end_by_start[start] = max(
                    max_end_by_start.get(start, cue["end_ms"]),
                    cue["end_ms"],
                )

            previous_starts: list[int] = []
            max_previous_end = 0
            for start in sorted(max_end_by_start):
                if previous_starts and start < max_previous_end:
                    return False
                previous_starts.append(start)
                max_previous_end = max(
                    max_previous_end, max_end_by_start[start]
                )
            return True

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self.SNAPSHOT_VERSION,
                "config": {
                    "max_cps": self.max_cps,
                    "max_columns": self.max_columns,
                    "max_lines": self.max_lines,
                    "min_duration_ms": self.min_duration_ms,
                    "max_duration_ms": self.max_duration_ms,
                },
                "cues": copy.deepcopy(
                    sorted(self._cues, key=lambda cue: cue["start_ms"])
                ),
                "next_number": self._auto_number,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                parsed = self._parse_snapshot(state)
            except CaptionError:
                self._stats["rejected"] += 1
                raise

            self.max_cps = parsed["config"]["max_cps"]
            self.max_columns = parsed["config"]["max_columns"]
            self.max_lines = parsed["config"]["max_lines"]
            self.min_duration_ms = parsed["config"]["min_duration_ms"]
            self.max_duration_ms = parsed["config"]["max_duration_ms"]
            self._cues = parsed["cues"]
            if parsed["next_number"] is not None:
                self._auto_number = max(
                    self._auto_number, parsed["next_number"]
                )
            else:
                for cue in self._cues:
                    cue_id = cue["cue_id"]
                    if isinstance(cue_id, str) and cue_id.startswith("cue-"):
                        suffix = cue_id[4:]
                        if suffix.isdigit():
                            self._auto_number = max(
                                self._auto_number, int(suffix) + 1
                            )

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in STAT_KEYS}

    def _next_cue_id(self) -> str:
        while any(
            cue["cue_id"] == f"cue-{self._auto_number}"
            for cue in self._cues
        ):
            self._auto_number += 1
        cue_id = f"cue-{self._auto_number}"
        self._auto_number += 1
        return cue_id

    def _find_cue(self, cue_id: str) -> tuple[int, dict]:
        for index, cue in enumerate(self._cues):
            if cue["cue_id"] == cue_id:
                return index, cue
        raise CaptionError("CAPTION_CUE_NOT_FOUND", cue_id)

    @staticmethod
    def _validate_timing(start_ms: object, end_ms: object) -> None:
        if (isinstance(start_ms, bool)
                or isinstance(end_ms, bool)
                or not isinstance(start_ms, int)
                or not isinstance(end_ms, int)):
            raise CaptionError(
                "CAPTION_INVALID_TIMING", (start_ms, end_ms)
            )
        if start_ms < 0 or end_ms <= start_ms:
            raise CaptionError(
                "CAPTION_INVALID_TIMING", (start_ms, end_ms)
            )

    @classmethod
    def _nonspace_len(cls, text: str) -> int:
        return sum(1 for char in text if not char.isspace())

    @staticmethod
    def _char_columns(char: str) -> int:
        if unicodedata.combining(char):
            return 0
        if unicodedata.east_asian_width(char) in {"W", "F"}:
            return 2
        return 1

    def _cps_value(self, text: str, start_ms: int, end_ms: int) -> float:
        return round(
            self._nonspace_len(text) * 1000 / (end_ms - start_ms), 3
        )

    def _duration_ms_for(self, text: str) -> int:
        duration = math.ceil(
            self._nonspace_len(text) * 1000 / self.max_cps
        )
        duration = max(duration, self.min_duration_ms)
        return min(duration, self.max_duration_ms)

    def _build_split_pieces(self, cue: dict) -> list[dict]:
        chars = self._nonspace_len(cue["text"])
        duration_ms = max(
            cue["end_ms"] - cue["start_ms"],
            self.min_duration_ms,
        )
        cps = round(chars * 1000 / duration_ms, 3)
        if cps <= self.max_cps and self._layout_fits(cue["text"]):
            return [copy.deepcopy(cue)]

        piece_count = 2
        if cps > self.max_cps:
            piece_count = max(2, math.ceil(cps / self.max_cps))
        chunks = self._split_text(cue["text"], piece_count)

        max_budget_chars = math.floor(
            self.max_cps * self.max_duration_ms / 1000
        )
        index = 0
        while index < len(chunks):
            chunk = chunks[index]
            needs_split = (
                self._nonspace_len(chunk) > max_budget_chars
                or not self._layout_fits(chunk)
            )
            if not needs_split:
                index += 1
                continue

            smaller_chunks = self._split_text(chunk, 2)
            if len(smaller_chunks) <= 1:
                index += 1
                continue
            chunks[index:index + 1] = smaller_chunks

        if len(chunks) <= 1:
            return [copy.deepcopy(cue)]

        pieces: list[dict] = []
        start_ms = cue["start_ms"]
        for chunk in chunks:
            end_ms = start_ms + self._duration_ms_for(chunk)
            pieces.append({
                "cue_id": cue["cue_id"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": chunk,
            })
            start_ms = end_ms
        return pieces

    def _layout_fits(self, text: str) -> bool:
        if (not isinstance(self.max_columns, int)
                or isinstance(self.max_columns, bool)
                or self.max_columns <= 0
                or not isinstance(self.max_lines, int)
                or isinstance(self.max_lines, bool)
                or self.max_lines <= 0):
            return False
        return len(self._wrap_lines(text, self.max_columns)) <= self.max_lines

    def _assign_split_ids(self, cue: dict, pieces: list[dict]) -> list[dict]:
        base_id = str(cue["cue_id"])
        existing = [
            item["cue_id"]
            for item in self._cues
            if item["cue_id"] != cue["cue_id"]
        ]
        assigned: list[dict] = []
        used: list[str] = []

        for index, piece in enumerate(pieces):
            if index == 0 and base_id not in existing:
                piece_id = base_id
            else:
                suffix = index + 1
                piece_id = f"{base_id}__part{suffix}"
                while piece_id in existing or piece_id in used:
                    suffix += 1
                    piece_id = f"{base_id}__part{suffix}"
            piece["cue_id"] = piece_id
            used.append(piece_id)
            assigned.append(piece)
        return assigned

    def _split_text(self, text: str, piece_count: int) -> list[str]:
        normalized_whole = " ".join(text.split())
        if piece_count <= 1:
            return [normalized_whole] if normalized_whole else []

        length = len(text)
        prefix = [0] * (length + 1)
        for index, char in enumerate(text):
            prefix[index + 1] = prefix[index] + (
                0 if char.isspace() else 1
            )

        total = prefix[length]
        starter_count = sum(
            1
            for index, char in enumerate(text)
            if not char.isspace() and not self._is_punctuation(char)
        )
        piece_count = max(1, min(piece_count, total, starter_count or 1))
        if piece_count == 1:
            return [normalized_whole] if normalized_whole else []

        next_nonspace = [length] * (length + 1)
        next_index = length
        for index in range(length - 1, -1, -1):
            if not text[index].isspace():
                next_index = index
            next_nonspace[index] = next_index

        positions = [[], [], []]
        counts = [[], [], []]
        for boundary in range(1, length + 1):
            next_char_index = next_nonspace[boundary]
            if next_char_index == length:
                continue
            if self._is_punctuation(text[next_char_index]):
                continue

            if self._is_break_after_punctuation(text[boundary - 1]):
                quality = 0
            elif boundary < length and text[boundary].isspace():
                quality = 1
            else:
                quality = 2

            positions[quality].append(boundary)
            counts[quality].append(prefix[boundary])

        boundaries: list[int] = []
        last_boundary = 0
        for piece_index in range(1, piece_count):
            target = total * piece_index / piece_count
            remaining_after = piece_count - piece_index - 1
            boundary = self._select_boundary(
                positions,
                counts,
                prefix,
                last_boundary,
                target,
                remaining_after,
                total,
            )
            if boundary is None:
                break
            boundaries.append(boundary)
            last_boundary = boundary

        chunks: list[str] = []
        previous = 0
        for boundary in boundaries + [length]:
            chunk = " ".join(text[previous:boundary].split())
            if chunk:
                chunks.append(chunk)
            previous = boundary
        return chunks

    def _select_boundary(self, positions: list[list[int]],
                         counts: list[list[int]], prefix: list[int],
                         last_boundary: int, target: float,
                         remaining_after: int, total: int) -> int | None:
        required_right = remaining_after + 1
        maximum_count = total - required_right
        minimum_count = prefix[last_boundary] + 1

        for quality in range(3):
            quality_positions = positions[quality]
            quality_counts = counts[quality]
            lower = bisect.bisect_right(
                quality_positions, last_boundary
            )
            lower = max(
                lower,
                bisect.bisect_left(quality_counts, minimum_count),
            )
            upper = bisect.bisect_right(quality_counts, maximum_count)
            if lower >= upper:
                continue

            nearest = bisect.bisect_left(
                quality_counts, target, lower, upper
            )
            candidates = []
            if nearest < upper:
                candidates.append(nearest)
            if nearest - 1 >= lower:
                candidates.append(nearest - 1)

            best_index = min(
                candidates,
                key=lambda index: (
                    abs(quality_counts[index] - target),
                    quality_positions[index],
                ),
            )
            return quality_positions[best_index]

        return None

    @staticmethod
    def _is_punctuation(char: str) -> bool:
        return unicodedata.category(char).startswith("P")

    @classmethod
    def _is_break_after_punctuation(cls, char: str) -> bool:
        category = unicodedata.category(char)
        if category in {"Ps", "Pi"}:
            return False
        return cls._is_punctuation(char)

    def _parse_snapshot(self, state: object) -> dict:
        if not isinstance(state, dict):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", state)

        version = state.get("version")
        if (isinstance(version, bool)
                or not isinstance(version, int)
                or version != self.SNAPSHOT_VERSION):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", version)

        raw_cues = state.get("cues")
        if not isinstance(raw_cues, list):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_cues)

        cues: list[dict] = []
        cue_ids: set[object] = set()
        for raw_cue in raw_cues:
            cue = self._parse_snapshot_cue(raw_cue)
            if cue["cue_id"] in cue_ids:
                raise CaptionError("CAPTION_SNAPSHOT_INVALID", cue)
            cue_ids.add(cue["cue_id"])
            cues.append(cue)
        cues.sort(key=lambda cue: cue["start_ms"])

        raw_config = state.get("config", {})
        config = self._parse_snapshot_config(raw_config)
        next_number = state.get("next_number")
        if next_number is not None and (
            isinstance(next_number, bool)
            or not isinstance(next_number, int)
            or next_number <= 0
        ):
            raise CaptionError(
                "CAPTION_SNAPSHOT_INVALID", next_number
            )

        return {
            "cues": cues,
            "config": config,
            "next_number": next_number,
        }

    def _parse_snapshot_cue(self, raw_cue: object) -> dict:
        if not isinstance(raw_cue, dict):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_cue)

        cue_id = raw_cue.get("cue_id")
        start_ms = raw_cue.get("start_ms")
        end_ms = raw_cue.get("end_ms")
        text = raw_cue.get("text")
        if not isinstance(cue_id, str) or not isinstance(text, str):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_cue)

        try:
            self._validate_timing(start_ms, end_ms)
        except CaptionError as exc:
            raise CaptionError(
                "CAPTION_SNAPSHOT_INVALID", raw_cue
            ) from exc

        if self._nonspace_len(text) == 0:
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_cue)

        return {
            "cue_id": cue_id,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "text": text,
        }

    def _parse_snapshot_config(self, raw_config: object) -> dict:
        if not isinstance(raw_config, dict):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_config)

        def number(name: str, default: object) -> object:
            value = raw_config.get(name, default)
            if isinstance(value, bool) or not isinstance(
                value, (int, float)
            ):
                raise CaptionError(
                    "CAPTION_SNAPSHOT_INVALID", {name: value}
                )
            return value

        max_cps = number("max_cps", self.max_cps)
        max_columns = number("max_columns", self.max_columns)
        max_lines = number("max_lines", self.max_lines)
        min_duration_ms = number(
            "min_duration_ms", self.min_duration_ms
        )
        max_duration_ms = number(
            "max_duration_ms", self.max_duration_ms
        )

        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or value <= 0
            for value in (
                max_columns,
                max_lines,
                min_duration_ms,
                max_duration_ms,
            )
        ):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_config)
        if (
            not isinstance(max_cps, (int, float))
            or max_cps <= 0
            or min_duration_ms > max_duration_ms
        ):
            raise CaptionError("CAPTION_SNAPSHOT_INVALID", raw_config)

        return {
            "max_cps": float(max_cps),
            "max_columns": max_columns,
            "max_lines": max_lines,
            "min_duration_ms": min_duration_ms,
            "max_duration_ms": max_duration_ms,
        }
