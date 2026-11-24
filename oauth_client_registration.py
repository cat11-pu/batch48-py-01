"""OAuth dynamic client registration with metadata and secret rotation."""

from __future__ import annotations

import threading


class OAuthRegistrationError(ValueError):
    """Raised when a client registration operation is refused."""

    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClientRegistrationAuthority:
    """Register OAuth clients, normalize redirect URIs and rotate secrets."""

    def __init__(self, *, secret_overlap: int = 300,
                 max_clients_per_tenant: int = 100, clock=None) -> None:
        self.secret_overlap = secret_overlap
        self.max_clients_per_tenant = max_clients_per_tenant
        self._clock = clock
        self._lock = threading.RLock()

    def register_client(self, tenant: str, metadata: dict, *, now: int) -> dict:
        raise NotImplementedError

    def update_client(self, tenant: str, client_id: str, metadata: dict,
                      *, now: int) -> dict:
        raise NotImplementedError

    def rotate_secret(self, tenant: str, client_id: str, *, now: int,
                      overlap: int | None = None) -> dict:
        raise NotImplementedError

    def resolve_client(self, tenant: str, client_id: str) -> dict:
        raise NotImplementedError

    def authenticate_secret(self, tenant: str, client_id: str, secret: str,
                            *, now: int) -> dict:
        raise NotImplementedError

    def normalize_redirect_uri(self, uri: str) -> str:
        raise NotImplementedError

    def match_redirect_uri(self, tenant: str, client_id: str, uri: str) -> str:
        raise NotImplementedError

    def list_clients(self, tenant: str, *, limit: int = 2,
                     cursor: str | None = None) -> dict:
        raise NotImplementedError

    def revoke_client(self, tenant: str, client_id: str) -> dict:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError
