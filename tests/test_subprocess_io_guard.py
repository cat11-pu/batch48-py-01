import sys
import threading

import pytest

import subprocess_io_guard as sg

PY = sys.executable


def make():
    return sg.SubprocessIoGuard()


def test_spawn_returns_handle_and_unknown_handles_are_refused():
    guard = make()
    handle = guard.spawn([PY, "-c", "pass"])
    assert handle.startswith("proc-")
    assert guard.handles() == [handle]
    with pytest.raises(sg.IoGuardError) as exc:
        guard.write_stdin("proc-9999", b"x")
    assert exc.value.code == "IO_UNKNOWN_HANDLE"
    assert guard.wait(handle, 5.0) == 0
    guard.close(handle)


def test_wait_then_read_returns_captured_stdout():
    guard = make()
    handle = guard.spawn([PY, "-c", "import sys; sys.stdout.write('hello')"])
    assert guard.wait(handle, 5.0) == 0
    assert guard.read_stdout(handle, 100) == b"hello"
    assert guard.read_stdout(handle, 100) == b""
    guard.close(handle)


def test_read_limit_counts_bytes_not_characters():
    guard = make()
    code = "import sys; sys.stdout.write('\\u4f60\\u597d')"
    handle = guard.spawn([PY, "-c", code])
    assert guard.wait(handle, 5.0) == 0
    payload = "你好".encode("utf-8")
    assert len(payload) == 6
    assert guard.read_stdout(handle, 3) == payload[:3]
    assert guard.read_stdout(handle, 100) == payload[3:]
    guard.close(handle)


def test_stdin_roundtrip_and_close():
    guard = make()
    code = "import sys; sys.stdout.write(sys.stdin.read().upper())"
    handle = guard.spawn([PY, "-c", code])
    assert guard.write_stdin(handle, b"abc") == 3
    guard.close_stdin(handle)
    assert guard.wait(handle, 5.0) == 0
    assert guard.read_stdout(handle, 100) == b"ABC"
    with pytest.raises(sg.IoGuardError) as exc:
        guard.write_stdin(handle, b"more")
    assert exc.value.code == "IO_STDIN_CLOSED"
    guard.close(handle)


def test_wait_reports_plain_exit_code():
    guard = make()
    handle = guard.spawn([PY, "-c", "raise SystemExit(3)"])
    assert guard.wait(handle, 5.0) == 3
    guard.close(handle)


def test_wait_reports_negative_signal_for_killed_child():
    guard = make()
    code = (
        "import sys, time; sys.stdout.write('ready'); "
        "sys.stdout.flush(); time.sleep(30)"
    )
    handle = guard.spawn([PY, "-c", code])
    assert guard.read_stdout(handle, 10) == b"ready"
    assert guard.kill_tree(handle) == "killed"
    assert guard.wait(handle, 5.0) == -9
    guard.close(handle)


def test_wait_timeout_raises_and_leaves_child_alive():
    guard = make()
    handle = guard.spawn([PY, "-c", "import time; time.sleep(30)"])
    with pytest.raises(sg.IoGuardError) as exc:
        guard.wait(handle, 0.2)
    assert exc.value.code == "IO_WAIT_TIMEOUT"
    assert guard.kill_tree(handle) == "killed"
    assert guard.wait(handle, 5.0) == -9
    guard.close(handle)


def test_kill_tree_is_idempotent_after_exit():
    guard = make()
    handle = guard.spawn([PY, "-c", "pass"])
    assert guard.wait(handle, 5.0) == 0
    assert guard.kill_tree(handle) == "already-exited"
    assert guard.kill_tree(handle) == "already-exited"
    guard.close(handle)


def test_close_is_idempotent_and_blocks_further_reads():
    guard = make()
    handle = guard.spawn([PY, "-c", "import sys; sys.stdout.write('x')"])
    assert guard.wait(handle, 5.0) == 0
    guard.close(handle)
    guard.close(handle)
    assert guard.handles() == []
    with pytest.raises(sg.IoGuardError) as exc:
        guard.read_stdout(handle, 10)
    assert exc.value.code == "IO_HANDLE_CLOSED"
    with pytest.raises(sg.IoGuardError) as exc:
        guard.drain(handle)
    assert exc.value.code == "IO_HANDLE_CLOSED"
    with pytest.raises(sg.IoGuardError) as exc:
        guard.write_stdin(handle, b"z")
    assert exc.value.code == "IO_HANDLE_CLOSED"


def test_drain_returns_remaining_output_once():
    guard = make()
    handle = guard.spawn([PY, "-c", "import sys; sys.stdout.write('abcdef')"])
    assert guard.wait(handle, 5.0) == 0
    assert guard.drain(handle) == b"abcdef"
    assert guard.drain(handle) == b""
    guard.close(handle)


def test_read_lines_delivers_only_complete_lines():
    guard = make()
    code = "import sys; sys.stdout.write('one\\ntwo\\n')"
    handle = guard.spawn([PY, "-c", code])
    assert guard.wait(handle, 5.0) == 0
    assert guard.read_lines(handle) == [b"one\n", b"two\n"]
    assert guard.read_lines(handle) == []
    guard.close(handle)


def test_line_limit_is_enforced_for_unterminated_output():
    guard = make()
    code = "import sys; sys.stdout.write('x' * 5000)"
    handle = guard.spawn([PY, "-c", code])
    assert guard.wait(handle, 5.0) == 0
    with pytest.raises(sg.IoGuardError) as exc:
        guard.read_lines(handle)
    assert exc.value.code == "IO_LINE_TOO_LONG"
    guard.close(handle)


def test_concurrent_readers_never_duplicate_output():
    guard = make()
    code = "import sys; sys.stdout.write('0123456789')"
    handle = guard.spawn([PY, "-c", code])
    assert guard.wait(handle, 5.0) == 0
    barrier = threading.Barrier(2)
    chunks = []
    lock = threading.Lock()

    def reader():
        barrier.wait()
        try:
            chunk = guard.read_stdout(handle, 5)
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures
            chunk = type(exc).__name__.encode()
        with lock:
            chunks.append(chunk)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(chunks) == [b"01234", b"56789"]
    guard.close(handle)


def test_snapshot_is_deep_copy_and_restore_validates():
    guard = make()
    handle = guard.spawn([PY, "-c", "import time; time.sleep(30)"])
    state = guard.snapshot()
    assert list(state["handles"]) == [handle]
    assert isinstance(state["handles"][handle]["state"], str)
    assert isinstance(state["handles"][handle]["pid"], int)
    state["handles"][handle]["pid"] = 1
    assert guard.snapshot()["handles"][handle]["pid"] != 1
    with pytest.raises(sg.IoGuardError) as exc:
        guard.restore({"handles": "broken"})
    assert exc.value.code == "IO_SNAPSHOT_INVALID"
    assert guard.handles() == [handle]
    guard.kill_tree(handle)
    guard.wait(handle, 5.0)
    guard.close(handle)


def test_invalid_arguments_are_rejected():
    guard = make()
    with pytest.raises(sg.IoGuardError) as exc:
        guard.spawn([])
    assert exc.value.code == "IO_INVALID_ARGV"
    with pytest.raises(sg.IoGuardError) as exc:
        guard.spawn(PY + " -c pass")
    assert exc.value.code == "IO_INVALID_ARGV"
    for limits in (sg.IoLimits(max_buffer=0), sg.IoLimits(line_limit=0)):
        with pytest.raises(sg.IoGuardError) as exc:
            guard.spawn([PY, "-c", "pass"], limits)
        assert exc.value.code == "IO_INVALID_LIMITS"
    handle = guard.spawn([PY, "-c", "pass"])
    with pytest.raises(sg.IoGuardError) as exc:
        guard.read_stdout(handle, 0)
    assert exc.value.code == "IO_INVALID_LIMIT"
    with pytest.raises(sg.IoGuardError) as exc:
        guard.read_stdout("proc-9999", 1)
    assert exc.value.code == "IO_UNKNOWN_HANDLE"
    assert guard.wait(handle, 5.0) == 0
    guard.close(handle)
