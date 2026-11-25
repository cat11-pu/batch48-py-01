"""Credential file validation and atomic rotation guard."""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None


_SECRET_NAME_RE = re.compile(r"[a-z0-9_-]{1,32}\Z")
_TEMP_NAME_RE = re.compile(
    r"[a-z0-9_-]{1,32}\.secret\.tmp\.\d+(?:\.\d+)*\Z"
)
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_COUNTER_KEYS = ("rotations", "reads", "denied", "temp_cleaned")


class SecretFileError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


def local_probe(path: str):
    """Metadata probe backed by lstat; returns None when the path is absent."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode):
        kind = "symlink"
    elif stat.S_ISDIR(info.st_mode):
        kind = "dir"
    else:
        kind = "file"
    return {
        "kind": kind,
        "mode": stat.S_IMODE(info.st_mode),
        "uid": info.st_uid,
        "gid": info.st_gid,
    }


class SecretFileGuard:
    """Validate credential file metadata and rotate atomically."""

    def __init__(self, root: str, *, expected_uid: int = 0,
                 expected_gid: int = 0, allow_follow_symlinks: bool = False,
                 probe=None, max_secret_bytes: int = 8192) -> None:
        self.root = os.path.abspath(os.fsdecode(root))
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.allow_follow_symlinks = bool(allow_follow_symlinks)
        self._probe = probe or local_probe
        self.max_secret_bytes = max_secret_bytes
        self._lock = threading.RLock()
        self._names = set()
        self._versions = {}
        self._counters = {key: 0 for key in _COUNTER_KEYS}
        self._temp_seq = 0

    def rotate(self, name: str, payload: dict) -> dict:
        with self._lock:
            self._validate_name(name)
            payload_bytes = self._serialize_payload(payload)
            if len(payload_bytes) > self.max_secret_bytes:
                self._deny("SECRET_PAYLOAD_TOO_LARGE", len(payload_bytes))

            with self._root_lock() as root_fd:
                old_version = 0
                if self._secret_metadata(name) is not None:
                    _, old_version = self._load_secret(root_fd, name)

                version = old_version + 1
                document = json.dumps(
                    {"version": version, "payload": payload},
                    separators=(",", ":"),
                ).encode("utf-8")
                temp_name, temp_fd = self._create_temp(root_fd, name)
                temp_exists = True
                try:
                    try:
                        self._write_all(temp_fd, document)
                        os.fchmod(temp_fd, 0o600)
                        os.fsync(temp_fd)
                    finally:
                        os.close(temp_fd)
                        temp_fd = -1

                    os.replace(
                        temp_name,
                        self._secret_filename(name),
                        src_dir_fd=root_fd,
                        dst_dir_fd=root_fd,
                    )
                    temp_exists = False
                    try:
                        os.fsync(root_fd)
                    except OSError:
                        pass
                except SecretFileError:
                    raise
                except OSError as exc:
                    self._deny("SECRET_IO_ERROR", exc)
                finally:
                    if temp_fd >= 0:
                        try:
                            os.close(temp_fd)
                        except OSError:
                            pass
                    if temp_exists:
                        try:
                            os.unlink(temp_name, dir_fd=root_fd)
                        except FileNotFoundError:
                            pass
                        except OSError:
                            pass

                self._names.add(name)
                self._versions[name] = version
                self._counters["rotations"] += 1
                return {
                    "status": "rotated",
                    "name": name,
                    "version": version,
                    "size": len(document),
                    "mode": 0o600,
                }

    def read(self, name: str) -> dict:
        with self._lock:
            self._validate_name(name)
            with self._root_lock() as root_fd:
                payload, version = self._load_secret(root_fd, name)
                info = self._secret_metadata(name)
                if info is None:
                    self._deny("SECRET_NOT_FOUND", name)
                self._names.add(name)
                self._versions[name] = version
                self._counters["reads"] += 1
                return {
                    "payload": payload,
                    "version": version,
                    "mode": info["mode"],
                }

    def check(self, name: str) -> dict:
        with self._lock:
            try:
                self._validate_name(name)
                with self._root_lock() as root_fd:
                    self._load_secret(root_fd, name)
                return {"ok": True, "reason": None, "code": None}
            except SecretFileError as exc:
                return {"ok": False, "reason": exc.code, "code": exc.code}
            except Exception:
                self._counters["denied"] += 1
                return {
                    "ok": False,
                    "reason": "SECRET_INTERNAL",
                    "code": "SECRET_INTERNAL",
                }

    def version(self, name: str) -> int:
        with self._lock:
            self._validate_name(name)
            with self._root_lock() as root_fd:
                if self._secret_metadata(name) is None:
                    return int(self._versions.get(name, 0))
                _, version = self._load_secret(root_fd, name)
                self._names.add(name)
                self._versions[name] = version
                return version

    def list_names(self) -> list:
        with self._lock:
            with self._root_lock() as root_fd:
                found = set()
                try:
                    entries = os.scandir(root_fd)
                except OSError as exc:
                    self._deny("SECRET_IO_ERROR", exc)
                with entries:
                    for entry in entries:
                        match = re.fullmatch(
                            r"([a-z0-9_-]{1,32})\.secret", entry.name
                        )
                        if match is None:
                            continue
                        try:
                            if entry.is_file(follow_symlinks=self.allow_follow_symlinks):
                                found.add(match.group(1))
                        except OSError:
                            continue
                return sorted(found | self._names)

    def remove(self, name: str) -> int:
        with self._lock:
            self._validate_name(name)
            with self._root_lock() as root_fd:
                info = self._secret_metadata(name)
                if info is None:
                    return 0
                if info["kind"] != "file":
                    self._deny("SECRET_NOT_REGULAR", info)
                try:
                    os.unlink(self._secret_filename(name), dir_fd=root_fd)
                except FileNotFoundError:
                    return 0
                except OSError as exc:
                    self._deny("SECRET_IO_ERROR", exc)
                try:
                    os.fsync(root_fd)
                except OSError:
                    pass
                self._names.discard(name)
                self._versions.pop(name, None)
                return 1

    def cleanup_temp(self) -> int:
        with self._lock:
            with self._root_lock() as root_fd:
                removed = 0
                try:
                    entries = os.scandir(root_fd)
                except OSError as exc:
                    self._deny("SECRET_IO_ERROR", exc)
                with entries:
                    for entry in entries:
                        if _TEMP_NAME_RE.fullmatch(entry.name) is None:
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                continue
                            os.unlink(entry.name, dir_fd=root_fd)
                            removed += 1
                        except FileNotFoundError:
                            continue
                        except OSError as exc:
                            self._deny("SECRET_IO_ERROR", exc)
                self._counters["temp_cleaned"] += removed
                return removed

    def snapshot(self) -> dict:
        with self._lock:
            names = self.list_names()
            versions = {}
            for name in names:
                versions[name] = self.version(name)
            return {
                "names": list(names),
                "versions": dict(versions),
                "counters": {key: int(self._counters[key]) for key in _COUNTER_KEYS},
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                names, versions, counters = self._validated_snapshot(state)
            except (TypeError, ValueError, SecretFileError):
                raise SecretFileError("SECRET_SNAPSHOT_INVALID", state)
            self._names = names
            self._versions = versions
            self._counters = counters

    def stats(self) -> dict:
        with self._lock:
            return {key: int(self._counters[key]) for key in _COUNTER_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._counters = {key: 0 for key in _COUNTER_KEYS}

    def set_expected_owner(self, uid, gid) -> None:
        with self._lock:
            self.expected_uid = uid
            self.expected_gid = gid

    def _validated_snapshot(self, state):
        if not isinstance(state, dict):
            raise ValueError("state")
        if not all(key in state for key in ("names", "versions", "counters")):
            raise ValueError("state")

        raw_names = state["names"]
        raw_versions = state["versions"]
        raw_counters = state["counters"]
        if not isinstance(raw_names, list):
            raise ValueError("names")
        if not isinstance(raw_versions, dict):
            raise ValueError("versions")
        if not isinstance(raw_counters, dict):
            raise ValueError("counters")

        names = []
        for name in raw_names:
            if not isinstance(name, str) or _SECRET_NAME_RE.fullmatch(name) is None:
                raise ValueError("names")
            if name in names:
                raise ValueError("names")
            names.append(name)
        name_set = set(names)

        versions = {}
        for name, version in raw_versions.items():
            if not isinstance(name, str) or name not in name_set:
                raise ValueError("versions")
            if isinstance(version, bool) or not isinstance(version, int):
                raise ValueError("versions")
            if version < 0:
                raise ValueError("versions")
            versions[name] = version
        if set(versions) != name_set:
            raise ValueError("versions")

        counters = {}
        for key in _COUNTER_KEYS:
            value = raw_counters.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("counters")
            if value < 0:
                raise ValueError("counters")
            counters[key] = value
        return set(names), versions, counters

    def _validate_name(self, name) -> None:
        if not isinstance(name, str) or _SECRET_NAME_RE.fullmatch(name) is None:
            self._deny("SECRET_NAME_INVALID", name)

    def _serialize_payload(self, payload) -> bytes:
        if not isinstance(payload, dict):
            self._deny("SECRET_PAYLOAD_INVALID", type(payload).__name__)
        try:
            return json.dumps(payload, separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            self._deny("SECRET_PAYLOAD_INVALID", exc)

    def _deny(self, code: str, detail: object = None):
        self._counters["denied"] += 1
        raise SecretFileError(code, detail)

    def _secret_filename(self, name: str) -> str:
        return f"{name}.secret"

    def _secret_path(self, name: str) -> str:
        return os.path.join(self.root, self._secret_filename(name))

    def _probe_info(self, path: str):
        try:
            raw = self._probe(path)
        except SecretFileError:
            raise
        except Exception as exc:
            self._deny("SECRET_METADATA_INVALID", exc)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            self._deny("SECRET_METADATA_INVALID", raw)
        kind = raw.get("kind")
        if kind == "symlink":
            if not self.allow_follow_symlinks:
                self._deny("SECRET_SYMLINK", path)
            raw = self._follow_symlink_info(path)
            if raw is None:
                self._deny("SECRET_NOT_FOUND", path)
            kind = raw.get("kind")
        if kind not in ("file", "dir"):
            self._deny("SECRET_METADATA_INVALID", raw)
        mode = raw.get("mode")
        uid = raw.get("uid")
        gid = raw.get("gid")
        if any(isinstance(value, bool) or not isinstance(value, int)
               for value in (mode, uid, gid)):
            self._deny("SECRET_METADATA_INVALID", raw)
        return {
            "kind": kind,
            "mode": stat.S_IMODE(mode),
            "uid": uid,
            "gid": gid,
        }

    def _follow_symlink_info(self, path: str):
        resolved = os.path.realpath(path)
        if resolved != os.path.abspath(path):
            try:
                target = self._probe(resolved)
            except Exception:
                target = None
            if isinstance(target, dict) and target.get("kind") != "symlink":
                return dict(target)
        try:
            info = os.stat(path)
        except OSError:
            return None
        if stat.S_ISDIR(info.st_mode):
            kind = "dir"
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            kind = "other"
        return {
            "kind": kind,
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
        }

    def _validate_parent(self):
        info = self._probe_info(self.root)
        if info is None:
            self._deny("SECRET_NOT_FOUND", self.root)
        if info["kind"] != "dir":
            self._deny("SECRET_PARENT_INSECURE", info)
        if info["uid"] != self.expected_uid:
            self._deny("SECRET_OWNER_MISMATCH", info)
        if info["gid"] != self.expected_gid:
            self._deny("SECRET_GROUP_MISMATCH", info)
        if info["mode"] & 0o022:
            self._deny("SECRET_PARENT_INSECURE", info)
        return info

    def _secret_metadata(self, name: str):
        info = self._probe_info(self._secret_path(name))
        if info is None:
            return None
        if info["kind"] != "file":
            self._deny("SECRET_NOT_REGULAR", info)
        if info["uid"] != self.expected_uid:
            self._deny("SECRET_OWNER_MISMATCH", info)
        if info["gid"] != self.expected_gid:
            self._deny("SECRET_GROUP_MISMATCH", info)
        if info["mode"] != 0o600:
            self._deny("SECRET_MODE_TOO_OPEN", info)
        return info

    @contextmanager
    def _root_lock(self):
        self._validate_parent()
        flags = os.O_RDONLY | _DIRECTORY | _CLOEXEC
        if not self.allow_follow_symlinks:
            flags |= _NOFOLLOW
        try:
            root_fd = os.open(self.root, flags)
        except FileNotFoundError:
            self._deny("SECRET_NOT_FOUND", self.root)
        except OSError as exc:
            if not self.allow_follow_symlinks and exc.errno in (
                    getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)):
                self._deny("SECRET_SYMLINK", self.root)
            self._deny("SECRET_IO_ERROR", exc)
        try:
            if fcntl is not None:
                try:
                    fcntl.flock(root_fd, fcntl.LOCK_EX)
                except OSError as exc:
                    self._deny("SECRET_IO_ERROR", exc)
            yield root_fd
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(root_fd, fcntl.LOCK_UN)
                except OSError:
                    pass
            os.close(root_fd)

    def _open_secret(self, root_fd: int, name: str) -> int:
        flags = os.O_RDONLY | _CLOEXEC
        if not self.allow_follow_symlinks:
            flags |= _NOFOLLOW
        try:
            fd = os.open(
                self._secret_filename(name),
                flags,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            self._deny("SECRET_NOT_FOUND", name)
        except IsADirectoryError:
            self._deny("SECRET_NOT_REGULAR", name)
        except OSError as exc:
            if not self.allow_follow_symlinks and exc.errno == getattr(
                    os, "ELOOP", 40):
                self._deny("SECRET_SYMLINK", name)
            self._deny("SECRET_IO_ERROR", exc)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                self._deny("SECRET_NOT_REGULAR", name)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _load_secret(self, root_fd: int, name: str):
        info = self._secret_metadata(name)
        if info is None:
            self._deny("SECRET_NOT_FOUND", name)
        fd = self._open_secret(root_fd, name)
        try:
            raw = self._read_limited(fd)
        finally:
            os.close(fd)
        payload, version = self._decode_document(raw, name)
        return payload, version

    def _read_limited(self, fd: int) -> bytes:
        limit = max(0, int(self.max_secret_bytes)) + 4096
        chunks = []
        total = 0
        while True:
            try:
                chunk = os.read(fd, min(65536, limit - total + 1))
            except OSError as exc:
                self._deny("SECRET_IO_ERROR", exc)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                self._deny("SECRET_PAYLOAD_TOO_LARGE", total)
            chunks.append(chunk)
        return b"".join(chunks)

    def _decode_document(self, raw: bytes, name: str):
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            self._deny("SECRET_CORRUPT", exc)
        if not isinstance(document, dict):
            self._deny("SECRET_CORRUPT", "document")

        if "version" in document and "payload" in document:
            version = document["version"]
            payload = document["payload"]
            if isinstance(version, bool) or not isinstance(version, int):
                self._deny("SECRET_CORRUPT", "version")
            if version < 0:
                self._deny("SECRET_CORRUPT", "version")
            if not isinstance(payload, dict):
                self._deny("SECRET_CORRUPT", "payload")
        else:
            payload = document
            version = int(self._versions.get(name, 1))

        size = len(self._serialize_payload(payload))
        if size > self.max_secret_bytes:
            self._deny("SECRET_PAYLOAD_TOO_LARGE", size)
        return payload, version

    def _create_temp(self, root_fd: int, name: str):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _CLOEXEC | _NOFOLLOW
        while True:
            self._temp_seq += 1
            temp_name = (
                f"{name}.secret.tmp.{os.getpid()}.{self._temp_seq}"
            )
            try:
                fd = os.open(temp_name, flags, 0o600, dir_fd=root_fd)
            except FileExistsError:
                continue
            except OSError as exc:
                self._deny("SECRET_IO_ERROR", exc)
            return temp_name, fd

    @staticmethod
    def _write_all(fd: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            try:
                written = os.write(fd, view)
            except InterruptedError:
                continue
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
