import threading

import pytest

import proxy_chain_resolver as pr


def make(**kwargs):
    base = {"max_hops": 8, "cooldown": 60.0, "failure_threshold": 3}
    base.update(kwargs)
    return pr.ProxyChainResolver(**base)


def test_parse_single_http_proxy():
    resolver = make()
    hops = resolver.parse("http://proxy.example:8080")
    assert hops == [
        {"scheme": "http", "host": "proxy.example", "port": 8080, "auth": False},
    ]
    assert resolver.stats()["parses"] == 1


def test_parse_chain_keeps_order_scheme_and_auth_flag():
    resolver = make()
    hops = resolver.parse("socks5://alice@a.example:1080, https://b.example:8443")
    assert hops == [
        {"scheme": "socks5", "host": "a.example", "port": 1080, "auth": True},
        {"scheme": "https", "host": "b.example", "port": 8443, "auth": False},
    ]
    assert resolver.stats()["parses"] == 1


def test_parse_rejects_unsupported_scheme():
    resolver = make()
    for spec in ("ftp://proxy.example:21", "socks4://proxy.example:1080"):
        with pytest.raises(pr.ProxyError) as exc:
            resolver.parse(spec)
        assert exc.value.code == "PROXY_UNSUPPORTED_SCHEME"


def test_parse_rejects_bad_port_and_empty_segment():
    resolver = make()
    for spec in ("http://proxy.example:0", "http://proxy.example:70000",
                 "http://proxy.example:port"):
        with pytest.raises(pr.ProxyError) as exc:
            resolver.parse(spec)
        assert exc.value.code == "PROXY_INVALID_PORT"
    for spec in ("", "http://a.example:8080,,http://b.example:8080",
                 "http://a.example:8080,  "):
        with pytest.raises(pr.ProxyError) as exc:
            resolver.parse(spec)
        assert exc.value.code == "PROXY_INVALID_SPEC"


def test_parse_enforces_max_hops():
    resolver = make(max_hops=2)
    assert len(resolver.parse("http://a.example:1,http://b.example:2")) == 2
    with pytest.raises(pr.ProxyError) as exc:
        resolver.parse("http://a.example:1,http://b.example:2,http://c.example:3")
    assert exc.value.code == "PROXY_TOO_MANY_HOPS"


def test_parse_detects_repeated_hop_as_loop():
    resolver = make()
    with pytest.raises(pr.ProxyError) as exc:
        resolver.parse("http://a.example:8080, socks5://a.example:8080")
    assert exc.value.code == "PROXY_LOOP"


def test_parse_rejects_missing_host_or_port():
    resolver = make()
    for spec in ("http://:8080", "http://user@:8080", "http://a.example",
                 "http://a.example:", "proxy.example:8080"):
        with pytest.raises(pr.ProxyError) as exc:
            resolver.parse(spec)
        assert exc.value.code == "PROXY_INVALID_SPEC"


def test_bypass_matches_cidr_rules():
    resolver = make()
    resolver.set_bypass(["10.0.0.0/8", "192.168.1.0/24"])
    assert resolver.bypass("10.1.2.3") is True
    assert resolver.bypass("192.168.1.200") is True
    assert resolver.bypass("192.168.2.1") is False
    assert resolver.bypass("8.8.8.8") is False
    with pytest.raises(pr.ProxyError) as exc:
        resolver.set_bypass(["10.0.0.0/99"])
    assert exc.value.code == "PROXY_INVALID_BYPASS"


def test_bypass_domain_suffix_and_wildcard_differ():
    resolver = make()
    resolver.set_bypass([".internal.example", "*.corp.example"])
    assert resolver.bypass("api.internal.example") is True
    assert resolver.bypass("internal.example") is True
    assert resolver.bypass("other.example") is False
    assert resolver.bypass("a.corp.example") is True
    assert resolver.bypass("corp.example") is False


def test_bypass_exact_host_and_empty_rule_list():
    empty = make()
    assert empty.bypass("exact.example") is False
    resolver = make()
    resolver.set_bypass(["exact.example"])
    assert resolver.bypass("exact.example") is True
    assert resolver.bypass("sub.exact.example") is False
    assert resolver.bypass("other.example") is False


def test_select_prefers_smaller_priority_then_registration_order():
    resolver = make()
    resolver.register_egress("egress-slow", priority=5, address="slow.example:8080")
    resolver.register_egress("egress-fast", priority=2, address="fast.example:8080")
    resolver.register_egress("egress-twin", priority=2, address="twin.example:8080")
    assert resolver.select(now=0.0) == "egress-fast"
    with pytest.raises(pr.ProxyError) as exc:
        resolver.register_egress("egress-fast", priority=1, address="x.example:1")
    assert exc.value.code == "PROXY_DUPLICATE_EGRESS"
    assert resolver.select(now=0.0) == "egress-fast"


def test_degrade_after_consecutive_failures_and_skip_degraded():
    resolver = make(failure_threshold=3, cooldown=60.0)
    resolver.register_egress("egress-a", priority=1, address="a.example:8080")
    resolver.register_egress("egress-b", priority=9, address="b.example:8080")
    assert resolver.select(now=0.0) == "egress-a"
    resolver.probe_result("egress-a", False, now=0.0)
    resolver.probe_result("egress-a", False, now=1.0)
    assert resolver.health("egress-a")["degraded"] is False
    resolver.probe_result("egress-a", False, now=2.0)
    health = resolver.health("egress-a")
    assert health["degraded"] is True
    assert health["consecutive_failures"] == 3
    assert health["degraded_until"] == 62.0
    assert resolver.select(now=3.0) == "egress-b"
    resolver.remove_egress("egress-b")
    with pytest.raises(pr.ProxyError) as exc:
        resolver.select(now=4.0)
    assert exc.value.code == "PROXY_NO_HEALTHY_EGRESS"
    assert resolver.stats()["degradations"] == 1


def test_recovery_requires_cooldown_then_successful_probe():
    resolver = make(failure_threshold=2, cooldown=60.0)
    resolver.register_egress("egress-a", priority=1, address="a.example:8080")
    resolver.register_egress("egress-b", priority=9, address="b.example:8080")
    resolver.probe_result("egress-a", False, now=0.0)
    resolver.probe_result("egress-a", False, now=1.0)
    assert resolver.select(now=2.0) == "egress-b"
    recovered = resolver.probe_result("egress-a", True, now=61.0)
    assert recovered["degraded"] is False
    assert recovered["consecutive_failures"] == 0
    assert resolver.select(now=62.0) == "egress-a"
    assert resolver.stats()["recoveries"] == 1


def test_snapshot_restore_keeps_health_and_cooldown():
    resolver = make(failure_threshold=2, cooldown=60.0)
    resolver.register_egress("egress-a", priority=1, address="a.example:8080")
    resolver.register_egress("egress-b", priority=9, address="b.example:8080")
    resolver.probe_result("egress-a", False, now=0.0)
    resolver.probe_result("egress-a", False, now=1.0)
    state = resolver.snapshot()
    assert state["version"] == 1
    restored = make(failure_threshold=2, cooldown=60.0)
    restored.restore(state)
    assert restored.stats() == resolver.stats()
    assert restored.health("egress-a")["degraded"] is True
    assert restored.health("egress-a")["degraded_until"] == 61.0
    assert restored.select(now=10.0) == "egress-b"
    assert restored.probe_result("egress-a", True, now=61.0)["degraded"] is False
    assert restored.select(now=62.0) == "egress-a"
    with pytest.raises(pr.ProxyError) as exc:
        restored.restore({"version": 3})
    assert exc.value.code == "PROXY_SNAPSHOT_INVALID"
    assert restored.health("egress-a")["degraded"] is False


def test_concurrent_probes_and_select_keep_counters_exact():
    resolver = make(failure_threshold=100, cooldown=60.0)
    ids = ["egress-0", "egress-1", "egress-2"]
    for index, egress_id in enumerate(ids):
        resolver.register_egress(egress_id, priority=index, address="h%d.example:80" % index)
    failures = []
    resolver_lock = threading.Lock()

    def worker(index):
        try:
            for step in range(25):
                resolver.probe_result(ids[index % 3], step % 2 == 0, now=float(step))
                resolver.select(now=float(step))
        except Exception as exc:  # pragma: no cover - surfaced through failures
            with resolver_lock:
                failures.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert failures == []
    assert resolver.stats()["probes"] == 200
    assert resolver.stats()["failures"] == 100
    assert resolver.select(now=1000.0) in ids
