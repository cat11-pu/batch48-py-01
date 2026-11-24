import threading

import pytest

import privileged_helper_guard as ph

PEER = {"uid": 0, "gid": 0, "exe": "/usr/lib/privhelper/helperd"}
NOW = "2026-11-24T09:00:00+00:00"


def make(**kwargs):
    guard = ph.PrivilegedHelperGuard(**kwargs)
    guard.allow_peer(0, 0, "/usr/lib/privhelper")
    guard.register_command("proxy.set", argv_prefix=("proxyctl", "set"), max_args=5)
    guard.register_command(
        "proxy.status", argv_prefix=("proxyctl", "status"), max_args=2
    )
    return guard


def request(**overrides):
    base = {
        "request_id": "req-1",
        "peer": dict(PEER),
        "command": "proxy.status",
        "args": ["proxyctl", "status"],
    }
    base.update(overrides)
    return base


def test_allowed_request_executes_with_normalized_argv():
    guard = make()
    out = guard.handle(request(), now=NOW)
    assert out["status"] == "executed"
    assert out["request_id"] == "req-1"
    assert out["command"] == "proxy.status"
    assert out["argv"] == ["proxyctl", "status"]
    assert out["whitelist_version"] == 2
    assert guard.whitelist_version() == 2
    assert guard.stats()["executed"] == 1


def test_unregistered_peer_uid_is_denied():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.handle(request(peer={"uid": 1000, "gid": 0, "exe": PEER["exe"]}))
    assert exc.value.code == "HELPER_PEER_DENIED"
    assert guard.stats()["executed"] == 0


def test_registered_uid_with_wrong_gid_is_denied():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.handle(request(peer={"uid": 0, "gid": 1000, "exe": PEER["exe"]}))
    assert exc.value.code == "HELPER_PEER_DENIED"


def test_executable_outside_registered_prefix_is_denied():
    guard = make()
    for exe in ("/tmp/helperd", "usr/lib/privhelper/helperd"):
        with pytest.raises(ph.HelperError) as exc:
            guard.handle(request(peer={"uid": 0, "gid": 0, "exe": exe}))
        assert exc.value.code == "HELPER_EXE_DENIED"


def test_unknown_command_is_denied():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.handle(request(command="proxy.reset", args=["proxyctl", "reset"]))
    assert exc.value.code == "HELPER_COMMAND_DENIED"


def test_argv_not_matching_command_prefix_is_denied():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.handle(
            request(
                command="proxy.set",
                args=["proxyctl", "delete", "http_proxy"],
            )
        )
    assert exc.value.code == "HELPER_ARG_DENIED"


def test_too_many_arguments_is_denied():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.handle(
            request(
                command="proxy.set",
                args=["proxyctl", "set", "a", "b", "c", "d"],
            )
        )
    assert exc.value.code == "HELPER_ARGC_EXCEEDED"
    assert guard.stats()["executed"] == 0


def test_malformed_requests_are_rejected():
    guard = make()
    cases = [
        {},
        {"request_id": "r-2", "peer": {}, "command": "proxy.status", "args": []},
        {
            "request_id": "r-3",
            "peer": dict(PEER),
            "command": "proxy.status",
            "args": "proxyctl status",
        },
        {
            "request_id": "",
            "peer": dict(PEER),
            "command": "proxy.status",
            "args": ["proxyctl", "status"],
        },
    ]
    for case in cases:
        with pytest.raises(ph.HelperError) as exc:
            guard.handle(case)
        assert exc.value.code == "HELPER_REQUEST_INVALID"


def test_conflicting_command_registration_is_rejected():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.register_command(
            "proxy.status", argv_prefix=("otherctl", "status"), max_args=2
        )
    assert exc.value.code == "HELPER_COMMAND_CONFLICT"
    assert guard.whitelist_version() == 2


def test_normalize_returns_a_defensive_copy():
    guard = make()
    args = ["proxyctl", "status", "svc-a"]
    out = guard.normalize(args)
    assert isinstance(out, list)
    assert out == ["proxyctl", "status", "svc-a"]
    out.append("extra")
    assert args == ["proxyctl", "status", "svc-a"]


def test_audit_records_allowed_calls_with_peer_and_code():
    guard = make()
    guard.handle(request(), now=NOW)
    guard.handle(request(request_id="req-2"), now=NOW)
    entries = guard.audit_entries()
    assert len(entries) == 2
    assert entries[0]["request_id"] == "req-1"
    assert entries[0]["code"] == "HELPER_OK"
    assert entries[0]["uid"] == 0
    assert entries[1]["command"] == "proxy.status"
    assert guard.stats() == {"executed": 2, "denied": 0, "audited": 2}


def test_audit_capacity_blocks_further_execution():
    guard = make(max_audit=1)
    guard.handle(request(), now=NOW)
    with pytest.raises(ph.HelperError) as exc:
        guard.handle(request(request_id="req-2"), now=NOW)
    assert exc.value.code == "HELPER_AUDIT_FULL"
    assert guard.stats()["executed"] == 1
    assert len(guard.audit_entries()) == 1


def test_snapshot_restores_peers_whitelist_and_version():
    guard = make()
    guard.handle(request(), now=NOW)
    snap = guard.snapshot()

    other = ph.PrivilegedHelperGuard()
    other.restore(snap)
    assert other.whitelist_version() == 2
    out = other.handle(request(request_id="req-9"), now=NOW)
    assert out["status"] == "executed"
    assert other.stats()["executed"] == 1


def test_restore_rejects_malformed_state_without_dropping_whitelist():
    guard = make()
    with pytest.raises(ph.HelperError) as exc:
        guard.restore({"whitelist_version": 2})
    assert exc.value.code == "HELPER_SNAPSHOT_INVALID"
    assert guard.whitelist_version() == 2
    assert guard.handle(request(), now=NOW)["status"] == "executed"


def test_concurrent_requests_execute_and_audit_once_each():
    guard = make()
    errors = []

    def worker(index):
        try:
            guard.handle(request(request_id="req-%d" % index), now=NOW)
        except Exception as exc:  # pragma: no cover - only on broken locking
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["executed"] == 10
    assert guard.stats()["audited"] == 10
    ids = {entry["request_id"] for entry in guard.audit_entries()}
    assert len(ids) == 10
