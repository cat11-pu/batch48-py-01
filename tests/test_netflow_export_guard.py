import threading

import pytest

import netflow_export_guard as ng

T0 = 1_700_000_000
FLOW_A = "10.0.0.1:1234->10.0.0.2:80/tcp"


def make(**kwargs):
    guard = ng.NetflowExportGuard(**kwargs)
    guard.set_template(7, ["src", "dst", "bytes"])
    return guard


def packet(**overrides):
    base = {
        "src": "10.0.0.1",
        "dst": "10.0.0.2",
        "src_port": 1234,
        "dst_port": 80,
        "protocol": "tcp",
        "length": 100,
    }
    base.update(overrides)
    return base


def test_add_packet_creates_active_flow():
    guard = make()
    info = guard.add_packet(**packet(), now=T0)
    assert info["flow_key"] == FLOW_A
    assert info["packets"] == 1
    assert info["bytes"] == 100
    assert info["first_seen"] == T0
    assert info["last_seen"] == T0

    flows = guard.active_flows(now=T0)
    assert len(flows) == 1
    assert flows[0]["flow_key"] == FLOW_A
    assert guard.stats()["packets"] == 1


def test_packets_aggregate_and_sampling_scales_bytes():
    guard = make(sampling_rate=4)
    guard.add_packet(**packet(length=100), now=T0)
    guard.add_packet(**packet(length=200), now=T0 + 5)
    info = guard.add_packet(**packet(length=300), now=T0 + 10)
    assert info["packets"] == 3
    assert info["bytes"] == 600

    packets = guard.flush(now=T0 + 10)
    assert len(packets) == 1
    record = packets[0]["records"][0]
    assert record["packets"] == 3
    assert record["bytes"] == 600
    assert record["estimated_bytes"] == 2400


def test_ports_and_protocol_make_distinct_flows():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    guard.add_packet(**packet(dst_port=443), now=T0)
    guard.add_packet(**packet(protocol="udp"), now=T0)
    keys = sorted(item["flow_key"] for item in guard.active_flows(now=T0))
    assert keys == [
        "10.0.0.1:1234->10.0.0.2:443/tcp",
        "10.0.0.1:1234->10.0.0.2:80/tcp",
        "10.0.0.1:1234->10.0.0.2:80/udp",
    ]


def test_active_timeout_boundary_exports_once():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    assert guard.export_due(now=T0 + 29) == []

    packets = guard.export_due(now=T0 + 30)
    assert len(packets) == 1
    assert packets[0]["sequence"] == 1
    assert packets[0]["records"][0]["flow_key"] == FLOW_A
    assert guard.export_due(now=T0 + 31) == []
    assert guard.active_flows(now=T0 + 31) == []


def test_new_packet_refreshes_idle_timer():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    guard.add_packet(**packet(), now=T0 + 20)
    assert guard.export_due(now=T0 + 49) == []

    packets = guard.export_due(now=T0 + 50)
    assert len(packets) == 1
    record = packets[0]["records"][0]
    assert record["packets"] == 2
    assert record["first_seen"] == T0
    assert record["last_seen"] == T0 + 20


def test_template_resend_only_on_content_change():
    guard = ng.NetflowExportGuard()
    assert guard.set_template(7, ["src", "dst", "bytes"]) is True
    assert guard.set_template(7, ["src", "dst", "bytes"]) is False
    assert guard.set_template(7, ["src", "bytes"]) is True

    guard.add_packet(**packet(), now=T0)
    out = guard.flush(now=T0)
    assert len(out) == 1
    assert out[0]["template_id"] == 7
    assert out[0]["template_version"] == 2
    assert out[0]["record_count"] == 1


def test_template_validation():
    guard = ng.NetflowExportGuard()
    with pytest.raises(ng.ExportGuardError) as exc:
        guard.set_template(1, [])
    assert exc.value.code == "TEMPLATE_INVALID"

    with pytest.raises(ng.ExportGuardError) as exc:
        guard.set_template(1, ["src", "src"])
    assert exc.value.code == "TEMPLATE_INVALID"


def test_export_without_template_is_refused():
    guard = ng.NetflowExportGuard()
    guard.add_packet(**packet(), now=T0)
    with pytest.raises(ng.ExportGuardError) as exc:
        guard.export_due(now=T0 + 30)
    assert exc.value.code == "TEMPLATE_UNKNOWN"
    assert len(guard.active_flows(now=T0 + 30)) == 1


def test_flush_exports_all_and_second_flush_is_empty():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    guard.add_packet(**packet(dst_port=443), now=T0)

    first = guard.flush(now=T0)
    assert [item["sequence"] for item in first] == [1, 2]
    assert guard.flush(now=T0) == []
    assert guard.stats()["sequence"] == 2


def test_sequence_is_gap_free_across_export_paths():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    first = guard.export_due(now=T0 + 30)
    guard.add_packet(**packet(dst_port=443), now=T0 + 40)
    guard.add_packet(**packet(dst_port=8080), now=T0 + 40)
    rest = guard.flush(now=T0 + 40)

    assert [item["sequence"] for item in first] == [1]
    assert [item["sequence"] for item in rest] == [2, 3]


def test_close_flow_exports_immediately():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    out = guard.close_flow(FLOW_A, now=T0 + 1)
    assert out["sequence"] == 1
    assert out["records"][0]["last_seen"] == T0 + 1
    assert guard.active_flows(now=T0 + 1) == []

    with pytest.raises(ng.ExportGuardError) as exc:
        guard.close_flow("10.9.9.9:1->10.9.9.9:2/tcp", now=T0 + 1)
    assert exc.value.code == "FLOW_UNKNOWN"


def test_active_flow_table_capacity():
    guard = make(max_flows=1)
    guard.add_packet(**packet(), now=T0)
    with pytest.raises(ng.ExportGuardError) as exc:
        guard.add_packet(**packet(dst_port=443), now=T0 + 1)
    assert exc.value.code == "FLOW_FULL"
    assert len(guard.active_flows(now=T0 + 1)) == 1


def test_snapshot_restore_continues_sequence():
    guard = make()
    guard.add_packet(**packet(), now=T0)
    assert guard.flush(now=T0)[0]["sequence"] == 1
    guard.add_packet(**packet(dst_port=443), now=T0 + 5)

    state = guard.snapshot()
    revived = ng.NetflowExportGuard()
    revived.restore(state)
    assert revived.stats()["sequence"] == 1

    packets = revived.flush(now=T0 + 6)
    assert [item["sequence"] for item in packets] == [2]
    assert packets[0]["records"][0]["flow_key"] == "10.0.0.1:1234->10.0.0.2:443/tcp"


def test_malformed_snapshot_is_rejected():
    guard = make()
    with pytest.raises(ng.ExportGuardError) as exc:
        guard.restore({"sequence": "zero"})
    assert exc.value.code == "STATE_CORRUPT"

    guard.add_packet(**packet(), now=T0)
    assert len(guard.flush(now=T0)) == 1


def test_concurrent_add_and_flush_keep_totals():
    guard = make(max_queue=10000)
    collected = []
    errors = []
    guard_lock = threading.Lock()

    def producer():
        for _ in range(50):
            guard.add_packet(**packet(), now=T0 + 1)

    def flusher():
        for _ in range(20):
            try:
                for item in guard.flush(now=T0 + 1):
                    with guard_lock:
                        collected.append(item["sequence"])
            except ng.ExportGuardError as err:
                with guard_lock:
                    errors.append(err.code)

    threads = [threading.Thread(target=producer) for _ in range(6)]
    threads += [threading.Thread(target=flusher) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert guard.stats()["packets"] == 300
    assert len(collected) == len(set(collected))
    assert sorted(collected) == list(range(1, len(collected) + 1))
