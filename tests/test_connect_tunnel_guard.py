import copy
import threading

import pytest

import connect_tunnel_guard as ct

PUBLIC = "93.184.216.34"


def public_resolver(host):
    return [PUBLIC]


def policy(**overrides):
    base = {
        "ports": [443],
        "deny_private": True,
        "deny_hosts": [],
        "max_tunnels": 4,
        "max_bytes": 0,
    }
    base.update(overrides)
    return base


def guard(**overrides):
    overrides.setdefault("resolver", public_resolver)
    return ct.ConnectTunnelGuard(**overrides)


def ready(**overrides):
    g = guard(**overrides)
    g.set_policy("t1", policy())
    return g


def test_policy_versions_are_monotonic_and_views_are_detached():
    g = guard()
    out = g.set_policy("t1", policy(ports=[8443, 443, 443],
                                    deny_hosts=["Internal.Example.com"],
                                    max_tunnels=2, max_bytes=5000))
    assert out["tenant"] == "t1"
    assert out["version"] == 1
    assert out["ports"] == [443, 8443]
    assert out["deny_hosts"] == ["internal.example.com"]
    assert out["deny_private"] is True
    assert out["max_tunnels"] == 2
    assert out["max_bytes"] == 5000

    view = g.policy_of("t1")
    view["ports"].append(9999)
    view["deny_hosts"].append("evil.example.com")
    assert g.policy_of("t1")["ports"] == [443, 8443]
    assert g.policy_of("t1")["deny_hosts"] == ["internal.example.com"]

    second = g.set_policy("t1", policy(max_bytes=7000))
    assert second["version"] == 2
    assert g.policy_of("t1")["version"] == 2

    bad = [
        None,
        {},
        policy(ports=[]),
        policy(ports=[0]),
        policy(ports=["443"]),
        policy(deny_private="yes"),
        policy(deny_hosts=[7]),
        policy(max_tunnels=0),
        policy(max_bytes=-1),
    ]
    for item in bad:
        with pytest.raises(ct.TunnelError) as exc:
            g.set_policy("t1", item)
        assert exc.value.code == "TUNNEL_POLICY_INVALID"
    assert g.policy_of("t1")["version"] == 2
    with pytest.raises(ct.TunnelError) as exc:
        g.policy_of("t9")
    assert exc.value.code == "TUNNEL_UNKNOWN_TENANT"


def test_open_tunnel_records_target_and_usage():
    g = ready()
    out = g.open_tunnel("api.example.com:443", tenant="t1", now=100)
    assert out["tunnel_id"] == "tn-0001"
    assert out["tenant"] == "t1"
    assert out["host"] == "api.example.com"
    assert out["port"] == 443
    assert out["resolved"] == [PUBLIC]
    assert out["policy_version"] == 1
    assert out["state"] == "open"
    assert out["opened_at"] == 100

    assert g.stats() == {"opened": 1, "closed": 0,
                         "rejected": 0, "revalidations": 0}
    usage = g.tenant_usage("t1")
    assert usage["tenant"] == "t1"
    assert usage["concurrent"] == 1
    assert usage["queued"] == 0
    assert usage["up"] == 0 and usage["down"] == 0
    assert g.list_tunnels("t1")[0]["tunnel_id"] == "tn-0001"


def test_malformed_authorities_are_refused_before_any_tunnel_exists():
    g = ready()
    bad = ["", "   ", "example.com", "example.com:", ":443", " :443",
           "example.com:0", "example.com:65536", "example.com:abc",
           "example.com:44 3", "::1:443", "[::1]443", "[::1]:", "[::1]:0",
           "[]:443"]
    for authority in bad:
        with pytest.raises(ct.TunnelError) as exc:
            g.open_tunnel(authority, tenant="t1")
        assert exc.value.code == "TUNNEL_AUTHORITY_INVALID"
    assert g.stats()["opened"] == 0
    assert g.stats()["rejected"] == 0
    assert g.list_tunnels() == []


def test_literal_private_and_loopback_targets_are_refused():
    g = ready()
    ok = g.open_tunnel("93.184.216.34:443", tenant="t1")
    assert ok["resolved"] == [PUBLIC]

    blocked = ["10.1.2.3:443", "127.0.0.1:443", "169.254.10.10:443",
               "0.0.0.0:443", "[::1]:443", "[fe80::1]:443"]
    for authority in blocked:
        with pytest.raises(ct.TunnelError) as exc:
            g.open_tunnel(authority, tenant="t1")
        assert exc.value.code == "TUNNEL_PRIVATE_TARGET"
    assert g.stats()["opened"] == 1
    assert g.tenant_usage("t1")["concurrent"] == 1


def test_domain_is_rejected_when_any_resolved_address_is_private():
    def resolver(host):
        if host == "mixed.example.com":
            return [PUBLIC, "10.0.0.5"]
        if host == "empty.example.com":
            return []
        if host == "boom.example.com":
            raise RuntimeError("resolver down")
        return []

    g = ct.ConnectTunnelGuard(resolver=resolver)
    g.set_policy("t1", policy())
    with pytest.raises(ct.TunnelError) as exc:
        g.open_tunnel("mixed.example.com:443", tenant="t1")
    assert exc.value.code == "TUNNEL_PRIVATE_TARGET"
    with pytest.raises(ct.TunnelError) as exc:
        g.open_tunnel("empty.example.com:443", tenant="t1")
    assert exc.value.code == "TUNNEL_RESOLVE_FAILED"
    with pytest.raises(ct.TunnelError) as exc:
        g.open_tunnel("boom.example.com:443", tenant="t1")
    assert exc.value.code == "TUNNEL_RESOLVE_FAILED"
    assert g.list_tunnels() == []


def test_port_allowlist_is_independent_of_resolution():
    g = ready()
    with pytest.raises(ct.TunnelError) as exc:
        g.open_tunnel("api.example.com:8443", tenant="t1")
    assert exc.value.code == "TUNNEL_PORT_DENIED"
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1")
    assert tunnel["port"] == 443
    assert g.stats()["rejected"] == 1


def test_deny_hosts_matches_labels_not_raw_suffixes():
    g = guard()
    g.set_policy("t1", policy(deny_hosts=["internal.example.com"]))
    for authority in ["internal.example.com:443", "a.internal.example.com:443"]:
        with pytest.raises(ct.TunnelError) as exc:
            g.open_tunnel(authority, tenant="t1")
        assert exc.value.code == "TUNNEL_TARGET_DENIED"
    allowed = g.open_tunnel("notinternal.example.com:443", tenant="t1")
    assert allowed["host"] == "notinternal.example.com"


def test_tenant_limit_queues_in_order_and_admission_frees_slots():
    g = guard(max_tunnels_per_tenant=2, queue_timeout_ms=1000)
    g.set_policy("t1", policy(max_tunnels=2))
    first = g.open_tunnel("a.example.com:443", tenant="t1", now=0)
    second = g.open_tunnel("b.example.com:443", tenant="t1", now=5)

    queued_one = g.open_tunnel("c.example.com:443", tenant="t1", now=10)
    assert queued_one["status"] == "queued"
    assert queued_one["request_id"] == "q-0001"
    assert queued_one["expires_at"] == 1010
    queued_two = g.open_tunnel("d.example.com:443", tenant="t1", now=20)
    assert queued_two["request_id"] == "q-0002"
    assert g.tenant_usage("t1")["queued"] == 2
    assert g.tenant_usage("t1")["concurrent"] == 2

    g.close_tunnel(first["tunnel_id"], now=30)
    admitted = g.admit_queued("t1", now=40)
    assert [item["request_id"] for item in admitted["admitted"]] == ["q-0001"]
    assert admitted["admitted"][0]["tunnel_id"] == "tn-0003"
    assert admitted["rejected"] == []
    assert g.tenant_usage("t1")["queued"] == 1

    g.close_tunnel(second["tunnel_id"], now=50)
    second_admit = g.admit_queued("t1", now=60)
    assert [item["request_id"] for item in second_admit["admitted"]] == ["q-0002"]
    assert g.tenant_usage("t1")["concurrent"] == 2
    assert g.tenant_usage("t1")["queued"] == 0


def test_meter_accumulates_bytes_and_idle_time():
    g = ready()
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1", now=100)
    first = g.meter(tunnel["tunnel_id"], 100, 50, now=1000)
    assert first["up"] == 100
    assert first["down"] == 50
    assert first["total"] == 150
    assert first["idle_ms"] == 900
    assert first["state"] == "open"

    second = g.meter(tunnel["tunnel_id"], 20, 10, now=1500)
    assert second["up"] == 120
    assert second["down"] == 60
    assert second["total"] == 180
    assert second["idle_ms"] == 500
    usage = g.tenant_usage("t1")
    assert usage["up"] == 120 and usage["down"] == 60


def test_byte_quota_closes_tunnel_and_frees_the_slot():
    g = guard()
    g.set_policy("t1", policy(max_bytes=1000, max_tunnels=1))
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1", now=0)
    assert g.meter(tunnel["tunnel_id"], 600, 0, now=10)["state"] == "open"

    over = g.meter(tunnel["tunnel_id"], 500, 0, now=20)
    assert over["state"] == "closed"
    assert over["reason"] == "quota_exceeded"
    assert over["total"] == 1100
    assert g.stats()["closed"] == 1
    assert g.tenant_usage("t1")["concurrent"] == 0

    with pytest.raises(ct.TunnelError) as exc:
        g.meter(tunnel["tunnel_id"], 1, 0, now=30)
    assert exc.value.code == "TUNNEL_CLOSED"


def test_close_tunnel_is_idempotent():
    g = ready()
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1", now=0)
    first = g.close_tunnel(tunnel["tunnel_id"], reason="client_closed", now=5)
    assert first["state"] == "closed"
    assert first["already_closed"] is False
    assert first["reason"] == "client_closed"

    second = g.close_tunnel(tunnel["tunnel_id"], reason="other", now=6)
    assert second["state"] == "closed"
    assert second["already_closed"] is True
    assert second["reason"] == "client_closed"
    assert g.stats()["closed"] == 1

    with pytest.raises(ct.TunnelError) as exc:
        g.close_tunnel("tn-9999")
    assert exc.value.code == "TUNNEL_UNKNOWN"


def test_revalidate_keeps_public_targets_open_and_counts_cycles():
    calls = []

    def resolver(host):
        calls.append(host)
        return [PUBLIC]

    g = ct.ConnectTunnelGuard(resolver=resolver)
    g.set_policy("t1", policy())
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1", now=0)
    out = g.revalidate(tunnel["tunnel_id"], now=200)
    assert out["status"] == "ok"
    assert out["resolved"] == [PUBLIC]
    assert out["cycles"] == 1
    assert out["policy_version"] == 1
    assert calls == ["api.example.com", "api.example.com"]
    assert g.stats()["revalidations"] == 1
    assert g.list_tunnels("t1")[0]["state"] == "open"


def test_policy_refresh_is_adopted_on_next_metering_window():
    g = ready()
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1", now=0)
    assert tunnel["policy_version"] == 1
    g.set_policy("t1", policy(ports=[443, 8443], max_bytes=9000))
    assert g.policy_of("t1")["version"] == 2

    out = g.meter(tunnel["tunnel_id"], 10, 5, now=25)
    assert out["state"] == "open"
    assert out["policy_version"] == 2
    assert g.policy_of("t1")["ports"] == [443, 8443]


def test_snapshot_restore_keeps_usage_and_rejects_corrupt_state():
    g = ready()
    tunnel = g.open_tunnel("api.example.com:443", tenant="t1", now=0)
    g.meter(tunnel["tunnel_id"], 120, 30, now=40)
    state = g.snapshot()
    assert state["version"] == 1
    assert state["tunnels"][0]["up"] == 120
    pristine = copy.deepcopy(state)

    state["tunnels"][0]["up"] = 99999
    assert g.tenant_usage("t1")["up"] == 120

    restored = ct.ConnectTunnelGuard(resolver=public_resolver)
    restored.restore(pristine)
    assert restored.stats()["opened"] == 1
    assert restored.policy_of("t1")["version"] == 1
    assert restored.tenant_usage("t1")["concurrent"] == 1
    after = restored.meter(tunnel["tunnel_id"], 10, 5, now=100)
    assert after["up"] == 130
    assert after["down"] == 35
    assert after["idle_ms"] == 60

    bad_states = [
        None,
        {},
        {"version": 2, "policies": {}, "tunnels": [], "queues": {},
         "stats": {}, "seq": 0, "queue_seq": 0},
        {"version": 1, "policies": "nope", "tunnels": [], "queues": {},
         "stats": {}, "seq": 0, "queue_seq": 0},
        {"version": 1, "policies": {}, "tunnels": "nope", "queues": {},
         "stats": {}, "seq": 0, "queue_seq": 0},
    ]
    for bad in bad_states:
        with pytest.raises(ct.TunnelError) as exc:
            restored.restore(bad)
        assert exc.value.code == "TUNNEL_SNAPSHOT_INVALID"
    assert restored.tenant_usage("t1")["up"] == 130
    assert restored.meter(tunnel["tunnel_id"], 1, 0, now=110)["total"] == 166


def test_concurrent_open_and_meter_keep_counts_exact():
    g = guard(max_total_tunnels=64)
    for index in range(8):
        g.set_policy(f"t{index}", policy(max_tunnels=8))
    errors = []

    def worker(index):
        try:
            for round_no in range(3):
                tunnel = g.open_tunnel(f"n{index}-{round_no}.example.com:443",
                                       tenant=f"t{index}", now=round_no)
                g.meter(tunnel["tunnel_id"], 10, 20, now=round_no + 1)
        except Exception as exc:  # pragma: no cover - surfaced through errors
            errors.append((index, repr(exc)))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert g.stats()["opened"] == 24
    tunnels = g.list_tunnels()
    ids = [item["tunnel_id"] for item in tunnels]
    assert len(ids) == 24
    assert len(set(ids)) == 24
    total = sum(g.tenant_usage(f"t{i}")["concurrent"] for i in range(8))
    assert total == 24
    assert sum(item["up"] for item in tunnels) == 240
    assert sum(item["down"] for item in tunnels) == 480
