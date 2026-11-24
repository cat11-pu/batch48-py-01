import copy
import os
import threading

import pytest

import secret_file_guard as sg

UID = os.getuid() if hasattr(os, "getuid") else 1000
GID = os.getgid() if hasattr(os, "getgid") else 1000


class Probe:
    """Deterministic metadata provider; safe defaults, per-path overrides."""

    def __init__(self):
        self.overrides = {}

    def set(self, path, **fields):
        base = {"kind": "file", "mode": 0o600, "uid": UID, "gid": GID}
        base.update(fields)
        self.overrides[os.path.abspath(str(path))] = base

    def __call__(self, path):
        key = os.path.abspath(str(path))
        if key in self.overrides:
            return dict(self.overrides[key])
        if os.path.isdir(key):
            return {"kind": "dir", "mode": 0o700, "uid": UID, "gid": GID}
        if os.path.exists(key):
            return {"kind": "file", "mode": 0o600, "uid": UID, "gid": GID}
        return None


def make(tmp_path, **kwargs):
    probe = kwargs.pop("probe", None) or Probe()
    kwargs.setdefault("expected_uid", UID)
    kwargs.setdefault("expected_gid", GID)
    guard = sg.SecretFileGuard(str(tmp_path), probe=probe, **kwargs)
    return guard, probe


def secret_path(tmp_path, name):
    return tmp_path / (name + ".secret")


def test_rotate_then_read_roundtrip(tmp_path):
    guard, probe = make(tmp_path)
    out = guard.rotate("api-token", {"token": "s3cr3t", "scopes": ["read"]})
    assert out["status"] == "rotated"
    assert out["name"] == "api-token"
    assert out["version"] == 1
    assert out["mode"] == 0o600
    assert out["size"] > 0
    assert secret_path(tmp_path, "api-token").exists()

    data = guard.read("api-token")
    assert data["payload"] == {"token": "s3cr3t", "scopes": ["read"]}
    assert data["version"] == 1
    assert data["mode"] == 0o600
    assert guard.stats() == {
        "rotations": 1,
        "reads": 1,
        "denied": 0,
        "temp_cleaned": 0,
    }


def test_invalid_names_are_rejected_before_touching_disk(tmp_path):
    guard, probe = make(tmp_path)
    for bad in ("../escape", "UPPER", "a/b", "", "x" * 40):
        with pytest.raises(sg.SecretFileError) as exc:
            guard.rotate(bad, {"a": 1})
        assert exc.value.code == "SECRET_NAME_INVALID"
    assert guard.list_names() == []
    assert list(tmp_path.iterdir()) == []


def test_payload_larger_than_budget_is_rejected(tmp_path):
    guard, probe = make(tmp_path, max_secret_bytes=64)
    with pytest.raises(sg.SecretFileError) as exc:
        guard.rotate("big", {"blob": "x" * 200})
    assert exc.value.code == "SECRET_PAYLOAD_TOO_LARGE"
    assert guard.version("big") == 0
    assert guard.stats()["rotations"] == 0


def test_payload_must_be_a_mapping(tmp_path):
    guard, probe = make(tmp_path)
    for bad in (["a"], "text", 7):
        with pytest.raises(sg.SecretFileError) as exc:
            guard.rotate("cfg", bad)
        assert exc.value.code == "SECRET_PAYLOAD_INVALID"
    assert guard.list_names() == []


def test_read_missing_secret_reports_not_found(tmp_path):
    guard, probe = make(tmp_path)
    with pytest.raises(sg.SecretFileError) as exc:
        guard.read("absent")
    assert exc.value.code == "SECRET_NOT_FOUND"
    assert guard.stats()["denied"] == 1


def test_read_rejects_group_or_other_readable_mode(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    probe.set(secret_path(tmp_path, "api-token"), mode=0o644)
    with pytest.raises(sg.SecretFileError) as exc:
        guard.read("api-token")
    assert exc.value.code == "SECRET_MODE_TOO_OPEN"
    assert guard.stats()["denied"] == 1


def test_read_rejects_foreign_owner(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    probe.set(secret_path(tmp_path, "api-token"), uid=UID + 1)
    with pytest.raises(sg.SecretFileError) as exc:
        guard.read("api-token")
    assert exc.value.code == "SECRET_OWNER_MISMATCH"
    assert guard.stats()["denied"] == 1


def test_read_rejects_foreign_group(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    probe.set(secret_path(tmp_path, "api-token"), gid=GID + 1)
    with pytest.raises(sg.SecretFileError) as exc:
        guard.read("api-token")
    assert exc.value.code == "SECRET_GROUP_MISMATCH"


def test_read_rejects_world_writable_parent(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    probe.set(tmp_path, kind="dir", mode=0o777)
    with pytest.raises(sg.SecretFileError) as exc:
        guard.read("api-token")
    assert exc.value.code == "SECRET_PARENT_INSECURE"


def test_second_rotation_bumps_version_and_leaves_no_temp_files(tmp_path):
    guard, probe = make(tmp_path)
    first = guard.rotate("api-token", {"token": "one"})
    second = guard.rotate("api-token", {"token": "two"})
    assert first["version"] == 1
    assert second["version"] == 2
    assert guard.read("api-token")["payload"] == {"token": "two"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["api-token.secret"]
    assert guard.stats()["rotations"] == 2


def test_failed_rotation_preserves_previous_secret(tmp_path):
    guard, probe = make(tmp_path, max_secret_bytes=64)
    guard.rotate("api-token", {"token": "one"})
    with pytest.raises(sg.SecretFileError) as exc:
        guard.rotate("api-token", {"token": "x" * 200})
    assert exc.value.code == "SECRET_PAYLOAD_TOO_LARGE"
    data = guard.read("api-token")
    assert data["payload"] == {"token": "one"}
    assert data["version"] == 1
    assert guard.version("api-token") == 1


def test_cleanup_temp_removes_only_stale_temp_files(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    stale = tmp_path / "api-token.secret.tmp.4242.3"
    stale.write_text("half written", encoding="utf-8")
    assert guard.cleanup_temp() == 1
    assert not stale.exists()
    assert guard.read("api-token")["payload"] == {"token": "one"}
    assert guard.stats()["temp_cleaned"] == 1
    assert guard.cleanup_temp() == 0


def test_snapshot_is_deep_copy_and_restore_keeps_version(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    snap = guard.snapshot()
    assert snap["names"] == ["api-token"]
    assert snap["versions"] == {"api-token": 1}

    saved = copy.deepcopy(snap)
    snap["versions"]["api-token"] = 99
    snap["names"].append("ghost")
    assert guard.version("api-token") == 1
    assert guard.list_names() == ["api-token"]

    other, other_probe = make(tmp_path)
    other.restore(saved)
    assert other.version("api-token") == 1
    assert other.read("api-token")["payload"] == {"token": "one"}


def test_restore_rejects_malformed_state_without_resetting(tmp_path):
    guard, probe = make(tmp_path)
    guard.rotate("api-token", {"token": "one"})
    with pytest.raises(sg.SecretFileError) as exc:
        guard.restore({"names": ["api-token"]})
    assert exc.value.code == "SECRET_SNAPSHOT_INVALID"
    assert guard.version("api-token") == 1
    assert guard.read("api-token")["payload"] == {"token": "one"}


def test_concurrent_rotations_produce_consecutive_versions(tmp_path):
    guard, probe = make(tmp_path)
    errors = []

    def worker(index):
        try:
            guard.rotate("shared", {"n": index})
        except Exception as exc:  # pragma: no cover - only on broken locking
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.version("shared") == 8
    assert guard.stats()["rotations"] == 8
    payload = guard.read("shared")["payload"]
    assert 0 <= payload["n"] <= 7
