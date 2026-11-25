"""ClientHello router: SNI/ECH parsing, grayscale buckets and fallback."""

from __future__ import annotations

import copy
import hashlib
import threading


class ClientHelloError(ValueError):
    def __init__(self, code: str, detail: object = None) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


_STAT_KEYS = ("parsed", "routed", "gray", "fallback", "rejected")


class ClientHelloRouter:
    """Parse captured ClientHello frames and pick a backend cluster."""

    def __init__(self, *, bucket_count: int = 64, grayscale_percent: int = 10,
                 supported_alpn: tuple = ("h2", "http/1.1"),
                 salt: bytes = b"", gray_cluster: str = "canary") -> None:
        if type(bucket_count) is not int or bucket_count <= 0:
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        if type(grayscale_percent) is not int or grayscale_percent < 0:
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        if isinstance(supported_alpn, str):
            supported_alpn = (supported_alpn,)
        if not isinstance(supported_alpn, (tuple, list)):
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        if any(not isinstance(item, str) for item in supported_alpn):
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        if not isinstance(salt, (bytes, bytearray, memoryview)):
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        if not isinstance(gray_cluster, str):
            raise ClientHelloError("CH_INVALID_ARGUMENT")

        self.bucket_count = bucket_count
        self.grayscale_percent = grayscale_percent
        self.supported_alpn = tuple(supported_alpn)
        self.salt = bytes(salt)
        self.gray_cluster = gray_cluster
        self._gray_cutoff = bucket_count * grayscale_percent // 100

        self._lock = threading.RLock()
        self._clusters: dict[str, dict] = {}
        self._bucket_map: dict[int, str] = {}
        self._bucket_version = 1
        self._connections: dict[object, dict] = {}
        self._stats = {key: 0 for key in _STAT_KEYS}

    @property
    def gray_cutoff(self) -> int:
        return self._gray_cutoff

    @property
    def bucket_version(self) -> int:
        return self._bucket_version

    @property
    def bucket_map(self) -> dict:
        with self._lock:
            return dict(self._bucket_map)

    def parse_hello(self, data: bytes) -> dict:
        with self._lock:
            try:
                if not isinstance(data, (bytes, bytearray, memoryview)):
                    raise ValueError("frame must be bytes")
                raw = bytes(data)
                position = 0

                def take(length: int) -> bytes:
                    nonlocal position
                    if length < 0 or position + length > len(raw):
                        raise ValueError("truncated frame")
                    chunk = raw[position:position + length]
                    position += length
                    return chunk

                def u8() -> int:
                    return take(1)[0]

                def u16() -> int:
                    return int.from_bytes(take(2), "big")

                def text(length: int, encoding: str) -> str:
                    return take(length).decode(encoding)

                if take(3) != b"CH1":
                    raise ValueError("bad magic")
                flags = u8()
                has_ech = bool(flags & 0x01)

                sni = text(u16(), "utf-8")
                outer_sni = None
                inner_sni = None
                if has_ech:
                    outer_sni = text(u16(), "utf-8")
                    inner_sni = text(u16(), "utf-8")

                alpns = [text(u8(), "ascii") for _ in range(u8())]
                groups = [u16() for _ in range(u8())]
                tls_version = u16()

                if position != len(raw):
                    raise ValueError("trailing bytes")
            except (UnicodeError, ValueError, TypeError):
                self._stats["rejected"] += 1
                raise ClientHelloError("CH_MALFORMED") from None

            routed_sni = sni
            if has_ech:
                if inner_sni:
                    routed_sni = inner_sni
                elif outer_sni:
                    routed_sni = outer_sni

            self._stats["parsed"] += 1
            return {
                "sni": routed_sni,
                "ech": has_ech,
                "ech_outer_sni": outer_sni,
                "ech_inner_sni": inner_sni,
                "alpn": alpns,
                "groups": groups,
                "tls_version": tls_version,
            }

    def add_cluster(self, name: str, *, snis=(), fallback: bool = False) -> dict:
        with self._lock:
            if not isinstance(name, str) or not name:
                raise ClientHelloError("CH_INVALID_ARGUMENT")
            if name in self._clusters:
                raise ClientHelloError("CH_DUPLICATE_CLUSTER")
            if fallback and any(item["fallback"] for item in self._clusters.values()):
                raise ClientHelloError("CH_FALLBACK_EXISTS")

            if isinstance(snis, str):
                snis = (snis,)
            try:
                normalized = sorted({item for item in snis})
            except TypeError:
                raise ClientHelloError("CH_INVALID_ARGUMENT") from None
            if any(not isinstance(item, str) for item in normalized):
                raise ClientHelloError("CH_INVALID_ARGUMENT")

            cluster = {
                "name": name,
                "snis": normalized,
                "fallback": bool(fallback),
            }
            self._clusters[name] = cluster
            return copy.deepcopy(cluster)

    def list_clusters(self) -> list:
        with self._lock:
            return [copy.deepcopy(self._clusters[name])
                    for name in sorted(self._clusters)]

    def bucket_for(self, client_ip: str) -> int:
        with self._lock:
            return self._bucket_for_unlocked(client_ip)

    def _bucket_for_unlocked(self, client_ip: str) -> int:
        if isinstance(client_ip, str):
            address = client_ip.encode("utf-8")
        elif isinstance(client_ip, (bytes, bytearray, memoryview)):
            address = bytes(client_ip)
        else:
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        digest = hashlib.sha256(self.salt + address).digest()
        return int.from_bytes(digest, "big") % self.bucket_count

    def set_bucket_map(self, mapping: dict, *, version: int) -> dict:
        with self._lock:
            if type(version) is not int or version <= 0:
                raise ClientHelloError("CH_INVALID_VERSION")
            if not isinstance(mapping, dict):
                raise ClientHelloError("CH_INVALID_ARGUMENT")

            candidate: dict[int, str] = {}
            for bucket, cluster in mapping.items():
                if type(bucket) is not int:
                    raise ClientHelloError("CH_INVALID_BUCKET")
                if bucket < 0 or bucket >= self.bucket_count:
                    raise ClientHelloError("CH_INVALID_BUCKET")
                if not isinstance(cluster, str) or cluster not in self._clusters:
                    raise ClientHelloError("CH_UNKNOWN_CLUSTER")
                candidate[bucket] = cluster

            self._bucket_map = candidate
            self._bucket_version = version
            return {
                "bucket_count": self.bucket_count,
                "version": version,
                "gray_cutoff": self._gray_cutoff,
                "mapped": len(candidate),
            }

    def route(self, client_ip: str, sni, alpn, *, conn_id=None) -> dict:
        with self._lock:
            if conn_id is not None:
                try:
                    cached = self._connections.get(conn_id)
                except TypeError:
                    self._stats["rejected"] += 1
                    raise ClientHelloError("CH_INVALID_ARGUMENT") from None
                if cached is not None:
                    result = copy.deepcopy(cached)
                    self._stats["routed"] += 1
                    if result["gray"]:
                        self._stats["gray"] += 1
                    if result["fallback"]:
                        self._stats["fallback"] += 1
                    return result

            try:
                bucket = self._bucket_for_unlocked(client_ip)
            except ClientHelloError:
                self._stats["rejected"] += 1
                raise

            gray = bucket < self._gray_cutoff
            client_alpns = self._normalize_alpn(alpn)
            selected_alpn = next(
                (item for item in client_alpns
                 if item in self.supported_alpn),
                None,
            )

            fallback = False
            if gray:
                cluster = self._bucket_map.get(bucket, self.gray_cluster)
                if cluster not in self._clusters:
                    self._stats["rejected"] += 1
                    raise ClientHelloError("CH_UNKNOWN_CLUSTER")
            else:
                cluster = self._match_sni(sni)
                if cluster is None:
                    cluster = self._fallback_cluster()
                    if cluster is None:
                        self._stats["rejected"] += 1
                        raise ClientHelloError("CH_UNKNOWN_SNI")
                    fallback = True

            result = {
                "status": "ok" if selected_alpn is not None
                else "no_application_protocol",
                "cluster": cluster,
                "bucket": bucket,
                "bucket_version": self._bucket_version,
                "alpn": selected_alpn,
                "fallback": fallback,
                "gray": gray,
            }

            if selected_alpn is None:
                self._stats["rejected"] += 1
                return result

            self._stats["routed"] += 1
            if gray:
                self._stats["gray"] += 1
            if fallback:
                self._stats["fallback"] += 1
            if conn_id is not None:
                self._connections[conn_id] = copy.deepcopy(result)
            return result

    def _normalize_alpn(self, alpn) -> list[str]:
        if alpn is None:
            return []
        if isinstance(alpn, str):
            values = [alpn]
        elif isinstance(alpn, (tuple, list)):
            values = list(alpn)
        else:
            self._stats["rejected"] += 1
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        if any(not isinstance(item, str) for item in values):
            self._stats["rejected"] += 1
            raise ClientHelloError("CH_INVALID_ARGUMENT")
        return values

    def _match_sni(self, sni) -> str | None:
        if not isinstance(sni, str):
            return None
        target = sni.rstrip(".").lower()
        exact = None
        wildcard = None
        wildcard_length = -1

        for name, cluster in self._clusters.items():
            for pattern in cluster["snis"]:
                normalized = pattern.rstrip(".").lower()
                if normalized.startswith("*."):
                    base = normalized[2:]
                    if base and target.endswith("." + base):
                        if len(base) > wildcard_length:
                            wildcard = name
                            wildcard_length = len(base)
                elif normalized == target and exact is None:
                    exact = name

        if exact is not None:
            return exact
        return wildcard

    def _fallback_cluster(self) -> str | None:
        for name, cluster in self._clusters.items():
            if cluster["fallback"]:
                return name
        return None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": 1,
                "clusters": {
                    name: {
                        "name": self._clusters[name]["name"],
                        "snis": list(self._clusters[name]["snis"]),
                        "fallback": self._clusters[name]["fallback"],
                    }
                    for name in sorted(self._clusters)
                },
                "bucket_map": dict(self._bucket_map),
                "bucket_version": self._bucket_version,
                "stats": {key: self._stats[key] for key in _STAT_KEYS},
            }

    def restore(self, state: dict) -> None:
        with self._lock:
            try:
                clusters, bucket_map, bucket_version, stats = (
                    self._validate_snapshot(state)
                )
            except Exception:
                raise ClientHelloError("CH_SNAPSHOT_INVALID") from None

            self._clusters = clusters
            self._bucket_map = bucket_map
            self._bucket_version = bucket_version
            self._stats = stats
            self._connections = {}

    def _validate_snapshot(self, state):
        required = {"version", "clusters", "bucket_map",
                    "bucket_version", "stats"}
        if not isinstance(state, dict) or set(state) != required:
            raise ValueError("bad snapshot keys")
        if type(state["version"]) is not int or state["version"] != 1:
            raise ValueError("bad snapshot version")
        if not isinstance(state["clusters"], dict):
            raise ValueError("bad clusters")

        clusters: dict[str, dict] = {}
        fallback_seen = False
        for name, item in state["clusters"].items():
            if not isinstance(name, str) or not name:
                raise ValueError("bad cluster name")
            if not isinstance(item, dict):
                raise ValueError("bad cluster record")
            if not {"snis", "fallback"}.issubset(item):
                raise ValueError("missing cluster fields")
            if not set(item).issubset({"name", "snis", "fallback"}):
                raise ValueError("unexpected cluster fields")
            if "name" in item and item["name"] != name:
                raise ValueError("cluster name mismatch")
            snis = item["snis"]
            if not isinstance(snis, (list, tuple)):
                raise ValueError("bad cluster snis")
            if any(not isinstance(pattern, str) for pattern in snis):
                raise ValueError("bad cluster sni")
            fallback = item["fallback"]
            if type(fallback) is not bool:
                raise ValueError("bad fallback")
            if fallback:
                if fallback_seen:
                    raise ValueError("multiple fallback clusters")
                fallback_seen = True
            clusters[name] = {
                "name": name,
                "snis": sorted(set(snis)),
                "fallback": fallback,
            }

        if not isinstance(state["bucket_map"], dict):
            raise ValueError("bad bucket map")
        bucket_map: dict[int, str] = {}
        for raw_bucket, cluster in state["bucket_map"].items():
            if type(raw_bucket) is int:
                bucket = raw_bucket
            elif (isinstance(raw_bucket, str) and raw_bucket
                  and raw_bucket.isascii() and raw_bucket.isdigit()):
                bucket = int(raw_bucket)
            else:
                raise ValueError("bad bucket")
            if bucket < 0 or bucket >= self.bucket_count:
                raise ValueError("bucket out of range")
            if bucket in bucket_map:
                raise ValueError("duplicate bucket")
            if not isinstance(cluster, str) or cluster not in clusters:
                raise ValueError("unknown cluster in bucket map")
            bucket_map[bucket] = cluster

        bucket_version = state["bucket_version"]
        if type(bucket_version) is not int or bucket_version <= 0:
            raise ValueError("bad bucket version")

        raw_stats = state["stats"]
        if not isinstance(raw_stats, dict) or set(raw_stats) != set(_STAT_KEYS):
            raise ValueError("bad stats")
        stats = {}
        for key in _STAT_KEYS:
            value = raw_stats[key]
            if type(value) is not int or value < 0:
                raise ValueError("bad stat value")
            stats[key] = value

        return clusters, bucket_map, bucket_version, stats

    def stats(self) -> dict:
        with self._lock:
            return {key: self._stats[key] for key in _STAT_KEYS}

    def reset_stats(self) -> None:
        with self._lock:
            self._stats = {key: 0 for key in _STAT_KEYS}
