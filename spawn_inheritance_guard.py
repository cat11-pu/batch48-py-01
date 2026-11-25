"""Spawn inheritance guard: env allowlist, descriptor plan, cwd and umask."""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import os
import platform
import posixpath
import re
import stat
import sys
import threading
from collections.abc import Mapping


class SpawnGuardError(ValueError):
    """Rejection raised while composing or reviewing a spawn plan."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_SNAPSHOT_VERSION = 1
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_FD_KINDS = frozenset({"file", "pipe", "socket", "dir", "tty"})
_REQUIRED_PLAN_KEYS = (
    "argv",
    "env",
    "fd_plan",
    "cwd",
    "umask",
    "revision",
    "fingerprint",
)


class InheritanceGuard:
    """Compose the execution environment handed to a child process."""

    def __init__(self, *, max_env: int = 64, max_env_value: int = 4096,
                 max_fds: int = 32, cwd_root: str = "/srv/sandbox",
                 default_umask: int = 0o077, clock=None) -> None:
        self.max_env = max_env
        self.max_env_value = max_env_value
        self.max_fds = max_fds
        self.cwd_root = cwd_root
        self.default_umask = self._valid_umask(default_umask)
        self._clock = clock
        self._lock = threading.RLock()
        self._revision = 0
        self._pending_umask = self.default_umask
        self._plans: list[dict] = []
        self._fd_kinds: dict[int, dict[int, str]] = {}
        self._stats = {"planned": 0, "verified": 0}

    @staticmethod
    def _error(code: str, detail: object = None) -> SpawnGuardError:
        return SpawnGuardError(code, detail)

    @classmethod
    def _valid_env_name(cls, name: object) -> bool:
        return isinstance(name, str) and _ENV_NAME_RE.fullmatch(name) is not None

    @staticmethod
    def _case_insensitive_env() -> bool:
        return (
            os.name == "nt"
            or sys.platform.lower().startswith("win")
            or platform.system() == "Windows"
            or os.path.normcase("ENV_NAME") == os.path.normcase("env_name")
        )

    @classmethod
    def _validate_allowlist(cls, allowlist: object) -> list:
        try:
            entries = list(allowlist)  # type: ignore[arg-type]
        except TypeError as exc:
            raise cls._error("SPAWN_ALLOWLIST_INVALID", "allowlist must be iterable") from exc

        for entry in entries:
            if not cls._valid_env_name(entry):
                raise cls._error("SPAWN_ALLOWLIST_INVALID", {"entry": entry})
        return entries

    @staticmethod
    def _valid_umask(mask: object) -> int:
        if isinstance(mask, bool) or not isinstance(mask, int):
            raise SpawnGuardError("SPAWN_UMASK_INVALID", {"umask": mask})
        if mask < 0 or mask > 0o777:
            raise SpawnGuardError("SPAWN_UMASK_INVALID", {"umask": mask})
        return mask

    def build_env(self, base: dict, allowlist, overrides: dict | None = None) -> dict:
        entries = self._validate_allowlist(allowlist)

        if not isinstance(base, Mapping):
            raise self._error("SPAWN_ENV_NAME_INVALID", "env_base must be a mapping")
        if overrides is None:
            overrides = {}
        if not isinstance(overrides, Mapping):
            raise self._error("SPAWN_ENV_NAME_INVALID", "env_overrides must be a mapping")

        case_insensitive = self._case_insensitive_env()

        def lookup_key(name: str) -> str:
            return name.casefold() if case_insensitive else name

        canonical_names: dict[str, str] = {}
        for entry in entries:
            canonical_names.setdefault(lookup_key(entry), entry)

        for name in overrides:
            if not self._valid_env_name(name):
                raise self._error("SPAWN_ENV_NAME_INVALID", {"name": name})
            if lookup_key(name) not in canonical_names:
                raise self._error("SPAWN_ENV_NOT_ALLOWED", {"name": name})

        env: dict[str, str] = {}
        for lookup, canonical_name in canonical_names.items():
            if canonical_name in base:
                env[canonical_name] = base[canonical_name]
                continue

            if case_insensitive:
                for base_name, base_value in base.items():
                    if isinstance(base_name, str) and lookup_key(base_name) == lookup:
                        env[canonical_name] = base_value
                        break
            elif lookup in base:
                env[canonical_name] = base[lookup]

        for name, value in overrides.items():
            canonical_name = canonical_names[lookup_key(name)]
            env[canonical_name] = value

        for name, value in env.items():
            if not isinstance(value, str):
                raise self._error("SPAWN_ENV_VALUE_INVALID", {"name": name})
            if "\n" in value or "\r" in value or "\0" in value:
                raise self._error("SPAWN_ENV_VALUE_INVALID", {"name": name})

        if len(env) > self.max_env:
            raise self._error(
                "SPAWN_ENV_LIMIT",
                {"count": len(env), "max_env": self.max_env},
            )

        for name, value in env.items():
            if len(value) > self.max_env_value:
                raise self._error(
                    "SPAWN_ENV_LIMIT",
                    {"name": name, "length": len(value), "max_env_value": self.max_env_value},
                )

        return {name: env[name] for name in sorted(env)}

    def _index_fds(self, inherited: object) -> dict[int, dict]:
        if inherited is None:
            rows = []
        else:
            try:
                rows = list(inherited)
            except TypeError as exc:
                raise self._error("SPAWN_FD_INVALID", "fds must be iterable") from exc

        by_fd: dict[int, dict] = {}
        for item in rows:
            if not isinstance(item, Mapping):
                raise self._error("SPAWN_FD_INVALID", {"fd": item})

            fd = item.get("fd")
            if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
                raise self._error("SPAWN_FD_INVALID", {"fd": fd})
            if fd in by_fd:
                raise self._error("SPAWN_FD_DUPLICATE", {"fd": fd})

            kind = item.get("kind")
            if kind not in _FD_KINDS:
                raise self._error("SPAWN_FD_INVALID", {"fd": fd, "kind": kind})

            cloexec = item.get("cloexec")
            if not isinstance(cloexec, bool):
                raise self._error("SPAWN_FD_INVALID", {"fd": fd, "cloexec": cloexec})

            by_fd[fd] = {"fd": fd, "kind": kind, "cloexec": cloexec}

        return by_fd

    def _parse_fd_keep(self, keep: object, known_fds: Mapping[int, dict]) -> set[int]:
        if keep is None:
            raw_keep = []
        else:
            try:
                raw_keep = list(keep)
            except TypeError as exc:
                raise self._error("SPAWN_FD_INVALID", "fd_keep must be iterable") from exc

        kept: set[int] = set()
        for fd in raw_keep:
            if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
                raise self._error("SPAWN_FD_INVALID", {"fd": fd})
            if fd in kept:
                raise self._error("SPAWN_FD_DUPLICATE", {"fd": fd})
            kept.add(fd)

        if len(kept) > self.max_fds:
            raise self._error(
                "SPAWN_FD_INVALID",
                {"kept": len(kept), "max_fds": self.max_fds},
            )

        for fd in kept:
            if fd not in known_fds:
                raise self._error("SPAWN_FD_UNKNOWN", {"fd": fd})

        return kept

    @staticmethod
    def _build_fd_plan(by_fd: Mapping[int, dict], kept: set[int]) -> list[dict]:
        rows = []
        for fd in sorted(by_fd):
            descriptor = by_fd[fd]
            if fd in kept:
                action = "keep"
                cloexec = False
            elif descriptor["cloexec"]:
                action = "drop"
                cloexec = True
            else:
                action = "close"
                cloexec = False
            rows.append({"fd": fd, "action": action, "cloexec": cloexec})
        return rows

    def filter_fds(self, inherited, keep) -> list:
        by_fd = self._index_fds(inherited)
        kept = self._parse_fd_keep(keep, by_fd)
        return self._build_fd_plan(by_fd, kept)

    def prepare_cwd(self, path: str, root: str | None = None) -> str:
        if not isinstance(path, str) or not path or "\0" in path:
            raise self._error("SPAWN_CWD_INVALID", {"cwd": path})
        if not posixpath.isabs(path):
            raise self._error("SPAWN_CWD_INVALID", {"cwd": path})

        root_path = self.cwd_root if root is None else root
        if not isinstance(root_path, str) or not root_path or "\0" in root_path:
            raise self._error("SPAWN_CWD_INVALID", {"cwd_root": root_path})
        if not posixpath.isabs(root_path):
            raise self._error("SPAWN_CWD_INVALID", {"cwd_root": root_path})

        normalized_root = posixpath.normpath(root_path)
        normalized_path = posixpath.normpath(path)

        if normalized_path == normalized_root:
            return normalized_path

        root_prefix = normalized_root if normalized_root == "/" else normalized_root + "/"
        if not normalized_path.startswith(root_prefix):
            raise self._error(
                "SPAWN_CWD_ESCAPE",
                {"cwd": normalized_path, "root": normalized_root},
            )

        return normalized_path

    def apply_umask(self, mask: int) -> int:
        valid_mask = self._valid_umask(mask)
        with self._lock:
            self._pending_umask = valid_mask
        return valid_mask

    def pending_umask(self) -> int:
        with self._lock:
            return self._pending_umask

    @staticmethod
    def _clean_argv(argv: object) -> list[str]:
        if not isinstance(argv, (list, tuple)) or not argv:
            raise SpawnGuardError("SPAWN_ARGV_INVALID", {"argv": argv})

        cleaned = []
        for argument in argv:
            if not isinstance(argument, str) or "\0" in argument:
                raise SpawnGuardError("SPAWN_ARGV_INVALID", {"argv": argv})
            cleaned.append(argument)
        return cleaned

    @staticmethod
    def _fingerprint(plan_without_fingerprint: Mapping[str, object]) -> str:
        payload = {
            key: value
            for key, value in plan_without_fingerprint.items()
            if key != "fingerprint"
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _validate_plan_shape(self, plan: object) -> Mapping[str, object]:
        if not isinstance(plan, Mapping):
            raise self._error("SPAWN_PLAN_INVALID", "plan must be a mapping")
        if any(key not in plan for key in _REQUIRED_PLAN_KEYS):
            raise self._error("SPAWN_PLAN_INVALID", "plan is missing required fields")
        return plan

    def _reject_unsafe_kept_descriptors(
        self, plan: Mapping[str, object], declared_kinds: Mapping[int, str] | None = None
    ) -> None:
        fd_plan = plan.get("fd_plan")
        if not isinstance(fd_plan, list):
            raise self._error("SPAWN_PLAN_INVALID", "fd_plan must be a list")

        for row in fd_plan:
            if not isinstance(row, Mapping) or row.get("action") != "keep":
                continue

            fd = row.get("fd")
            if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
                raise self._error("SPAWN_FD_INVALID", {"fd": fd})

            declared_kind = None if declared_kinds is None else declared_kinds.get(fd)
            if declared_kind == "dir":
                raise self._error("SPAWN_FD_INVALID", {"fd": fd, "kind": "dir"})

            try:
                descriptor_stat = os.fstat(fd)
            except OSError:
                descriptor_stat = None

            if descriptor_stat is not None:
                if stat.S_ISDIR(descriptor_stat.st_mode):
                    raise self._error("SPAWN_FD_INVALID", {"fd": fd, "kind": "dir"})
                if declared_kind == "file" and not stat.S_ISREG(descriptor_stat.st_mode):
                    raise self._error(
                        "SPAWN_FD_INVALID",
                        {"fd": fd, "kind": declared_kind},
                    )

    def plan(self, spec: dict) -> dict:
        if not isinstance(spec, Mapping):
            raise self._error("SPAWN_PLAN_INVALID", "spec must be a mapping")

        argv = self._clean_argv(spec.get("argv"))

        if spec.get("cwd_missing"):
            raise self._error("SPAWN_CWD_MISSING", {"cwd": spec.get("cwd")})

        env = self.build_env(
            spec.get("env_base", {}),
            spec.get("env_allowlist", []),
            spec.get("env_overrides"),
        )

        by_fd = self._index_fds(spec.get("fds", []))
        kept_fds = self._parse_fd_keep(spec.get("fd_keep", []), by_fd)
        fd_plan = self._build_fd_plan(by_fd, kept_fds)

        cwd = self.prepare_cwd(spec.get("cwd"), spec.get("cwd_root"))

        if "umask" in spec:
            umask = self._valid_umask(spec["umask"])
        else:
            umask = self.pending_umask()

        with self._lock:
            revision = self._revision + 1
            plan = {
                "argv": argv,
                "env": env,
                "fd_plan": fd_plan,
                "cwd": cwd,
                "umask": umask,
                "revision": revision,
            }
            plan["fingerprint"] = self._fingerprint(plan)

            self._revision = revision
            self._pending_umask = umask
            retained_plan = copy.deepcopy(plan)
            self._plans.append(retained_plan)
            self._fd_kinds[revision] = {
                fd: by_fd[fd]["kind"] for fd in kept_fds
            }
            self._stats["planned"] += 1

            return copy.deepcopy(plan)

    def verify(self, plan: dict) -> dict:
        checked_plan = self._validate_plan_shape(plan)
        fingerprint = checked_plan["fingerprint"]

        try:
            expected_fingerprint = self._fingerprint(dict(checked_plan))
        except (TypeError, ValueError) as exc:
            raise self._error("SPAWN_PLAN_TAMPERED", "plan cannot be canonicalized") from exc

        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) < 16
            or not hmac.compare_digest(fingerprint, expected_fingerprint)
        ):
            raise self._error("SPAWN_PLAN_TAMPERED", "fingerprint mismatch")

        revision = checked_plan["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise self._error("SPAWN_PLAN_INVALID", "invalid revision")

        env = checked_plan["env"]
        fd_plan = checked_plan["fd_plan"]
        if not isinstance(env, Mapping) or not isinstance(fd_plan, list):
            raise self._error("SPAWN_PLAN_INVALID", "invalid env or fd_plan")

        with self._lock:
            declared_kinds = self._fd_kinds.get(revision)

        self._reject_unsafe_kept_descriptors(checked_plan, declared_kinds)

        with self._lock:
            self._stats["verified"] += 1

        return {
            "ok": True,
            "revision": revision,
            "env_keys": sorted(env.keys()),
            "fd_kept": [
                row["fd"]
                for row in fd_plan
                if isinstance(row, Mapping) and row.get("action") == "keep"
            ],
        }

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "revision": self._revision,
                "pending_umask": self._pending_umask,
                "plans": copy.deepcopy(self._plans),
                "stats": {
                    "planned": self._stats["planned"],
                    "verified": self._stats["verified"],
                    "plans_retained": len(self._plans),
                },
            }

    def restore(self, state: dict) -> None:
        if not isinstance(state, Mapping):
            raise self._error("SPAWN_SNAPSHOT_INVALID", "snapshot must be a mapping")
        version = state.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version != _SNAPSHOT_VERSION
        ):
            raise self._error("SPAWN_SNAPSHOT_INVALID", "unsupported snapshot version")

        required = ("revision", "pending_umask", "plans", "stats")
        if any(key not in state for key in required):
            raise self._error("SPAWN_SNAPSHOT_INVALID", "snapshot is incomplete")

        revision = state["revision"]
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise self._error("SPAWN_SNAPSHOT_INVALID", "invalid revision")

        try:
            pending_umask = self._valid_umask(state["pending_umask"])
        except SpawnGuardError as exc:
            raise self._error("SPAWN_SNAPSHOT_INVALID", "invalid pending_umask") from exc

        plans = state["plans"]
        if not isinstance(plans, list):
            raise self._error("SPAWN_SNAPSHOT_INVALID", "plans must be a list")
        for retained_plan in plans:
            if not isinstance(retained_plan, Mapping):
                raise self._error("SPAWN_SNAPSHOT_INVALID", "plans must contain mappings")

        stats = state["stats"]
        if not isinstance(stats, Mapping):
            raise self._error("SPAWN_SNAPSHOT_INVALID", "stats must be a mapping")

        restored_stats = {}
        for key in ("planned", "verified", "plans_retained"):
            value = stats.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise self._error("SPAWN_SNAPSHOT_INVALID", {"stat": key})
            restored_stats[key] = value

        if restored_stats["plans_retained"] != len(plans):
            raise self._error("SPAWN_SNAPSHOT_INVALID", "plans_retained mismatch")

        try:
            restored_plans = copy.deepcopy(plans)
        except Exception as exc:
            raise self._error("SPAWN_SNAPSHOT_INVALID", "plans are not restorable") from exc
        with self._lock:
            self._revision = revision
            self._pending_umask = pending_umask
            self._plans = restored_plans
            self._fd_kinds = {}
            self._stats = {
                "planned": restored_stats["planned"],
                "verified": restored_stats["verified"],
            }

    def stats(self) -> dict:
        with self._lock:
            return {
                "planned": self._stats["planned"],
                "verified": self._stats["verified"],
                "plans_retained": len(self._plans),
            }

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {"planned": 0, "verified": 0}
            self._plans.clear()
