"""OAuth dynamic client registration with metadata and secret rotation."""

from __future__ import annotations

import bisect
import copy
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit


_ALLOWED_GRANT_TYPES = frozenset(
    {"authorization_code", "client_credentials", "refresh_token"}
)
_ALLOWED_AUTH_METHODS = frozenset(
    {"client_secret_basic", "client_secret_post", "none"}
)
_STAT_KEYS = (
    "registered",
    "reused",
    "updated",
    "rotated",
    "revoked",
    "authentication_failures",
)
_CLIENT_ID_PATTERN = re.compile(r"^c_([0-9a-f]+)_")
_SNAPSHOT_VERSION = 1


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
        self._clients: dict[str, dict[str, dict]] = {}
        self._stats = self._fresh_stats()
        self._next_sequence = 1

    def register_client(self, tenant: str, metadata: dict,
                        now: int | None = None) -> dict:
        now = self._resolve_now(now)
        with self._lock:
            self._require_tenant(tenant)
            normalized = self._normalize_metadata(metadata)
            key = self._metadata_key(normalized)
            bucket = self._clients.get(tenant, {})

            for record in bucket.values():
                if self._metadata_key(record["metadata"]) == key:
                    self._stats["reused"] += 1
                    return self._external(record, secret=None, reused=True)

            if len(bucket) >= self.max_clients_per_tenant:
                raise OAuthRegistrationError(
                    "OAUTH_CLIENT_LIMIT_EXCEEDED",
                    {"tenant": tenant, "limit": self.max_clients_per_tenant},
                )

            client_id = self._new_client_id()
            auth_method = normalized["token_endpoint_auth_method"]
            if auth_method == "none":
                secret = None
                secret_salt = None
                secret_hash = None
            else:
                secret = self._new_secret()
                secret_salt = secrets.token_hex(16)
                secret_hash = self._hash_secret(secret_salt, secret)

            record = {
                "client_id": client_id,
                "tenant": tenant,
                "metadata": normalized,
                "version": 1,
                "created_at": now,
                "updated_at": now,
                "secret_salt": secret_salt,
                "secret_hash": secret_hash,
                "previous_secret_salt": None,
                "previous_secret_hash": None,
                "previous_valid_until": None,
            }
            self._clients.setdefault(tenant, {})[client_id] = record
            self._stats["registered"] += 1
            return self._external(record, secret=secret, reused=False)

    def update_client(self, tenant: str, client_id: str, metadata: dict,
                      now: int | None = None) -> dict:
        now = self._resolve_now(now)
        with self._lock:
            self._require_tenant(tenant)
            record = self._find_client(tenant, client_id)
            normalized = self._normalize_metadata(metadata)

            auth_method = normalized["token_endpoint_auth_method"]
            issued_secret = None
            if auth_method == "none":
                record["secret_salt"] = None
                record["secret_hash"] = None
                record["previous_secret_salt"] = None
                record["previous_secret_hash"] = None
                record["previous_valid_until"] = None
            elif record["secret_hash"] is None:
                issued_secret = self._new_secret()
                record["secret_salt"] = secrets.token_hex(16)
                record["secret_hash"] = self._hash_secret(
                    record["secret_salt"], issued_secret
                )
                record["previous_secret_salt"] = None
                record["previous_secret_hash"] = None
                record["previous_valid_until"] = None

            record["metadata"] = normalized
            record["version"] += 1
            record["updated_at"] = now
            self._stats["updated"] += 1
            return self._external(record, secret=issued_secret)

    def rotate_secret(self, tenant: str, client_id: str,
                      now: int | None = None,
                      overlap: int | None = None) -> dict:
        now = self._resolve_now(now)
        if overlap is None:
            overlap = self.secret_overlap
        if (not isinstance(overlap, int) or isinstance(overlap, bool)
                or overlap < 0):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "invalid secret overlap"
            )

        with self._lock:
            self._require_tenant(tenant)
            record = self._find_client(tenant, client_id)
            if record["secret_hash"] is None:
                raise OAuthRegistrationError(
                    "OAUTH_SECRET_INVALID", "client does not use a secret"
                )

            new_secret = self._new_secret()
            new_salt = secrets.token_hex(16)
            record["previous_secret_salt"] = record["secret_salt"]
            record["previous_secret_hash"] = record["secret_hash"]
            record["previous_valid_until"] = now + overlap
            record["secret_salt"] = new_salt
            record["secret_hash"] = self._hash_secret(new_salt, new_secret)
            record["updated_at"] = now
            self._stats["rotated"] += 1
            return self._external(record, secret=new_secret)

    def resolve_client(self, tenant: str, client_id: str) -> dict:
        with self._lock:
            self._require_tenant(tenant)
            return self._external(self._find_client(tenant, client_id))

    def authenticate_secret(self, tenant: str, client_id: str, secret: str,
                            now: int | None = None) -> dict:
        now = self._resolve_now(now)
        with self._lock:
            self._require_tenant(tenant)
            record = self._find_client(tenant, client_id)
            if not isinstance(secret, str) or record["secret_hash"] is None:
                self._stats["authentication_failures"] += 1
                raise OAuthRegistrationError(
                    "OAUTH_SECRET_INVALID", {"client_id": client_id}
                )

            if hmac.compare_digest(
                self._hash_secret(record["secret_salt"], secret),
                record["secret_hash"],
            ):
                return {
                    "status": "ok",
                    "secret_slot": "current",
                    "client_id": record["client_id"],
                    "tenant": tenant,
                }

            previous_until = record["previous_valid_until"]
            if (
                record["previous_secret_hash"] is not None
                and previous_until is not None
                and now < previous_until
                and hmac.compare_digest(
                    self._hash_secret(record["previous_secret_salt"], secret),
                    record["previous_secret_hash"],
                )
            ):
                return {
                    "status": "ok",
                    "secret_slot": "previous",
                    "client_id": record["client_id"],
                    "tenant": tenant,
                }

            self._stats["authentication_failures"] += 1
            raise OAuthRegistrationError(
                "OAUTH_SECRET_INVALID", {"client_id": client_id}
            )

    def normalize_redirect_uri(self, uri: str) -> str:
        if not isinstance(uri, str) or not uri:
            raise OAuthRegistrationError(
                "OAUTH_REDIRECT_INVALID", "redirect URI must be a string"
            )
        if uri != uri.strip() or any(ch.isspace() for ch in uri):
            raise OAuthRegistrationError(
                "OAUTH_REDIRECT_INVALID", "redirect URI contains whitespace"
            )
        if "#" in uri:
            raise OAuthRegistrationError(
                "OAUTH_REDIRECT_INVALID", "redirect URI must not contain a fragment"
            )

        try:
            parts = urlsplit(uri)
            scheme = parts.scheme.lower()
            if scheme not in {"http", "https"}:
                raise ValueError("unsupported redirect URI scheme")
            if not parts.netloc or parts.hostname is None:
                raise ValueError("redirect URI requires a host")
            if parts.username is not None or parts.password is not None:
                raise ValueError("redirect URI must not contain userinfo")
            host = parts.hostname.lower()
            if not host:
                raise ValueError("redirect URI requires a host")
            port = parts.port
        except (TypeError, ValueError):
            raise OAuthRegistrationError(
                "OAUTH_REDIRECT_INVALID", "malformed redirect URI"
            ) from None

        default_port = 443 if scheme == "https" else 80
        shown_host = f"[{host}]" if ":" in host else host
        if port is None or port == default_port:
            netloc = shown_host
        else:
            netloc = f"{shown_host}:{port}"

        path = parts.path or "/"
        return urlunsplit((scheme, netloc, path, parts.query, ""))

    def match_redirect_uri(self, tenant: str, client_id: str, uri: str) -> str:
        with self._lock:
            self._require_tenant(tenant)
            record = self._find_client(tenant, client_id)
            redirect_uris = list(record["metadata"].get("redirect_uris", []))

        normalized = self.normalize_redirect_uri(uri)
        candidate = urlsplit(normalized)
        candidate_loopback = self._is_loopback(candidate.hostname)

        for expected_uri in redirect_uris:
            if normalized == expected_uri:
                return normalized
            expected = urlsplit(expected_uri)
            if (
                candidate_loopback
                and self._is_loopback(expected.hostname)
                and expected.scheme == candidate.scheme
                and expected.hostname == candidate.hostname
                and expected.path == candidate.path
                and expected.query == candidate.query
            ):
                return normalized

        raise OAuthRegistrationError(
            "OAUTH_REDIRECT_INVALID", {"client_id": client_id}
        )

    def list_clients(self, tenant: str, limit: int = 2,
                     cursor: str | None = None) -> dict:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise OAuthRegistrationError(
                "OAUTH_PAGINATION_INVALID", "limit must be a positive integer"
            )
        if cursor is not None and not isinstance(cursor, str):
            raise OAuthRegistrationError(
                "OAUTH_PAGINATION_INVALID", "cursor must be a string"
            )

        with self._lock:
            self._require_tenant(tenant)
            records = sorted(
                self._clients.get(tenant, {}).values(),
                key=lambda record: record["client_id"],
            )
            client_ids = [record["client_id"] for record in records]
            start = 0 if cursor is None else bisect.bisect_right(client_ids, cursor)
            page = records[start:start + limit]
            next_cursor = None
            if page and start + len(page) < len(records):
                next_cursor = page[-1]["client_id"]
            return {
                "clients": [
                    self._external(record, secret=None) for record in page
                ],
                "next_cursor": next_cursor,
            }

    def revoke_client(self, tenant: str, client_id: str) -> dict:
        with self._lock:
            self._require_tenant(tenant)
            record = self._find_client(tenant, client_id)
            del self._clients[tenant][client_id]
            if not self._clients[tenant]:
                del self._clients[tenant]
            self._stats["revoked"] += 1
            return {
                "client_id": record["client_id"],
                "tenant": tenant,
                "revoked": True,
            }

    def stats(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = self._fresh_stats()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": _SNAPSHOT_VERSION,
                "clients": copy.deepcopy(self._clients),
                "stats": copy.deepcopy(self._stats),
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                clients, stats, next_sequence = self._validate_snapshot(state)
            except OAuthRegistrationError:
                raise
            except Exception as exc:
                raise OAuthRegistrationError(
                    "OAUTH_SNAPSHOT_INVALID", repr(exc)
                ) from None

            self._clients = clients
            self._stats = stats
            self._next_sequence = max(
                next_sequence, self._max_sequence(clients) + 1
            )

    @staticmethod
    def _fresh_stats() -> dict:
        return {key: 0 for key in _STAT_KEYS}

    def _resolve_now(self, now: int | None) -> int:
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return int(time.time())

    @staticmethod
    def _require_tenant(tenant: str) -> None:
        if not isinstance(tenant, str) or not tenant:
            raise OAuthRegistrationError(
                "OAUTH_TENANT_INVALID", "tenant must be a non-empty string"
            )

    def _find_client(self, tenant: str, client_id: str) -> dict:
        bucket = self._clients.get(tenant)
        if not bucket or not isinstance(client_id, str) or client_id not in bucket:
            raise OAuthRegistrationError(
                "OAUTH_UNKNOWN_CLIENT",
                {"tenant": tenant, "client_id": client_id},
            )
        return bucket[client_id]

    def _normalize_metadata(self, metadata: dict) -> dict:
        try:
            normalized = copy.deepcopy(metadata)
        except Exception:
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "metadata must be copyable"
            ) from None
        if not isinstance(normalized, dict):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "metadata must be an object"
            )
        if any(not isinstance(key, str) for key in normalized):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "metadata keys must be strings"
            )

        auth_method = normalized.get(
            "token_endpoint_auth_method", "client_secret_basic"
        )
        if (
            not isinstance(auth_method, str)
            or auth_method not in _ALLOWED_AUTH_METHODS
        ):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "unsupported token endpoint auth method"
            )
        if auth_method == "none" and "client_secret" in normalized:
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID",
                "public clients must not provide a client_secret",
            )
        normalized.pop("client_secret", None)

        grant_types = normalized.get("grant_types")
        if (
            not isinstance(grant_types, list)
            or not grant_types
            or any(not isinstance(item, str) for item in grant_types)
        ):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "grant_types must be a non-empty list"
            )
        grant_types = self._dedupe(grant_types)
        if any(item not in _ALLOWED_GRANT_TYPES for item in grant_types):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "unsupported grant type"
            )
        if not ({"authorization_code", "client_credentials"} & set(grant_types)):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID",
                "refresh_token requires another grant type",
            )

        response_types = normalized.get("response_types", [])
        if (
            not isinstance(response_types, list)
            or any(not isinstance(item, str) for item in response_types)
        ):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "response_types must be a list"
            )
        response_types = self._dedupe(response_types)

        redirect_uris = normalized.get("redirect_uris", [])
        if not isinstance(redirect_uris, list):
            raise OAuthRegistrationError(
                "OAUTH_METADATA_INVALID", "redirect_uris must be a list"
            )
        normalized_redirects = self._dedupe(
            [self.normalize_redirect_uri(uri) for uri in redirect_uris]
        )

        if "authorization_code" in grant_types:
            if "code" not in response_types:
                raise OAuthRegistrationError(
                    "OAUTH_METADATA_INVALID",
                    "authorization_code requires the code response type",
                )
            if not normalized_redirects:
                raise OAuthRegistrationError(
                    "OAUTH_METADATA_INVALID",
                    "authorization_code requires a redirect URI",
                )
        else:
            if response_types:
                raise OAuthRegistrationError(
                    "OAUTH_METADATA_INVALID",
                    "response types require authorization_code",
                )
            if normalized_redirects:
                raise OAuthRegistrationError(
                    "OAUTH_METADATA_INVALID",
                    "redirect URIs require authorization_code",
                )

        if "client_credentials" in grant_types:
            if response_types or normalized_redirects:
                raise OAuthRegistrationError(
                    "OAUTH_METADATA_INVALID",
                    "client_credentials cannot use response types or redirect URIs",
                )

        normalized["grant_types"] = grant_types
        normalized["response_types"] = response_types
        normalized["redirect_uris"] = normalized_redirects
        normalized["token_endpoint_auth_method"] = auth_method
        return normalized

    @staticmethod
    def _dedupe(items: list) -> list:
        seen = set()
        result = []
        for item in items:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result

    def _metadata_key(self, metadata: dict):
        key_metadata = dict(metadata)
        for field in ("grant_types", "response_types", "redirect_uris"):
            key_metadata[field] = sorted(key_metadata.get(field, []))
        return self._freeze(key_metadata)

    @classmethod
    def _freeze(cls, value):
        if isinstance(value, dict):
            return tuple(
                sorted(
                    (repr(key), cls._freeze(item))
                    for key, item in value.items()
                )
            )
        if isinstance(value, (list, tuple)):
            return tuple(cls._freeze(item) for item in value)
        if isinstance(value, (set, frozenset)):
            return tuple(sorted(repr(cls._freeze(item)) for item in value))
        try:
            hash(value)
        except TypeError:
            return repr(value)
        return value

    def _new_client_id(self) -> str:
        while True:
            sequence = self._next_sequence
            self._next_sequence += 1
            client_id = f"c_{sequence:016x}_{secrets.token_hex(6)}"
            if all(client_id not in bucket for bucket in self._clients.values()):
                return client_id

    @staticmethod
    def _new_secret() -> str:
        return secrets.token_urlsafe(32)

    @staticmethod
    def _hash_secret(salt: str, secret: str) -> str:
        payload = f"{salt}:{secret}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _is_loopback(host: str | None) -> bool:
        if not host:
            return False
        host = host.lower()
        if host == "localhost" or host.endswith(".localhost"):
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    @staticmethod
    def _external(record: dict, *, secret=None, reused=None) -> dict:
        result = {
            "client_id": record["client_id"],
            "tenant": record["tenant"],
            "metadata": copy.deepcopy(record["metadata"]),
            "version": record["version"],
            "created_at": record["created_at"],
            "updated_at": record["updated_at"],
            "client_secret": secret,
            "previous_valid_until": record["previous_valid_until"],
        }
        if reused is not None:
            result["reused"] = reused
        return result

    def _validate_snapshot(self, state: dict):
        if not isinstance(state, dict):
            self._snapshot_invalid("snapshot must be an object")
        version = state.get("version")
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != _SNAPSHOT_VERSION
        ):
            self._snapshot_invalid("unsupported snapshot version")
        if "clients" not in state or not isinstance(state["clients"], dict):
            self._snapshot_invalid("clients must be an object")
        if "stats" not in state or not isinstance(state["stats"], dict):
            self._snapshot_invalid("stats must be an object")

        try:
            raw_clients = copy.deepcopy(state["clients"])
            raw_stats = copy.deepcopy(state["stats"])
        except Exception as exc:
            self._snapshot_invalid(repr(exc))

        stats = self._fresh_stats()
        for key, value in raw_stats.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                self._snapshot_invalid("invalid stats entry")
            stats[key] = value

        clients: dict[str, dict[str, dict]] = {}
        for tenant, bucket in raw_clients.items():
            if not isinstance(tenant, str) or not tenant:
                self._snapshot_invalid("invalid tenant in snapshot")
            if not isinstance(bucket, dict):
                self._snapshot_invalid("tenant clients must be an object")
            tenant_clients: dict[str, dict] = {}
            for client_id, record in bucket.items():
                validated = self._validate_snapshot_record(
                    tenant, client_id, record
                )
                tenant_clients[client_id] = validated
            clients[tenant] = tenant_clients

        next_sequence = state.get("next_sequence", 1)
        if (
            not isinstance(next_sequence, int)
            or isinstance(next_sequence, bool)
            or next_sequence < 1
        ):
            self._snapshot_invalid("next_sequence must be a positive integer")
        return clients, stats, next_sequence

    def _validate_snapshot_record(self, tenant: str, client_id: str,
                                  record: dict) -> dict:
        if not isinstance(client_id, str) or not client_id:
            self._snapshot_invalid("invalid client id")
        if not isinstance(record, dict):
            self._snapshot_invalid("client record must be an object")
        if "client_secret" in record:
            self._snapshot_invalid("snapshot must not contain plaintext secrets")

        required = {
            "client_id",
            "tenant",
            "metadata",
            "version",
            "created_at",
            "updated_at",
            "secret_salt",
            "secret_hash",
            "previous_secret_salt",
            "previous_secret_hash",
            "previous_valid_until",
        }
        if not required.issubset(record):
            self._snapshot_invalid("client record is incomplete")
        if record["client_id"] != client_id or record["tenant"] != tenant:
            self._snapshot_invalid("client record identity mismatch")
        if not isinstance(record["metadata"], dict):
            self._snapshot_invalid("client metadata must be an object")
        if (
            not isinstance(record["version"], int)
            or isinstance(record["version"], bool)
            or record["version"] < 1
        ):
            self._snapshot_invalid("client version must be a positive integer")
        if not self._is_number(record["created_at"]):
            self._snapshot_invalid("created_at must be numeric")
        if not self._is_number(record["updated_at"]):
            self._snapshot_invalid("updated_at must be numeric")

        for salt_key, hash_key in (
            ("secret_salt", "secret_hash"),
            ("previous_secret_salt", "previous_secret_hash"),
        ):
            salt = record[salt_key]
            secret_hash = record[hash_key]
            if secret_hash is None:
                if salt is not None:
                    self._snapshot_invalid("orphaned secret salt")
            elif (
                not isinstance(secret_hash, str)
                or not secret_hash
                or not isinstance(salt, str)
                or not salt
            ):
                self._snapshot_invalid("invalid secret hash")

        previous_until = record["previous_valid_until"]
        if previous_until is not None and not self._is_number(previous_until):
            self._snapshot_invalid("previous_valid_until must be numeric")
        if (
            record["previous_secret_hash"] is not None
            and previous_until is None
        ):
            self._snapshot_invalid("previous secret requires a validity deadline")

        if self._contains_key(record["metadata"], "client_secret"):
            self._snapshot_invalid("snapshot must not contain plaintext secrets")
        try:
            normalized = self._normalize_metadata(record["metadata"])
        except OAuthRegistrationError:
            self._snapshot_invalid("client metadata is invalid")

        validated = copy.deepcopy(record)
        validated["metadata"] = normalized
        return validated

    @staticmethod
    def _is_number(value) -> bool:
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
        )

    @classmethod
    def _contains_key(cls, value, key: str) -> bool:
        if isinstance(value, dict):
            return key in value or any(
                cls._contains_key(item, key) for item in value.values()
            )
        if isinstance(value, (list, tuple)):
            return any(cls._contains_key(item, key) for item in value)
        return False

    @classmethod
    def _max_sequence(cls, clients: dict[str, dict[str, dict]]) -> int:
        maximum = 0
        for bucket in clients.values():
            for client_id in bucket:
                match = _CLIENT_ID_PATTERN.match(client_id)
                if match:
                    maximum = max(maximum, int(match.group(1), 16))
        return maximum

    @staticmethod
    def _snapshot_invalid(detail: object) -> None:
        raise OAuthRegistrationError("OAUTH_SNAPSHOT_INVALID", detail)
