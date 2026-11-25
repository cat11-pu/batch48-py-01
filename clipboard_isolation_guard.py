"""Per-session clipboard isolation with sensitivity marks and TTL."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field
from typing import Any


_STAT_NAMES = ("written", "read", "pasted", "denied", "expired", "evicted")
_NO_PAYLOAD = object()


class ClipboardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass
class _Session:
    user: str
    locked: bool = False
    closed: bool = False
    entry_ids: list[str] = field(default_factory=list)


@dataclass
class _Entry:
    entry_id: str
    session: str
    user: str
    mime: str
    size: int
    sensitive: bool
    pinned: bool
    expires_at: int
    pasted_from: str | None = None
    payload: Any = _NO_PAYLOAD


class ClipboardIsolationGuard:
    """Keep clipboard entries scoped to a session, with limits and expiry."""

    def __init__(self, *, max_entries: int = 8, max_bytes: int = 4096,
                 default_ttl: int = 300, clock=None) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.default_ttl = default_ttl
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._entries: dict[str, _Entry] = {}
        self._next_entry_id = 0
        self._stats = {name: 0 for name in _STAT_NAMES}

    def _now(self, now=None) -> int:
        if now is not None:
            return int(now)
        if self._clock is not None:
            return int(self._clock())
        return int(time.time())

    def _reject(self, code: str, detail: object = None):
        self._stats["denied"] += 1
        raise ClipboardError(code, detail)

    def _session(self, name: str, *, check_locked: bool = False) -> _Session:
        state = self._sessions.get(name)
        if state is None:
            self._reject("CLIPBOARD_SESSION_INVALID", name)
        if state.closed:
            self._reject("CLIPBOARD_SESSION_CLOSED", name)
        if check_locked and state.locked:
            self._reject("CLIPBOARD_SESSION_LOCKED", name)
        return state

    @staticmethod
    def _metadata(entry: _Entry) -> dict:
        meta = {
            "entry_id": entry.entry_id,
            "session": entry.session,
            "user": entry.user,
            "mime": entry.mime,
            "size": entry.size,
            "sensitive": entry.sensitive,
            "pinned": entry.pinned,
            "expires_at": entry.expires_at,
        }
        if entry.pasted_from is not None:
            meta["pasted_from"] = entry.pasted_from
        return meta

    @staticmethod
    def _session_info(name: str, state: _Session) -> dict:
        return {
            "session": name,
            "user": state.user,
            "locked": state.locked,
        }

    @staticmethod
    def _payload_info(payload, mime) -> tuple[str | bytes, int]:
        if not isinstance(payload, (str, bytes)) or not payload:
            raise ClipboardError("CLIPBOARD_PAYLOAD_INVALID")
        if not isinstance(mime, str) or not mime:
            raise ClipboardError("CLIPBOARD_PAYLOAD_INVALID")
        try:
            size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        except UnicodeEncodeError:
            raise ClipboardError("CLIPBOARD_PAYLOAD_INVALID")
        return payload, size

    def _find_entry(self, session: str, entry_id: str) -> _Entry:
        entry = self._entries.get(entry_id)
        if entry is None:
            self._reject("CLIPBOARD_ENTRY_NOT_FOUND", entry_id)
        if entry.session != session:
            self._reject("CLIPBOARD_CROSS_SESSION_DENIED", entry_id)
        return entry

    def _eviction_plan(self, state: _Session, new_size: int) -> list[str]:
        count = len(state.entry_ids)
        byte_count = sum(self._entries[entry_id].size for entry_id in state.entry_ids)
        if count + 1 <= self.max_entries and byte_count + new_size <= self.max_bytes:
            return []

        evicted: list[str] = []
        for entry_id in state.entry_ids:
            if count + 1 <= self.max_entries and byte_count + new_size <= self.max_bytes:
                break
            entry = self._entries[entry_id]
            if entry.pinned:
                continue
            evicted.append(entry_id)
            count -= 1
            byte_count -= entry.size

        if count + 1 > self.max_entries or byte_count + new_size > self.max_bytes:
            self._reject("CLIPBOARD_QUOTA_EXCEEDED")
        return evicted

    def _store(self, session: str, state: _Session, payload, *,
               mime: str, sensitive: bool, pinned: bool,
               expires_at: int, pasted_from: str | None = None) -> dict:
        self._next_entry_id += 1
        entry_id = "e%d" % self._next_entry_id
        size = len(payload.encode("utf-8")) if isinstance(payload, str) else len(payload)
        entry = _Entry(
            entry_id=entry_id,
            session=session,
            user=state.user,
            mime=mime,
            size=size,
            sensitive=bool(sensitive),
            pinned=bool(pinned),
            expires_at=expires_at,
            pasted_from=pasted_from,
            payload=payload,
        )
        self._entries[entry_id] = entry
        state.entry_ids.append(entry_id)
        return self._metadata(entry)

    def open_session(self, session: str, *, user: str) -> dict:
        with self._lock:
            state = self._sessions.get(session)
            if state is not None and not state.closed:
                if state.user != user:
                    self._reject("CLIPBOARD_CROSS_SESSION_DENIED", session)
                return self._session_info(session, state)

            state = _Session(user=user)
            self._sessions[session] = state
            return self._session_info(session, state)

    def write(self, session: str, payload, *, mime: str = "text/plain",
              sensitive: bool = False, pinned: bool = False,
              ttl=None, now=None) -> dict:
        with self._lock:
            state = self._session(session, check_locked=True)
            try:
                payload, size = self._payload_info(payload, mime)
            except ClipboardError:
                self._stats["denied"] += 1
                raise

            evicted = self._eviction_plan(state, size)
            for entry_id in evicted:
                self._remove_entry(entry_id)
            self._stats["evicted"] += len(evicted)

            lifetime = self.default_ttl if ttl is None else int(ttl)
            entry = self._store(
                session,
                state,
                payload,
                mime=mime,
                sensitive=sensitive,
                pinned=pinned,
                expires_at=self._now(now) + lifetime,
            )
            self._stats["written"] += 1
            return entry

    def read(self, session: str, entry_id: str, *, now=None) -> dict:
        with self._lock:
            self._session(session, check_locked=True)
            entry = self._find_entry(session, entry_id)
            if self._now(now) >= entry.expires_at:
                self._reject("CLIPBOARD_ENTRY_EXPIRED", entry_id)

            result = self._metadata(entry)
            if entry.payload is not _NO_PAYLOAD:
                result["payload"] = entry.payload
            self._stats["read"] += 1
            return result

    def paste(self, source: str, target: str, entry_id: str, *,
              now=None) -> dict:
        with self._lock:
            source_state = self._session(source, check_locked=True)
            target_state = self._session(target, check_locked=True)
            entry = self._find_entry(source, entry_id)
            if self._now(now) >= entry.expires_at:
                self._reject("CLIPBOARD_ENTRY_EXPIRED", entry_id)
            if source != target:
                if source_state.user != target_state.user:
                    self._reject("CLIPBOARD_CROSS_SESSION_DENIED", entry_id)
                if entry.sensitive:
                    self._reject("CLIPBOARD_CROSS_SESSION_DENIED", entry_id)
            if entry.payload is _NO_PAYLOAD:
                self._reject("CLIPBOARD_PAYLOAD_INVALID", entry_id)

            evicted = self._eviction_plan(target_state, entry.size)
            for old_entry_id in evicted:
                self._remove_entry(old_entry_id)
            self._stats["evicted"] += len(evicted)

            pasted = self._store(
                target,
                target_state,
                copy.deepcopy(entry.payload),
                mime=entry.mime,
                sensitive=entry.sensitive,
                pinned=False,
                expires_at=self._now(now) + self.default_ttl,
                pasted_from=entry.entry_id,
            )
            self._stats["pasted"] += 1
            return pasted

    def list_entries(self, session: str, *, now=None) -> list:
        with self._lock:
            state = self._session(session)
            current = self._now(now)
            return [
                self._metadata(self._entries[entry_id])
                for entry_id in state.entry_ids
                if current < self._entries[entry_id].expires_at
            ]

    def lock_session(self, session: str) -> int:
        with self._lock:
            state = self._session(session)
            removed = len(state.entry_ids)
            for entry_id in list(state.entry_ids):
                self._remove_entry(entry_id)
            state.locked = True
            return removed

    def unlock_session(self, session: str) -> dict:
        with self._lock:
            state = self._session(session)
            state.locked = False
            return self._session_info(session, state)

    def logout(self, session: str) -> int:
        with self._lock:
            state = self._session(session)
            removed = len(state.entry_ids)
            for entry_id in list(state.entry_ids):
                self._remove_entry(entry_id)
            state.locked = False
            state.closed = True
            return removed

    def expire(self, *, now=None) -> int:
        with self._lock:
            current = self._now(now)
            expired = [
                entry_id
                for entry_id, entry in self._entries.items()
                if current >= entry.expires_at
            ]
            for entry_id in expired:
                self._remove_entry(entry_id)
            self._stats["expired"] += len(expired)
            return len(expired)

    def snapshot(self) -> dict:
        with self._lock:
            sessions = {
                name: {
                    "user": state.user,
                    "locked": state.locked,
                    "closed": state.closed,
                }
                for name, state in self._sessions.items()
            }
            entries = [copy.deepcopy(self._metadata(entry))
                       for entry in self._entries.values()]
            counters = dict(self._stats)
            counters["next_entry_id"] = self._next_entry_id
            return {
                "sessions": sessions,
                "entries": entries,
                "counters": counters,
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                sessions, entries, next_entry_id, stats = self._parse_snapshot(state)
            except Exception:
                self._stats["denied"] += 1
                raise ClipboardError("CLIPBOARD_SNAPSHOT_INVALID")

            self._sessions = sessions
            self._entries = entries
            self._next_entry_id = next_entry_id
            self._stats = stats

    def _parse_snapshot(self, state: dict):
        if not isinstance(state, dict):
            raise ValueError
        raw_sessions = state.get("sessions")
        raw_entries = state.get("entries")
        raw_counters = state.get("counters")
        if not isinstance(raw_sessions, dict):
            raise ValueError
        if not isinstance(raw_entries, list):
            raise ValueError
        if not isinstance(raw_counters, dict):
            raise ValueError

        sessions: dict[str, _Session] = {}
        for name, raw in raw_sessions.items():
            if not isinstance(name, str) or not isinstance(raw, dict):
                raise ValueError
            user = raw.get("user")
            locked = raw.get("locked")
            closed = raw.get("closed")
            if not isinstance(user, str):
                raise ValueError
            if not isinstance(locked, bool) or not isinstance(closed, bool):
                raise ValueError
            sessions[name] = _Session(
                user=user,
                locked=locked,
                closed=closed,
                entry_ids=[],
            )

        entries: dict[str, _Entry] = {}
        for raw in raw_entries:
            if not isinstance(raw, dict):
                raise ValueError
            entry_id = raw.get("entry_id")
            session = raw.get("session")
            user = raw.get("user")
            mime = raw.get("mime")
            size = raw.get("size")
            sensitive = raw.get("sensitive")
            pinned = raw.get("pinned")
            expires_at = raw.get("expires_at")
            pasted_from = raw.get("pasted_from")
            if not isinstance(entry_id, str) or not entry_id:
                raise ValueError
            if entry_id in entries:
                raise ValueError
            if not isinstance(session, str) or session not in sessions:
                raise ValueError
            session_state = sessions[session]
            if session_state.closed:
                raise ValueError
            if not isinstance(user, str) or user != session_state.user:
                raise ValueError
            if not isinstance(mime, str) or not mime:
                raise ValueError
            if not isinstance(size, int) or isinstance(size, bool) or size < 0:
                raise ValueError
            if not isinstance(sensitive, bool) or not isinstance(pinned, bool):
                raise ValueError
            if not isinstance(expires_at, int) or isinstance(expires_at, bool):
                raise ValueError
            if pasted_from is not None and not isinstance(pasted_from, str):
                raise ValueError

            entry = _Entry(
                entry_id=entry_id,
                session=session,
                user=user,
                mime=mime,
                size=size,
                sensitive=sensitive,
                pinned=pinned,
                expires_at=expires_at,
                pasted_from=pasted_from,
            )
            entries[entry_id] = entry
            session_state.entry_ids.append(entry_id)

        next_entry_id = raw_counters.get("next_entry_id")
        if not isinstance(next_entry_id, int) or isinstance(next_entry_id, bool):
            raise ValueError
        if next_entry_id < 0:
            raise ValueError
        stats = {}
        for name in _STAT_NAMES:
            value = raw_counters.get(name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError
            stats[name] = value

        return sessions, entries, next_entry_id, stats

    def stats(self) -> dict:
        with self._lock:
            return {name: self._stats[name] for name in _STAT_NAMES}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {name: 0 for name in _STAT_NAMES}

    def _remove_entry(self, entry_id: str) -> None:
        entry = self._entries.pop(entry_id)
        state = self._sessions.get(entry.session)
        if state is not None:
            try:
                state.entry_ids.remove(entry_id)
            except ValueError:
                pass
