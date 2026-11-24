"""CSRF token guard with session, action and origin binding."""

from __future__ import annotations

import threading


class CsrfError(ValueError):
    """Raised when a CSRF token operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class CsrfTokenGuard:
    """Issue, verify, rotate and revoke session-bound CSRF tokens."""

    def __init__(self, *, token_ttl: int = 600, max_tokens_per_session: int = 8,
                 clock=None) -> None:
        self.token_ttl = token_ttl
        self.max_tokens_per_session = max_tokens_per_session
        self._clock = clock
        self._lock = threading.RLock()

    def create_session(self, session_id: str, *, now: int) -> dict:
        raise NotImplementedError

    def issue_token(self, session_id: str, action: str, *, origin: str,
                    one_time: bool = True, now: int) -> dict:
        raise NotImplementedError

    def verify_token(self, session_id: str, token: str, *, action: str,
                     origin: str, cookie_token: str, method: str = "POST",
                     now: int) -> dict:
        raise NotImplementedError

    def rotate_session_token(self, session_id: str, *, now: int) -> dict:
        raise NotImplementedError

    def revoke_for_session(self, session_id: str) -> int:
        raise NotImplementedError

    def purge_expired(self, now: int) -> int:
        raise NotImplementedError

    def token_count(self, session_id: str) -> int:
        raise NotImplementedError

    def session_state(self, session_id: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
