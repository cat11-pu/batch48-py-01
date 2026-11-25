"""Admission control for a privileged helper: peers, argv whitelist, audit."""

from __future__ import annotations

import copy
import posixpath
import re
import threading
import time


class HelperError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STAT_KEYS = ("executed", "denied", "audited")
_SNAPSHOT_KEYS = ("peers", "commands", "whitelist_version", "stats")
_URL_SCHEME = re.compile(r"^([A-Za-z][A-Za-z0-9+.-]*://)(.*)$")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _normalize_path(value: str) -> str:
    """Collapse path separators and resolve . and .. segments lexically."""
    if not value:
        return value
    if "/" not in value:
        return "" if value == "." else value

    absolute = value.startswith("/")
    parts: list[str] = []
    for part in value.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if parts and parts[-1] != "..":
                parts.pop()
            elif not absolute:
                # Keep leading parent references.  Dropping them could turn
                # "../../proxyctl" into an allowed "proxyctl".
                parts.append(part)
            continue
        parts.append(part)

    if absolute:
        return "/" + "/".join(parts) if parts else "/"
    return "/".join(parts)


def _normalize_argument(value: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        return ""

    match = _URL_SCHEME.match(cleaned)
    if match is None:
        return _normalize_path(cleaned)

    scheme, remainder = match.groups()
    slash = remainder.find("/")
    if slash < 0:
        return scheme + remainder
    authority = remainder[:slash]
    path = remainder[slash:]
    return scheme + authority + _normalize_path(path)


def _path_within(path: str, prefix: str) -> bool:
    if prefix == "/":
        return path.startswith("/")
    return path == prefix or path.startswith(prefix + "/")


class PrivilegedHelperGuard:
    """Check peer credentials, normalize argv, match whitelist and audit."""

    def __init__(self, *, max_audit: int = 256, max_args: int = 16,
                 clock=None) -> None:
        self.max_audit = max_audit
        self.max_args = max_args
        self._clock = clock
        self._lock = threading.RLock()
        self._peers: dict[tuple[int, int], set[str]] = {}
        self._commands: dict[str, dict] = {}
        self._whitelist_version = 0
        self._audit: list[dict] = []
        self._results: dict[str, dict] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    def _now(self, now=None):
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return time.time()

    def allow_peer(self, uid: int, gid: int, exe_prefix: str) -> None:
        if not _is_int(uid) or not _is_int(gid):
            raise HelperError("HELPER_PEER_DENIED", "uid/gid must be integers")
        if not isinstance(exe_prefix, str) or not exe_prefix:
            raise HelperError("HELPER_PEER_DENIED", "exe prefix is required")
        prefix = _normalize_path(exe_prefix)
        if not posixpath.isabs(prefix):
            raise HelperError("HELPER_EXE_DENIED", exe_prefix)
        with self._lock:
            self._peers.setdefault((uid, gid), set()).add(prefix)

    def revoke_peer(self, uid: int, gid: int) -> None:
        with self._lock:
            self._peers.pop((uid, gid), None)

    def register_command(self, command: str, *, argv_prefix=(),
                         max_args: int = 8) -> dict:
        if not isinstance(command, str) or not command.strip():
            raise HelperError("HELPER_COMMAND_DENIED", command)
        if isinstance(argv_prefix, str) or not isinstance(
                argv_prefix, (list, tuple)):
            raise HelperError("HELPER_COMMAND_DENIED", command)
        if any(not isinstance(part, str) for part in argv_prefix):
            raise HelperError("HELPER_COMMAND_DENIED", command)
        if not _is_int(max_args) or max_args < 0:
            raise HelperError("HELPER_COMMAND_DENIED", command)

        normalized_prefix = tuple(
            _normalize_argument(part) for part in argv_prefix
        )
        with self._lock:
            existing = self._commands.get(command)
            if (existing is not None
                    and tuple(existing["argv_prefix"]) != normalized_prefix):
                raise HelperError("HELPER_COMMAND_CONFLICT", command)
            self._commands[command] = {
                "argv_prefix": normalized_prefix,
                "max_args": max_args,
            }
            self._whitelist_version += 1
            return {
                "command": command,
                "argv_prefix": normalized_prefix,
                "version": self._whitelist_version,
            }

    def normalize(self, args) -> list:
        if not isinstance(args, list) or any(
                not isinstance(argument, str) for argument in args):
            raise HelperError(
                "HELPER_REQUEST_INVALID", "args must be a list of strings"
            )
        return [_normalize_argument(argument) for argument in args]

    @staticmethod
    def _parse_request(request: dict) -> dict:
        if not isinstance(request, dict):
            raise HelperError(
                "HELPER_REQUEST_INVALID", "request must be a dict"
            )
        try:
            request_id = request["request_id"]
            peer = request["peer"]
            command = request["command"]
            args = request["args"]
        except KeyError as exc:
            raise HelperError(
                "HELPER_REQUEST_INVALID", "missing %s" % exc.args[0]
            ) from None

        if (not isinstance(request_id, str)
                or not request_id.strip()):
            raise HelperError(
                "HELPER_REQUEST_INVALID", "request_id"
            )
        if not isinstance(peer, dict):
            raise HelperError("HELPER_REQUEST_INVALID", "peer")
        try:
            uid = peer["uid"]
            gid = peer["gid"]
            exe = peer["exe"]
        except KeyError as exc:
            raise HelperError(
                "HELPER_REQUEST_INVALID", "peer.%s" % exc.args[0]
            ) from None
        if not _is_int(uid) or not _is_int(gid):
            raise HelperError("HELPER_REQUEST_INVALID", "peer.uid/gid")
        if not isinstance(exe, str):
            raise HelperError("HELPER_REQUEST_INVALID", "peer.exe")
        if not isinstance(command, str):
            raise HelperError("HELPER_REQUEST_INVALID", "command")
        if not isinstance(args, list) or any(
                not isinstance(argument, str) for argument in args):
            raise HelperError("HELPER_REQUEST_INVALID", "args")
        return {
            "request_id": request_id,
            "uid": uid,
            "gid": gid,
            "exe": exe,
            "command": command,
            "args": list(args),
        }

    @staticmethod
    def _request_metadata(request, now):
        metadata = {
            "request_id": None,
            "uid": None,
            "gid": None,
            "exe": None,
            "command": None,
            "args": None,
            "at": now,
        }
        if not isinstance(request, dict):
            return metadata
        request_id = request.get("request_id")
        if isinstance(request_id, str):
            metadata["request_id"] = request_id
        command = request.get("command")
        if isinstance(command, str):
            metadata["command"] = command
        args = request.get("args")
        if isinstance(args, list) and all(
                isinstance(argument, str) for argument in args):
            metadata["args"] = list(args)
        peer = request.get("peer")
        if isinstance(peer, dict):
            metadata["uid"] = peer.get("uid")
            metadata["gid"] = peer.get("gid")
            metadata["exe"] = peer.get("exe")
        return metadata

    def _append_audit(self, entry: dict) -> None:
        if len(self._audit) >= self.max_audit:
            raise HelperError(
                "HELPER_AUDIT_FULL", {"max_audit": self.max_audit}
            )
        record = copy.deepcopy(entry)
        record["whitelist_version"] = self._whitelist_version
        self._audit.append(record)
        self._stats["audited"] += 1

    def _check_peer(self, peer: dict) -> None:
        key = (peer["uid"], peer["gid"])
        prefixes = self._peers.get(key)
        if prefixes is None:
            uid_match = any(uid == key[0] for uid, _ in self._peers)
            gid_match = any(gid == key[1] for _, gid in self._peers)
            mismatch = []
            if not uid_match:
                mismatch.append("uid")
            if not gid_match:
                mismatch.append("gid")
            if uid_match and gid_match:
                mismatch.append("pair")
            raise HelperError("HELPER_PEER_DENIED", {
                "uid": key[0],
                "gid": key[1],
                "uid_match": uid_match,
                "gid_match": gid_match,
                "uid_mismatch": not uid_match,
                "gid_mismatch": not gid_match,
                "mismatch": mismatch,
                "reason": "+".join(mismatch),
            })

        exe = peer["exe"]
        if not exe or not posixpath.isabs(exe):
            raise HelperError("HELPER_EXE_DENIED", {
                "exe": exe, "reason": "not_absolute"
            })
        normalized_exe = _normalize_path(exe)
        if not any(_path_within(normalized_exe, prefix)
                   for prefix in prefixes):
            raise HelperError("HELPER_EXE_DENIED", {
                "exe": exe, "reason": "prefix"
            })

    def handle(self, request: dict, *, now=None) -> dict:
        with self._lock:
            at = self._now(now)
            parsed = None
            try:
                parsed = self._parse_request(request)
                request_id = parsed["request_id"]
                cached = self._results.get(request_id)
                if cached is not None:
                    return copy.deepcopy(cached)

                self._check_peer(parsed)
                command = self._commands.get(parsed["command"])
                if command is None:
                    raise HelperError(
                        "HELPER_COMMAND_DENIED", parsed["command"]
                    )

                limit = min(command["max_args"], self.max_args)
                if len(parsed["args"]) > limit:
                    raise HelperError("HELPER_ARGC_EXCEEDED", {
                        "count": len(parsed["args"]),
                        "limit": limit,
                    })

                argv = [_normalize_argument(argument)
                        for argument in parsed["args"]]
                prefix = command["argv_prefix"]
                if tuple(argv[:len(prefix)]) != prefix:
                    raise HelperError("HELPER_ARG_DENIED", {
                        "argv": list(argv),
                        "expected_prefix": list(prefix),
                    })

                result = {
                    "status": "executed",
                    "request_id": request_id,
                    "command": parsed["command"],
                    "argv": list(argv),
                    "whitelist_version": self._whitelist_version,
                }
                audit_entry = {
                    "request_id": request_id,
                    "uid": parsed["uid"],
                    "gid": parsed["gid"],
                    "exe": parsed["exe"],
                    "command": parsed["command"],
                    "args": list(parsed["args"]),
                    "argv": list(argv),
                    "status": "executed",
                    "code": "HELPER_OK",
                    "detail": None,
                    "at": at,
                }
                self._append_audit(audit_entry)
                self._stats["executed"] += 1
                self._results[request_id] = copy.deepcopy(result)
                return copy.deepcopy(result)
            except HelperError as exc:
                if exc.code == "HELPER_AUDIT_FULL":
                    raise
                audit_entry = self._request_metadata(request, at)
                if parsed is not None:
                    audit_entry.update({
                        "uid": parsed["uid"],
                        "gid": parsed["gid"],
                        "exe": parsed["exe"],
                        "command": parsed["command"],
                        "args": list(parsed["args"]),
                    })
                audit_entry.update({
                    "status": "denied",
                    "code": exc.code,
                    "detail": copy.deepcopy(exc.detail),
                })
                self._append_audit(audit_entry)
                self._stats["denied"] += 1
                raise

    def audit_entries(self) -> list:
        with self._lock:
            return copy.deepcopy(self._audit)

    def whitelist_version(self) -> int:
        with self._lock:
            return self._whitelist_version

    def snapshot(self) -> dict:
        with self._lock:
            peers = [
                {"uid": uid, "gid": gid, "exe_prefix": prefix}
                for (uid, gid), prefixes in sorted(self._peers.items())
                for prefix in sorted(prefixes)
            ]
            commands = {
                name: {
                    "argv_prefix": list(command["argv_prefix"]),
                    "max_args": command["max_args"],
                }
                for name, command in self._commands.items()
            }
            return copy.deepcopy({
                "version": self._whitelist_version,
                "whitelist_version": self._whitelist_version,
                "peers": peers,
                "commands": commands,
                "stats": dict(self._stats),
                "audit": self._audit,
                "replay": self._results,
            })

    @staticmethod
    def _restore_peers(raw_peers) -> dict[tuple[int, int], set[str]]:
        peers: dict[tuple[int, int], set[str]] = {}

        if isinstance(raw_peers, dict):
            entries = []
            for key, prefixes in raw_peers.items():
                if (not isinstance(key, (list, tuple))
                        or len(key) != 2):
                    raise HelperError("HELPER_SNAPSHOT_INVALID", "peers")
                entries.append({
                    "uid": key[0],
                    "gid": key[1],
                    "exe_prefix": prefixes,
                })
        elif isinstance(raw_peers, list):
            entries = raw_peers
        else:
            raise HelperError("HELPER_SNAPSHOT_INVALID", "peers")

        for item in entries:
            if not isinstance(item, dict):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "peers")
            uid = item.get("uid")
            gid = item.get("gid")
            prefixes = item.get("exe_prefix", item.get("prefix"))
            if not _is_int(uid) or not _is_int(gid):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "peers")
            if isinstance(prefixes, str):
                prefixes = [prefixes]
            if not isinstance(prefixes, (list, tuple, set)):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "peers")
            clean = set()
            for prefix in prefixes:
                if not isinstance(prefix, str) or not posixpath.isabs(prefix):
                    raise HelperError(
                        "HELPER_SNAPSHOT_INVALID", "peers"
                    )
                clean.add(_normalize_path(prefix))
            peers.setdefault((uid, gid), set()).update(clean)
        return peers

    @staticmethod
    def _restore_commands(raw_commands, default_max_args: int) -> dict:
        if not isinstance(raw_commands, dict):
            raise HelperError("HELPER_SNAPSHOT_INVALID", "commands")
        commands = {}
        for name, info in raw_commands.items():
            if not isinstance(name, str) or not name.strip():
                raise HelperError("HELPER_SNAPSHOT_INVALID", "commands")
            if isinstance(info, (list, tuple)):
                prefix = info
                command_max_args = default_max_args
            elif isinstance(info, dict):
                prefix = info.get("argv_prefix")
                command_max_args = info.get("max_args", default_max_args)
            else:
                raise HelperError("HELPER_SNAPSHOT_INVALID", "commands")
            if isinstance(prefix, str) or not isinstance(prefix, (list, tuple)):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "commands")
            if any(not isinstance(part, str) for part in prefix):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "commands")
            if not _is_int(command_max_args) or command_max_args < 0:
                raise HelperError("HELPER_SNAPSHOT_INVALID", "commands")
            commands[name] = {
                "argv_prefix": tuple(
                    _normalize_argument(part) for part in prefix
                ),
                "max_args": command_max_args,
            }
        return commands

    @staticmethod
    def _restore_stats(raw_stats) -> dict:
        if not isinstance(raw_stats, dict):
            raise HelperError("HELPER_SNAPSHOT_INVALID", "stats")
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats.get(key)
            if not _is_int(value) or value < 0:
                raise HelperError("HELPER_SNAPSHOT_INVALID", "stats")
            stats[key] = value
        return stats

    def restore(self, state: dict) -> None:
        if not isinstance(state, dict):
            raise HelperError("HELPER_SNAPSHOT_INVALID", "state")
        for key in _SNAPSHOT_KEYS:
            if key not in state:
                raise HelperError("HELPER_SNAPSHOT_INVALID", key)

        version = state["whitelist_version"]
        if not _is_int(version) or version < 0:
            raise HelperError(
                "HELPER_SNAPSHOT_INVALID", "whitelist_version"
            )
        peers = self._restore_peers(state["peers"])
        commands = self._restore_commands(state["commands"], self.max_args)
        self._restore_stats(state["stats"])

        if "audit" in state:
            audit = state["audit"]
            if not isinstance(audit, list) or any(
                    not isinstance(entry, dict) for entry in audit):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "audit")
        if "replay" in state:
            replay = state["replay"]
            if not isinstance(replay, dict) or any(
                    not isinstance(key, str) or not isinstance(value, dict)
                    for key, value in replay.items()):
                raise HelperError("HELPER_SNAPSHOT_INVALID", "replay")

        with self._lock:
            self._peers = copy.deepcopy(peers)
            self._commands = copy.deepcopy(commands)
            self._whitelist_version = version
            # Runtime counters and replay history belong to this process
            # incarnation; only the admission policy is restored.
            self._stats = {key: 0 for key in _STAT_KEYS}
            self._audit = []
            self._results = {}

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}
