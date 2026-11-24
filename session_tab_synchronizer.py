"""Session generation coordination across browser tabs of one login session."""

from __future__ import annotations

import copy
import threading
import time
from dataclasses import dataclass, field


class SessionTabError(ValueError):
    """Raised when a tab/session operation is refused; code is SESSION_ prefixed."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class TabView:
    tab_id: str
    session_id: str
    generation: int
    capabilities: tuple
    last_seen: float
    valid: bool


@dataclass(frozen=True)
class RefreshClaim:
    session_id: str
    tab_id: str
    claim_id: str
    generation: int
    owner: bool
    status: str


@dataclass
class _Tab:
    tab_id: str
    session_id: str
    generation: int
    capabilities: tuple
    last_seen: float


@dataclass
class _Session:
    session_id: str
    generation: int = 1
    logged_out: bool = False
    tabs: dict = field(default_factory=dict)
    next_claim: int = 1
    claim: dict | None = None
    last_result: dict | None = None


_STAT_KEYS = (
    "opened",
    "reopened",
    "closed",
    "expired",
    "heartbeats",
    "refreshes",
    "refresh_failures",
    "logouts",
)


class SessionTabSynchronizer:
    """Register tabs, heartbeat them and coordinate exactly one refresher."""

    def __init__(self, *, heartbeat_ttl: float = 30.0,
                 max_tabs_per_session: int = 8, clock=None) -> None:
        self.heartbeat_ttl = heartbeat_ttl
        self.max_tabs_per_session = max_tabs_per_session
        self._clock = clock
        self._lock = threading.RLock()
        self._sessions: dict[str, _Session] = {}
        self._tabs: dict[str, _Tab] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    @staticmethod
    def _invalid(name: str, value: object) -> SessionTabError:
        return SessionTabError(
            "SESSION_INVALID_REQUEST", {"field": name, "value": value}
        )

    @classmethod
    def _valid_id(cls, name: str, value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise cls._invalid(name, value)
        return value

    @classmethod
    def _normalize_capabilities(cls, capabilities: object) -> tuple:
        if isinstance(capabilities, (str, bytes)):
            raise cls._invalid("capabilities", capabilities)
        try:
            values = list(capabilities)
        except TypeError:
            raise cls._invalid("capabilities", capabilities) from None
        if any(not isinstance(value, str) for value in values):
            raise cls._invalid("capabilities", capabilities)
        return tuple(sorted(set(values)))

    def _now(self, now):
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return time.monotonic()

    @staticmethod
    def _tab_view(tab: _Tab, session: _Session) -> TabView:
        return TabView(
            tab_id=tab.tab_id,
            session_id=tab.session_id,
            generation=tab.generation,
            capabilities=tab.capabilities,
            last_seen=tab.last_seen,
            valid=not session.logged_out
            and tab.generation == session.generation,
        )

    def _require_session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionTabError(
                "SESSION_UNKNOWN", {"session_id": session_id}
            )
        return session

    def _require_tab(self, session_id: str, tab_id: str) -> _Tab:
        tab = self._tabs.get(tab_id)
        if tab is None:
            raise SessionTabError("SESSION_TAB_UNKNOWN", {"tab_id": tab_id})
        if tab.session_id != session_id:
            raise SessionTabError(
                "SESSION_TAB_MISMATCH",
                {"session_id": session_id, "tab_id": tab_id},
            )
        return tab

    def _expire_locked(self, now) -> int:
        expired = 0
        for session in self._sessions.values():
            for tab_id, tab in list(session.tabs.items()):
                if now - tab.last_seen <= self.heartbeat_ttl:
                    continue
                del session.tabs[tab_id]
                self._tabs.pop(tab_id, None)
                expired += 1
                claim = session.claim
                if claim is not None and claim["tab_id"] == tab_id:
                    session.claim = None
        if expired:
            self._stats["expired"] += expired
        return expired

    def open_tab(self, session_id: str, tab_id: str, capabilities=(),
                 now=None) -> TabView:
        session_id = self._valid_id("session_id", session_id)
        tab_id = self._valid_id("tab_id", tab_id)
        capabilities = self._normalize_capabilities(capabilities)
        now = self._now(now)

        with self._lock:
            existing = self._tabs.get(tab_id)
            if existing is not None:
                if existing.session_id != session_id:
                    raise SessionTabError(
                        "SESSION_TAB_TAKEN",
                        {
                            "tab_id": tab_id,
                            "session_id": session_id,
                            "owner_session_id": existing.session_id,
                        },
                    )
                session = self._require_session(session_id)
                if session.logged_out:
                    raise SessionTabError(
                        "SESSION_LOGGED_OUT", {"session_id": session_id}
                    )
                existing.last_seen = now
                self._stats["reopened"] += 1
                return self._tab_view(existing, session)

            session = self._sessions.get(session_id)
            if session is not None and session.logged_out:
                raise SessionTabError(
                    "SESSION_LOGGED_OUT", {"session_id": session_id}
                )
            if session is None:
                if self.max_tabs_per_session <= 0:
                    raise SessionTabError(
                        "SESSION_TAB_CAPACITY_EXCEEDED",
                        {"session_id": session_id, "limit": 0},
                    )
                session = _Session(session_id=session_id)
                self._sessions[session_id] = session

            if len(session.tabs) >= self.max_tabs_per_session:
                raise SessionTabError(
                    "SESSION_TAB_CAPACITY_EXCEEDED",
                    {
                        "session_id": session_id,
                        "limit": self.max_tabs_per_session,
                    },
                )

            tab = _Tab(
                tab_id=tab_id,
                session_id=session_id,
                generation=session.generation,
                capabilities=capabilities,
                last_seen=now,
            )
            session.tabs[tab_id] = tab
            self._tabs[tab_id] = tab
            self._stats["opened"] += 1
            return self._tab_view(tab, session)

    def heartbeat(self, tab_id: str, now=None) -> dict:
        tab_id = self._valid_id("tab_id", tab_id)
        now = self._now(now)

        with self._lock:
            self._expire_locked(now)
            tab = self._tabs.get(tab_id)
            if tab is None:
                raise SessionTabError(
                    "SESSION_TAB_UNKNOWN", {"tab_id": tab_id}
                )
            session = self._require_session(tab.session_id)
            if session.logged_out:
                raise SessionTabError(
                    "SESSION_LOGGED_OUT",
                    {"session_id": session.session_id},
                )

            valid = tab.generation == session.generation
            tab.generation = session.generation
            tab.last_seen = now
            self._stats["heartbeats"] += 1
            return {
                "tab_id": tab.tab_id,
                "session_id": session.session_id,
                "generation": session.generation,
                "capabilities": tab.capabilities,
                "last_seen": now,
                "valid": valid,
            }

    def close_tab(self, tab_id: str, now=None) -> bool:
        if not isinstance(tab_id, str) or not tab_id.strip():
            return False
        with self._lock:
            tab = self._tabs.pop(tab_id, None)
            if tab is None:
                return False
            session = self._sessions.get(tab.session_id)
            if session is not None:
                session.tabs.pop(tab_id, None)
                if (
                    session.claim is not None
                    and session.claim["tab_id"] == tab_id
                ):
                    session.claim = None
            self._stats["closed"] += 1
            return True

    def claim_refresh(self, session_id: str, tab_id: str,
                      now=None) -> RefreshClaim:
        session_id = self._valid_id("session_id", session_id)
        tab_id = self._valid_id("tab_id", tab_id)
        self._now(now)

        with self._lock:
            session = self._require_session(session_id)
            tab = self._require_tab(session_id, tab_id)
            if session.logged_out:
                raise SessionTabError(
                    "SESSION_LOGGED_OUT", {"session_id": session_id}
                )
            if tab.generation != session.generation:
                raise SessionTabError(
                    "SESSION_STALE",
                    {
                        "session_id": session_id,
                        "tab_id": tab_id,
                        "tab_generation": tab.generation,
                        "generation": session.generation,
                    },
                )

            claim = session.claim
            if claim is not None:
                if claim["generation"] == session.generation:
                    owner = claim["tab_id"] == tab_id
                    return RefreshClaim(
                        session_id=session_id,
                        tab_id=tab_id,
                        claim_id=claim["claim_id"],
                        generation=session.generation,
                        owner=owner,
                        status="granted" if owner else "wait",
                    )
                session.claim = None

            claim = {
                "claim_id": f"rf-{session.next_claim:04d}",
                "tab_id": tab_id,
                "generation": session.generation,
            }
            session.next_claim += 1
            session.claim = claim
            self._stats["refreshes"] += 1
            return RefreshClaim(
                session_id=session_id,
                tab_id=tab_id,
                claim_id=claim["claim_id"],
                generation=session.generation,
                owner=True,
                status="granted",
            )

    def complete_refresh(self, session_id: str, claim_id: str, *, ok: bool,
                         reason: str = "", now=None) -> dict:
        session_id = self._valid_id("session_id", session_id)
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise SessionTabError(
                "SESSION_CLAIM_UNKNOWN", {"claim_id": claim_id}
            )
        self._now(now)

        with self._lock:
            session = self._require_session(session_id)
            if session.logged_out:
                raise SessionTabError(
                    "SESSION_LOGGED_OUT", {"session_id": session_id}
                )

            claim = session.claim
            if (
                claim is None
                or claim["claim_id"] != claim_id
                or claim["generation"] != session.generation
            ):
                raise SessionTabError(
                    "SESSION_CLAIM_UNKNOWN",
                    {"session_id": session_id, "claim_id": claim_id},
                )

            session.claim = None
            if ok:
                session.generation += 1
                result = {
                    "status": "ok",
                    "claim_id": claim_id,
                    "tab_id": claim["tab_id"],
                    "generation": session.generation,
                    "reason": reason,
                }
            else:
                self._stats["refresh_failures"] += 1
                result = {
                    "status": "failed",
                    "claim_id": claim_id,
                    "tab_id": claim["tab_id"],
                    "generation": session.generation,
                    "reason": reason,
                }
            session.last_result = result
            return dict(result)

    def await_refresh(self, session_id: str, tab_id: str, now=None) -> dict:
        session_id = self._valid_id("session_id", session_id)
        tab_id = self._valid_id("tab_id", tab_id)
        self._now(now)

        with self._lock:
            session = self._require_session(session_id)
            tab = self._require_tab(session_id, tab_id)
            if session.logged_out:
                raise SessionTabError(
                    "SESSION_LOGGED_OUT", {"session_id": session_id}
                )

            claim = session.claim
            if (
                claim is not None
                and claim["generation"] == session.generation
            ):
                owner = claim["tab_id"] == tab_id
                return {
                    "session_id": session_id,
                    "tab_id": tab_id,
                    "claim_id": claim["claim_id"],
                    "generation": session.generation,
                    "owner": owner,
                    "status": "granted" if owner else "wait",
                }

            if session.last_result is not None:
                result = copy.deepcopy(session.last_result)
                result.update(
                    {
                        "session_id": session_id,
                        "tab_id": tab_id,
                        "owner": False,
                    }
                )
                return result

            return {
                "session_id": session_id,
                "tab_id": tab_id,
                "claim_id": None,
                "generation": session.generation,
                "owner": False,
                "status": "idle",
            }

    def publish_session(self, session_id: str, *, generation: int | None = None,
                        now=None) -> dict:
        session_id = self._valid_id("session_id", session_id)
        self._now(now)

        with self._lock:
            session = self._require_session(session_id)
            if session.logged_out:
                raise SessionTabError(
                    "SESSION_LOGGED_OUT", {"session_id": session_id}
                )

            if generation is None:
                target = session.generation + 1
            elif (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation <= session.generation
            ):
                raise SessionTabError(
                    "SESSION_GENERATION_REGRESSION",
                    {
                        "session_id": session_id,
                        "generation": generation,
                        "current_generation": session.generation,
                    },
                )
            else:
                target = generation

            session.generation = target
            session.claim = None
            session.last_result = None
            return {
                "session_id": session_id,
                "generation": session.generation,
                "tabs": len(session.tabs),
            }

    def broadcast_logout(self, session_id: str, *, reason: str = "admin_logout",
                         now=None) -> int:
        session_id = self._valid_id("session_id", session_id)
        self._now(now)

        with self._lock:
            session = self._require_session(session_id)
            if session.logged_out:
                return 0

            notified = len(session.tabs)
            session.logged_out = True
            session.generation += 1
            session.claim = None
            session.last_result = None
            self._stats["logouts"] += 1
            return notified

    def expire_tabs(self, now=None) -> int:
        now = self._now(now)
        with self._lock:
            return self._expire_locked(now)

    def tabs_for(self, session_id: str) -> tuple:
        session_id = self._valid_id("session_id", session_id)
        with self._lock:
            session = self._require_session(session_id)
            return tuple(
                self._tab_view(tab, session)
                for tab in session.tabs.values()
            )

    def session_info(self, session_id: str) -> dict:
        session_id = self._valid_id("session_id", session_id)
        with self._lock:
            session = self._require_session(session_id)
            return {
                "session_id": session_id,
                "generation": session.generation,
                "logged_out": session.logged_out,
                "tabs": len(session.tabs),
                "claim": copy.deepcopy(session.claim),
                "last_result": copy.deepcopy(session.last_result),
            }

    def snapshot(self) -> dict:
        with self._lock:
            sessions = {}
            for session_id, session in self._sessions.items():
                tabs = []
                for tab in session.tabs.values():
                    tabs.append(
                        {
                            "tab_id": tab.tab_id,
                            "session_id": tab.session_id,
                            "generation": tab.generation,
                            "capabilities": list(tab.capabilities),
                            "last_seen": tab.last_seen,
                            "valid": not session.logged_out
                            and tab.generation == session.generation,
                        }
                    )
                sessions[session_id] = {
                    "generation": session.generation,
                    "logged_out": session.logged_out,
                    "tabs": tabs,
                    "next_claim": session.next_claim,
                    "claim": copy.deepcopy(session.claim),
                    "last_result": copy.deepcopy(session.last_result),
                }
            return {
                "version": 1,
                "sessions": sessions,
                "stats": dict(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            sessions, tabs, stats = self._parse_snapshot(
                state, self.max_tabs_per_session
            )
            self._sessions = sessions
            self._tabs = tabs
            self._stats = stats

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    @staticmethod
    def _snapshot_invalid(detail: object) -> SessionTabError:
        return SessionTabError("SESSION_SNAPSHOT_INVALID", detail)

    @classmethod
    def _parse_snapshot(cls, state, max_tabs):
        invalid = cls._snapshot_invalid
        if not isinstance(state, dict):
            raise invalid({"state": type(state).__name__})
        if (
            not isinstance(state.get("version"), int)
            or isinstance(state.get("version"), bool)
            or state["version"] != 1
        ):
            raise invalid({"version": state.get("version")})

        raw_sessions = state.get("sessions")
        raw_stats = state.get("stats")
        if not isinstance(raw_sessions, dict):
            raise invalid({"sessions": type(raw_sessions).__name__})
        if not isinstance(raw_stats, dict):
            raise invalid({"stats": type(raw_stats).__name__})
        if set(raw_stats) != set(_STAT_KEYS):
            raise invalid({"stats_keys": sorted(map(str, raw_stats))})

        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise invalid({"stats": key, "value": value})
            stats[key] = value

        sessions = {}
        tabs = {}
        for session_id, raw_session in raw_sessions.items():
            if not isinstance(session_id, str) or not session_id.strip():
                raise invalid({"session_id": session_id})
            if not isinstance(raw_session, dict):
                raise invalid({"session": session_id})
            required = {
                "generation",
                "logged_out",
                "tabs",
                "next_claim",
                "claim",
                "last_result",
            }
            if not required.issubset(raw_session):
                raise invalid(
                    {
                        "session": session_id,
                        "missing": sorted(required - set(raw_session)),
                    }
                )

            generation = raw_session["generation"]
            logged_out = raw_session["logged_out"]
            next_claim = raw_session["next_claim"]
            raw_tabs = raw_session["tabs"]
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 1
            ):
                raise invalid({"session": session_id, "generation": generation})
            if not isinstance(logged_out, bool):
                raise invalid({"session": session_id, "logged_out": logged_out})
            if (
                not isinstance(next_claim, int)
                or isinstance(next_claim, bool)
                or next_claim < 1
            ):
                raise invalid({"session": session_id, "next_claim": next_claim})
            if not isinstance(raw_tabs, (list, tuple)):
                raise invalid({"session": session_id, "tabs": raw_tabs})
            if len(raw_tabs) > max_tabs:
                raise invalid(
                    {
                        "session": session_id,
                        "tabs": len(raw_tabs),
                        "limit": max_tabs,
                    }
                )

            session = _Session(
                session_id=session_id,
                generation=generation,
                logged_out=logged_out,
                next_claim=next_claim,
            )
            for raw_tab in raw_tabs:
                if not isinstance(raw_tab, dict):
                    raise invalid(
                        {"session": session_id, "tab": type(raw_tab).__name__}
                    )
                needed = {
                    "tab_id",
                    "session_id",
                    "generation",
                    "capabilities",
                    "last_seen",
                }
                if not needed.issubset(raw_tab):
                    raise invalid(
                        {
                            "session": session_id,
                            "missing_tab_fields": sorted(
                                needed - set(raw_tab)
                            ),
                        }
                    )
                tab_id = raw_tab["tab_id"]
                if not isinstance(tab_id, str) or not tab_id.strip():
                    raise invalid({"session": session_id, "tab_id": tab_id})
                if tab_id in tabs:
                    raise invalid({"duplicate_tab_id": tab_id})
                if raw_tab["session_id"] != session_id:
                    raise invalid(
                        {
                            "session": session_id,
                            "tab_id": tab_id,
                            "tab_session": raw_tab["session_id"],
                        }
                    )
                tab_generation = raw_tab["generation"]
                if (
                    not isinstance(tab_generation, int)
                    or isinstance(tab_generation, bool)
                    or tab_generation < 1
                    or tab_generation > generation
                ):
                    raise invalid(
                        {
                            "session": session_id,
                            "tab_id": tab_id,
                            "generation": tab_generation,
                        }
                    )
                try:
                    capabilities = cls._normalize_capabilities(
                        raw_tab["capabilities"]
                    )
                except SessionTabError:
                    raise invalid(
                        {
                            "session": session_id,
                            "tab_id": tab_id,
                            "capabilities": raw_tab["capabilities"],
                        }
                    ) from None
                last_seen = raw_tab["last_seen"]
                if (
                    not isinstance(last_seen, (int, float))
                    or isinstance(last_seen, bool)
                ):
                    raise invalid(
                        {
                            "session": session_id,
                            "tab_id": tab_id,
                            "last_seen": last_seen,
                        }
                    )
                if "valid" in raw_tab and not isinstance(
                    raw_tab["valid"], bool
                ):
                    raise invalid(
                        {
                            "session": session_id,
                            "tab_id": tab_id,
                            "valid": raw_tab["valid"],
                        }
                    )

                tab = _Tab(
                    tab_id=tab_id,
                    session_id=session_id,
                    generation=tab_generation,
                    capabilities=capabilities,
                    last_seen=last_seen,
                )
                session.tabs[tab_id] = tab
                tabs[tab_id] = tab

            session.claim = cls._parse_claim(
                session_id, raw_session["claim"], session
            )
            session.last_result = cls._parse_last_result(
                session_id, raw_session["last_result"], session
            )
            sessions[session_id] = session

        return sessions, tabs, stats

    @classmethod
    def _parse_claim(cls, session_id, raw_claim, session):
        if raw_claim is None:
            return None
        if not isinstance(raw_claim, dict):
            raise cls._snapshot_invalid(
                {"session": session_id, "claim": raw_claim}
            )
        needed = {"claim_id", "tab_id", "generation"}
        if not needed.issubset(raw_claim):
            raise cls._snapshot_invalid(
                {
                    "session": session_id,
                    "missing_claim_fields": sorted(
                        needed - set(raw_claim)
                    ),
                }
            )
        claim_id = raw_claim["claim_id"]
        tab_id = raw_claim["tab_id"]
        generation = raw_claim["generation"]
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise cls._snapshot_invalid(
                {"session": session_id, "claim_id": claim_id}
            )
        if not isinstance(tab_id, str) or tab_id not in session.tabs:
            raise cls._snapshot_invalid(
                {"session": session_id, "claim_tab": tab_id}
            )
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation != session.generation
        ):
            raise cls._snapshot_invalid(
                {"session": session_id, "claim_generation": generation}
            )
        return {
            "claim_id": claim_id,
            "tab_id": tab_id,
            "generation": generation,
        }

    @classmethod
    def _parse_last_result(cls, session_id, raw_result, session):
        if raw_result is None:
            return None
        if not isinstance(raw_result, dict):
            raise cls._snapshot_invalid(
                {"session": session_id, "last_result": raw_result}
            )
        needed = {"status", "claim_id", "generation", "reason"}
        if not needed.issubset(raw_result):
            raise cls._snapshot_invalid(
                {
                    "session": session_id,
                    "missing_result_fields": sorted(
                        needed - set(raw_result)
                    ),
                }
            )
        status = raw_result["status"]
        claim_id = raw_result["claim_id"]
        generation = raw_result["generation"]
        reason = raw_result["reason"]
        if status not in ("ok", "failed"):
            raise cls._snapshot_invalid(
                {"session": session_id, "status": status}
            )
        if not isinstance(claim_id, str) or not claim_id.strip():
            raise cls._snapshot_invalid(
                {"session": session_id, "claim_id": claim_id}
            )
        if (
            not isinstance(generation, int)
            or isinstance(generation, bool)
            or generation < 1
            or generation > session.generation
        ):
            raise cls._snapshot_invalid(
                {"session": session_id, "result_generation": generation}
            )
        if not isinstance(reason, str):
            raise cls._snapshot_invalid(
                {"session": session_id, "reason": reason}
            )

        result = {
            "status": status,
            "claim_id": claim_id,
            "generation": generation,
            "reason": reason,
        }
        if "tab_id" in raw_result:
            tab_id = raw_result["tab_id"]
            if not isinstance(tab_id, str):
                raise cls._snapshot_invalid(
                    {"session": session_id, "result_tab": tab_id}
                )
            result["tab_id"] = tab_id
        return result
