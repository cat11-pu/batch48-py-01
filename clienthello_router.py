"""ClientHello router: SNI/ECH parsing, grayscale buckets and fallback."""

from __future__ import annotations

import threading


class ClientHelloError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


class ClientHelloRouter:
    """Parse captured ClientHello frames and pick a backend cluster."""

    def __init__(self, *, bucket_count: int = 64, grayscale_percent: int = 10,
                 supported_alpn: tuple = ("h2", "http/1.1"),
                 salt: bytes = b"", gray_cluster: str = "canary") -> None:
        self.bucket_count = bucket_count
        self.grayscale_percent = grayscale_percent
        self.supported_alpn = supported_alpn
        self.salt = salt
        self.gray_cluster = gray_cluster
        self._lock = threading.RLock()

    def parse_hello(self, data: bytes) -> dict:
        raise NotImplementedError

    def add_cluster(self, name: str, *, snis=(), fallback: bool = False) -> dict:
        raise NotImplementedError

    def list_clusters(self) -> list:
        raise NotImplementedError

    def bucket_for(self, client_ip: str) -> int:
        raise NotImplementedError

    def set_bucket_map(self, mapping: dict, *, version: int) -> dict:
        raise NotImplementedError

    def route(self, client_ip: str, sni, alpn, *, conn_id=None) -> dict:
        raise NotImplementedError

    def snapshot(self) -> dict:
        raise NotImplementedError

    def restore(self, state: dict) -> None:
        raise NotImplementedError

    def stats(self) -> dict:
        raise NotImplementedError

    def reset_stats(self) -> None:
        raise NotImplementedError
