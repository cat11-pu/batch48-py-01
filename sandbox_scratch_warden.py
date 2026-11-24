"""Sandbox scratch warden: quota-scoped work directories with path containment."""

from __future__ import annotations

import copy
import os
import re
import shutil
import stat
import threading

BASE_ROOT = "/srv/scratch"
_JOB_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_MIB = 1024 * 1024
_MAX_LINK_EXPANSIONS = 64


class ScratchError(ValueError):
    """Raised when a scratch directory operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ScratchWarden:
    """Allocate, meter and reclaim per-job scratch directories."""

    def __init__(self, *, base_root: str = BASE_ROOT, retention: int = 5) -> None:
        self.base_root = os.path.normpath(os.fspath(base_root))
        self.retention = retention
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._links: dict[str, dict[str, str]] = {}
        self._tombstones: dict[str, dict] = {}
        self._handles: dict[str, dict] = {}
        self._next_handle = 1
        self._observed_at = 0

    def allocate(self, job_id: str, quota_mb: int = 16, max_files: int = 64,
                 now: int = 0) -> dict:
        self._validate_job_id(job_id)
        if not self._positive_int(quota_mb):
            raise ScratchError("SCRATCH_INVALID_QUOTA", quota_mb)
        if not self._positive_int(max_files):
            raise ScratchError("SCRATCH_INVALID_MAX_FILES", max_files)

        with self._lock:
            if job_id in self._jobs:
                raise ScratchError("SCRATCH_DUPLICATE_JOB", job_id)

            root = self._job_root(job_id)
            job = {
                "job_id": job_id,
                "root": root,
                "quota_bytes": quota_mb * _MIB,
                "max_files": max_files,
                "used_bytes": 0,
                "files": 0,
                "created_at": now,
                "state": "ACTIVE",
                "released_at": None,
                "released_bytes": 0,
                "released_files": 0,
                "paths": set(),
            }
            self._ensure_root(root)
            self._jobs[job_id] = job
            self._links[job_id] = {}
            self._observe(now)
            return {
                "job_id": job_id,
                "root": root,
                "quota_bytes": job["quota_bytes"],
                "used_bytes": 0,
                "files": 0,
                "created_at": now,
            }

    def resolve(self, job_id: str, relative_path: str) -> str:
        with self._lock:
            job = self._active_job(job_id)
            return self._resolve_path(job, relative_path)

    def link(self, job_id: str, relative_path: str, target: str) -> str:
        with self._lock:
            job = self._active_job(job_id)
            relative = self._validate_path_string(relative_path)
            self._reject_absolute(relative)
            target = self._validate_path_string(target)
            if not target:
                raise ScratchError("SCRATCH_INVALID_PATH", target)

            parts = relative.split("/")
            while parts and parts[-1] in ("", "."):
                parts.pop()
            if not parts or parts[-1] == "..":
                raise ScratchError("SCRATCH_INVALID_PATH", relative)
            name = parts.pop()
            parent = self._resolve_parts(job, parts)
            link_rel = name if not parent else parent + "/" + name

            if os.path.isabs(target):
                root_abs = os.path.abspath(job["root"])
                target_abs = os.path.abspath(os.path.normpath(target))
                if not self._within(target_abs, root_abs):
                    raise ScratchError("SCRATCH_PATH_ESCAPE", target)
                target_parts = os.path.relpath(target_abs, root_abs).split(os.sep)
                if target_parts == ["."]:
                    target_parts = []
            else:
                parent_parts = parent.split("/") if parent else []
                target_parts = parent_parts + target.split("/")

            target_rel = self._resolve_parts(job, target_parts)
            self._links.setdefault(job_id, {})[link_rel] = target_rel
            self._make_physical_link(job, link_rel, target_rel)
            return self._absolute(job, link_rel)

    def charge(self, job_id: str, byte_count: int, path: str | None = None) -> dict:
        if isinstance(byte_count, bool) or not isinstance(byte_count, int) or byte_count < 0:
            raise ScratchError("SCRATCH_INVALID_CHARGE", byte_count)

        with self._lock:
            job = self._active_job(job_id)
            relative = None
            new_file = False
            if path is not None:
                self._validate_path_string(path)
                relative = self._resolve_relative(job, path)
                if relative:
                    new_file = relative not in job["paths"]

            if new_file and job["files"] + 1 > job["max_files"]:
                raise ScratchError("SCRATCH_FILE_LIMIT", path)
            if job["used_bytes"] + byte_count > job["quota_bytes"]:
                raise ScratchError("SCRATCH_QUOTA_EXCEEDED", byte_count)

            job["used_bytes"] += byte_count
            if new_file:
                job["paths"].add(relative)
                job["files"] = len(job["paths"])
            return self._usage_payload(job)

    def release(self, job_id: str, now: int = 0) -> dict:
        with self._lock:
            job = self._lookup(job_id)
            if job["state"] == "RELEASED":
                raise ScratchError("SCRATCH_UNKNOWN_JOB", job_id)

            reclaimed_bytes = job["used_bytes"]
            reclaimed_files = job["files"]
            job["used_bytes"] = 0
            job["files"] = 0
            job["paths"].clear()
            job["state"] = "RELEASED"
            job["released_at"] = now
            job["released_bytes"] = reclaimed_bytes
            job["released_files"] = reclaimed_files
            self._tombstones[job_id] = {
                "released_at": now,
                "reclaimed_bytes": reclaimed_bytes,
                "reclaimed_files": reclaimed_files,
            }
            self._observe(now)
            return {
                "job_id": job_id,
                "reclaimed_bytes": reclaimed_bytes,
                "reclaimed_files": reclaimed_files,
                "released_at": now,
            }

    def sweep(self, now: int) -> list:
        with self._lock:
            self._observe(now)
            reclaimed = []
            for job_id in sorted(self._jobs):
                job = self._jobs[job_id]
                if job["state"] != "RELEASED":
                    continue
                if now - job["released_at"] <= self.retention:
                    continue
                if self._has_open_handles(job):
                    continue
                if not self._delete_root(job["root"]):
                    continue
                del self._jobs[job_id]
                self._links.pop(job_id, None)
                self._tombstones.pop(job_id, None)
                reclaimed.append(job_id)
            return reclaimed

    def usage(self, job_id: str) -> dict:
        with self._lock:
            return self._usage_payload(self._active_job(job_id))

    def list_jobs(self) -> list:
        with self._lock:
            return [
                {
                    "job_id": job_id,
                    "state": self._jobs[job_id]["state"],
                    "used_bytes": self._jobs[job_id]["used_bytes"],
                    "files": self._jobs[job_id]["files"],
                }
                for job_id in sorted(self._jobs)
            ]

    def orphans(self) -> list:
        with self._lock:
            return [
                job_id for job_id in sorted(self._jobs)
                if self._jobs[job_id]["state"] == "RELEASED"
            ]

    def open_file(self, job_id: str, relative_path: str) -> str:
        with self._lock:
            job = self._active_job(job_id)
            relative = self._resolve_relative(job, relative_path)
            absolute = self._absolute(job, relative)

            file_object = None
            try:
                parent = os.path.dirname(absolute) or job["root"]
                os.makedirs(parent, exist_ok=True)
                root_real = os.path.realpath(job["root"])
                if not self._within(os.path.realpath(parent), root_real):
                    raise ScratchError("SCRATCH_PATH_ESCAPE", relative_path)
                if not self._within(os.path.realpath(absolute), root_real):
                    raise ScratchError("SCRATCH_PATH_ESCAPE", relative_path)
                file_object = open(absolute, "a+b")
            except ScratchError:
                raise
            except OSError:
                file_object = None

            handle = "handle-" + str(self._next_handle)
            self._next_handle += 1
            self._handles[handle] = {
                "job_id": job_id,
                "path": absolute,
                "file": file_object,
            }
            return handle

    def close_file(self, handle: str) -> None:
        with self._lock:
            record = self._handles.pop(handle, None) if isinstance(handle, str) else None
            if record is None:
                raise ScratchError("SCRATCH_UNKNOWN_HANDLE", handle)
            if record["file"] is not None:
                try:
                    record["file"].close()
                except OSError:
                    pass

    def snapshot(self) -> dict:
        with self._lock:
            jobs = {}
            for job_id in sorted(self._jobs):
                job = self._jobs[job_id]
                jobs[job_id] = {
                    "job_id": job["job_id"],
                    "root": job["root"],
                    "quota_bytes": job["quota_bytes"],
                    "max_files": job["max_files"],
                    "used_bytes": job["used_bytes"],
                    "files": job["files"],
                    "created_at": job["created_at"],
                    "state": job["state"],
                    "released_at": job["released_at"],
                    "released_bytes": job["released_bytes"],
                    "released_files": job["released_files"],
                    "paths": sorted(job["paths"]),
                }
            links = {
                job_id: dict(sorted(self._links.get(job_id, {}).items()))
                for job_id in sorted(self._jobs)
            }
            tombstones = {
                job_id: copy.deepcopy(self._tombstones[job_id])
                for job_id in sorted(self._tombstones)
            }
            return {
                "jobs": jobs,
                "links": links,
                "tombstones": tombstones,
                "observed_at": self._observed_at,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            jobs, links, tombstones, observed_at = self._restore_state(state)
            self._jobs = jobs
            self._links = links
            self._tombstones = tombstones
            self._observed_at = observed_at
            self._handles = {}
            self._next_handle = 1

    @staticmethod
    def _positive_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    @staticmethod
    def _nonnegative_int(value: object) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value >= 0

    @staticmethod
    def _number(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def _validate_job_id(self, job_id: object) -> None:
        if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
            raise ScratchError("SCRATCH_INVALID_JOB", job_id)

    def _validate_path_string(self, path: object) -> str:
        if not isinstance(path, str) or "\x00" in path:
            raise ScratchError("SCRATCH_INVALID_PATH", path)
        return path

    @staticmethod
    def _reject_absolute(path: str) -> None:
        if path.startswith("/") or os.path.isabs(path):
            raise ScratchError("SCRATCH_PATH_ESCAPE", path)

    def _job_root(self, job_id: str) -> str:
        return os.path.join(self.base_root, job_id)

    def _absolute(self, job: dict, relative: str) -> str:
        if not relative:
            return job["root"]
        return job["root"] + "/" + relative

    def _lookup(self, job_id: object) -> dict:
        if not isinstance(job_id, str) or job_id not in self._jobs:
            raise ScratchError("SCRATCH_UNKNOWN_JOB", job_id)
        return self._jobs[job_id]

    def _active_job(self, job_id: object) -> dict:
        job = self._lookup(job_id)
        if job["state"] == "RELEASED":
            raise ScratchError("SCRATCH_JOB_RELEASED", job_id)
        return job

    def _observe(self, now: object) -> None:
        if self._number(now) and now > self._observed_at:
            self._observed_at = now

    def _usage_payload(self, job: dict) -> dict:
        return {
            "job_id": job["job_id"],
            "root": job["root"],
            "quota_bytes": job["quota_bytes"],
            "max_files": job["max_files"],
            "used_bytes": job["used_bytes"],
            "remaining_bytes": job["quota_bytes"] - job["used_bytes"],
            "files": job["files"],
            "state": job["state"],
            "created_at": job["created_at"],
        }

    def _resolve_relative(self, job: dict, relative_path: object) -> str:
        relative = self._validate_path_string(relative_path)
        self._reject_absolute(relative)
        return self._resolve_parts(job, relative.split("/"))

    def _resolve_path(self, job: dict, relative_path: object) -> str:
        return self._absolute(job, self._resolve_relative(job, relative_path))

    def _resolve_parts(self, job: dict, parts: list[str]) -> str:
        links = self._links.get(job["job_id"], {})
        stack: list[str] = []
        queue = list(parts)
        expansions = 0

        while queue:
            part = queue.pop(0)
            if part == "" or part == ".":
                continue
            if part == "..":
                if not stack:
                    raise ScratchError("SCRATCH_PATH_ESCAPE", "/".join(parts))
                stack.pop()
                continue

            candidate = "/".join(stack + [part])
            target = self._actual_link_target(job, candidate)
            if target is None:
                target = links.get(candidate)
            if target is not None:
                expansions += 1
                if expansions > _MAX_LINK_EXPANSIONS:
                    raise ScratchError("SCRATCH_PATH_ESCAPE", candidate)
                stack = []
                if target:
                    queue = target.split("/") + queue
                continue
            stack.append(part)

        return "/".join(stack)

    def _actual_link_target(self, job: dict, relative: str) -> str | None:
        path = self._absolute(job, relative)
        try:
            status = os.lstat(path)
        except (OSError, ValueError):
            return None
        if not stat.S_ISLNK(status.st_mode):
            return None
        try:
            raw_target = os.readlink(path)
        except OSError:
            return None

        root_abs = os.path.abspath(job["root"])
        if os.path.isabs(raw_target):
            target_abs = os.path.abspath(os.path.normpath(raw_target))
        else:
            parent_abs = os.path.dirname(os.path.abspath(path))
            target_abs = os.path.abspath(
                os.path.normpath(os.path.join(parent_abs, raw_target))
            )
        if not self._within(target_abs, root_abs):
            raise ScratchError("SCRATCH_PATH_ESCAPE", relative)
        relative_target = os.path.relpath(target_abs, root_abs)
        if relative_target == ".":
            return ""
        return relative_target.replace(os.sep, "/")

    @staticmethod
    def _within(path: str, root: str) -> bool:
        path_abs = os.path.abspath(path)
        root_abs = os.path.abspath(root)
        try:
            return os.path.commonpath([path_abs, root_abs]) == root_abs
        except ValueError:
            return False

    def _ensure_root(self, root: str) -> None:
        try:
            os.makedirs(self.base_root, exist_ok=True)
            try:
                status = os.lstat(root)
            except FileNotFoundError:
                status = None
            if status is not None and (
                stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode)
            ):
                os.unlink(root)
            os.makedirs(root, exist_ok=True)
        except OSError:
            pass

    def _make_physical_link(self, job: dict, link_rel: str, target_rel: str) -> None:
        link_path = self._absolute(job, link_rel)
        target_path = self._absolute(job, target_rel)
        try:
            os.makedirs(os.path.dirname(link_path), exist_ok=True)
            if os.path.islink(link_path):
                os.unlink(link_path)
            if os.path.lexists(link_path):
                return
            os.symlink(target_path, link_path)
        except OSError:
            pass

    def _has_open_handles(self, job: dict) -> bool:
        job_id = job["job_id"]
        if any(record["job_id"] == job_id for record in self._handles.values()):
            return True
        return self._process_holds_path(job["root"])

    def _process_holds_path(self, root: str) -> bool:
        root_abs = os.path.abspath(root)
        try:
            pids = os.listdir("/proc")
        except OSError:
            return False
        for pid in pids:
            if not pid.isdigit():
                continue
            fd_dir = os.path.join("/proc", pid, "fd")
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target.endswith(" (deleted)"):
                    target = target[:-len(" (deleted)")]
                if os.path.isabs(target) and self._within(target, root_abs):
                    return True
        return False

    @staticmethod
    def _delete_root(root: str) -> bool:
        try:
            status = os.lstat(root)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        try:
            if stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode):
                os.unlink(root)
            else:
                shutil.rmtree(root)
            return True
        except OSError:
            return False

    def _restore_state(self, state: object) -> tuple[dict, dict, dict, object]:
        if not isinstance(state, dict):
            raise self._snapshot_invalid("state must be an object")
        raw_jobs = state.get("jobs")
        raw_links = state.get("links")
        raw_tombstones = state.get("tombstones")
        observed_at = state.get("observed_at")
        if not isinstance(raw_jobs, dict):
            raise self._snapshot_invalid("jobs must be an object")
        if not isinstance(raw_links, dict):
            raise self._snapshot_invalid("links must be an object")
        if not isinstance(raw_tombstones, dict):
            raise self._snapshot_invalid("tombstones must be an object")
        if not self._number(observed_at) or observed_at < 0:
            raise self._snapshot_invalid("observed_at must be non-negative")

        jobs = {}
        for job_id, raw in raw_jobs.items():
            if not isinstance(job_id, str) or _JOB_ID.fullmatch(job_id) is None:
                raise self._snapshot_invalid("invalid job id")
            if not isinstance(raw, dict):
                raise self._snapshot_invalid("job entry must be an object")
            if raw.get("job_id", job_id) != job_id:
                raise self._snapshot_invalid("job id mismatch")

            quota_bytes = raw.get("quota_bytes")
            max_files = raw.get("max_files")
            used_bytes = raw.get("used_bytes")
            files = raw.get("files")
            created_at = raw.get("created_at")
            job_state = raw.get("state")
            if not self._positive_int(quota_bytes):
                raise self._snapshot_invalid("invalid quota_bytes")
            if not self._positive_int(max_files):
                raise self._snapshot_invalid("invalid max_files")
            if not self._nonnegative_int(used_bytes) or used_bytes > quota_bytes:
                raise self._snapshot_invalid("invalid used_bytes")
            if not self._nonnegative_int(files) or files > max_files:
                raise self._snapshot_invalid("invalid files")
            if not self._number(created_at):
                raise self._snapshot_invalid("invalid created_at")
            if job_state not in ("ACTIVE", "RELEASED"):
                raise self._snapshot_invalid("invalid job state")
            if "root" in raw and not isinstance(raw["root"], str):
                raise self._snapshot_invalid("invalid root")

            raw_paths = raw.get("paths", [])
            if not isinstance(raw_paths, list):
                raise self._snapshot_invalid("paths must be a list")
            paths = set()
            for path in raw_paths:
                normalized = self._stored_relative(path, allow_root=False)
                if normalized != path:
                    raise self._snapshot_invalid("path is not canonical")
                paths.add(path)
            if len(paths) != len(raw_paths) or files != len(paths):
                raise self._snapshot_invalid("files do not match paths")

            released_at = raw.get("released_at")
            released_bytes = raw.get("released_bytes", 0)
            released_files = raw.get("released_files", 0)
            if not self._nonnegative_int(released_bytes) or released_bytes > quota_bytes:
                raise self._snapshot_invalid("invalid released_bytes")
            if not self._nonnegative_int(released_files) or released_files > max_files:
                raise self._snapshot_invalid("invalid released_files")
            if job_state == "ACTIVE":
                if released_at is not None:
                    raise self._snapshot_invalid("active job has released_at")
            else:
                if not self._number(released_at):
                    raise self._snapshot_invalid("invalid released_at")
                if used_bytes != 0 or files != 0 or paths:
                    raise self._snapshot_invalid("released job still has usage")

            jobs[job_id] = {
                "job_id": job_id,
                "root": self._job_root(job_id),
                "quota_bytes": quota_bytes,
                "max_files": max_files,
                "used_bytes": used_bytes,
                "files": files,
                "created_at": created_at,
                "state": job_state,
                "released_at": released_at,
                "released_bytes": released_bytes,
                "released_files": released_files,
                "paths": paths,
            }

        links = {}
        for job_id, raw_map in raw_links.items():
            if job_id not in jobs or not isinstance(raw_map, dict):
                raise self._snapshot_invalid("invalid links entry")
            restored_links = {}
            for link_path, target in raw_map.items():
                normalized = self._stored_relative(link_path, allow_root=False)
                if normalized != link_path:
                    raise self._snapshot_invalid("link path is not canonical")
                if not isinstance(target, str) or "\x00" in target:
                    raise self._snapshot_invalid("invalid link target")
                if os.path.isabs(target):
                    root_abs = os.path.abspath(jobs[job_id]["root"])
                    target_abs = os.path.abspath(os.path.normpath(target))
                    if not self._within(target_abs, root_abs):
                        raise self._snapshot_invalid("link target escapes root")
                    target = os.path.relpath(target_abs, root_abs)
                    if target == ".":
                        target = ""
                else:
                    target = self._stored_relative(target, allow_root=True)
                restored_links[link_path] = target
            links[job_id] = restored_links
        for job_id in jobs:
            links.setdefault(job_id, {})

        tombstones = {}
        for job_id, raw in raw_tombstones.items():
            if job_id not in jobs or not isinstance(raw, dict):
                raise self._snapshot_invalid("invalid tombstone")
            job = jobs[job_id]
            if job["state"] != "RELEASED":
                raise self._snapshot_invalid("tombstone for active job")
            released_at = raw.get("released_at")
            reclaimed_bytes = raw.get("reclaimed_bytes")
            reclaimed_files = raw.get("reclaimed_files")
            if not self._number(released_at):
                raise self._snapshot_invalid("invalid tombstone time")
            if not self._nonnegative_int(reclaimed_bytes):
                raise self._snapshot_invalid("invalid tombstone bytes")
            if not self._nonnegative_int(reclaimed_files):
                raise self._snapshot_invalid("invalid tombstone files")
            if (
                released_at != job["released_at"]
                or reclaimed_bytes != job["released_bytes"]
                or reclaimed_files != job["released_files"]
            ):
                raise self._snapshot_invalid("tombstone does not match job")
            tombstones[job_id] = {
                "released_at": released_at,
                "reclaimed_bytes": reclaimed_bytes,
                "reclaimed_files": reclaimed_files,
            }
        for job_id, job in jobs.items():
            if job["state"] == "RELEASED" and job_id not in tombstones:
                tombstones[job_id] = {
                    "released_at": job["released_at"],
                    "reclaimed_bytes": job["released_bytes"],
                    "reclaimed_files": job["released_files"],
                }

        return jobs, links, tombstones, observed_at

    @staticmethod
    def _stored_relative(path: object, *, allow_root: bool) -> str:
        if not isinstance(path, str) or "\x00" in path:
            raise ScratchError("SCRATCH_SNAPSHOT_INVALID", "invalid stored path")
        if path.startswith("/") or os.path.isabs(path):
            raise ScratchError("SCRATCH_SNAPSHOT_INVALID", "absolute stored path")
        if path == "":
            if allow_root:
                return ""
            raise ScratchError("SCRATCH_SNAPSHOT_INVALID", "empty stored path")
        stack = []
        for part in path.split("/"):
            if part == "" or part == ".":
                raise ScratchError("SCRATCH_SNAPSHOT_INVALID", "non-canonical path")
            if part == "..":
                if not stack:
                    raise ScratchError("SCRATCH_SNAPSHOT_INVALID", "escaping path")
                stack.pop()
            else:
                stack.append(part)
        return "/".join(stack)

    @staticmethod
    def _snapshot_invalid(detail: object) -> ScratchError:
        return ScratchError("SCRATCH_SNAPSHOT_INVALID", detail)
