"""Incremental scan planning over a reverse dependency closure."""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Set, Tuple

DEFAULT_MAX_BATCH = 10000
DEFAULT_MAX_EDGES = 500000
SNAPSHOT_VERSION = 1


class PlannerError(ValueError):
    """Raised for every rejected change, dependency or plan."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def _is_nonempty_string(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _is_positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


class IncrementalScanPlanner:
    """Track file digests, dependency edges and batched rescan plans."""

    def __init__(
        self,
        *,
        max_batch: int = DEFAULT_MAX_BATCH,
        max_edges: int = DEFAULT_MAX_EDGES,
    ) -> None:
        self.max_batch = max_batch
        self.max_edges = max_edges
        self._lock = threading.RLock()
        self._files: Dict[str, Dict[str, Any]] = {}
        self._dependencies: Dict[str, Set[str]] = {}
        self._dependents: Dict[str, Set[str]] = {}
        self._edge_count = 0
        self._pending: Set[str] = set()
        self._roots: Dict[str, Set[str]] = {}
        self._plans: Dict[int, Dict[str, Any]] = {}
        self._next_plan_id = 1

    def update(self, path: str, digest: str) -> bool:
        if not _is_nonempty_string(path):
            raise PlannerError("PLAN_INVALID_PATH", "path must be a non-empty string")
        if not _is_nonempty_string(digest):
            raise PlannerError(
                "PLAN_INVALID_DIGEST", "digest must be a non-empty string"
            )

        with self._lock:
            record = self._files.get(path)
            if record is not None and record["digest"] == digest:
                record["deleted"] = False
                return False

            self._files[path] = {
                "path": path,
                "digest": digest,
                "deleted": False,
            }
            self._apply_root_locked(path)
            return True

    def remove(self, path: str) -> bool:
        with self._lock:
            record = self._files.get(path)
            if record is None or record["deleted"]:
                return False

            record["deleted"] = True
            self._pending.discard(path)
            self._roots.pop(path, None)
            self._apply_root_locked(path)
            return True

    def add_dependency(self, referrer: str, dependency: str) -> tuple:
        if not _is_nonempty_string(referrer):
            raise PlannerError(
                "PLAN_INVALID_PATH", "referrer must be a non-empty string"
            )
        if not _is_nonempty_string(dependency):
            raise PlannerError(
                "PLAN_INVALID_PATH", "dependency must be a non-empty string"
            )

        edge = (referrer, dependency)
        with self._lock:
            existing = self._dependencies.get(referrer)
            if existing is not None and dependency in existing:
                return edge

            if self._edge_count >= self.max_edges:
                raise PlannerError(
                    "PLAN_EDGE_CAPACITY_EXCEEDED",
                    {"max_edges": self.max_edges, "edge": list(edge)},
                )

            self._ensure_file_locked(referrer)
            self._ensure_file_locked(dependency)
            self._dependencies.setdefault(referrer, set()).add(dependency)
            self._dependents.setdefault(dependency, set()).add(referrer)
            self._edge_count += 1
            self._propagate_new_edge_locked(referrer, dependency)
            return edge

    def plan(self, budget: int) -> dict:
        if not _is_positive_int(budget):
            raise PlannerError(
                "PLAN_BUDGET_INVALID", "budget must be a positive integer"
            )
        if budget > self.max_batch:
            raise PlannerError(
                "PLAN_BUDGET_EXCEEDED",
                {"budget": budget, "max_batch": self.max_batch},
            )

        with self._lock:
            files = sorted(self._pending)
            if not files:
                raise PlannerError("PLAN_NOTHING_TO_SCAN", "no pending files")

            selected = files[:budget]
            total = len(files)
            plan_id = self._next_plan_id
            self._next_plan_id += 1
            self._plans[plan_id] = {
                "plan_id": plan_id,
                "files": selected,
                "total": total,
                "truncated": len(selected) < total,
                "committed": False,
                "committed_count": 0,
            }
            return {
                "plan_id": plan_id,
                "files": list(selected),
                "total": total,
                "truncated": len(selected) < total,
            }

    def commit(self, plan_id: int) -> dict:
        if not _is_positive_int(plan_id):
            raise PlannerError("PLAN_UNKNOWN", {"plan_id": plan_id})

        with self._lock:
            plan_record = self._plans.get(plan_id)
            if plan_record is None:
                raise PlannerError("PLAN_UNKNOWN", {"plan_id": plan_id})
            if plan_record["committed"]:
                raise PlannerError(
                    "PLAN_ALREADY_COMMITTED", {"plan_id": plan_id}
                )

            planned_files = set(plan_record["files"])
            self._pending.difference_update(planned_files)
            for path in planned_files:
                self._roots.pop(path, None)

            committed = len(plan_record["files"])
            plan_record["committed"] = True
            plan_record["committed_count"] = committed
            plan_record["remaining"] = len(self._pending)

            return {
                "plan_id": plan_id,
                "committed": committed,
                "remaining": len(self._pending),
            }

    def pending(self) -> list[str]:
        with self._lock:
            return sorted(self._pending)

    def explain(self, path: str) -> dict:
        with self._lock:
            record = self._files.get(path)
            pending = path in self._pending

            if record is not None and record["deleted"]:
                reason = "deleted"
                roots: List[str] = []
            elif not pending:
                reason = "clean"
                roots = []
            else:
                path_roots = self._roots.get(path, set())
                roots = sorted(path_roots)
                reason = "changed" if path in path_roots else "dependent"

            return {
                "path": path,
                "pending": pending,
                "reason": reason,
                "roots": roots,
            }

    def files(self) -> list[dict]:
        with self._lock:
            return copy.deepcopy(
                [self._files[path] for path in sorted(self._files)]
            )

    def dependencies(self, path: str) -> list[str]:
        with self._lock:
            return sorted(self._dependencies.get(path, set()))

    def dependents(self, path: str) -> list[str]:
        with self._lock:
            return sorted(self._dependents.get(path, set()))

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": SNAPSHOT_VERSION,
                "files": copy.deepcopy(
                    [self._files[path] for path in sorted(self._files)]
                ),
                "edges": [
                    [referrer, dependency]
                    for referrer in sorted(self._dependencies)
                    for dependency in sorted(self._dependencies[referrer])
                ],
                "pending": sorted(self._pending),
                "roots": {
                    path: sorted(self._roots[path])
                    for path in sorted(self._roots)
                },
                "plans": [
                    copy.deepcopy(self._plans[plan_id])
                    for plan_id in sorted(self._plans)
                ],
                "next_plan_id": self._next_plan_id,
            }

    def restore(self, state: dict) -> None:
        parsed = self._parse_snapshot(state)
        with self._lock:
            self._files = parsed["files"]
            self._dependencies = parsed["dependencies"]
            self._dependents = parsed["dependents"]
            self._edge_count = len(parsed["edges"])
            self._pending = parsed["pending"]
            self._roots = parsed["roots"]
            self._plans = parsed["plans"]
            self._next_plan_id = parsed["next_plan_id"]

    def stats(self) -> dict:
        with self._lock:
            return {
                "files": len(self._files),
                "edges": self._edge_count,
                "dirty": len(self._pending),
            }

    def reset_stats(self) -> None:
        with self._lock:
            return None

    def _ensure_file_locked(self, path: str) -> None:
        if path not in self._files:
            self._files[path] = {
                "path": path,
                "digest": None,
                "deleted": False,
            }

    def _reverse_closure_locked(self, start: str) -> Set[str]:
        seen: Set[str] = set()
        stack = [start]
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self._dependents.get(current, set()))
        return seen

    def _apply_root_locked(self, root: str) -> None:
        closure = self._reverse_closure_locked(root)
        for path in closure:
            record = self._files.get(path)
            if record is not None and record["deleted"]:
                continue
            self._pending.add(path)
            self._roots.setdefault(path, set()).add(root)

    def _propagate_new_edge_locked(self, referrer: str, dependency: str) -> None:
        origins = set(self._roots.get(dependency, set()))
        if not origins:
            return

        for path in self._reverse_closure_locked(referrer):
            record = self._files.get(path)
            if record is not None and record["deleted"]:
                continue
            self._pending.add(path)
            self._roots.setdefault(path, set()).update(origins)

    @classmethod
    def _snapshot_invalid(cls, detail: object = None) -> PlannerError:
        return PlannerError("PLAN_SNAPSHOT_INVALID", detail)

    def _parse_snapshot(self, state: object) -> Dict[str, Any]:
        if not isinstance(state, dict):
            raise self._snapshot_invalid("snapshot must be a dict")

        version = state.get("version")
        if isinstance(version, bool) or version != SNAPSHOT_VERSION:
            raise self._snapshot_invalid({"version": version})

        required = (
            "files",
            "edges",
            "pending",
            "roots",
            "plans",
            "next_plan_id",
        )
        if any(key not in state for key in required):
            raise self._snapshot_invalid("snapshot is missing required fields")

        files = self._parse_snapshot_files(state["files"])
        edges, dependencies, dependents = self._parse_snapshot_edges(
            state["edges"], files
        )
        pending = self._parse_snapshot_pending(state["pending"], files)
        roots = self._parse_snapshot_roots(state["roots"], pending, files)
        plans = self._parse_snapshot_plans(state["plans"], files)
        next_plan_id = state["next_plan_id"]
        if not _is_positive_int(next_plan_id):
            raise self._snapshot_invalid({"next_plan_id": next_plan_id})
        if any(plan_id >= next_plan_id for plan_id in plans):
            raise self._snapshot_invalid("next_plan_id does not match plans")

        return {
            "files": files,
            "edges": edges,
            "dependencies": dependencies,
            "dependents": dependents,
            "pending": pending,
            "roots": roots,
            "plans": plans,
            "next_plan_id": next_plan_id,
        }

    def _parse_snapshot_files(
        self, raw_files: object
    ) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_files, list):
            raise self._snapshot_invalid("files must be a list")

        files: Dict[str, Dict[str, Any]] = {}
        for raw_record in raw_files:
            if not isinstance(raw_record, dict):
                raise self._snapshot_invalid("file record must be a dict")

            path = raw_record.get("path")
            if not _is_nonempty_string(path):
                raise self._snapshot_invalid("file path is invalid")
            if path in files:
                raise self._snapshot_invalid({"path": path, "reason": "duplicate"})
            if "digest" not in raw_record or "deleted" not in raw_record:
                raise self._snapshot_invalid({"path": path, "reason": "missing field"})

            digest = raw_record["digest"]
            if digest is not None and not _is_nonempty_string(digest):
                raise self._snapshot_invalid({"path": path, "digest": digest})

            deleted = raw_record["deleted"]
            if not isinstance(deleted, bool):
                raise self._snapshot_invalid({"path": path, "deleted": deleted})

            files[path] = {
                "path": path,
                "digest": digest,
                "deleted": deleted,
            }

        return files

    def _parse_snapshot_edges(
        self, raw_edges: object, files: Dict[str, Dict[str, Any]]
    ) -> Tuple[Set[Tuple[str, str]], Dict[str, Set[str]], Dict[str, Set[str]]]:
        if not isinstance(raw_edges, list):
            raise self._snapshot_invalid("edges must be a list")

        edges: Set[Tuple[str, str]] = set()
        dependencies: Dict[str, Set[str]] = {}
        dependents: Dict[str, Set[str]] = {}

        for raw_edge in raw_edges:
            if not isinstance(raw_edge, (list, tuple)) or len(raw_edge) != 2:
                raise self._snapshot_invalid("edge must contain two paths")

            referrer, dependency = raw_edge
            if not _is_nonempty_string(referrer) or not _is_nonempty_string(
                dependency
            ):
                raise self._snapshot_invalid("edge path is invalid")
            if referrer not in files or dependency not in files:
                raise self._snapshot_invalid("edge endpoint is missing from files")

            edge = (referrer, dependency)
            if edge in edges:
                continue
            if len(edges) >= self.max_edges:
                raise PlannerError(
                    "PLAN_EDGE_CAPACITY_EXCEEDED",
                    {"max_edges": self.max_edges, "edge": list(edge)},
                )

            edges.add(edge)
            dependencies.setdefault(referrer, set()).add(dependency)
            dependents.setdefault(dependency, set()).add(referrer)

        return edges, dependencies, dependents

    def _parse_snapshot_pending(
        self, raw_pending: object, files: Dict[str, Dict[str, Any]]
    ) -> Set[str]:
        if not isinstance(raw_pending, list):
            raise self._snapshot_invalid("pending must be a list")

        pending: Set[str] = set()
        for path in raw_pending:
            if not _is_nonempty_string(path) or path not in files:
                raise self._snapshot_invalid({"pending_path": path})
            if files[path]["deleted"]:
                raise self._snapshot_invalid(
                    {"pending_path": path, "reason": "deleted"}
                )
            pending.add(path)
        return pending

    def _parse_snapshot_roots(
        self,
        raw_roots: object,
        pending: Set[str],
        files: Dict[str, Dict[str, Any]],
    ) -> Dict[str, Set[str]]:
        if not isinstance(raw_roots, dict):
            raise self._snapshot_invalid("roots must be a dict")
        if set(raw_roots) != pending:
            raise self._snapshot_invalid("roots do not match pending files")

        roots: Dict[str, Set[str]] = {}
        for path, raw_path_roots in raw_roots.items():
            if path not in pending:
                raise self._snapshot_invalid({"root_path": path})
            if not isinstance(raw_path_roots, list):
                raise self._snapshot_invalid({"root_path": path})

            path_roots: Set[str] = set()
            for root in raw_path_roots:
                if not _is_nonempty_string(root) or root not in files:
                    raise self._snapshot_invalid({"root": root})
                path_roots.add(root)

            if not path_roots:
                raise self._snapshot_invalid(
                    {"root_path": path, "reason": "empty roots"}
                )
            roots[path] = path_roots

        return roots

    def _parse_snapshot_plans(
        self, raw_plans: object, files: Dict[str, Dict[str, Any]]
    ) -> Dict[int, Dict[str, Any]]:
        if not isinstance(raw_plans, list):
            raise self._snapshot_invalid("plans must be a list")

        plans: Dict[int, Dict[str, Any]] = {}
        for raw_plan in raw_plans:
            if not isinstance(raw_plan, dict):
                raise self._snapshot_invalid("plan must be a dict")

            plan_id = raw_plan.get("plan_id")
            if not _is_positive_int(plan_id) or plan_id in plans:
                raise self._snapshot_invalid({"plan_id": plan_id})

            raw_files = raw_plan.get("files")
            if not isinstance(raw_files, list):
                raise self._snapshot_invalid({"plan_id": plan_id})
            if any(not _is_nonempty_string(path) for path in raw_files):
                raise self._snapshot_invalid({"plan_id": plan_id})
            if any(path not in files for path in raw_files):
                raise self._snapshot_invalid(
                    {"plan_id": plan_id, "reason": "missing file"}
                )

            committed = raw_plan.get("committed")
            if not isinstance(committed, bool):
                raise self._snapshot_invalid({"plan_id": plan_id})

            total = raw_plan.get("total", len(raw_files))
            if not _is_positive_int(total) or total < len(set(raw_files)):
                raise self._snapshot_invalid({"plan_id": plan_id, "total": total})

            truncated = raw_plan.get("truncated", total > len(raw_files))
            if not isinstance(truncated, bool):
                raise self._snapshot_invalid({"plan_id": plan_id})

            files_list = sorted(set(raw_files))
            plans[plan_id] = {
                "plan_id": plan_id,
                "files": files_list,
                "total": total,
                "truncated": truncated,
                "committed": committed,
                "committed_count": raw_plan.get("committed_count", 0),
            }
            if "remaining" in raw_plan:
                plans[plan_id]["remaining"] = raw_plan["remaining"]

        return plans
