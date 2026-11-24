"""Credential file validation and atomic rotation guard."""

from __future__ import annotations

import os
import stat
import threading


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
        self.root = root
        self.expected_uid = expected_uid
        self.expected_gid = expected_gid
        self.allow_follow_symlinks = allow_follow_symlinks
        self._probe = probe or local_probe
        self.max_secret_bytes = max_secret_bytes
        self._lock = threading.RLock()

    def rotate(self, name: str, payload: dict) -> dict:
        raise NotImplementedError

    def read(self, name: str) -> dict:
        raise NotImplementedError

    def check(self, name: str) -> dict:
        raise NotImplementedError

    def version(self, name: str) -> int:
        raise NotImplementedError

    def list_names(self) -> list:
        raise NotImplementedError

    def remove(self, name: str) -> int:
        raise NotImplementedError

    def cleanup_temp(self) -> int:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def set_expected_owner(self, uid: int, gid: int) -> None:
        raise NotImplementedError
