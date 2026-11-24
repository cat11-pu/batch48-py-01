import copy
import threading

import pytest

import dns_view_resolver as dns


def build(**kwargs):
    resolver = dns.DnsViewResolver(**kwargs)
    resolver.declare_view("default")
    resolver.declare_zone("default", "example.com", serial=1,
                          soa_ttl=300, soa_minimum=60)
    return resolver


def test_declare_view_validates_input():
    resolver = dns.DnsViewResolver()
    bad_calls = [
        lambda: resolver.declare_view(""),
        lambda: resolver.declare_view(None),
        lambda: resolver.declare_view("ok", ndots=-1),
        lambda: resolver.declare_view("ok", ndots=16),
        lambda: resolver.declare_view("ok", ndots="1"),
        lambda: resolver.declare_view("ok", search=["bad domain"]),
        lambda: resolver.declare_view("ok", search="example.com"),
    ]
    for call in bad_calls:
        with pytest.raises(dns.DnsError) as exc:
            call()
        assert exc.value.code == "DNS_INVALID_VIEW"
    created = resolver.declare_view("ok", ndots=2, search=["Corp.Example"])
    assert created["ndots"] == 2
    assert created["search"] == ["corp.example"]
    with pytest.raises(dns.DnsError) as exc:
        resolver.declare_view("ok")
    assert exc.value.code == "DNS_DUPLICATE_VIEW"
    assert resolver.stats()["rejected"] == len(bad_calls) + 1


def test_declare_zone_requires_view_and_valid_soa():
    resolver = dns.DnsViewResolver()
    with pytest.raises(dns.DnsError) as exc:
        resolver.declare_zone("missing", "example.com")
    assert exc.value.code == "DNS_UNKNOWN_VIEW"
    resolver.declare_view("default")
    for bad in (
        lambda: resolver.declare_zone("default", "bad zone"),
        lambda: resolver.declare_zone("default", "example.com", serial=0),
        lambda: resolver.declare_zone("default", "example.com", soa_ttl=0),
        lambda: resolver.declare_zone("default", "example.com", soa_minimum=0),
    ):
        with pytest.raises(dns.DnsError) as exc:
            bad()
        assert exc.value.code == "DNS_INVALID_ZONE"
    info = resolver.declare_zone("default", "example.com", serial=7,
                                 soa_ttl=200, soa_minimum=60)
    assert info == {"view": "default", "zone": "example.com", "serial": 7,
                    "soa_ttl": 200, "soa_minimum": 60, "records": 0, "names": 0}
    with pytest.raises(dns.DnsError) as exc:
        resolver.declare_zone("default", "example.com")
    assert exc.value.code == "DNS_DUPLICATE_ZONE"


def test_add_record_validates_type_zone_and_duplicates():
    resolver = build()
    with pytest.raises(dns.DnsError) as exc:
        resolver.add_record("missing", "example.com", "www.example.com", "A", "192.0.2.1")
    assert exc.value.code == "DNS_UNKNOWN_VIEW"
    with pytest.raises(dns.DnsError) as exc:
        resolver.add_record("default", "other.net", "www.other.net", "A", "192.0.2.1")
    assert exc.value.code == "DNS_UNKNOWN_ZONE"
    with pytest.raises(dns.DnsError) as exc:
        resolver.add_record("default", "example.com", "www.example.com", "MX", "mail")
    assert exc.value.code == "DNS_UNSUPPORTED_TYPE"
    with pytest.raises(dns.DnsError) as exc:
        resolver.add_record("default", "example.com", "www.other.net", "A", "192.0.2.1")
    assert exc.value.code == "DNS_OUT_OF_ZONE"
    with pytest.raises(dns.DnsError) as exc:
        resolver.add_record("default", "example.com", "www.example.com", "A", "192.0.2.1", ttl=0)
    assert exc.value.code == "DNS_INVALID_RECORD"

    resolver.add_record("default", "example.com", "www.example.com", "A", "192.0.2.1")
    with pytest.raises(dns.DnsError) as exc:
        resolver.add_record("default", "example.com", "www.example.com", "A", "192.0.2.1")
    assert exc.value.code == "DNS_DUPLICATE_RECORD"
    resolver.add_record("default", "example.com", "www.example.com", "A", "192.0.2.2")
    info = resolver.zone_info("default", "example.com")
    assert info["records"] == 2
    assert info["names"] == 1


def test_resolve_a_record_and_positive_cache():
    resolver = build()
    resolver.add_record("default", "example.com", "www.example.com", "A", "192.0.2.10")
    first = resolver.resolve("WWW.Example.COM.", "A", view="default", now=0)
    assert first["status"] == "NOERROR"
    assert first["cached"] is False
    assert first["remaining_ttl"] == 300
    assert first["answers"] == [
        {"name": "www.example.com", "type": "A", "ttl": 300, "value": "192.0.2.10"},
    ]
    assert first["qname"] == "www.example.com"
    second = resolver.resolve("www.example.com", "A", view="default", now=100)
    assert second["cached"] is True
    assert second["remaining_ttl"] == 200
    assert second["answers"] == first["answers"]
    assert resolver.stats()["cache_hits"] == 1
    assert resolver.stats()["cache_misses"] == 1


def test_nxdomain_is_negatively_cached():
    resolver = build()
    first = resolver.resolve("nope.example.com", "A", view="default", now=0)
    assert first["status"] == "NXDOMAIN"
    assert first["answers"] == []
    assert first["cached"] is False
    entries = resolver.negative_cache()
    assert len(entries) == 1
    assert entries[0]["name"] == "nope.example.com"
    assert entries[0]["status"] == "NXDOMAIN"
    assert entries[0]["ttl"] == 60
    assert entries[0]["zone"] == "example.com"
    assert entries[0]["serial"] == 1
    second = resolver.resolve("nope.example.com", "A", view="default", now=10)
    assert second["status"] == "NXDOMAIN"
    assert second["cached"] is True
    assert second["remaining_ttl"] == 50
    assert resolver.stats()["negative_hits"] == 1


def test_nodata_returns_empty_answer_and_negatively_caches():
    resolver = build()
    resolver.add_record("default", "example.com", "www.example.com", "TXT", "hello")
    first = resolver.resolve("www.example.com", "AAAA", view="default", now=0)
    assert first["status"] == "NOERROR"
    assert first["answers"] == []
    assert first["nodata"] is True
    assert first["cached"] is False
    entries = resolver.negative_cache()
    assert len(entries) == 1
    assert entries[0]["status"] == "NOERROR"
    assert entries[0]["nodata"] is True
    second = resolver.resolve("www.example.com", "AAAA", view="default", now=20)
    assert second["status"] == "NOERROR"
    assert second["cached"] is True
    assert second["remaining_ttl"] == 40
    assert second["nodata"] is True


def test_cname_chain_is_followed():
    resolver = build()
    resolver.add_record("default", "example.com", "alias.example.com",
                        "CNAME", "target.example.com")
    resolver.add_record("default", "example.com", "target.example.com",
                        "A", "192.0.2.20")
    result = resolver.resolve("alias.example.com", "A", view="default", now=0)
    assert result["status"] == "NOERROR"
    assert result["cname_chain"] == ["alias.example.com"]
    assert result["answers"][0] == {"name": "alias.example.com", "type": "CNAME",
                                    "ttl": 300, "value": "target.example.com"}
    assert result["answers"][1] == {"name": "target.example.com", "type": "A",
                                    "ttl": 300, "value": "192.0.2.20"}
    assert resolver.stats()["cname_followed"] == 1
    cached = resolver.resolve("alias.example.com", "A", view="default", now=10)
    assert cached["cached"] is True
    assert cached["answers"] == result["answers"]


def test_cname_loop_returns_servfail():
    resolver = build()
    resolver.add_record("default", "example.com", "a.example.com",
                        "CNAME", "b.example.com")
    resolver.add_record("default", "example.com", "b.example.com",
                        "CNAME", "a.example.com")
    result = resolver.resolve("a.example.com", "A", view="default", now=0)
    assert result["status"] == "SERVFAIL"
    assert result["code"] == "DNS_CNAME_LOOP"
    assert result["answers"] == []


def test_cname_depth_limit_returns_servfail():
    resolver = build(max_cname_depth=2)
    for source, target in (("a", "b"), ("b", "c"), ("c", "d")):
        resolver.add_record("default", "example.com", source + ".example.com",
                            "CNAME", target + ".example.com")
    resolver.add_record("default", "example.com", "d.example.com", "A", "192.0.2.30")
    result = resolver.resolve("a.example.com", "A", view="default", now=0)
    assert result["status"] == "SERVFAIL"
    assert result["code"] == "DNS_CNAME_DEPTH"
    assert result["answers"] == []


def test_search_domains_expand_before_bare_name():
    resolver = dns.DnsViewResolver()
    resolver.declare_view("default", ndots=2, search=["corp.example", "example.com"])
    resolver.declare_zone("default", "corp.example", soa_minimum=60, soa_ttl=300)
    resolver.declare_zone("default", "example.com", soa_minimum=60, soa_ttl=300)
    resolver.add_record("default", "example.com", "db.example.com", "A", "192.0.2.40")
    result = resolver.resolve("db", "A", view="default", now=0)
    assert result["status"] == "NOERROR"
    assert result["qname"] == "db.example.com"
    assert result["candidates_tried"] == 2
    assert result["expanded"] is True
    assert result["answers"][0]["value"] == "192.0.2.40"


def test_positive_cache_ttl_decays_and_expires():
    resolver = build()
    resolver.add_record("default", "example.com", "short.example.com",
                        "A", "192.0.2.50", ttl=50)
    assert resolver.resolve("short.example.com", "A", view="default", now=0)["cached"] is False
    hit = resolver.resolve("short.example.com", "A", view="default", now=49)
    assert hit["cached"] is True
    assert hit["remaining_ttl"] == 1
    expired = resolver.resolve("short.example.com", "A", view="default", now=50)
    assert expired["cached"] is False
    assert expired["remaining_ttl"] == 50
    assert resolver.stats()["cache_hits"] == 1
    assert resolver.stats()["cache_misses"] == 2


def test_view_and_dnssec_cache_are_isolated():
    resolver = dns.DnsViewResolver()
    resolver.declare_view("internal")
    resolver.declare_view("external")
    for view in ("internal", "external"):
        resolver.declare_zone(view, "example.com", soa_minimum=60, soa_ttl=300)
    resolver.add_record("internal", "example.com", "www.example.com", "A", "10.0.0.1")
    resolver.add_record("external", "example.com", "www.example.com", "A", "203.0.113.1")
    internal = resolver.resolve("www.example.com", "A", view="internal", now=0)
    assert internal["answers"][0]["value"] == "10.0.0.1"
    external = resolver.resolve("www.example.com", "A", view="external", now=0)
    assert external["cached"] is False
    assert external["answers"][0]["value"] == "203.0.113.1"
    secure = resolver.resolve("www.example.com", "A", view="internal",
                              now=0, dnssec=True)
    assert secure["cached"] is False
    assert secure["answers"][0]["value"] == "10.0.0.1"
    assert len(resolver.cache_entries()) == 3


def test_negative_ttl_uses_soa_minimum():
    resolver = dns.DnsViewResolver()
    resolver.declare_view("default")
    resolver.declare_zone("default", "example.com", soa_ttl=200, soa_minimum=60)
    first = resolver.resolve("gone.example.com", "A", view="default", now=0)
    assert first["status"] == "NXDOMAIN"
    entries = resolver.negative_cache()
    assert entries[0]["ttl"] == 60
    assert entries[0]["expires_at"] == 60
    assert resolver.resolve("gone.example.com", "A", view="default", now=59)["cached"] is True
    assert resolver.expire(now=60) == {"expired": 1}
    assert resolver.negative_cache() == []
    refreshed = resolver.resolve("gone.example.com", "A", view="default", now=60)
    assert refreshed["cached"] is False
    assert refreshed["remaining_ttl"] == 60


def test_snapshot_restore_and_invalid_state():
    resolver = build()
    resolver.add_record("default", "example.com", "www.example.com", "A", "192.0.2.60")
    resolver.resolve("www.example.com", "A", view="default", now=0)
    snapshot = resolver.snapshot()
    resolver.add_record("default", "example.com", "new.example.com", "A", "192.0.2.61")
    resolver.resolve("new.example.com", "A", view="default", now=0)
    resolver.restore(copy.deepcopy(snapshot))
    hit = resolver.resolve("www.example.com", "A", view="default", now=10)
    assert hit["cached"] is True
    assert hit["remaining_ttl"] == 290
    assert resolver.resolve("new.example.com", "A", view="default", now=10)["cached"] is False

    snapshot["positive"][0]["answers"][0]["value"] = "bad"
    assert resolver.resolve("www.example.com", "A", view="default", now=10)["answers"][0]["value"] == "192.0.2.60"

    broken = copy.deepcopy(snapshot)
    broken["positive"] = "not-a-list"
    with pytest.raises(dns.DnsError) as exc:
        resolver.restore(broken)
    assert exc.value.code == "DNS_SNAPSHOT_INVALID"
    assert resolver.resolve("www.example.com", "A", view="default", now=10)["cached"] is True


def test_concurrent_resolve_counts_queries():
    resolver = build()
    names = []
    for index in range(80):
        name = "host%d.example.com" % index
        resolver.add_record("default", "example.com", name, "A", "192.0.2.%d" % (index + 1))
        names.append(name)
    barrier = threading.Barrier(4)
    errors = []

    def worker(chunk):
        barrier.wait()
        try:
            for name in chunk:
                result = resolver.resolve(name, "A", view="default", now=0)
                assert result["status"] == "NOERROR"
                assert result["cached"] is False
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    chunks = [names[index * 20:(index + 1) * 20] for index in range(4)]
    threads = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    stats = resolver.stats()
    assert stats["queries"] == 80
    assert stats["cache_misses"] == 80
    assert stats["cache_hits"] == 0
