import copy
import threading

import pytest

import traffic_policer_guard as tp


def make(**kwargs):
    return tp.TrafficPolicerGuard(**kwargs)


def single_policy(guard, name="edge", **overrides):
    params = {"committed_rate": 10000, "burst": 1000}
    params.update(overrides)
    return guard.declare_policy(name, **params)


def test_declare_policy_validates_fields():
    guard = make()
    bad_calls = [
        lambda: guard.declare_policy(""),
        lambda: guard.declare_policy(None),
        lambda: guard.declare_policy("p", mode="triple"),
        lambda: guard.declare_policy("p", committed_rate=0),
        lambda: guard.declare_policy("p", committed_rate=-100),
        lambda: guard.declare_policy("p", committed_rate=1.5),
        lambda: guard.declare_policy("p", burst=0),
        lambda: guard.declare_policy("p", version=0),
        lambda: guard.declare_policy("p", peak_rate=-5),
    ]
    for call in bad_calls:
        with pytest.raises(tp.PolicerError) as exc:
            call()
        assert exc.value.code == "TP_INVALID_POLICY"
    assert guard.stats()["rejected"] == len(bad_calls)
    assert guard.stats()["policies_declared"] == 0


def test_declare_policy_is_idempotent_and_guards_duplicates():
    guard = make()
    first = single_policy(guard)
    second = single_policy(guard)
    assert first == second
    assert first["mode"] == "single"
    assert first["version"] == 1
    assert guard.stats()["policies_declared"] == 1
    with pytest.raises(tp.PolicerError) as exc:
        single_policy(guard, burst=2000)
    assert exc.value.code == "TP_DUPLICATE_POLICY"
    assert guard.policy_info("edge")["burst"] == 1000


def test_dual_mode_requires_peak_settings():
    guard = make()
    with pytest.raises(tp.PolicerError) as exc:
        guard.declare_policy("p", mode="dual", committed_rate=10000, burst=1000)
    assert exc.value.code == "TP_INVALID_POLICY"
    with pytest.raises(tp.PolicerError) as exc:
        guard.declare_policy("p", mode="dual", committed_rate=10000,
                             peak_rate=20000, burst=1000)
    assert exc.value.code == "TP_INVALID_POLICY"
    policy = guard.declare_policy("p", mode="dual", committed_rate=10000,
                                 peak_rate=20000, burst=1000, peak_burst=2000)
    assert policy["mode"] == "dual"
    assert policy["peak_rate"] == 20000
    assert policy["peak_burst"] == 2000


def test_attach_validates_flow_and_policy():
    guard = make()
    single_policy(guard)
    with pytest.raises(tp.PolicerError) as exc:
        guard.attach("flow-a", "missing")
    assert exc.value.code == "TP_UNKNOWN_POLICY"
    for bad in ("", None, 7):
        with pytest.raises(tp.PolicerError) as exc:
            guard.attach(bad, "edge")
        assert exc.value.code == "TP_INVALID_FLOW"
    first = guard.attach("flow-a", "edge")
    assert first["policy"] == "edge"
    assert first["mode"] == "single"
    assert guard.attach("flow-a", "edge") == first
    assert guard.stats()["flows_attached"] == 1
    state = guard.flow_state("flow-a")
    assert state["tokens"]["committed"] == 1000
    assert state["last_ms"] == 0


def test_flow_capacity_is_a_hard_cap():
    guard = make(max_flows=2)
    single_policy(guard)
    guard.attach("a", "edge")
    guard.attach("b", "edge")
    with pytest.raises(tp.PolicerError) as exc:
        guard.attach("c", "edge")
    assert exc.value.code == "TP_FLOW_LIMIT"
    assert exc.value.detail["max_flows"] == 2
    assert guard.stats()["rejected"] == 1


def test_classify_green_and_yellow_in_single_mode():
    guard = make()
    single_policy(guard)
    guard.attach("flow-a", "edge")
    first = guard.classify("flow-a", 400, now_ms=0)
    assert first["color"] == "green"
    assert first["action"] == "forward"
    assert first["allowed"] is True
    assert first["tokens"]["committed"] == 600
    second = guard.classify("flow-a", 400, now_ms=0)
    assert second["color"] == "green"
    assert second["tokens"]["committed"] == 200
    third = guard.classify("flow-a", 300, now_ms=0)
    assert third["color"] == "yellow"
    assert third["action"] == "mark"
    assert third["allowed"] is True
    assert third["tokens"]["committed"] == 200
    stats = guard.stats()
    assert stats["packets_classified"] == 3
    assert stats["green"] == 2
    assert stats["yellow"] == 1


def test_packet_larger_than_burst_is_red():
    guard = make()
    single_policy(guard)
    guard.attach("flow-a", "edge")
    result = guard.classify("flow-a", 1500, now_ms=0)
    assert result["color"] == "red"
    assert result["action"] == "drop"
    assert result["allowed"] is False
    assert result["tokens"]["committed"] == 1000
    assert guard.stats()["red"] == 1


def test_dual_mode_produces_three_colors():
    guard = make()
    guard.declare_policy("pipe", mode="dual", committed_rate=10000,
                        peak_rate=20000, burst=1000, peak_burst=2000)
    guard.attach("flow-a", "pipe")
    green = guard.classify("flow-a", 800, now_ms=0)
    assert green["color"] == "green"
    assert green["tokens"] == {"committed": 200, "peak": 1200}
    yellow = guard.classify("flow-a", 800, now_ms=0)
    assert yellow["color"] == "yellow"
    assert yellow["action"] == "mark"
    assert yellow["tokens"] == {"committed": 200, "peak": 400}
    red = guard.classify("flow-a", 900, now_ms=0)
    assert red["color"] == "red"
    assert red["action"] == "drop"
    assert red["tokens"] == {"committed": 200, "peak": 400}
    stats = guard.stats()
    assert (stats["green"], stats["yellow"], stats["red"]) == (1, 1, 1)


def test_flow_buckets_are_isolated():
    guard = make()
    single_policy(guard)
    guard.attach("a", "edge")
    guard.attach("b", "edge")
    assert guard.classify("a", 1000, now_ms=0)["color"] == "green"
    assert guard.classify("b", 500, now_ms=0)["color"] == "green"
    assert guard.classify("a", 1, now_ms=0)["color"] == "yellow"
    late = guard.classify("b", 1, now_ms=0)
    assert late["color"] == "green"
    assert late["tokens"]["committed"] == 499
    assert guard.flow_state("a", now_ms=0)["tokens"]["committed"] == 0


def test_refill_uses_integer_milliseconds():
    guard = make()
    guard.declare_policy("slow", committed_rate=8000, burst=1000)
    guard.attach("flow-a", "slow")
    assert guard.classify("flow-a", 500, now_ms=0)["color"] == "green"
    assert guard.classify("flow-a", 500, now_ms=0)["color"] == "green"
    assert guard.flow_state("flow-a", now_ms=0)["tokens"]["committed"] == 0
    refilled = guard.classify("flow-a", 100, now_ms=100)
    assert refilled["color"] == "green"
    assert refilled["tokens"]["committed"] == 700
    assert guard.stats()["green"] == 3


def test_precolored_packets_are_not_promoted():
    guard = make()
    single_policy(guard)
    guard.attach("flow-a", "edge")
    yellow = guard.classify("flow-a", 100, now_ms=0, color="yellow")
    assert yellow["color"] == "yellow"
    assert yellow["action"] == "mark"
    assert yellow["tokens"]["committed"] == 1000
    red = guard.classify("flow-a", 100, now_ms=0, color="red")
    assert red["color"] == "red"
    assert red["allowed"] is False
    green = guard.classify("flow-a", 100, now_ms=0, color="green")
    assert green["color"] == "green"
    assert green["tokens"]["committed"] == 900


def test_upgrade_policy_bumps_version_and_keeps_attachment():
    guard = make()
    single_policy(guard)
    guard.attach("flow-a", "edge")
    upgraded = guard.upgrade_policy("edge", committed_rate=20000)
    assert upgraded["version"] == 2
    assert upgraded["committed_rate"] == 20000
    assert guard.policy_info("edge")["version"] == 2
    state = guard.flow_state("flow-a", now_ms=0)
    assert state["version"] == 2
    assert state["policy"] == "edge"
    assert guard.stats()["policies_upgraded"] == 1
    with pytest.raises(tp.PolicerError) as exc:
        guard.upgrade_policy("edge")
    assert exc.value.code == "TP_INVALID_POLICY"


def test_set_mode_switches_mode_and_version():
    guard = make()
    guard.declare_policy("edge", committed_rate=10000, burst=1000,
                        peak_rate=20000, peak_burst=2000)
    guard.attach("flow-a", "edge")
    assert guard.flow_state("flow-a", now_ms=0)["mode"] == "single"
    switched = guard.set_mode("edge", "dual")
    assert switched["mode"] == "dual"
    assert switched["version"] == 2
    assert guard.flow_state("flow-a", now_ms=0)["mode"] == "dual"
    again = guard.set_mode("edge", "dual")
    assert again["version"] == 2
    with pytest.raises(tp.PolicerError) as exc:
        guard.set_mode("edge", "triple")
    assert exc.value.code == "TP_INVALID_MODE"
    assert guard.stats()["policies_upgraded"] == 1


def test_invalid_flow_queries_and_snapshot_restore():
    guard = make()
    single_policy(guard)
    guard.attach("flow-a", "edge")
    guard.classify("flow-a", 300, now_ms=0)
    snapshot = guard.snapshot()
    guard.detach("flow-a")
    with pytest.raises(tp.PolicerError) as exc:
        guard.flow_state("flow-a")
    assert exc.value.code == "TP_UNKNOWN_FLOW"
    guard.restore(copy.deepcopy(snapshot))
    state = guard.flow_state("flow-a", now_ms=0)
    assert state["tokens"]["committed"] == 700

    snapshot["flows"][0]["committed"]["tokens"] = 1
    assert guard.flow_state("flow-a", now_ms=0)["tokens"]["committed"] == 700

    broken = copy.deepcopy(snapshot)
    broken["flows"] = "not-a-list"
    with pytest.raises(tp.PolicerError) as exc:
        guard.restore(broken)
    assert exc.value.code == "TP_SNAPSHOT_INVALID"
    assert guard.flow_state("flow-a", now_ms=0)["tokens"]["committed"] == 700
    assert guard.stats()["flows_detached"] == 0
    assert guard.stats()["rejected"] == 1


def test_concurrent_classify_counts_are_exact():
    guard = make(max_flows=8)
    guard.declare_policy("bulk", committed_rate=10000000, burst=10000000)
    flows = ["flow-%d" % index for index in range(4)]
    for flow in flows:
        guard.attach(flow, "bulk")
    barrier = threading.Barrier(4)
    errors = []

    def worker(flow):
        barrier.wait()
        try:
            for _ in range(25):
                result = guard.classify(flow, 1000, now_ms=0)
                assert result["color"] == "green"
                assert result["action"] == "forward"
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(flow,)) for flow in flows]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    stats = guard.stats()
    assert stats["packets_classified"] == 100
    assert stats["green"] == 100
    assert stats["red"] == 0
