import copy
import os
import threading

import pytest

import spawn_inheritance_guard as sg


def make(**kwargs):
    return sg.InheritanceGuard(**kwargs)


def spawn_spec(**overrides):
    spec = {
        "argv": ["/bin/sh", "-c", "id"],
        "env_base": {"PATH": "/usr/bin", "HOME": "/root", "LD_PRELOAD": "/tmp/x.so"},
        "env_allowlist": ["PATH", "LANG"],
        "env_overrides": {"LANG": "C.UTF-8"},
        "fds": [
            {"fd": 0, "kind": "tty", "cloexec": False},
            {"fd": 1, "kind": "tty", "cloexec": True},
            {"fd": 2, "kind": "tty", "cloexec": False},
            {"fd": 7, "kind": "pipe", "cloexec": False},
        ],
        "fd_keep": [0, 7],
        "cwd": "/srv/sandbox/work/./tmp",
        "cwd_root": "/srv/sandbox",
        "umask": 0o027,
    }
    spec.update(overrides)
    return spec


def test_build_env_keeps_only_exact_allowlisted_names():
    guard = make()
    base = {"PATH": "/usr/bin", "LANG": "C", "HOME": "/root", "TERM": "xterm"}
    env = guard.build_env(base, ["PATH", "LANG", "TERM"])
    assert env == {"LANG": "C", "PATH": "/usr/bin", "TERM": "xterm"}
    assert list(env) == ["LANG", "PATH", "TERM"]
    assert base == {"PATH": "/usr/bin", "LANG": "C", "HOME": "/root", "TERM": "xterm"}


def test_allowlist_entries_are_literal_not_prefixes():
    guard = make()
    env = guard.build_env(
        {"LD_PRELOAD": "/tmp/evil.so", "PATH": "/usr/bin", "PATHX": "/opt"},
        ["LD_", "PATH"],
    )
    assert env == {"PATH": "/usr/bin"}
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({}, ["LD_*"])
    assert exc.value.code == "SPAWN_ALLOWLIST_INVALID"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({}, ["PATH", ""])
    assert exc.value.code == "SPAWN_ALLOWLIST_INVALID"


def test_env_values_with_control_characters_are_refused():
    guard = make()
    for bad in ("/usr/bin\nLD_PRELOAD=/tmp/e.so", "/usr/bin\rX=1", "/usr\x00bin"):
        with pytest.raises(sg.SpawnGuardError) as exc:
            guard.build_env({"PATH": "/bin"}, ["PATH"], {"PATH": bad})
        assert exc.value.code == "SPAWN_ENV_VALUE_INVALID"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({"PATH": "/bin"}, ["PATH"], {"PATH": 5})
    assert exc.value.code == "SPAWN_ENV_VALUE_INVALID"


def test_overrides_must_stay_inside_allowlist_and_be_well_named():
    guard = make()
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({"PATH": "/bin"}, ["PATH"], {"HOME": "/tmp"})
    assert exc.value.code == "SPAWN_ENV_NOT_ALLOWED"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({"PATH": "/bin"}, ["BAD-NAME"])
    assert exc.value.code == "SPAWN_ALLOWLIST_INVALID"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({"PATH": "/bin"}, ["PATH"], {"BAD-NAME": "x"})
    assert exc.value.code == "SPAWN_ENV_NAME_INVALID"
    assert guard.build_env({"PATH": "/bin"}, ["PATH"], {"PATH": "/opt"}) == {"PATH": "/opt"}


def test_env_size_and_value_ceilings_are_hard():
    guard = make(max_env=2, max_env_value=4)
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({"A": "1", "B": "2", "C": "3"}, ["A", "B", "C"])
    assert exc.value.code == "SPAWN_ENV_LIMIT"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.build_env({"A": "1", "B": "2"}, ["A", "B"], {"A": "abcde"})
    assert exc.value.code == "SPAWN_ENV_LIMIT"
    assert guard.build_env({"A": "1", "B": "2"}, ["A", "B"]) == {"A": "1", "B": "2"}


def test_filter_fds_never_closes_the_channels_asked_to_keep():
    guard = make()
    inherited = [
        {"fd": 4, "kind": "socket", "cloexec": True},
        {"fd": 0, "kind": "file", "cloexec": False},
        {"fd": 3, "kind": "pipe", "cloexec": False},
        {"fd": 2, "kind": "tty", "cloexec": False},
        {"fd": 1, "kind": "tty", "cloexec": True},
    ]
    plan = guard.filter_fds(inherited, [3, 0])
    assert plan == [
        {"fd": 0, "action": "keep", "cloexec": False},
        {"fd": 1, "action": "drop", "cloexec": True},
        {"fd": 2, "action": "close", "cloexec": False},
        {"fd": 3, "action": "keep", "cloexec": False},
        {"fd": 4, "action": "drop", "cloexec": True},
    ]
    kept = [row["fd"] for row in plan if row["action"] == "keep"]
    assert kept == [0, 3]
    assert inherited[0]["cloexec"] is True


def test_filter_fds_rejects_ambiguous_descriptor_sets():
    guard = make()
    dup = [{"fd": 2, "kind": "tty", "cloexec": False},
           {"fd": 2, "kind": "pipe", "cloexec": False}]
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.filter_fds(dup, [])
    assert exc.value.code == "SPAWN_FD_DUPLICATE"

    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.filter_fds([{"fd": 2, "kind": "tty", "cloexec": False}], [9])
    assert exc.value.code == "SPAWN_FD_UNKNOWN"

    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.filter_fds([{"fd": 2, "kind": "fifo", "cloexec": False}], [])
    assert exc.value.code == "SPAWN_FD_INVALID"

    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.filter_fds([{"fd": -1, "kind": "pipe", "cloexec": False}], [])
    assert exc.value.code == "SPAWN_FD_INVALID"

    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.filter_fds([{"fd": 2, "kind": "tty", "cloexec": False}], [2, 2])
    assert exc.value.code == "SPAWN_FD_DUPLICATE"


def test_prepare_cwd_normalises_dot_segments_and_contains_root():
    guard = make()
    assert guard.prepare_cwd("/srv/sandbox/a/./b/../c") == "/srv/sandbox/a/c"
    assert guard.prepare_cwd("/srv/sandbox") == "/srv/sandbox"
    assert guard.prepare_cwd("/srv/sandbox/") == "/srv/sandbox"
    assert guard.prepare_cwd("/opt/x/y/../z", "/opt/x") == "/opt/x/z"

    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.prepare_cwd("work/inner")
    assert exc.value.code == "SPAWN_CWD_INVALID"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.prepare_cwd("/srv/sandbox/../etc")
    assert exc.value.code == "SPAWN_CWD_ESCAPE"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.prepare_cwd("/srv/sandboxx/a")
    assert exc.value.code == "SPAWN_CWD_ESCAPE"


def test_umask_is_recorded_without_polluting_the_caller():
    guard = make()
    before = os.umask(0)
    os.umask(before)
    assert guard.pending_umask() == 0o077
    assert guard.apply_umask(0o002) == 0o002
    assert guard.pending_umask() == 0o002
    current = os.umask(0)
    os.umask(current)
    assert current == before

    for bad in (-1, 0o1000, "022", 22.5):
        with pytest.raises(sg.SpawnGuardError) as exc:
            guard.apply_umask(bad)
        assert exc.value.code == "SPAWN_UMASK_INVALID"
    assert guard.pending_umask() == 0o002


def test_plan_freezes_copies_and_bumps_revision():
    guard = make()
    spec = spawn_spec()
    plan = guard.plan(spec)
    assert plan["revision"] == 1
    assert plan["argv"] == ["/bin/sh", "-c", "id"]
    assert plan["env"] == {"LANG": "C.UTF-8", "PATH": "/usr/bin"}
    assert plan["fd_plan"] == [
        {"fd": 0, "action": "keep", "cloexec": False},
        {"fd": 1, "action": "drop", "cloexec": True},
        {"fd": 2, "action": "close", "cloexec": False},
        {"fd": 7, "action": "keep", "cloexec": False},
    ]
    assert plan["cwd"] == "/srv/sandbox/work/tmp"
    assert plan["umask"] == 0o027
    assert isinstance(plan["fingerprint"], str) and len(plan["fingerprint"]) >= 16

    spec["env_base"]["PATH"] = "/tmp/hijacked"
    spec["env_allowlist"].append("HOME")
    spec["argv"].append("--evil")
    assert plan["env"]["PATH"] == "/usr/bin"
    assert plan["argv"] == ["/bin/sh", "-c", "id"]
    assert guard.plan(spawn_spec())["revision"] == 2


def test_verify_accepts_a_fresh_plan_and_summarises_it():
    guard = make()
    plan = guard.plan(spawn_spec())
    out = guard.verify(plan)
    assert out == {"ok": True, "revision": 1,
                   "env_keys": ["LANG", "PATH"], "fd_kept": [0, 7]}
    assert guard.stats()["verified"] == 1


def test_verify_refuses_a_plan_edited_after_creation():
    guard = make()
    plan = guard.plan(spawn_spec())
    tampered = copy.deepcopy(plan)
    tampered["env"]["LD_PRELOAD"] = "/tmp/evil.so"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.verify(tampered)
    assert exc.value.code == "SPAWN_PLAN_TAMPERED"

    tampered_umask = copy.deepcopy(plan)
    tampered_umask["umask"] = 0o000
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.verify(tampered_umask)
    assert exc.value.code == "SPAWN_PLAN_TAMPERED"

    missing = copy.deepcopy(plan)
    del missing["fingerprint"]
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.verify(missing)
    assert exc.value.code == "SPAWN_PLAN_INVALID"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.verify(None)
    assert exc.value.code == "SPAWN_PLAN_INVALID"


def test_plan_refuses_broken_specs():
    guard = make()
    for spec in ({"argv": []}, {"argv": "id"}, {"argv": None}):
        with pytest.raises(sg.SpawnGuardError) as exc:
            guard.plan(spec)
        assert exc.value.code == "SPAWN_ARGV_INVALID"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.plan(spawn_spec(cwd_missing=True))
    assert exc.value.code == "SPAWN_CWD_MISSING"
    with pytest.raises(sg.SpawnGuardError) as exc:
        guard.plan(None)
    assert exc.value.code == "SPAWN_PLAN_INVALID"


def test_snapshot_restore_keeps_revision_umask_and_counters():
    guard = make()
    plan = guard.plan(spawn_spec())
    guard.verify(plan)
    guard.apply_umask(0o022)
    state = guard.snapshot()
    assert state["revision"] == 1
    assert state["pending_umask"] == 0o022
    assert state["stats"]["planned"] == 1
    assert state["stats"]["verified"] == 1

    state["revision"] = 99
    state["stats"]["planned"] = 900
    assert guard.stats()["planned"] == 1

    restored = make()
    restored.restore(guard.snapshot())
    assert restored.pending_umask() == 0o022
    assert restored.stats()["planned"] == 1
    assert restored.plan(spawn_spec())["revision"] == 2

    for bad in (None, {}, {"version": 2}, {"version": 1, "revision": "x"}):
        with pytest.raises(sg.SpawnGuardError) as exc:
            restored.restore(bad)
        assert exc.value.code == "SPAWN_SNAPSHOT_INVALID"
    assert restored.stats()["planned"] == 2


def test_parallel_plans_get_distinct_revisions_and_env_objects():
    guard = make()
    spec = spawn_spec()
    plans = []
    errors = []
    lock = threading.Lock()

    def worker():
        local = []
        try:
            for _ in range(3):
                local.append(guard.plan(spec))
        except Exception as exc:  # pragma: no cover - surfaced through errors
            with lock:
                errors.append(repr(exc))
        with lock:
            plans.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert len(plans) == 24
    assert sorted(plan["revision"] for plan in plans) == list(range(1, 25))
    assert len({id(plan["env"]) for plan in plans}) == 24
    assert guard.stats()["planned"] == 24
