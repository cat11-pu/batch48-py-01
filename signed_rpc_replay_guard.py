"""Replay guard for HMAC signed RPC requests."""

from __future__ import annotations

import copy
import hashlib
import hmac
import threading
import time


class RpcGuardError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STATS_KEYS = ("verified", "rejected", "replayed", "conflicts", "executed")


def _canonical(method: str, path: str, body_sha256: str,
               timestamp: int, nonce: str) -> bytes:
    return "\n".join([method, path, body_sha256, str(timestamp), nonce]).encode("utf-8")


class SignedRpcReplayGuard:
    """Verify signed RPC envelopes and keep nonce windows per client."""

    def __init__(self, *, clock_skew: int = 30, window: int = 300,
                 per_client_capacity: int = 256, max_clients: int = 1000,
                 clock=None) -> None:
        self.clock_skew = clock_skew
        self.window = window
        self.per_client_capacity = per_client_capacity
        self.max_clients = max_clients
        self._clock = clock
        self._lock = threading.RLock()
        self._clients = {}
        self._nonces = {}
        self._responses = {}
        self._stats = {key: 0 for key in _STATS_KEYS}

    def _now(self, now):
        if now is not None:
            return now
        if self._clock is not None:
            return self._clock()
        return int(time.time())

    def _client(self, client_id):
        client = self._clients.get(client_id)
        if client is None:
            raise RpcGuardError("UNKNOWN_CLIENT", client_id)
        return client

    def _reclaim_client(self, client_id: str, now) -> int:
        removed = 0
        entries = self._nonces.get(client_id)
        if entries:
            for nonce in [n for n, e in entries.items()
                          if now - e["timestamp"] > self.window]:
                del entries[nonce]
                removed += 1
        cached = self._responses.get(client_id)
        if cached:
            for nonce in [n for n, e in cached.items()
                          if now - e["timestamp"] > self.window]:
                del cached[nonce]
                removed += 1
        return removed

    def add_client(self, client_id: str, secret: bytes) -> dict:
        with self._lock:
            existing = self._clients.get(client_id)
            if existing is not None:
                if existing["secret"] == secret:
                    return {"client_id": client_id, "status": "registered"}
                raise RpcGuardError("CLIENT_CONFLICT", client_id)
            if len(self._clients) >= self.max_clients:
                raise RpcGuardError("CLIENT_FULL", client_id)
            self._clients[client_id] = {"secret": bytes(secret), "revoked": False}
            return {"client_id": client_id, "status": "registered"}

    def revoke_client(self, client_id: str) -> None:
        with self._lock:
            self._client(client_id)["revoked"] = True

    def sign_request(self, *, client_id: str, method: str, path: str,
                     body: bytes, timestamp: int, nonce: str) -> dict:
        with self._lock:
            client = self._client(client_id)
            if client["revoked"]:
                raise RpcGuardError("CLIENT_REVOKED", client_id)
            body_sha256 = hashlib.sha256(body).hexdigest()
            signature = hmac.new(
                client["secret"],
                _canonical(method, path, body_sha256, timestamp, nonce),
                hashlib.sha256,
            ).hexdigest()
            return {
                "client_id": client_id,
                "method": method,
                "path": path,
                "body_sha256": body_sha256,
                "timestamp": timestamp,
                "nonce": nonce,
                "signature": signature,
            }

    def verify_request(self, request: dict, *, now=None) -> dict:
        with self._lock:
            now = self._now(now)
            client_id = request.get("client_id") if isinstance(request, dict) else None
            client = self._clients.get(client_id)
            if client is None:
                self._stats["rejected"] += 1
                raise RpcGuardError("UNKNOWN_CLIENT", client_id)
            if client["revoked"]:
                self._stats["rejected"] += 1
                raise RpcGuardError("CLIENT_REVOKED", client_id)
            try:
                method = request["method"]
                path = request["path"]
                body_sha256 = request["body_sha256"]
                timestamp = request["timestamp"]
                nonce = request["nonce"]
                signature = request["signature"]
            except (KeyError, TypeError):
                self._stats["rejected"] += 1
                raise RpcGuardError("BAD_SIGNATURE", "missing envelope field")
            expected = hmac.new(
                client["secret"],
                _canonical(method, path, body_sha256, timestamp, nonce),
                hashlib.sha256,
            ).hexdigest()
            if not isinstance(signature, str) or not hmac.compare_digest(expected, signature):
                self._stats["rejected"] += 1
                raise RpcGuardError("BAD_SIGNATURE", nonce)
            if abs(now - timestamp) > self.clock_skew:
                self._stats["rejected"] += 1
                raise RpcGuardError("STALE_TIMESTAMP", timestamp)
            self._reclaim_client(client_id, now)
            entries = self._nonces.setdefault(client_id, {})
            existing = entries.get(nonce)
            if existing is not None:
                if existing["body_sha256"] != body_sha256:
                    self._stats["conflicts"] += 1
                    raise RpcGuardError("NONCE_CONFLICT", nonce)
                self._stats["replayed"] += 1
                return {
                    "status": "ok",
                    "client_id": client_id,
                    "nonce": nonce,
                    "replayed": True,
                }
            if len(entries) >= self.per_client_capacity:
                self._stats["rejected"] += 1
                raise RpcGuardError("NONCE_FULL", client_id)
            entries[nonce] = {"timestamp": timestamp, "body_sha256": body_sha256}
            self._stats["verified"] += 1
            return {
                "status": "ok",
                "client_id": client_id,
                "nonce": nonce,
                "replayed": False,
            }

    def _record_response(self, client_id: str, nonce: str,
                         response: dict, now) -> None:
        cached = self._responses.setdefault(client_id, {})
        if nonce not in cached and len(cached) >= self.per_client_capacity:
            self._reclaim_client(client_id, now)
            if len(cached) >= self.per_client_capacity:
                oldest = min(cached, key=lambda n: cached[n]["timestamp"])
                del cached[oldest]
        timestamp = now
        entry = self._nonces.get(client_id, {}).get(nonce)
        if entry is not None:
            timestamp = entry["timestamp"]
        cached[nonce] = {"timestamp": timestamp, "response": response}

    def record_response(self, client_id: str, nonce: str,
                        response: dict) -> dict:
        with self._lock:
            self._client(client_id)
            self._record_response(client_id, nonce, response, self._now(None))
            return {"status": "ok", "client_id": client_id, "nonce": nonce}

    def execute_once(self, request: dict, handler, *, now=None) -> dict:
        with self._lock:
            now = self._now(now)
            outcome = self.verify_request(request, now=now)
            client_id = outcome["client_id"]
            nonce = outcome["nonce"]
            cached = self._responses.get(client_id, {}).get(nonce)
            if cached is not None:
                return {"status": "ok", "nonce": nonce,
                        "response": cached["response"]}
            response = handler(request)
            self._record_response(client_id, nonce, response, now)
            self._stats["executed"] += 1
            return {"status": "ok", "nonce": nonce, "response": response}

    def nonce_window(self, client_id: str, *, now=None) -> list[str]:
        with self._lock:
            now = self._now(now)
            self._client(client_id)
            entries = self._nonces.get(client_id, {})
            live = [entry for entry in entries.items()
                    if now - entry[1]["timestamp"] <= self.window]
            live.sort(key=lambda item: item[1]["timestamp"])
            return [nonce for nonce, _ in live]

    def purge(self, *, now=None) -> int:
        with self._lock:
            now = self._now(now)
            removed = 0
            for client_id in list(self._nonces) + [
                    c for c in self._responses if c not in self._nonces]:
                removed += self._reclaim_client(client_id, now)
            return removed

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy({
                "version": 1,
                "config": {
                    "clock_skew": self.clock_skew,
                    "window": self.window,
                    "per_client_capacity": self.per_client_capacity,
                    "max_clients": self.max_clients,
                },
                "clients": self._clients,
                "nonces": self._nonces,
                "responses": self._responses,
            })

    @staticmethod
    def _parse_state(state) -> dict:
        if not isinstance(state, dict):
            raise RpcGuardError("STATE_INVALID", "state must be a dict")
        clients = state.get("clients")
        if not isinstance(clients, dict):
            raise RpcGuardError("STATE_INVALID", "clients")
        for client_id, info in clients.items():
            if not isinstance(client_id, str) or not isinstance(info, dict):
                raise RpcGuardError("STATE_INVALID", "clients")
            if not isinstance(info.get("secret"), (bytes, bytearray)):
                raise RpcGuardError("STATE_INVALID", "clients")
            if not isinstance(info.get("revoked"), bool):
                raise RpcGuardError("STATE_INVALID", "clients")
        nonces = state.get("nonces", {})
        if not isinstance(nonces, dict):
            raise RpcGuardError("STATE_INVALID", "nonces")
        for entries in nonces.values():
            if not isinstance(entries, dict):
                raise RpcGuardError("STATE_INVALID", "nonces")
            for entry in entries.values():
                if not isinstance(entry, dict):
                    raise RpcGuardError("STATE_INVALID", "nonces")
                if not isinstance(entry.get("timestamp"), (int, float)):
                    raise RpcGuardError("STATE_INVALID", "nonces")
                if not isinstance(entry.get("body_sha256"), str):
                    raise RpcGuardError("STATE_INVALID", "nonces")
        responses = state.get("responses", {})
        if not isinstance(responses, dict):
            raise RpcGuardError("STATE_INVALID", "responses")
        for cached in responses.values():
            if not isinstance(cached, dict):
                raise RpcGuardError("STATE_INVALID", "responses")
            for entry in cached.values():
                if not isinstance(entry, dict):
                    raise RpcGuardError("STATE_INVALID", "responses")
                if not isinstance(entry.get("timestamp"), (int, float)):
                    raise RpcGuardError("STATE_INVALID", "responses")
                if "response" not in entry:
                    raise RpcGuardError("STATE_INVALID", "responses")
        config = state.get("config")
        if config is not None:
            if not isinstance(config, dict):
                raise RpcGuardError("STATE_INVALID", "config")
            for key in ("clock_skew", "window", "per_client_capacity",
                        "max_clients"):
                if key in config and not isinstance(config[key], int):
                    raise RpcGuardError("STATE_INVALID", "config")
        return {
            "clients": clients,
            "nonces": nonces,
            "responses": responses,
            "config": config or {},
        }

    def restore(self, state: dict) -> None:
        parsed = self._parse_state(state)
        clients = copy.deepcopy(parsed["clients"])
        for info in clients.values():
            info["secret"] = bytes(info["secret"])
        nonces = copy.deepcopy(parsed["nonces"])
        responses = copy.deepcopy(parsed["responses"])
        config = parsed["config"]
        with self._lock:
            self._clients = clients
            self._nonces = nonces
            self._responses = responses
            for key in ("clock_skew", "window", "per_client_capacity",
                        "max_clients"):
                if key in config:
                    setattr(self, key, config[key])

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats)

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STATS_KEYS}
