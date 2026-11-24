import threading

import pytest

import container_drain_orchestrator as cd


def make(policy=None, cid="c1"):
    orch = cd.DrainOrchestrator()
    orch.register(cid, policy)
    return orch


def to_exited(orch, cid="c1"):
    orch.begin_drain(cid, 100)
    orch.mark_idle(cid, 101)
    orch.note_exit(cid, 0)
    orch.advance(102)


def test_register_creates_container_and_rejects_duplicate():
    orch = cd.DrainOrchestrator()
    out = orch.register("c1")
    assert out["state"] == "CREATED"
    assert out["ready"] is True
    assert out["alive"] == 1
    assert out["signal_count"] == 0
    with pytest.raises(cd.DrainError) as exc:
        orch.register("c1")
    assert exc.value.code == "DRAIN_DUPLICATE_CONTAINER"
    with pytest.raises(cd.DrainError) as exc:
        orch.inspect("missing")
    assert exc.value.code == "DRAIN_UNKNOWN_CONTAINER"


def test_invalid_policy_and_container_id_are_refused():
    orch = cd.DrainOrchestrator()
    with pytest.raises(cd.DrainError) as exc:
        orch.register("", cd.DrainPolicy())
    assert exc.value.code == "DRAIN_INVALID_CONTAINER"
    for policy in (
        cd.DrainPolicy(grace_seconds=0),
        cd.DrainPolicy(sigterm_delay=-1),
        cd.DrainPolicy(max_children=0),
    ):
        with pytest.raises(cd.DrainError) as exc:
            orch.register("bad", policy)
        assert exc.value.code == "DRAIN_INVALID_POLICY"
    with pytest.raises(cd.DrainError) as exc:
        orch.inspect("bad")
    assert exc.value.code == "DRAIN_UNKNOWN_CONTAINER"


def test_begin_drain_clears_readiness_and_sets_deadline():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    assert orch.start("c1") == "RUNNING"
    assert orch.begin_drain("c1", 100) == "DRAINING"
    info = orch.inspect("c1")
    assert info["state"] == "DRAINING"
    assert info["ready"] is False
    assert info["drain_started"] == 100
    assert info["deadline"] == 130


def test_repeated_begin_drain_does_not_restart_grace():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    orch.begin_drain("c1", 100)
    with pytest.raises(cd.DrainError) as exc:
        orch.begin_drain("c1", 120)
    assert exc.value.code == "DRAIN_ALREADY_DRAINING"
    info = orch.inspect("c1")
    assert info["drain_started"] == 100
    assert info["deadline"] == 130


def test_drain_after_exit_and_start_after_drain_are_refused():
    orch = make()
    to_exited(orch)
    with pytest.raises(cd.DrainError) as exc:
        orch.begin_drain("c1", 200)
    assert exc.value.code == "DRAIN_ALREADY_EXITED"
    with pytest.raises(cd.DrainError) as exc:
        orch.start("c1")
    assert exc.value.code == "DRAIN_INVALID_STATE"


def test_mark_idle_emits_group_sigterm():
    orch = make()
    orch.begin_drain("c1", 100)
    sig = orch.mark_idle("c1", 104)
    assert sig == {"signal": "SIGTERM", "scope": "group", "seq": 1, "at": 104}
    assert orch.signals("c1") == [sig]
    info = orch.inspect("c1")
    assert info["state"] == "STOPPING"
    assert info["stopping_at"] == 104
    assert info["signal_count"] == 1


def test_mark_idle_outside_draining_is_refused():
    orch = make()
    with pytest.raises(cd.DrainError) as exc:
        orch.mark_idle("c1", 50)
    assert exc.value.code == "DRAIN_INVALID_STATE"
    to_exited(orch)
    with pytest.raises(cd.DrainError) as exc:
        orch.mark_idle("c1", 300)
    assert exc.value.code == "DRAIN_INVALID_STATE"


def test_advance_exits_once_process_group_is_empty():
    orch = make()
    orch.begin_drain("c1", 100)
    assert orch.add_child("c1", 42) == 2
    assert orch.note_exit("c1", 42) == 1
    assert orch.note_exit("c1", 0) == 0
    assert orch.advance(110) == {"c1": "exited"}
    info = orch.inspect("c1")
    assert info["state"] == "EXITED"
    assert info["exited_at"] == 110
    assert info["alive"] == 0


def test_grace_deadline_escalates_to_group_sigterm():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    orch.begin_drain("c1", 100)
    assert orch.advance(129) == {}
    assert orch.advance(130) == {"c1": "sigterm"}
    assert orch.signals("c1")[-1] == {
        "signal": "SIGTERM", "scope": "group", "seq": 1, "at": 130,
    }
    info = orch.inspect("c1")
    assert info["state"] == "STOPPING"
    assert info["stopping_at"] == 130


def test_sigkill_follows_sigterm_after_delay():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    orch.begin_drain("c1", 100)
    orch.mark_idle("c1", 104)
    assert orch.advance(108) == {}
    assert orch.advance(109) == {"c1": "sigkill"}
    sigs = orch.signals("c1")
    assert [s["signal"] for s in sigs] == ["SIGTERM", "SIGKILL"]
    assert sigs[1]["seq"] == 2
    assert sigs[1]["at"] == 109
    assert sigs[1]["scope"] == "group"
    assert orch.inspect("c1")["state"] == "STOPPING"


def test_deadline_and_delay_force_exit_with_single_sigkill():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    orch.begin_drain("c1", 100)
    orch.mark_idle("c1", 104)
    assert orch.advance(200) == {"c1": "exited"}
    info = orch.inspect("c1")
    assert info["state"] == "EXITED"
    assert info["exited_at"] == 200
    assert [s["signal"] for s in orch.signals("c1")] == ["SIGTERM", "SIGKILL"]
    assert orch.advance(300) == {}
    assert len(orch.signals("c1")) == 2


def test_advance_is_noop_when_nothing_changes():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    assert orch.advance(10) == {}
    orch.start("c1")
    assert orch.advance(20) == {}
    orch.begin_drain("c1", 100)
    assert orch.advance(120) == {}
    assert orch.signals("c1") == []


def test_child_pid_accounting_and_limits():
    orch = cd.DrainOrchestrator()
    orch.register("c1", cd.DrainPolicy(max_children=2))
    assert orch.add_child("c1", 5) == 2
    assert orch.add_child("c1", 6) == 3
    with pytest.raises(cd.DrainError) as exc:
        orch.add_child("c1", 7)
    assert exc.value.code == "DRAIN_CHILD_LIMIT"
    assert orch.inspect("c1")["alive"] == 3
    with pytest.raises(cd.DrainError) as exc:
        orch.add_child("c1", 5)
    assert exc.value.code == "DRAIN_DUPLICATE_PID"
    with pytest.raises(cd.DrainError) as exc:
        orch.note_exit("c1", 9)
    assert exc.value.code == "DRAIN_UNKNOWN_PID"
    assert orch.note_exit("c1", 5) == 2


def test_snapshot_restore_continues_remaining_grace():
    orch = make(cd.DrainPolicy(grace_seconds=40, sigterm_delay=5))
    orch.begin_drain("c1", 1000)
    state = orch.snapshot()
    state["containers"]["c1"]["deadline"] = 1
    assert orch.inspect("c1")["deadline"] == 1040
    fresh = cd.DrainOrchestrator()
    fresh.restore(orch.snapshot())
    info = fresh.inspect("c1")
    assert info["state"] == "DRAINING"
    assert info["drain_started"] == 1000
    assert info["deadline"] == 1040
    assert fresh.advance(1039) == {}
    assert fresh.advance(1040) == {"c1": "sigterm"}
    with pytest.raises(cd.DrainError) as exc:
        fresh.restore({"containers": "broken"})
    assert exc.value.code == "DRAIN_SNAPSHOT_INVALID"
    assert fresh.inspect("c1")["state"] == "STOPPING"


def test_concurrent_begin_drain_advances_exactly_once():
    orch = make(cd.DrainPolicy(grace_seconds=30, sigterm_delay=5))
    barrier = threading.Barrier(6)
    results = []
    lock = threading.Lock()

    def worker(now):
        barrier.wait()
        try:
            value = orch.begin_drain("c1", now)
        except cd.DrainError as exc:
            value = exc.code
        except Exception as exc:  # noqa: BLE001 - surface unexpected failures
            value = type(exc).__name__
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker, args=(100 + i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("DRAINING") == 1
    assert results.count("DRAIN_ALREADY_DRAINING") == 5
    info = orch.inspect("c1")
    assert info["state"] == "DRAINING"
    assert info["deadline"] - info["drain_started"] == 30
