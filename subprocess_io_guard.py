"""Subprocess I/O guard: bounded pipes, byte-exact reads, process-group teardown."""

from __future__ import annotations

import base64
import copy
import math
import os
import select
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Any


HANDLE_PREFIX = "proc-"
_READ_CHUNK = 65536
_STOP_POLL = 0.02


class IoGuardError(ValueError):
    """Raised when a subprocess I/O operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class IoLimits:
    max_buffer: int = 65536
    line_limit: int = 4096
    graceful_seconds: float = 1.0


class _StreamState:
    __slots__ = ("buffer", "offset", "eof", "error")

    def __init__(self) -> None:
        self.buffer = bytearray()
        self.offset = 0
        self.eof = False
        self.error: tuple[str, object] | None = None


class _Handle:
    __slots__ = (
        "name",
        "index",
        "proc",
        "argv",
        "limits",
        "pid",
        "pgid",
        "pid_starttime",
        "stdout",
        "stderr",
        "stdin_buffer",
        "stdin_user_closed",
        "stdin_fd_closed",
        "stdin_broken",
        "returncode",
        "closed",
        "restored",
        "kill_requested",
        "last_timeout",
        "active_waiters",
        "wait_deadline",
    )

    def __init__(
        self,
        *,
        name: str,
        index: int,
        proc: subprocess.Popen[bytes] | None,
        argv: list[str] | None,
        limits: IoLimits | None,
        pid: int,
        pgid: int | None,
        pid_starttime: int | None,
        restored: bool = False,
    ) -> None:
        self.name = name
        self.index = index
        self.proc = proc
        self.argv = argv
        self.limits = limits or IoLimits()
        self.pid = pid
        self.pgid = pgid if pgid is not None else pid
        self.pid_starttime = pid_starttime
        self.stdout = _StreamState()
        self.stderr = _StreamState()
        self.stdin_buffer = bytearray()
        self.stdin_user_closed = False
        self.stdin_fd_closed = proc is None or proc.stdin is None
        self.stdin_broken = False
        self.returncode: int | None = None
        self.closed = False
        self.restored = restored
        self.kill_requested = False
        self.last_timeout: float | None = None
        self.active_waiters = 0
        self.wait_deadline: float | None = None


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_negative_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _non_negative_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and value >= 0
        and math.isfinite(value)
    )


def _pid_starttime(pid: int) -> int | None:
    """Return the Linux start-time token used to detect PID reuse."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as stat_file:
            data = stat_file.read()
        after_comm = data.rindex(b")") + 2
        fields = data[after_comm:].split()
        return int(fields[19])
    except (FileNotFoundError, ProcessLookupError, IndexError, ValueError, OSError):
        return None


def _process_group_pids(pgid: int) -> list[int]:
    if os.name != "posix":
        return []
    pids: list[int] = []
    try:
        proc_names = os.listdir("/proc")
    except OSError:
        return []

    for name in proc_names:
        if not name.isdigit():
            continue
        pid = int(name)
        try:
            with open(f"/proc/{pid}/stat", "rb") as stat_file:
                data = stat_file.read()
            fields = data[data.rindex(b")") + 2 :].split()
            if int(fields[2]) == pgid:
                pids.append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return sorted(pids)


def _b64encode(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64decode(data: str) -> bytes:
    return base64.b64decode(data.encode("ascii"), validate=True)


class SubprocessIoGuard:
    """Wrap child processes with isolated, metered stdin/stdout/stderr."""

    def __init__(self, *, max_handles: int = 32) -> None:
        if not _positive_int(max_handles):
            raise IoGuardError("IO_INVALID_LIMITS", {"max_handles": max_handles})
        self.max_handles = max_handles
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._handles: dict[str, _Handle] = {}
        self._restored_entries: dict[str, dict[str, Any]] = {}
        self._next_index = 1

    def spawn(self, argv: object, limits: IoLimits | None = None) -> str:
        if (
            not isinstance(argv, list)
            or not argv
            or not all(isinstance(item, str) for item in argv)
        ):
            raise IoGuardError("IO_INVALID_ARGV", argv)

        if limits is None:
            limits = IoLimits()
        if not isinstance(limits, IoLimits):
            raise IoGuardError("IO_INVALID_LIMITS", {"limits": limits})
        if (
            not _positive_int(limits.max_buffer)
            or not _positive_int(limits.line_limit)
            or not _non_negative_number(limits.graceful_seconds)
        ):
            raise IoGuardError(
                "IO_INVALID_LIMITS",
                {
                    "max_buffer": limits.max_buffer,
                    "line_limit": limits.line_limit,
                    "graceful_seconds": limits.graceful_seconds,
                },
            )

        with self._condition:
            active_count = sum(not item.closed for item in self._handles.values())
            if active_count >= self.max_handles:
                raise IoGuardError("IO_HANDLE_LIMIT", self.max_handles)

            index = self._next_index
            name = f"{HANDLE_PREFIX}{index}"
            popen_kwargs: dict[str, Any] = {
                "stdin": subprocess.PIPE,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "bufsize": 0,
                "close_fds": True,
            }
            if os.name == "posix":
                popen_kwargs["start_new_session"] = True
            elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
                popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

            try:
                proc = subprocess.Popen(argv, **popen_kwargs)
            except (OSError, ValueError) as exc:
                raise IoGuardError("IO_SPAWN_FAILED", str(exc)) from exc

            if proc.stdin is not None:
                try:
                    os.set_blocking(proc.stdin.fileno(), False)
                except (OSError, ValueError):
                    pass

            try:
                pgid = os.getpgid(proc.pid) if os.name == "posix" else proc.pid
            except ProcessLookupError:
                pgid = proc.pid

            handle = _Handle(
                name=name,
                index=index,
                proc=proc,
                argv=list(argv),
                limits=limits,
                pid=proc.pid,
                pgid=pgid,
                pid_starttime=_pid_starttime(proc.pid),
            )
            self._next_index += 1
            self._handles[name] = handle

        threading.Thread(
            target=self._pump_stream, args=(name, "stdout"), daemon=True
        ).start()
        threading.Thread(
            target=self._pump_stream, args=(name, "stderr"), daemon=True
        ).start()
        threading.Thread(target=self._stdin_writer, args=(name,), daemon=True).start()
        return name

    def write_stdin(self, handle: str, data: bytes) -> int:
        with self._condition:
            item = self._require_open(handle)
            if not isinstance(data, (bytes, bytearray, memoryview)):
                raise IoGuardError("IO_INVALID_DATA", type(data).__name__)
            if item.stdin_user_closed:
                raise IoGuardError("IO_STDIN_CLOSED", handle)

            self._flush_stdin_locked(item)
            incoming = bytes(data)
            if len(item.stdin_buffer) + len(incoming) > item.limits.max_buffer:
                raise IoGuardError(
                    "IO_STDIN_BACKPRESSURE",
                    {
                        "handle": handle,
                        "backlog": len(item.stdin_buffer),
                        "data_length": len(incoming),
                        "max_buffer": item.limits.max_buffer,
                    },
                )

            item.stdin_buffer.extend(incoming)
            self._flush_stdin_locked(item)
            self._condition.notify_all()
            return len(incoming)

    def close_stdin(self, handle: str) -> None:
        with self._condition:
            item = self._require_open(handle)
            self._request_stdin_close_locked(item)

    def read_stdout(self, handle: str, max_bytes: int) -> bytes:
        return self._read_stream(handle, "stdout", max_bytes)

    def read_stderr(self, handle: str, max_bytes: int) -> bytes:
        return self._read_stream(handle, "stderr", max_bytes)

    def read_lines(self, handle: str) -> list:
        with self._condition:
            item = self._require_open(handle)
            stream = item.stdout
            deadline = time.monotonic() + item.limits.graceful_seconds

            while True:
                if item.closed:
                    raise IoGuardError("IO_HANDLE_CLOSED", handle)
                self._poll_locked(item)
                self._raise_stream_error(stream)

                lines = self._take_complete_lines_locked(item, stream)
                if lines is not None:
                    return lines

                if stream.eof:
                    return []

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(remaining)

    def drain(self, handle: str) -> bytes:
        with self._condition:
            item = self._require_open(handle)
            stream = item.stdout
            chunks: list[bytes] = []
            deadline = time.monotonic() + item.limits.graceful_seconds

            while True:
                if item.closed:
                    raise IoGuardError("IO_HANDLE_CLOSED", handle)
                self._poll_locked(item)
                self._raise_stream_error(stream)

                if stream.buffer:
                    chunk = bytes(stream.buffer)
                    stream.offset += len(chunk)
                    stream.buffer.clear()
                    chunks.append(chunk)
                    deadline = time.monotonic() + item.limits.graceful_seconds
                    continue

                if stream.eof:
                    return b"".join(chunks)

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b"".join(chunks)
                self._condition.wait(remaining)

    def wait(self, handle: str, timeout: float) -> int:
        if (
            not isinstance(timeout, (int, float))
            or isinstance(timeout, bool)
            or not math.isfinite(timeout)
        ):
            raise IoGuardError("IO_INVALID_TIMEOUT", timeout)

        with self._condition:
            item = self._require_open(handle)
            item.last_timeout = float(timeout)
            deadline = time.monotonic() + timeout
            item.active_waiters += 1
            item.wait_deadline = (
                deadline
                if item.wait_deadline is None
                else min(item.wait_deadline, deadline)
            )
            try:
                while True:
                    if item.closed:
                        raise IoGuardError("IO_HANDLE_CLOSED", handle)

                    returncode = self._poll_locked(item)
                    if (
                        returncode is not None
                        and item.stdout.eof
                        and item.stderr.eof
                    ):
                        self._raise_stream_error(item.stdout)
                        self._raise_stream_error(item.stderr)
                        return returncode

                    now = time.monotonic()
                    if now >= deadline:
                        raise IoGuardError("IO_WAIT_TIMEOUT", handle)

                    if returncode is not None:
                        pipe_deadline = now + max(
                            item.limits.graceful_seconds, _STOP_POLL
                        )
                        wait_until = min(deadline, pipe_deadline)
                        if wait_until <= now:
                            raise IoGuardError("IO_WAIT_TIMEOUT", handle)
                        self._condition.wait(wait_until - now)
                    else:
                        self._condition.wait(min(_STOP_POLL, deadline - now))
            finally:
                item.active_waiters = max(0, item.active_waiters - 1)
                if item.active_waiters == 0:
                    item.wait_deadline = None

    def kill_tree(self, handle: str) -> str:
        with self._condition:
            item = self._get_known(handle)
            if item.proc is None:
                return "already-exited"

            returncode = self._poll_locked(item)
            if returncode is not None:
                return "already-exited"

            self._close_stdin_fd_locked(item)
            self._signal_group_locked(item, signal.SIGKILL)
            item.kill_requested = True
            return "killed"

    def close(self, handle: str) -> None:
        with self._condition:
            item = self._get_known(handle)
            if item.closed:
                return

            item.closed = True
            item.stdout.buffer.clear()
            item.stderr.buffer.clear()
            item.stdin_buffer.clear()
            self._condition.notify_all()
            self._terminate_after_close_locked(item)

    def handles(self) -> list:
        with self._condition:
            active = [item for item in self._handles.values() if not item.closed]
            active.sort(key=lambda item: item.index)
            return [item.name for item in active]

    def snapshot(self) -> dict:
        with self._condition:
            handles: dict[str, dict[str, Any]] = {}
            for name, item in self._handles.items():
                if item.closed:
                    continue
                self._poll_locked(item)
                now = time.monotonic()
                if item.active_waiters and item.wait_deadline is not None:
                    remaining_timeout: float = max(
                        0.0, item.wait_deadline - now
                    )
                else:
                    remaining_timeout = 0.0

                stdout_offset = item.stdout.offset
                stderr_offset = item.stderr.offset
                state_name = "exited" if item.returncode is not None else "running"
                handles[name] = {
                    "handle": name,
                    "state": state_name,
                    "status": state_name,
                    "pid": item.pid,
                    "pgid": item.pgid,
                    "process_group_pids": _process_group_pids(item.pgid),
                    "pid_starttime": item.pid_starttime,
                    "returncode": item.returncode,
                    "stdout_offset": stdout_offset,
                    "stderr_offset": stderr_offset,
                    "stdout_read_offset": stdout_offset,
                    "stderr_read_offset": stderr_offset,
                    "read_offsets": {
                        "stdout": stdout_offset,
                        "stderr": stderr_offset,
                    },
                    "offsets": {
                        "stdout": stdout_offset,
                        "stderr": stderr_offset,
                    },
                    "remaining_timeout": remaining_timeout,
                    "timeout_remaining": remaining_timeout,
                    "active_wait": bool(item.active_waiters),
                    "last_timeout": item.last_timeout,
                    "timeouts": {
                        "remaining": remaining_timeout,
                        "last_wait": item.last_timeout,
                        "graceful_seconds": item.limits.graceful_seconds,
                    },
                    "max_buffer": item.limits.max_buffer,
                    "line_limit": item.limits.line_limit,
                    "graceful_seconds": item.limits.graceful_seconds,
                    "limits": {
                        "max_buffer": item.limits.max_buffer,
                        "line_limit": item.limits.line_limit,
                        "graceful_seconds": item.limits.graceful_seconds,
                    },
                    "argv": list(item.argv) if item.argv is not None else None,
                    "stdout_buffer": _b64encode(bytes(item.stdout.buffer)),
                    "stderr_buffer": _b64encode(bytes(item.stderr.buffer)),
                    "buffer_encoding": "base64",
                    "stdout_buffer_b64": _b64encode(bytes(item.stdout.buffer)),
                    "stderr_buffer_b64": _b64encode(bytes(item.stderr.buffer)),
                }

            for name, saved_entry in self._restored_entries.items():
                if name not in handles:
                    restored_entry = copy.deepcopy(saved_entry)
                    restored_entry["state"] = "reaped"
                    restored_entry["status"] = "reaped"
                    restored_entry["reclaimed"] = True
                    handles[name] = restored_entry

            ordered_handles = {
                name: handles[name]
                for name in sorted(handles, key=self._index_from_name)
            }
            snapshot = {
                "kind": "subprocess_io_guard_snapshot",
                "version": 1,
                "max_handles": self.max_handles,
                "next_index": self._next_index,
                "handles": ordered_handles,
            }
            return copy.deepcopy(snapshot)

    def restore(self, state: dict) -> None:
        parsed = self._validate_snapshot(state)

        with self._condition:
            restored = copy.deepcopy(parsed)
            entries = restored["handles"]
            self._restored_entries = copy.deepcopy(entries)
            for saved_entry in self._restored_entries.values():
                for buffer_key in ("stdout_buffer", "stderr_buffer"):
                    buffer_value = saved_entry.get(buffer_key)
                    if isinstance(buffer_value, bytes):
                        saved_entry[buffer_key] = _b64encode(buffer_value)
                        saved_entry["buffer_encoding"] = "base64"

            for name, entry in entries.items():
                existing = self._handles.get(name)
                if self._is_matching_runtime_locked(existing, entry):
                    continue
                self._reap_snapshot_group_locked(entry)

            for name, entry in entries.items():
                existing = self._handles.get(name)
                if self._is_matching_runtime_locked(existing, entry):
                    assert existing is not None
                    self._restore_runtime_entry_locked(existing, entry)
                    continue

                tombstone = _Handle(
                    name=name,
                    index=self._index_from_name(name),
                    proc=None,
                    argv=entry.get("argv"),
                    limits=self._limits_from_entry(entry),
                    pid=entry["pid"],
                    pgid=entry.get("pgid", entry["pid"]),
                    pid_starttime=entry.get("pid_starttime"),
                    restored=True,
                )
                tombstone.closed = True
                tombstone.returncode = entry.get("returncode")
                self._handles[name] = tombstone

            for name, item in list(self._handles.items()):
                if name not in entries and not item.closed and not item.restored:
                    item.closed = True
                    item.stdout.buffer.clear()
                    item.stderr.buffer.clear()
                    item.stdin_buffer.clear()
                    self._terminate_after_close_locked(item)

            next_index = restored.get("next_index")
            if _positive_int(next_index):
                self._next_index = max(self._next_index, int(next_index))
            self._condition.notify_all()

    def _require_open(self, handle: str) -> _Handle:
        item = self._get_known(handle)
        if item.closed:
            raise IoGuardError("IO_HANDLE_CLOSED", handle)
        return item

    def _get_known(self, handle: str) -> _Handle:
        if not isinstance(handle, str):
            raise IoGuardError("IO_UNKNOWN_HANDLE", handle)
        item = self._handles.get(handle)
        if item is None:
            raise IoGuardError("IO_UNKNOWN_HANDLE", handle)
        return item

    def _read_stream(self, handle: str, which: str, max_bytes: int) -> bytes:
        with self._condition:
            item = self._require_open(handle)
            if not _positive_int(max_bytes):
                raise IoGuardError("IO_INVALID_LIMIT", max_bytes)

            stream = item.stdout if which == "stdout" else item.stderr
            deadline = time.monotonic() + item.limits.graceful_seconds

            while True:
                if item.closed:
                    raise IoGuardError("IO_HANDLE_CLOSED", handle)
                self._poll_locked(item)
                self._raise_stream_error(stream)

                if stream.buffer:
                    size = min(max_bytes, len(stream.buffer))
                    piece = bytes(stream.buffer[:size])
                    del stream.buffer[:size]
                    stream.offset += size
                    self._condition.notify_all()
                    return piece

                if stream.eof:
                    return b""

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._condition.wait(remaining)

    def _pump_stream(self, name: str, which: str) -> None:
        item = self._handles.get(name)
        if item is None or item.proc is None:
            return
        pipe = item.proc.stdout if which == "stdout" else item.proc.stderr
        if pipe is None:
            return
        stream = item.stdout if which == "stdout" else item.stderr

        while True:
            try:
                chunk = os.read(pipe.fileno(), _READ_CHUNK)
            except OSError as exc:
                with self._condition:
                    stream.eof = True
                    stream.error = stream.error or (
                        "IO_PIPE_READ_FAILED",
                        {"stream": which, "error": exc.errno or str(exc)},
                    )
                    self._condition.notify_all()
                return

            if not chunk:
                with self._condition:
                    stream.eof = True
                    try:
                        pipe.close()
                    except OSError:
                        pass
                    self._condition.notify_all()
                return

            with self._condition:
                if item.closed:
                    continue
                self._append_chunk_locked(item, stream, chunk)
                self._condition.notify_all()

    def _append_chunk_locked(
        self, item: _Handle, stream: _StreamState, chunk: bytes
    ) -> None:
        max_buffer = item.limits.max_buffer
        if stream.error is not None:
            return

        free = max_buffer - len(stream.buffer)
        if free <= 0:
            stream.error = ("IO_BUFFER_OVERFLOW", {"max_buffer": max_buffer})
            self._begin_overflow_teardown_locked(item)
            return

        if len(chunk) > free:
            stream.buffer.extend(chunk[:free])
            stream.error = (
                "IO_BUFFER_OVERFLOW",
                {"max_buffer": max_buffer, "dropped_bytes": len(chunk) - free},
            )
            self._begin_overflow_teardown_locked(item)
            return

        stream.buffer.extend(chunk)

    def _begin_overflow_teardown_locked(self, item: _Handle) -> None:
        if item.closed or item.returncode is not None or item.kill_requested:
            return
        self._request_stdin_close_locked(item)
        self._signal_group_locked(item, signal.SIGKILL)
        item.kill_requested = True

    def _stdin_writer(self, name: str) -> None:
        while True:
            fd: int | None = None
            with self._condition:
                item = self._handles.get(name)
                if item is None:
                    return
                if item.closed or item.stdin_fd_closed:
                    if not item.stdin_fd_closed and item.proc is not None:
                        self._close_stdin_fd_locked(item)
                    return

                if item.stdin_buffer:
                    try:
                        self._flush_stdin_locked(item)
                    except IoGuardError:
                        return
                    if item.stdin_buffer:
                        if item.proc is not None and item.proc.stdin is not None:
                            fd = item.proc.stdin.fileno()
                    elif item.stdin_user_closed:
                        self._close_stdin_fd_locked(item)
                        return
                elif item.stdin_user_closed:
                    self._close_stdin_fd_locked(item)
                    return
                else:
                    self._condition.wait(0.2)
                    continue

            if fd is None:
                time.sleep(_STOP_POLL)
                continue

            try:
                select.select([], [fd], [], 0.2)
            except OSError:
                pass

    def _flush_stdin_locked(self, item: _Handle) -> None:
        if item.stdin_fd_closed or item.proc is None or item.proc.stdin is None:
            return
        fd = item.proc.stdin.fileno()
        while item.stdin_buffer:
            try:
                written = os.write(fd, memoryview(item.stdin_buffer))
            except BlockingIOError:
                return
            except BrokenPipeError as exc:
                item.stdin_broken = True
                item.stdin_user_closed = True
                self._close_stdin_fd_locked(item)
                raise IoGuardError("IO_STDIN_CLOSED", "broken pipe") from exc
            except OSError as exc:
                item.stdin_broken = True
                item.stdin_user_closed = True
                self._close_stdin_fd_locked(item)
                raise IoGuardError(
                    "IO_STDIN_WRITE_FAILED", exc.errno or str(exc)
                ) from exc

            if written <= 0:
                return
            del item.stdin_buffer[:written]

        if item.stdin_user_closed:
            self._close_stdin_fd_locked(item)

    def _request_stdin_close_locked(self, item: _Handle) -> None:
        item.stdin_user_closed = True
        try:
            self._flush_stdin_locked(item)
        except IoGuardError:
            pass
        if not item.stdin_buffer:
            self._close_stdin_fd_locked(item)
        self._condition.notify_all()

    def _close_stdin_fd_locked(self, item: _Handle) -> None:
        if item.stdin_fd_closed:
            return
        if item.proc is None or item.proc.stdin is None:
            item.stdin_fd_closed = True
            return
        try:
            item.proc.stdin.close()
        except OSError:
            try:
                os.close(item.proc.stdin.fileno())
            except OSError:
                pass
        item.stdin_fd_closed = True

    def _poll_locked(self, item: _Handle) -> int | None:
        if item.returncode is not None or item.proc is None:
            return item.returncode
        returncode = item.proc.poll()
        if returncode is not None:
            item.returncode = returncode
            self._condition.notify_all()
        return item.returncode

    def _signal_group_locked(self, item: _Handle, sig: int) -> None:
        if os.name != "posix":
            if item.proc is not None:
                try:
                    item.proc.send_signal(sig)
                except (ProcessLookupError, OSError):
                    pass
            return

        try:
            os.killpg(item.pgid, sig)
            return
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise IoGuardError("IO_KILL_FAILED", item.pgid) from exc

        if item.proc is not None:
            try:
                os.kill(item.pid, sig)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise IoGuardError("IO_KILL_FAILED", item.pid) from exc

    def _terminate_after_close_locked(self, item: _Handle) -> None:
        if item.proc is None:
            return
        if self._poll_locked(item) is not None:
            self._close_stdin_fd_locked(item)
            return

        self._request_stdin_close_locked(item)
        deadline = time.monotonic() + item.limits.graceful_seconds
        while self._poll_locked(item) is None and time.monotonic() < deadline:
            self._condition.wait(_STOP_POLL)

        if item.returncode is None:
            self._close_stdin_fd_locked(item)
            self._signal_group_locked(item, signal.SIGKILL)
            item.kill_requested = True
            kill_deadline = time.monotonic() + max(
                item.limits.graceful_seconds, _STOP_POLL
            )
            while self._poll_locked(item) is None and time.monotonic() < kill_deadline:
                self._condition.wait(_STOP_POLL)

    def _take_complete_lines_locked(
        self, item: _Handle, stream: _StreamState
    ) -> list[bytes] | None:
        limit = item.limits.line_limit
        lines: list[bytes] = []

        while True:
            newline = stream.buffer.find(b"\n")
            if newline < 0:
                if len(stream.buffer) > limit:
                    if lines:
                        return lines
                    raise IoGuardError(
                        "IO_LINE_TOO_LONG",
                        {"handle": item.name, "length": len(stream.buffer)},
                    )
                return lines or None

            line_length = newline + 1
            if line_length > limit:
                if lines:
                    return lines
                raise IoGuardError(
                    "IO_LINE_TOO_LONG",
                    {"handle": item.name, "length": line_length},
                )

            line = bytes(stream.buffer[:line_length])
            del stream.buffer[:line_length]
            stream.offset += line_length
            lines.append(line)

    def _raise_stream_error(self, stream: _StreamState) -> None:
        if stream.error is not None:
            code, detail = stream.error
            raise IoGuardError(code, detail)

    def _index_from_name(self, name: str) -> int:
        try:
            return int(name[len(HANDLE_PREFIX) :])
        except (ValueError, IndexError):
            return 0

    def _limits_from_entry(self, entry: dict[str, Any]) -> IoLimits:
        limits = entry.get("limits")
        if isinstance(limits, dict):
            max_buffer = limits.get("max_buffer", IoLimits.max_buffer)
            line_limit = limits.get("line_limit", IoLimits.line_limit)
            graceful = limits.get(
                "graceful_seconds", IoLimits.graceful_seconds
            )
        else:
            max_buffer = entry.get("max_buffer", IoLimits.max_buffer)
            line_limit = entry.get("line_limit", IoLimits.line_limit)
            graceful = entry.get(
                "graceful_seconds", IoLimits.graceful_seconds
            )
        try:
            return IoLimits(
                max_buffer=int(max_buffer),
                line_limit=int(line_limit),
                graceful_seconds=float(graceful),
            )
        except (TypeError, ValueError):
            return IoLimits()

    def _validate_snapshot(self, state: object) -> dict[str, Any]:
        try:
            cloned = copy.deepcopy(state)
        except Exception as exc:
            raise IoGuardError("IO_SNAPSHOT_INVALID", "un-copyable state") from exc

        if not isinstance(cloned, dict):
            raise IoGuardError("IO_SNAPSHOT_INVALID", "state must be a dict")
        handles = cloned.get("handles")
        if not isinstance(handles, dict):
            raise IoGuardError("IO_SNAPSHOT_INVALID", "handles must be a dict")

        for key, entry in handles.items():
            if not isinstance(key, str) or not key.startswith(HANDLE_PREFIX):
                raise IoGuardError("IO_SNAPSHOT_INVALID", {"bad_handle": key})
            if not isinstance(entry, dict):
                raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})
            if not isinstance(entry.get("state"), str) or not entry["state"]:
                raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})
            if not _positive_int(entry.get("pid")):
                raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})

            offsets = entry.get("read_offsets")
            if not isinstance(offsets, dict):
                offsets = {}
            stdout_offset = entry.get("stdout_offset", offsets.get("stdout"))
            stderr_offset = entry.get("stderr_offset", offsets.get("stderr"))
            if not _non_negative_int(stdout_offset) or not _non_negative_int(
                stderr_offset
            ):
                raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})

            remaining = entry.get("remaining_timeout", 0.0)
            if remaining is not None and not _non_negative_number(remaining):
                raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})

            buffer_encoding = entry.get("buffer_encoding")
            for buffer_key in ("stdout_buffer", "stderr_buffer"):
                buffer_value = entry.get(buffer_key, b"")
                if isinstance(buffer_value, bytearray):
                    entry[buffer_key] = bytes(buffer_value)
                elif isinstance(buffer_value, str):
                    if buffer_encoding not in (None, "base64"):
                        raise IoGuardError(
                            "IO_SNAPSHOT_INVALID", {"handle": key}
                        )
                    try:
                        entry[buffer_key] = _b64decode(buffer_value)
                    except (ValueError, TypeError):
                        raise IoGuardError(
                            "IO_SNAPSHOT_INVALID", {"handle": key}
                        ) from None
                elif not isinstance(buffer_value, bytes):
                    raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})

            for b64_key, buffer_key in (
                ("stdout_buffer_b64", "stdout_buffer"),
                ("stderr_buffer_b64", "stderr_buffer"),
            ):
                b64_value = entry.get(b64_key)
                if isinstance(b64_value, str):
                    try:
                        decoded = _b64decode(b64_value)
                    except (ValueError, TypeError):
                        raise IoGuardError(
                            "IO_SNAPSHOT_INVALID", {"handle": key}
                        ) from None
                    entry[buffer_key] = decoded
                elif b64_value is not None and not isinstance(b64_value, bytes):
                    raise IoGuardError("IO_SNAPSHOT_INVALID", {"handle": key})

        return cloned

    def _is_matching_runtime_locked(
        self, item: _Handle | None, entry: dict[str, Any]
    ) -> bool:
        if item is None or item.closed or item.proc is None:
            return False
        if item.pid != entry["pid"]:
            return False
        entry_starttime = entry.get("pid_starttime")
        if (
            isinstance(entry_starttime, int)
            and item.pid_starttime is not None
            and entry_starttime != item.pid_starttime
        ):
            return False
        return True

    def _restore_runtime_entry_locked(
        self, item: _Handle, entry: dict[str, Any]
    ) -> None:
        offsets = entry.get("read_offsets")
        if not isinstance(offsets, dict):
            offsets = {}
        stdout_offset = entry.get("stdout_offset", offsets.get("stdout", 0))
        stderr_offset = entry.get("stderr_offset", offsets.get("stderr", 0))
        if _non_negative_int(stdout_offset):
            item.stdout.offset = int(stdout_offset)
        if _non_negative_int(stderr_offset):
            item.stderr.offset = int(stderr_offset)

        stdout_buffer = entry.get("stdout_buffer", b"")
        stderr_buffer = entry.get("stderr_buffer", b"")
        if isinstance(stdout_buffer, (bytes, bytearray)):
            item.stdout.buffer = bytearray(stdout_buffer)
        if isinstance(stderr_buffer, (bytes, bytearray)):
            item.stderr.buffer = bytearray(stderr_buffer)

        remaining = entry.get("remaining_timeout")
        if _non_negative_number(remaining) and float(remaining) > 0:
            item.active_waiters = max(1, item.active_waiters)
            item.wait_deadline = time.monotonic() + float(remaining)

    def _reap_snapshot_group_locked(self, entry: dict[str, Any]) -> None:
        pid = int(entry["pid"])
        pgid = int(entry.get("pgid", pid))
        starttime = entry.get("pid_starttime")
        current_starttime = _pid_starttime(pid)
        if (
            isinstance(starttime, int)
            and starttime > 0
            and current_starttime is not None
            and current_starttime != starttime
        ):
            return

        if os.name == "posix":
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                raise IoGuardError("IO_KILL_FAILED", pgid) from exc
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            try:
                waited_pid, _status = os.waitpid(pid, os.WNOHANG)
                if waited_pid == pid:
                    break
            except ChildProcessError:
                break
            except OSError:
                break
            time.sleep(_STOP_POLL)
