"""Unified diff parser and patch applicability analyzer."""

from __future__ import annotations

import hashlib
import re
import threading
from copy import deepcopy
from typing import Any

MAX_SECTIONS = 512
MAX_HUNK_LINES = 4096

_HUNK_RE = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"
)


class PatchError(ValueError):
    """Refusal raised by the analyzer; ``code`` is a stable string."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _split_lines(text: str) -> list[str]:
    return text.splitlines()


def _path_value(line: str) -> str:
    value = line[4:]
    if "\t" in value:
        value = value.split("\t", 1)[0]
    return value


def _lines_match(left: str, right: str, whitespace_tolerance: int) -> bool:
    if left == right:
        return True

    left_core = left.rstrip()
    right_core = right.rstrip()
    if left_core != right_core:
        return False

    left_ws = len(left) - len(left_core)
    right_ws = len(right) - len(right_core)
    return abs(left_ws - right_ws) <= whitespace_tolerance


def _best_match(
    block: list[str],
    file_lines: list[str],
    start: int,
    whitespace_tolerance: int,
) -> dict[str, int] | None:
    length = len(block)
    first_position = max(0, start)
    last_position = len(file_lines) - length
    if first_position > last_position:
        return None

    best_position: int | None = None
    best_mismatches: int | None = None

    for position in range(first_position, last_position + 1):
        mismatches = 0
        for offset, expected in enumerate(block):
            actual = file_lines[position + offset]
            if not _lines_match(expected, actual, whitespace_tolerance):
                mismatches += 1
                if (
                    best_mismatches is not None
                    and mismatches >= best_mismatches
                ):
                    break

        if best_mismatches is None or mismatches < best_mismatches:
            best_position = position
            best_mismatches = mismatches
            if mismatches == 0:
                break

    if best_position is None or best_mismatches is None:
        return None
    return {"position": best_position, "mismatches": best_mismatches}


def _hunk_is_complete(hunk: dict[str, Any]) -> bool:
    old_actual = sum(
        1 for line in hunk["lines"] if line["kind"] in (" ", "-")
    )
    new_actual = sum(
        1 for line in hunk["lines"] if line["kind"] in (" ", "+")
    )
    return (
        old_actual == hunk["old_count"]
        and new_actual == hunk["new_count"]
    )


class PatchDiffAnalyzer:
    """Parse unified diffs and judge them against a target file."""

    def __init__(
        self,
        *,
        max_sections: int = MAX_SECTIONS,
        max_hunk_lines: int = MAX_HUNK_LINES,
        context_tolerance: int = 2,
        whitespace_tolerance: int = 1,
    ) -> None:
        self._max_sections = max_sections
        self._max_hunk_lines = max_hunk_lines
        self._context_tolerance = context_tolerance
        self._whitespace_tolerance = whitespace_tolerance
        self._lock = threading.RLock()
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._cache_hits = 0
        self._cache_misses = 0
        self._stats = {
            "classify_calls": 0,
            "parse_calls": 0,
            "locate_calls": 0,
        }

    def limits(self) -> dict[str, int]:
        return {
            "max_sections": self._max_sections,
            "max_hunk_lines": self._max_hunk_lines,
            "context_tolerance": self._context_tolerance,
            "whitespace_tolerance": self._whitespace_tolerance,
        }

    def parse(self, text: str) -> list[dict[str, Any]]:
        with self._lock:
            self._stats["parse_calls"] += 1
        return self._parse_text(text)

    def _parse_text(self, text: str) -> list[dict[str, Any]]:
        sections: list[dict[str, Any]] = []
        section: dict[str, Any] | None = None
        hunk: dict[str, Any] | None = None
        hunk_header_line: int | None = None
        section_header_line: int | None = None

        def validate_hunk() -> None:
            if hunk is None or hunk_header_line is None:
                return
            old_actual = sum(
                1 for line in hunk["lines"] if line["kind"] in (" ", "-")
            )
            new_actual = sum(
                1 for line in hunk["lines"] if line["kind"] in (" ", "+")
            )
            if (
                old_actual != hunk["old_count"]
                or new_actual != hunk["new_count"]
            ):
                raise PatchError(
                    "diff.count_mismatch",
                    {
                        "line": hunk_header_line,
                        "reason": (
                            "hunk line counts do not match its header: "
                            f"old expected {hunk['old_count']}, got {old_actual}; "
                            f"new expected {hunk['new_count']}, got {new_actual}"
                        ),
                    },
                )

        for line_number, raw_line in enumerate(_split_lines(text), start=1):
            is_old_path = raw_line.startswith("---") and (
                len(raw_line) == 3 or raw_line[3] in " \t"
            )
            if is_old_path and (
                hunk is None or _hunk_is_complete(hunk)
            ):
                if hunk is not None:
                    validate_hunk()
                    hunk = None
                    hunk_header_line = None

                if section is not None and section["new_path"] is None:
                    raise PatchError(
                        "diff.syntax",
                        {
                            "line": section_header_line,
                            "reason": "missing new-path header",
                        },
                    )

                if len(sections) >= self._max_sections:
                    raise PatchError(
                        "diff.section_limit",
                        {
                            "line": line_number,
                            "reason": (
                                "patch contains more than "
                                f"{self._max_sections} file sections"
                            ),
                            "limit": self._max_sections,
                        },
                    )

                path = _path_value(raw_line) if len(raw_line) > 3 else ""
                section = {
                    "old_path": path,
                    "new_path": None,
                    "hunks": [],
                }
                sections.append(section)
                section_header_line = line_number
                continue

            is_new_path = raw_line.startswith("+++") and (
                len(raw_line) == 3 or raw_line[3] in " \t"
            )
            if is_new_path and hunk is None:
                if section is None or section["new_path"] is not None:
                    raise PatchError(
                        "diff.syntax",
                        {
                            "line": line_number,
                            "reason": "unexpected new-path header",
                        },
                    )
                section["new_path"] = (
                    _path_value(raw_line) if len(raw_line) > 3 else ""
                )
                continue

            header_match = _HUNK_RE.match(raw_line)
            if raw_line.startswith("@@"):
                if header_match is None:
                    raise PatchError(
                        "diff.syntax",
                        {
                            "line": line_number,
                            "reason": f"invalid hunk header: {raw_line}",
                        },
                    )
                if section is None or section["new_path"] is None:
                    raise PatchError(
                        "diff.syntax",
                        {
                            "line": line_number,
                            "reason": "hunk header before file paths",
                        },
                    )

                old_start = int(header_match.group(1))
                old_count = (
                    int(header_match.group(2))
                    if header_match.group(2) is not None
                    else 1
                )
                new_start = int(header_match.group(3))
                new_count = (
                    int(header_match.group(4))
                    if header_match.group(4) is not None
                    else 1
                )

                if hunk is not None:
                    previous_start = hunk["new_start"]
                    previous_count = hunk["new_count"]
                    if new_start <= previous_start:
                        raise PatchError(
                            "diff.hunk_order",
                            {
                                "line": line_number,
                                "reason": (
                                    "hunks must be strictly ascending by "
                                    "new-file line number"
                                ),
                            },
                        )
                    if new_start < previous_start + previous_count:
                        raise PatchError(
                            "diff.hunk_overlap",
                            {
                                "line": line_number,
                                "reason": "hunk ranges overlap in the new file",
                            },
                        )
                    validate_hunk()

                hunk = {
                    "index": len(section["hunks"]),
                    "old_start": old_start,
                    "old_count": old_count,
                    "new_start": new_start,
                    "new_count": new_count,
                    "header": raw_line,
                    "lines": [],
                }
                hunk_header_line = line_number
                section["hunks"].append(hunk)
                continue

            if hunk is not None:
                if raw_line.startswith("\\"):
                    continue
                if not raw_line or raw_line[0] not in (" ", "-", "+"):
                    if _hunk_is_complete(hunk):
                        validate_hunk()
                        hunk = None
                        hunk_header_line = None
                        continue
                    raise PatchError(
                        "diff.syntax",
                        {
                            "line": line_number,
                            "reason": f"invalid hunk body line: {raw_line}",
                        },
                    )

                hunk["lines"].append(
                    {"kind": raw_line[0], "text": raw_line[1:]}
                )
                if len(hunk["lines"]) > self._max_hunk_lines:
                    raise PatchError(
                        "diff.hunk_limit",
                        {
                            "line": hunk_header_line,
                            "reason": (
                                "hunk contains more than "
                                f"{self._max_hunk_lines} lines"
                            ),
                            "limit": self._max_hunk_lines,
                        },
                    )
                continue

        if hunk is not None:
            validate_hunk()
        if section is not None and section["new_path"] is None:
            raise PatchError(
                "diff.syntax",
                {
                    "line": section_header_line,
                    "reason": "missing new-path header",
                },
            )

        return sections

    def locate(
        self, file_text: str, hunks: list[dict[str, Any]]
    ) -> list[dict[str, int]]:
        with self._lock:
            self._stats["locate_calls"] += 1
        return self._locate_many(file_text, hunks)

    def _locate_many(
        self, file_text: str, hunks: list[dict[str, Any]]
    ) -> list[dict[str, int]]:
        file_lines = _split_lines(file_text)
        positions: list[dict[str, int]] = []
        search_start = 0

        for position_index, hunk in enumerate(hunks):
            block = [
                line["text"]
                for line in hunk["lines"]
                if line["kind"] in (" ", "-")
            ]
            match = _best_match(
                block,
                file_lines,
                search_start,
                self._whitespace_tolerance,
            )
            hunk_index = hunk.get("index", position_index)
            if match is None or match["mismatches"] > self._context_tolerance:
                raise PatchError(
                    "diff.locate_conflict",
                    {"hunk": hunk_index},
                )

            position = match["position"]
            positions.append({"index": hunk_index, "position": position})
            search_start = position + 1

        return positions

    def _line_present(self, text: str, file_lines: list[str]) -> bool:
        return any(
            _lines_match(text, candidate, self._whitespace_tolerance)
            for candidate in file_lines
        )

    def _evaluate_hunk(
        self,
        file_lines: list[str],
        hunk: dict[str, Any],
        search_start: int,
    ) -> tuple[dict[str, Any], int]:
        old_block = [
            line["text"]
            for line in hunk["lines"]
            if line["kind"] in (" ", "-")
        ]
        removed_lines = [
            line["text"] for line in hunk["lines"] if line["kind"] == "-"
        ]
        removed_present = any(
            self._line_present(text, file_lines) for text in removed_lines
        )

        old_match = _best_match(
            old_block,
            file_lines,
            search_start,
            self._whitespace_tolerance,
        )
        next_start = (
            old_match["position"] + 1
            if old_match is not None
            else search_start
        )

        if old_match is None or old_match["mismatches"] > self._context_tolerance:
            return (
                {
                    "index": hunk["index"],
                    "verdict": "conflict",
                    "code": "patch.context_missing",
                },
                next_start,
            )

        if removed_lines and not removed_present:
            verdict = "already_applied"
            code = "patch.removed_absent"
        else:
            verdict = "applicable"
            code = "patch.removed_present"

        return (
            {
                "index": hunk["index"],
                "verdict": verdict,
                "code": code,
            },
            next_start,
        )

    def classify(self, file_text: str, patch_text: str) -> dict[str, Any]:
        with self._lock:
            self._stats["classify_calls"] += 1

        sections = self._parse_text(patch_text)
        file_lines = _split_lines(file_text)
        file_digest = _digest(file_text)
        patch_digest = _digest(patch_text)
        cache_key = (file_digest, patch_digest)

        with self._lock:
            cached = self._cache.get(cache_key)
            if cached is not None:
                self._cache_hits += 1
                return deepcopy(cached)

            self._cache_misses += 1
            result = self._build_classification(
                sections,
                file_lines,
                file_digest,
                patch_digest,
            )
            self._cache[cache_key] = deepcopy(result)
            return result

    def _build_classification(
        self,
        sections: list[dict[str, Any]],
        file_lines: list[str],
        file_digest: str,
        patch_digest: str,
    ) -> dict[str, Any]:
        result_sections: list[dict[str, Any]] = []

        for section in sections:
            hunk_results: list[dict[str, Any]] = []
            search_start = 0
            for hunk in section["hunks"]:
                hunk_result, search_start = self._evaluate_hunk(
                    file_lines, hunk, search_start
                )
                hunk_results.append(hunk_result)

            if any(item["verdict"] == "conflict" for item in hunk_results):
                section_verdict = "conflict"
            elif hunk_results and all(
                item["verdict"] == "already_applied"
                for item in hunk_results
            ):
                section_verdict = "already_applied"
            else:
                section_verdict = "applicable"

            result_sections.append(
                {
                    "old_path": section["old_path"],
                    "new_path": section["new_path"],
                    "verdict": section_verdict,
                    "hunks": hunk_results,
                }
            )

        if any(
            section["verdict"] == "conflict" for section in result_sections
        ):
            verdict = "conflict"
        elif result_sections and all(
            section["verdict"] == "already_applied"
            for section in result_sections
        ):
            verdict = "already_applied"
        else:
            verdict = "applicable"

        return {
            "verdict": verdict,
            "sections": result_sections,
            "file_digest": file_digest,
            "patch_digest": patch_digest,
        }

    def explain(self, result: dict[str, Any]) -> list[dict[str, Any]]:
        explanation: list[dict[str, Any]] = []
        for section_index, section in enumerate(result.get("sections", [])):
            for hunk in section.get("hunks", []):
                explanation.append(
                    {
                        "section": section_index,
                        "hunk": hunk["index"],
                        "verdict": hunk["verdict"],
                        "code": hunk["code"],
                    }
                )
        return explanation

    def invert(self, patch_text: str) -> str:
        lines = _split_lines(patch_text)
        inverted: list[str] = []
        pending_old_path: str | None = None
        in_hunk = False
        old_count = 0
        new_count = 0
        old_seen = 0
        new_seen = 0

        for raw_line in lines:
            header_match = _HUNK_RE.match(raw_line)
            if header_match is not None:
                old_start = header_match.group(1)
                old_count = int(header_match.group(2) or "1")
                new_start = header_match.group(3)
                new_count = int(header_match.group(4) or "1")
                suffix = header_match.group(5)
                inverted.append(
                    f"@@ -{new_start},{new_count} "
                    f"+{old_start},{old_count} @@{suffix}"
                )
                in_hunk = True
                old_seen = 0
                new_seen = 0
                continue

            is_old_path = raw_line.startswith("---") and (
                len(raw_line) == 3 or raw_line[3] in " \t"
            )
            hunk_incomplete = in_hunk and not (
                old_seen == old_count and new_seen == new_count
            )
            if is_old_path and not hunk_incomplete:
                pending_old_path = (
                    _path_value(raw_line) if len(raw_line) > 3 else ""
                )
                in_hunk = False
                continue

            is_new_path = (
                raw_line.startswith("+++")
                and (len(raw_line) == 3 or raw_line[3] in " \t")
                and pending_old_path is not None
            )
            if is_new_path and not hunk_incomplete:
                new_path = (
                    _path_value(raw_line) if len(raw_line) > 3 else ""
                )
                inverted.append(f"--- {new_path}")
                inverted.append(f"+++ {pending_old_path}")
                pending_old_path = None
                continue

            if pending_old_path is not None:
                inverted.append(f"--- {pending_old_path}")
                pending_old_path = None

            if raw_line.startswith("-"):
                inverted.append("+" + raw_line[1:])
                if in_hunk:
                    old_seen += 1
            elif raw_line.startswith("+"):
                inverted.append("-" + raw_line[1:])
                if in_hunk:
                    new_seen += 1
            else:
                inverted.append(raw_line)
                if in_hunk and raw_line.startswith(" "):
                    old_seen += 1
                    new_seen += 1

        if pending_old_path is not None:
            inverted.append(f"--- {pending_old_path}")

        output = "\n".join(inverted)
        if patch_text.endswith("\n"):
            output += "\n"
        return output

    def cache_info(self) -> dict[str, int]:
        with self._lock:
            return {
                "entries": len(self._cache),
                "hits": self._cache_hits,
                "misses": self._cache_misses,
            }

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._cache_hits = 0
            self._cache_misses = 0

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in self._stats:
                self._stats[key] = 0
