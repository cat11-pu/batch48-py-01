"""Multi render target attachment planning and compatibility grouping."""

from __future__ import annotations

import copy
import threading

STATS_KEYS = (
    "attachments_declared",
    "passes_declared",
    "plans_computed",
    "rejected",
)

_BYTES_PER_TEXEL = {
    "r8": 1,
    "rg8": 2,
    "rgba8": 4,
    "rgb10a2": 4,
    "rgba16f": 8,
    "rgba32f": 16,
    "depth24": 4,
    "depth32f": 4,
}
_DEPTH_FORMATS = frozenset({"depth24", "depth32f"})
_BLEND_UNSUPPORTED_FORMATS = frozenset({"rgba32f"})


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_name(value: object) -> bool:
    return isinstance(value, str) and bool(value)


class MrtPlannerError(ValueError):
    """Domain error carrying a stable machine readable code."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class MrtAttachmentPlanner:
    """Plan colour and depth attachments for multi render target passes."""

    def __init__(self, *, max_color_slots: int = 8, byte_budget: int = 67108864,
                 max_attachments: int = 16, clock=None) -> None:
        self.max_color_slots = max_color_slots
        self.byte_budget = byte_budget
        self.max_attachments = max_attachments
        self._clock = clock
        self._lock = threading.RLock()
        self._attachments: dict[str, dict] = {}
        self._passes: dict[str, dict] = {}
        self._stats = {key: 0 for key in STATS_KEYS}

    def _reject(self, code: str, detail: object = None):
        self._stats["rejected"] += 1
        raise MrtPlannerError(code, detail)

    @staticmethod
    def _build_attachment(name: str, format: str, width: int, height: int,
                          samples: int) -> dict:
        if (
            not _is_name(name)
            or format not in _BYTES_PER_TEXEL
            or not _is_int(width)
            or not 1 <= width <= 16384
            or not _is_int(height)
            or not 1 <= height <= 16384
            or not _is_int(samples)
            or not 1 <= samples <= 8
        ):
            raise MrtPlannerError("MRT_INVALID_ATTACHMENT")

        bytes_per_texel = _BYTES_PER_TEXEL[format]
        return {
            "name": name,
            "format": format,
            "width": width,
            "height": height,
            "samples": samples,
            "bytes_per_texel": bytes_per_texel,
            "bytes": width * height * samples * bytes_per_texel,
        }

    def _require_attachment(self, name: object) -> dict:
        attachment = self._attachments.get(name) if _is_name(name) else None
        if attachment is None:
            self._reject("MRT_UNKNOWN_ATTACHMENT", {"name": name})
        return attachment  # pragma: no cover - _reject always raises

    @staticmethod
    def _pass_attachment_names(pass_info: dict) -> list[str]:
        names = [binding[0] for binding in pass_info["color"]]
        if pass_info["depth"] is not None:
            names.append(pass_info["depth"])
        names.extend(pass_info["reads"])
        return names

    def declare_attachment(self, name: str, *, format: str, width: int,
                           height: int, samples: int = 1) -> dict:
        with self._lock:
            try:
                info = self._build_attachment(
                    name, format, width, height, samples
                )
            except MrtPlannerError as exc:
                self._reject(exc.code, exc.detail)

            current = self._attachments.get(name)
            if current is not None:
                if all(current[field] == info[field] for field in (
                    "format", "width", "height", "samples",
                    "bytes_per_texel", "bytes",
                )):
                    return copy.deepcopy(current)
                self._reject("MRT_DUPLICATE_ATTACHMENT", {"name": name})

            if len(self._attachments) >= self.max_attachments:
                self._reject("MRT_ATTACHMENT_LIMIT", {
                    "attachments": len(self._attachments),
                    "max_attachments": self.max_attachments,
                })

            self._attachments[name] = info
            self._stats["attachments_declared"] += 1
            return copy.deepcopy(info)

    def attachment_info(self, name: str) -> dict:
        with self._lock:
            attachment = self._attachments.get(name)
            if attachment is None:
                self._reject("MRT_UNKNOWN_ATTACHMENT", {"name": name})
            return copy.deepcopy(attachment)

    def drop_attachment(self, name: str) -> None:
        with self._lock:
            if not _is_name(name) or name not in self._attachments:
                self._reject("MRT_UNKNOWN_ATTACHMENT", {"name": name})

            referenced_by = sorted(
                pass_name
                for pass_name, pass_info in self._passes.items()
                if name in self._pass_attachment_names(pass_info)
            )
            if referenced_by:
                self._reject("MRT_IN_USE", {
                    "name": name,
                    "passes": referenced_by,
                })

            del self._attachments[name]

    def declare_pass(self, name: str, *, order: int, color=(), depth=None,
                     blend: bool = False, reads=(), samples: int = 1) -> dict:
        with self._lock:
            if not _is_name(name):
                self._reject("MRT_INVALID_PASS", {"name": name})
            if name in self._passes:
                self._reject("MRT_DUPLICATE_PASS", {"name": name})
            if not _is_int(samples):
                self._reject("MRT_INVALID_PASS", {"reason": "samples"})

            try:
                raw_color = list(color)
            except TypeError:
                self._reject("MRT_INVALID_PASS", {"reason": "color"})

            normalized_color = []
            for binding in raw_color:
                if (
                    not isinstance(binding, (list, tuple))
                    or len(binding) != 2
                    or not _is_name(binding[0])
                ):
                    self._reject("MRT_INVALID_PASS", {"reason": "color_binding"})
                normalized_color.append(
                    [binding[0], copy.deepcopy(binding[1])]
                )

            if len(normalized_color) > self.max_color_slots:
                self._reject("MRT_SLOT_LIMIT", {
                    "color_slots": len(normalized_color),
                })

            try:
                normalized_reads = list(reads)
            except TypeError:
                self._reject("MRT_INVALID_PASS", {"reason": "reads"})

            if any(not _is_name(read_name) for read_name in normalized_reads):
                self._reject("MRT_INVALID_PASS", {"reason": "read_binding"})
            normalized_reads = [
                copy.deepcopy(read_name) for read_name in normalized_reads
            ]

            if depth is not None and not _is_name(depth):
                self._reject("MRT_INVALID_PASS", {"reason": "depth"})

            if not normalized_color and depth is None:
                self._reject("MRT_INVALID_PASS", {"name": name})

            color_names = [binding[0] for binding in normalized_color]
            if len(color_names) != len(set(color_names)):
                self._reject("MRT_INVALID_PASS", {
                    "name": name,
                    "reason": "duplicate_attachment",
                })

            for attachment_name in color_names:
                self._require_attachment(attachment_name)
            if depth is not None:
                depth_attachment = self._require_attachment(depth)
            else:
                depth_attachment = None
            for attachment_name in normalized_reads:
                self._require_attachment(attachment_name)

            all_names = color_names.copy()
            if depth is not None:
                all_names.append(depth)
            all_names.extend(normalized_reads)
            if len(all_names) != len(set(all_names)):
                self._reject("MRT_INVALID_PASS", {
                    "name": name,
                    "reason": "duplicate_attachment",
                })

            for attachment_name in color_names:
                attachment = self._attachments[attachment_name]
                if attachment["samples"] != samples:
                    self._reject("MRT_SAMPLE_MISMATCH", {
                        "pass": name,
                        "attachment": attachment_name,
                        "pass_samples": samples,
                        "attachment_samples": attachment["samples"],
                    })

            if depth_attachment is not None:
                if depth_attachment["format"] not in _DEPTH_FORMATS:
                    self._reject("MRT_FORMAT_UNSUPPORTED", {
                        "pass": name,
                        "attachment": depth,
                        "format": depth_attachment["format"],
                    })
                if depth_attachment["samples"] != samples:
                    self._reject("MRT_SAMPLE_MISMATCH", {
                        "pass": name,
                        "attachment": depth,
                        "pass_samples": samples,
                        "attachment_samples": depth_attachment["samples"],
                    })

            pass_info = {
                "name": name,
                "order": order,
                "color": normalized_color,
                "depth": depth,
                "blend": bool(blend),
                "reads": normalized_reads,
                "samples": samples,
            }
            self._passes[name] = pass_info
            self._stats["passes_declared"] += 1
            return copy.deepcopy(pass_info)

    def pass_info(self, name: str) -> dict:
        with self._lock:
            pass_info = self._passes.get(name)
            if pass_info is None:
                self._reject("MRT_UNKNOWN_PASS", {"name": name})
            return copy.deepcopy(pass_info)

    def plan(self) -> dict:
        with self._lock:
            ordered_passes = sorted(
                (copy.deepcopy(pass_info)
                 for pass_info in self._passes.values()),
                key=lambda item: (item["order"], item["name"]),
            )

            planned_passes = []
            bound_attachments: set[str] = set()
            total_bytes = 0
            color_slots_used = 0

            for pass_info in ordered_passes:
                color_names = [binding[0] for binding in pass_info["color"]]
                depth_name = pass_info["depth"]
                color_slots_used = max(color_slots_used, len(color_names))

                if pass_info["blend"]:
                    for attachment_name in color_names:
                        attachment = self._attachments[attachment_name]
                        if attachment["format"] in _BLEND_UNSUPPORTED_FORMATS:
                            self._reject("MRT_BLEND_UNSUPPORTED", {
                                "pass": pass_info["name"],
                                "attachment": attachment_name,
                                "format": attachment["format"],
                            })

                pass_bytes = 0
                for attachment_name in color_names:
                    attachment = self._attachments[attachment_name]
                    pass_bytes += attachment["bytes"]
                    if attachment_name not in bound_attachments:
                        bound_attachments.add(attachment_name)
                        total_bytes += attachment["bytes"]

                if depth_name is not None:
                    attachment = self._attachments[depth_name]
                    pass_bytes += attachment["bytes"]
                    if depth_name not in bound_attachments:
                        bound_attachments.add(depth_name)
                        total_bytes += attachment["bytes"]

                planned_pass = copy.deepcopy(pass_info)
                planned_pass["bytes"] = pass_bytes
                planned_passes.append(planned_pass)

            if total_bytes > self.byte_budget:
                self._reject("MRT_BUDGET_EXCEEDED", {
                    "total_bytes": total_bytes,
                    "byte_budget": self.byte_budget,
                })

            result = {
                "passes": planned_passes,
                "attachments": [
                    copy.deepcopy(self._attachments[name])
                    for name in sorted(self._attachments)
                ],
                "total_bytes": total_bytes,
                "color_slots_used": color_slots_used,
                "alias_candidates": self._alias_candidates_locked(),
            }
            self._stats["plans_computed"] += 1
            return result

    def compatibility(self, name_a: str, name_b: str) -> dict:
        with self._lock:
            attachment_a = self._require_attachment(name_a)
            attachment_b = self._require_attachment(name_b)
            reasons = []

            if attachment_a["format"] != attachment_b["format"]:
                reasons.append("format_mismatch")
            if (
                attachment_a["width"] != attachment_b["width"]
                or attachment_a["height"] != attachment_b["height"]
            ):
                reasons.append("size_mismatch")
            if attachment_a["samples"] != attachment_b["samples"]:
                reasons.append("sample_mismatch")

            reasons.sort()
            return {"compatible": not reasons, "reasons": reasons}

    def _alias_candidates_locked(self) -> list[dict]:
        first_use: dict[str, object] = {}
        last_use: dict[str, object] = {}
        ordered_passes = sorted(
            self._passes.values(),
            key=lambda item: (item["order"], item["name"]),
        )

        for pass_info in ordered_passes:
            for attachment_name in self._pass_attachment_names(pass_info):
                if attachment_name not in self._attachments:
                    continue
                first_use.setdefault(attachment_name, pass_info["order"])
                last_use[attachment_name] = pass_info["order"]

        candidates = []
        for earlier in sorted(self._attachments):
            if earlier not in first_use:
                continue
            earlier_attachment = self._attachments[earlier]
            for later in sorted(self._attachments):
                if earlier == later or later not in first_use:
                    continue
                later_attachment = self._attachments[later]
                lifetimes_do_not_overlap = (
                    last_use[earlier] < first_use[later]
                )
                same_layout = (
                    earlier_attachment["format"] == later_attachment["format"]
                    and earlier_attachment["width"] == later_attachment["width"]
                    and earlier_attachment["height"] == later_attachment["height"]
                    and earlier_attachment["samples"] == later_attachment["samples"]
                )
                if lifetimes_do_not_overlap and same_layout:
                    candidates.append({
                        "earlier": earlier,
                        "later": later,
                        "bytes": earlier_attachment["bytes"],
                    })

        candidates.sort(key=lambda candidate: (
            candidate["earlier"], candidate["later"]
        ))
        return candidates

    def alias_candidates(self) -> list:
        with self._lock:
            return copy.deepcopy(self._alias_candidates_locked())

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            for key in STATS_KEYS:
                self._stats[key] = 0

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": 1,
                "attachments": self._attachments,
                "passes": self._passes,
                "stats": self._stats,
            })

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                attachments, passes, stats = self._decode_snapshot(state)
            except MrtPlannerError:
                raise
            except Exception as exc:
                raise MrtPlannerError(
                    "MRT_SNAPSHOT_INVALID", {"reason": "invalid_state"}
                ) from exc

            self._attachments = attachments
            self._passes = passes
            self._stats = stats

    def _decode_snapshot(self, state: object):
        if not isinstance(state, dict):
            raise MrtPlannerError("MRT_SNAPSHOT_INVALID")
        if not _is_int(state.get("version")) or state.get("version") != 1:
            raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {"version": state.get("version")})

        raw_attachments = state.get("attachments")
        raw_passes = state.get("passes")
        raw_stats = state.get("stats")
        if not isinstance(raw_attachments, dict):
            raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {"field": "attachments"})
        if not isinstance(raw_passes, dict):
            raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {"field": "passes"})
        if not isinstance(raw_stats, dict):
            raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {"field": "stats"})

        try:
            snapshot = copy.deepcopy(state)
        except Exception as exc:
            raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {"field": "state"}) from exc

        raw_attachments = snapshot["attachments"]
        raw_passes = snapshot["passes"]
        raw_stats = snapshot["stats"]

        attachments: dict[str, dict] = {}
        for name, raw_attachment in raw_attachments.items():
            if not _is_name(name) or not isinstance(raw_attachment, dict):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "attachments", "name": name,
                })
            if "name" in raw_attachment and raw_attachment["name"] != name:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "attachments", "name": name,
                })
            try:
                attachment = self._build_attachment(
                    name,
                    raw_attachment.get("format"),
                    raw_attachment.get("width"),
                    raw_attachment.get("height"),
                    raw_attachment.get("samples", 1),
                )
            except MrtPlannerError as exc:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "attachments", "name": name,
                }) from exc
            attachments[name] = attachment

        stats = {}
        for key in STATS_KEYS:
            value = raw_stats.get(key)
            if not _is_int(value) or value < 0:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "stats", "key": key,
                })
            stats[key] = value

        passes: dict[str, dict] = {}
        for name, raw_pass in raw_passes.items():
            if not _is_name(name) or not isinstance(raw_pass, dict):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name,
                })
            if "name" in raw_pass and raw_pass["name"] != name:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name,
                })
            if "order" not in raw_pass:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "order",
                })
            if not _is_int(raw_pass.get("samples")):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "samples",
                })
            if not isinstance(raw_pass.get("blend"), bool):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "blend",
                })

            raw_color = raw_pass.get("color", ())
            if not isinstance(raw_color, (list, tuple)):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "color",
                })
            color = []
            for binding in raw_color:
                if (
                    not isinstance(binding, (list, tuple))
                    or len(binding) != 2
                    or not _is_name(binding[0])
                ):
                    raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                        "field": "passes", "name": name, "key": "color",
                    })
                color.append([binding[0], binding[1]])

            reads = raw_pass.get("reads", [])
            if not isinstance(reads, (list, tuple)) or any(
                not _is_name(read_name) for read_name in reads
            ):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "reads",
                })

            depth = raw_pass.get("depth")
            if depth is not None and not _is_name(depth):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "depth",
                })
            if not color and depth is None:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "reason": "empty",
                })

            color_names = [binding[0] for binding in color]
            all_names = color_names.copy()
            if depth is not None:
                all_names.append(depth)
            all_names.extend(reads)
            if len(all_names) != len(set(all_names)):
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "reason": "duplicate",
                })

            for attachment_name in all_names:
                if attachment_name not in attachments:
                    raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                        "field": "passes",
                        "name": name,
                        "attachment": attachment_name,
                    })

            samples = raw_pass["samples"]
            if not 1 <= samples <= 8:
                raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                    "field": "passes", "name": name, "key": "samples",
                })
            for attachment_name in color_names:
                if attachments[attachment_name]["samples"] != samples:
                    raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                        "field": "passes", "name": name, "reason": "samples",
                    })
            if depth is not None:
                depth_attachment = attachments[depth]
                if depth_attachment["format"] not in _DEPTH_FORMATS:
                    raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                        "field": "passes", "name": name, "reason": "depth_format",
                    })
                if depth_attachment["samples"] != samples:
                    raise MrtPlannerError("MRT_SNAPSHOT_INVALID", {
                        "field": "passes", "name": name, "reason": "samples",
                    })

            passes[name] = {
                "name": name,
                "order": raw_pass["order"],
                "color": color,
                "depth": depth,
                "blend": raw_pass["blend"],
                "reads": list(reads),
                "samples": samples,
            }

        return attachments, passes, stats
