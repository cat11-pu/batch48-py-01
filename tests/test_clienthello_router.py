import copy
import threading

import pytest

import clienthello_router as ch


def frame(sni="www.example.com", ech=None, alpn=("h2",), groups=(29,),
          version=0x0304):
    out = bytearray(b"CH1")
    flags = 0x01 if ech is not None else 0x00
    out.append(flags)
    sni_bytes = sni.encode("utf-8")
    out += len(sni_bytes).to_bytes(2, "big") + sni_bytes
    if ech is not None:
        outer, inner = ech
        outer_bytes = outer.encode("utf-8")
        inner_bytes = inner.encode("utf-8")
        out += len(outer_bytes).to_bytes(2, "big") + outer_bytes
        out += len(inner_bytes).to_bytes(2, "big") + inner_bytes
    out.append(len(alpn))
    for name in alpn:
        raw = name.encode("ascii")
        out.append(len(raw))
        out += raw
    out.append(len(groups))
    for group in groups:
        out += int(group).to_bytes(2, "big")
    out += int(version).to_bytes(2, "big")
    return bytes(out)


def make_router(**kwargs):
    return ch.ClientHelloRouter(**kwargs)


def pick_ip(router, gray, exclude=None):
    cutoff = router.bucket_count * router.grayscale_percent // 100
    for index in range(2000):
        ip = "10.20.%d.%d" % (index // 200, index % 200 + 1)
        bucket = router.bucket_for(ip)
        if (bucket < cutoff) == gray and bucket != exclude:
            return ip, bucket
    raise AssertionError("no bucket matched")


def plain_router():
    router = make_router()
    router.add_cluster("prod", snis=("www.example.com",))
    return router


def test_parse_hello_without_ech():
    router = make_router()
    parsed = router.parse_hello(frame(alpn=("h2", "http/1.1"), groups=(29, 23)))
    assert parsed == {
        "sni": "www.example.com",
        "ech": False,
        "ech_outer_sni": None,
        "ech_inner_sni": None,
        "alpn": ["h2", "http/1.1"],
        "groups": [29, 23],
        "tls_version": 0x0304,
    }
    assert router.stats()["parsed"] == 1


def test_parse_hello_with_ech_reports_both_names():
    router = make_router()
    parsed = router.parse_hello(
        frame(ech=("outer.example.com", "inner.example.com"),
              alpn=("h2",), groups=(29,))
    )
    assert parsed["ech"] is True
    assert parsed["ech_outer_sni"] == "outer.example.com"
    assert parsed["ech_inner_sni"] == "inner.example.com"
    assert parsed["sni"] == "inner.example.com"


def test_parse_hello_rejects_malformed_frames():
    router = make_router()
    good = frame()

    with pytest.raises(ch.ClientHelloError) as exc:
        router.parse_hello(b"CH0" + good[3:])
    assert exc.value.code == "CH_MALFORMED"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.parse_hello(good[:-2])
    assert exc.value.code == "CH_MALFORMED"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.parse_hello(good + b"\x00")
    assert exc.value.code == "CH_MALFORMED"


def test_add_cluster_rejects_duplicates_and_duplicate_fallback():
    router = make_router()
    assert router.add_cluster("prod", snis=("www.example.com", "*.api.example.com")) == {
        "name": "prod",
        "snis": ["*.api.example.com", "www.example.com"],
        "fallback": False,
    }
    assert router.add_cluster("edge", fallback=True) == {
        "name": "edge", "snis": [], "fallback": True,
    }

    with pytest.raises(ch.ClientHelloError) as exc:
        router.add_cluster("prod")
    assert exc.value.code == "CH_DUPLICATE_CLUSTER"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.add_cluster("edge2", fallback=True)
    assert exc.value.code == "CH_FALLBACK_EXISTS"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.add_cluster("")
    assert exc.value.code == "CH_INVALID_ARGUMENT"

    assert [item["name"] for item in router.list_clusters()] == ["edge", "prod"]


def test_route_known_sni_outside_grayscale():
    router = plain_router()
    ip, bucket = pick_ip(router, gray=False)
    assert router.route(ip, "www.example.com", ["h2"]) == {
        "status": "ok",
        "cluster": "prod",
        "bucket": bucket,
        "bucket_version": 1,
        "alpn": "h2",
        "fallback": False,
        "gray": False,
    }


def test_wildcard_matches_subdomains_and_exact_wins():
    router = make_router()
    router.add_cluster("prod", snis=("example.com",))
    router.add_cluster("api", snis=("*.example.com",))
    ip, _ = pick_ip(router, gray=False)

    assert router.route(ip, "api.example.com", ["h2"])["cluster"] == "api"
    assert router.route(ip, "deep.api.example.com", ["h2"])["cluster"] == "api"
    assert router.route(ip, "example.com", ["h2"])["cluster"] == "prod"


def test_unknown_sni_lands_on_fallback_cluster():
    router = make_router()
    router.add_cluster("prod", snis=("www.example.com",))
    router.add_cluster("edge", fallback=True)
    ip, bucket = pick_ip(router, gray=False)

    routed = router.route(ip, "nothing.invalid", ["h2"])
    assert routed == {
        "status": "ok",
        "cluster": "edge",
        "bucket": bucket,
        "bucket_version": 1,
        "alpn": "h2",
        "fallback": True,
        "gray": False,
    }
    assert router.stats()["fallback"] == 1


def test_unknown_sni_without_fallback_is_rejected():
    router = plain_router()
    ip, _ = pick_ip(router, gray=False)
    with pytest.raises(ch.ClientHelloError) as exc:
        router.route(ip, "nothing.invalid", ["h2"])
    assert exc.value.code == "CH_UNKNOWN_SNI"
    assert router.stats()["rejected"] == 1


def test_bucket_for_is_bounded_deterministic_and_salted():
    first = make_router(salt=b"salt-one")
    second = make_router(salt=b"salt-one")
    other = make_router(salt=b"salt-two")

    ip = "192.0.2.10"
    bucket = first.bucket_for(ip)
    assert isinstance(bucket, int)
    assert 0 <= bucket < 64
    assert first.bucket_for(ip) == bucket
    assert second.bucket_for(ip) == bucket

    probes = ["192.0.2.%d" % index for index in range(1, 9)]
    assert any(other.bucket_for(item) != first.bucket_for(item) for item in probes)


def test_gray_bucket_uses_default_then_bucket_mapping():
    router = make_router()
    router.add_cluster("prod", snis=("www.example.com",))
    router.add_cluster("canary")
    router.add_cluster("beta")
    ip, bucket = pick_ip(router, gray=True)

    assert router.route(ip, "www.example.com", ["h2"]) == {
        "status": "ok",
        "cluster": "canary",
        "bucket": bucket,
        "bucket_version": 1,
        "alpn": "h2",
        "fallback": False,
        "gray": True,
    }

    assert router.set_bucket_map({bucket: "beta"}, version=7) == {
        "bucket_count": 64, "version": 7, "gray_cutoff": 6, "mapped": 1,
    }
    routed = router.route(ip, "www.example.com", ["h2"])
    assert routed["cluster"] == "beta"
    assert routed["bucket_version"] == 7
    assert routed["gray"] is True

    plain_ip, _ = pick_ip(router, gray=False)
    steady = router.route(plain_ip, "www.example.com", ["h2"])
    assert steady["cluster"] == "prod"
    assert steady["gray"] is False
    assert steady["bucket_version"] == 7


def test_set_bucket_map_validates_version_bucket_and_cluster():
    router = make_router()
    router.add_cluster("canary")

    with pytest.raises(ch.ClientHelloError) as exc:
        router.set_bucket_map({0: "canary"}, version=0)
    assert exc.value.code == "CH_INVALID_VERSION"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.set_bucket_map({64: "canary"}, version=1)
    assert exc.value.code == "CH_INVALID_BUCKET"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.set_bucket_map({-1: "canary"}, version=1)
    assert exc.value.code == "CH_INVALID_BUCKET"

    with pytest.raises(ch.ClientHelloError) as exc:
        router.set_bucket_map({0: "ghost"}, version=1)
    assert exc.value.code == "CH_UNKNOWN_CLUSTER"

    assert router.set_bucket_map({0: "canary"}, version=2) == {
        "bucket_count": 64, "version": 2, "gray_cutoff": 6, "mapped": 1,
    }


def test_alpn_follows_client_preference_among_supported():
    router = plain_router()
    ip, _ = pick_ip(router, gray=False)
    routed = router.route(ip, "www.example.com", ["spdy/3", "http/1.1", "h2"])
    assert routed["alpn"] == "http/1.1"
    assert router.route(ip, "www.example.com", ["h2"])["alpn"] == "h2"


def test_snapshot_is_deep_copy_and_rejects_malformed_restore():
    router = plain_router()
    router.add_cluster("canary")
    ip, bucket = pick_ip(router, gray=True)
    router.set_bucket_map({bucket: "prod"}, version=3)

    snap = router.snapshot()
    assert set(snap) == {"version", "clusters", "bucket_map",
                         "bucket_version", "stats"}
    assert snap["version"] == 1

    snap["clusters"]["prod"]["snis"].append("evil.example.com")
    snap["bucket_map"][bucket] = "canary"
    snap["stats"]["routed"] = 99

    prod = [item for item in router.list_clusters() if item["name"] == "prod"]
    assert prod == [{"name": "prod", "snis": ["www.example.com"],
                     "fallback": False}]
    assert router.stats()["routed"] == 0
    routed = router.route(ip, "www.example.com", ["h2"])
    assert routed["cluster"] == "prod"
    assert routed["bucket_version"] == 3

    bad_states = [
        {"version": 2},
        {"version": 1},
        {"version": 1, "clusters": [], "bucket_map": {},
         "bucket_version": 1, "stats": {}},
    ]
    for bad in bad_states:
        with pytest.raises(ch.ClientHelloError) as exc:
            router.restore(bad)
        assert exc.value.code == "CH_SNAPSHOT_INVALID"
    prod = [item for item in router.list_clusters() if item["name"] == "prod"]
    assert prod[0]["snis"] == ["www.example.com"]


def test_restore_preserves_clusters_and_bucket_version():
    router = plain_router()
    router.add_cluster("canary")
    ip, bucket = pick_ip(router, gray=True)
    router.set_bucket_map({bucket: "canary"}, version=5)
    before = router.route(ip, "www.example.com", ["h2"])
    frozen = router.stats()
    snap = copy.deepcopy(router.snapshot())

    revived = make_router()
    revived.restore(snap)
    assert revived.list_clusters() == router.list_clusters()
    assert revived.stats() == frozen
    assert revived.route(ip, "www.example.com", ["h2"]) == before


def test_stats_and_concurrent_routes_are_counted_once():
    router = plain_router()
    ip, _ = pick_ip(router, gray=False)

    router.parse_hello(frame())
    router.route(ip, "www.example.com", ["h2"])
    with pytest.raises(ch.ClientHelloError):
        router.route(ip, "nothing.invalid", ["h2"])
    assert router.stats() == {
        "parsed": 1, "routed": 1, "gray": 0, "fallback": 0, "rejected": 1,
    }
    router.reset_stats()
    assert router.stats() == {
        "parsed": 0, "routed": 0, "gray": 0, "fallback": 0, "rejected": 0,
    }

    errors = []
    barrier = threading.Barrier(6)

    def worker():
        try:
            barrier.wait(timeout=10)
            for _ in range(20):
                router.route(ip, "www.example.com", ["h2"])
        except Exception as exc:  # noqa: BLE001 - surfaced by assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert errors == []
    assert router.stats()["routed"] == 120
